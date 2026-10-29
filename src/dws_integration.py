"""DingTalk Todo adapter using the official DWS command-line client.

The app intentionally uses DWS's existing local login store.  It never copies
or reads refresh tokens, client secrets, or any Obsidian configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core import NEXT_STATUS, TZ, TaskStore, now_local, parse_datetime, to_iso, next_task_status
from productivity import SyncStatus


class DwsError(RuntimeError):
    pass


class PublishLock:
    """Cross-process mutex around "read task -> create todo -> store mapping".

    The CLI (``create --publish``, ``publish-dws``) and the background sync
    service are separate processes.  Without this lock both could observe an
    empty ``external_todo_id`` and each create its own DingTalk todo, leaving
    one of them orphaned.
    """

    def __init__(self, task_dir: Path, timeout: float = 30.0, stale: float = 120.0) -> None:
        self.path = task_dir.parent / ".dingtalk-publish.lock"
        self.timeout = timeout
        self.stale = stale

    def __enter__(self) -> "PublishLock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(str(os.getpid()))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise DwsError("另一个发布进程仍在运行，等待钉钉发布锁超时。")
                time.sleep(0.05)

    def __exit__(self, *exc_info: Any) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def _resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / relative


def default_dws_executable() -> str:
    configured = os.environ.get("TSR_DWS_EXE", "").strip()
    if configured:
        return configured
    bundled = _resource_path("tools/dws.exe")
    return str(bundled) if bundled.exists() else "dws.exe"


def find_value(value: Any, names: set[str]) -> Any | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower().replace("_", "") in names and item not in (None, ""):
                return item
        for item in value.values():
            found = find_value(item, names)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_value(item, names)
            if found not in (None, ""):
                return found
    return None


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "done", "completed", "finished", "closed"}


class DwsClient:
    def __init__(self, executable: str | None = None, profile: str = "") -> None:
        self.executable = executable or default_dws_executable()
        self.profile = profile.strip()

    def call(self, arguments: list[str]) -> dict[str, Any]:
        exe = Path(self.executable)
        if not exe.is_absolute() and "\\" not in self.executable and "/" not in self.executable:
            command = self.executable
        elif exe.exists():
            command = str(exe)
        else:
            raise DwsError(f"未找到 DWS：{self.executable}")
        args = [command, *arguments]
        if self.profile:
            args.extend(["--profile", self.profile])
        args.extend(["--format", "json"])
        # DWS is a console executable.  Hiding its child console preserves the
        # desktop application's GUI-only experience during manual and timed
        # synchronizations.
        run_options: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 45,
            "check": False,
        }
        if os.name == "nt":
            run_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            run_options["startupinfo"] = startupinfo
        try:
            run = subprocess.run(args, **run_options)
        except OSError as exc:
            raise DwsError(f"无法启动 DWS：{exc}") from exc
        output = (run.stdout or run.stderr).strip()
        if run.returncode != 0:
            raise DwsError(f"DWS 调用失败：{output[-800:]}")
        start, end = output.find("{"), output.rfind("}")
        if start < 0 or end < start:
            raise DwsError(f"DWS 未返回 JSON：{output[-800:]}")
        try:
            return json.loads(output[start : end + 1])
        except json.JSONDecodeError as exc:
            raise DwsError(f"DWS 返回的 JSON 无法读取：{exc}") from exc

    def auth_status(self) -> dict[str, Any]:
        result = self.call(["auth", "status"])
        if not truthy(result.get("authenticated")):
            raise DwsError("钉钉未登录。请在本应用工具目录运行 dws.exe auth login。")
        return result

    def current_user_id(self, auth_status: dict[str, Any]) -> str:
        value = find_value(auth_status, {"userid", "staffid"})
        if not value:
            raise DwsError("DWS 未返回当前钉钉用户 ID。")
        return str(value)

    def create_todo(self, title: str, due_at: str, executor: str) -> str:
        result = self.call([
            "todo", "task", "create", "--title", title, "--executors", executor,
            "--priority", "20", "--due", due_at, "--yes",
        ])
        todo_id = find_value(result, {"taskid", "todoid"})
        if not todo_id:
            raise DwsError("DWS 创建待办后未返回待办 ID。")
        return str(todo_id)

    def update_todo(self, todo_id: str, title: str, due_at: str) -> None:
        self.call(["todo", "task", "update", "--task-id", todo_id, "--title", title, "--due", due_at, "--priority", "20", "--yes"])

    def get_todo(self, todo_id: str) -> dict[str, Any]:
        return self.call(["todo", "task", "get", "--task-id", todo_id])

    def delete_todo(self, todo_id: str) -> None:
        try:
            self.call(['todo', 'task', 'delete', '--task-id', todo_id, '--yes'])
        except DwsError as exc:
            # A crash after remote deletion but before local acknowledgement is retryable.
            if not any(code in str(exc).lower() for code in ('tasknotexist', 'task.not.exist', 'task_not_exist', '待办不存在', '任务不存在')):
                raise

    def todo_done(self, todo: dict[str, Any]) -> bool:
        value = find_value(todo, {"isdone", "done", "completed", "finished"})
        if value is not None:
            return truthy(value)
        status = find_value(todo, {"status", "taskstatus"})
        return truthy(status)

    def reset_reminders(self, todo_id: str, rules: list[dict[str, Any]]) -> None:
        self.call(["todo", "task", "reset-reminder", "--task-id", todo_id, "--reminder-rules", json.dumps(rules, separators=(",", ":")), "--yes"])


class DwsWorkflow:
    PREFIX = {"open": "传达", "notified": "催办", "followed-up": "收取反馈"}

    def __init__(self, store: TaskStore, client: DwsClient | None = None) -> None:
        self.store = store
        self.client = client or DwsClient()
        self.sync_status = SyncStatus(store)

    @staticmethod
    def reminder_rules(task: dict[str, Any]) -> list[dict[str, Any]]:
        status = task["status"]
        due = parse_datetime(task["due_at"])
        if status == "open":
            at = now_local() + timedelta(minutes=1)
            return [{"baseTime": "customTime", "reminderTimeStamp": to_iso(at)}]
        if status == "notified":
            at = (due - timedelta(days=3)).replace(hour=8, minute=0, second=0, microsecond=0)
            at = max(at, now_local() + timedelta(minutes=1))
            return [{"baseTime": "customTime", "reminderTimeStamp": to_iso(at)}]
        if status == "followed-up":
            return [{"baseTime": "dueTime", "dueDateOffset": 0}]
        return []

    @classmethod
    def title_for(cls, task: dict[str, Any]) -> str:
        return f"{cls.PREFIX[task['status']]}：{task['title']}"

    @staticmethod
    def rules_hash(rules: list[dict[str, Any]]) -> str:
        return hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def publish(self, task_id: str, auth: dict[str, Any] | None = None) -> dict[str, Any]:
        with PublishLock(self.store.task_dir):
            return self._publish_locked(task_id, auth)

    def _publish_locked(self, task_id: str, auth: dict[str, Any] | None = None) -> dict[str, Any]:
        # Re-read inside the lock: another process may have just created the
        # todo and stored its id while we were waiting.
        task = self.store.get_task(task_id)
        if not task:
            raise DwsError("本地任务不存在。")
        if task["status"] == "done":
            return task
        auth = auth or self.client.auth_status()
        title = self.title_for(task)
        todo_id = task["external_todo_id"]
        target = next_task_status(task)
        due = task["due_at"]
        try:
            existed_before_publish = bool(todo_id)
            if todo_id:
                self.client.update_todo(todo_id, title, due)
            else:
                todo_id = self.client.create_todo(title, due, self.client.current_user_id(auth))
                # Save the mapping before writing the reminder: a transient
                # reminder error must never cause a duplicate DingTalk todo.
                self.store.set_external_state(task_id, todo_id=todo_id, target_status=target, error="", enabled=True)
            rules = self.reminder_rules(task)
            computed_hash = self.rules_hash(rules)
            existing_hash = task["external_reminder_hash"]
            # Any catch-up reminder may have a moving timestamp (for example,
            # "three days before" has already passed).  Once a stage has a
            # rule, preserve it until the task's due time changes; otherwise
            # periodic sync would keep postponing that reminder.
            preserve_existing = existed_before_publish and bool(existing_hash)
            should_write_rules = not preserve_existing and computed_hash != existing_hash
            reminder_hash = existing_hash if preserve_existing else computed_hash
            if should_write_rules:
                self.client.reset_reminders(todo_id, rules)
            updated = self.store.set_external_state(
                task_id,
                todo_id=todo_id,
                target_status=target,
                error="",
                reminder_hash=reminder_hash,
                enabled=True,
                close_stage_reminder=True,
            )
            self.sync_status.record(updated)
            return updated
        except Exception as exc:
            updated = self.store.set_external_state(task_id, error=str(exc)[:1200])
            self.sync_status.record(updated, 'error', exc)
            raise

    def remote_matches(self, task: dict[str, Any], remote: dict[str, Any]) -> bool:
        """True when the DingTalk todo already reflects the local task.

        Used by ``sync_all`` so an unchanged task is not rewritten on every
        poll; rewriting marked the Markdown file dirty and produced a burst of
        ``external_state_updated`` audit rows every few seconds.
        """
        subject = find_value(remote, {"subject"})
        if subject != self.title_for(task):
            return False
        due_ms = find_value(remote, {"duetime"})
        if due_ms in (None, ""):
            return False
        try:
            remote_due = datetime.fromtimestamp(int(due_ms) / 1000, tz=TZ)
        except (TypeError, ValueError, OSError):
            return False
        return remote_due == parse_datetime(task["due_at"])

    def sync_all(self, task_ids=None) -> list[dict[str, Any]]:
        with PublishLock(self.store.task_dir):
            try:
                return self._sync_all_locked(task_ids)
            except Exception as exc:
                for task in self.store.list_tasks(include_done=False):
                    if task['external_sync_enabled'] and (task_ids is None or task['id'] in task_ids):
                        self.sync_status.record(task, 'error', exc)
                raise

    def _sync_all_locked(self, task_ids=None) -> list[dict[str, Any]]:
        auth = self.client.auth_status()
        results: list[dict[str, Any]] = []
        blocked = set()
        for deleted, tasks in ((False, self.store.list_tasks()), (True, self.store.deleted_tasks())):
            for task in tasks:
                if task_ids is not None and task['id'] not in task_ids:
                    continue
                retired = list(task['metadata'].get('retired_todo_ids', []))
                if deleted and task['external_todo_id']:
                    retired.append(task['external_todo_id'])
                for todo_id in dict.fromkeys(retired):
                    try:
                        self.client.delete_todo(todo_id)
                        self.store.clear_retired_todo(task['id'], todo_id, deleted)
                        results.append({'task_id': task['id'], 'action': 'deleted', 'todo_id': todo_id})
                    except Exception as exc:
                        blocked.add(task['id'])
                        self.sync_status.record(task, 'error', exc)
                        results.append({'task_id': task['id'], 'action': 'error', 'error': str(exc)})
        for task in self.store.list_tasks(include_done=False):
            if task_ids is not None and task['id'] not in task_ids:
                continue
            if not task["external_sync_enabled"] or task['id'] in blocked:
                continue
            try:
                todo_id = task["external_todo_id"]
                if not todo_id:
                    updated = self._publish_locked(task["id"], auth)
                    results.append({"task_id": task["id"], "action": "created", "todo_id": updated["external_todo_id"]})
                    continue
                remote = self.client.get_todo(todo_id)
                if not self.client.todo_done(remote):
                    if self.remote_matches(task, remote):
                        self.sync_status.record(task)
                        continue
                    updated = self._publish_locked(task["id"], auth)
                    results.append({"task_id": task["id"], "action": "refreshed", "todo_id": updated["external_todo_id"]})
                    continue
                self.client.reset_reminders(todo_id, [])
                advanced = self.store.advance_task(task["id"])
                self.store.set_external_state(
                    task["id"], todo_id="", target_status="", completed_todo_id=todo_id,
                    reminder_hash="", error="", enabled=advanced["status"] != "done",
                )
                if advanced["status"] == "done":
                    self.sync_status.record(self.store.get_task(task['id']))
                    results.append({"task_id": task["id"], "action": "completed"})
                else:
                    next_task = self._publish_locked(task["id"], auth)
                    results.append({"task_id": task["id"], "action": "advanced", "todo_id": next_task["external_todo_id"], "status": next_task["status"]})
            except Exception as exc:
                updated = self.store.set_external_state(task["id"], error=str(exc)[:1200])
                self.sync_status.record(updated, 'error', exc)
                results.append({"task_id": task["id"], "action": "error", "error": str(exc)})
        return results
