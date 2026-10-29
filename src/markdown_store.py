"""Markdown-first task storage compatible with the TaskNotes field model.

Each task is one human-editable Markdown file with YAML frontmatter.  The
desktop program, the reminder worker and local agents all read the same files;
there is no Obsidian or database process in the data path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from core import (  # Imported after core has defined these helpers.
    LEGACY_SQLITE_DB_PATH,
    NEXT_STATUS,
    next_task_status,
    STATUS_LABELS,
    TZ,
    ValidationError,
    now_local,
    parse_datetime,
    to_iso,
)


DEFAULT_TASK_DIR = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / "TeachingSecretaryReminder" / "Tasks"


def _stamp() -> str:
    return to_iso(now_local())


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, datetime):
        return to_iso(value)
    return str(value)


class MarkdownTaskStore:
    """TaskNotes-style Markdown repository with the former TaskStore API."""

    def __init__(self, task_dir: str | Path | None = None) -> None:
        raw = Path(task_dir or DEFAULT_TASK_DIR)
        # Old callers pass a SQLite-looking path.  Treat it as a directory
        # name instead of ever overwriting a legacy database file.
        self.task_dir = raw.with_suffix("") if raw.suffix.lower() in {".sqlite", ".sqlite3", ".db"} else raw
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir = self.task_dir.parent / "Audit"
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        # Kept for existing UI text and command compatibility.
        self.db_path = self.task_dir
        # Custom repositories (including test/Agent sandboxes) must never import
        # the user's unrelated legacy database implicitly.
        if self.task_dir.resolve() == DEFAULT_TASK_DIR.resolve():
            self._migrate_legacy_if_empty()
        (self.task_dir / '.initialized').touch(exist_ok=True)

    # ---- file format -------------------------------------------------
    @staticmethod
    def _split(content: str) -> tuple[dict[str, Any], str]:
        if not content.startswith("---"):
            return {}, content
        match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)$", content, re.S)
        if not match:
            return {}, content
        try:
            data = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise ValidationError(f"任务 Markdown 的 YAML 属性无法读取：{exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("任务 Markdown 的 YAML 属性必须是键值对象")
        return data, match.group(2)

    @staticmethod
    def _render(data: dict[str, Any], body: str) -> str:
        frontmatter = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
        body = body.strip()
        if not body:
            body = "## 原通知摘要\n\n\n## 传达记录\n\n- 通知时间：\n- 通知方式：\n- 已通知对象：\n\n## 反馈记录\n\n- 反馈时间：\n- 反馈内容：\n\n## 处理备注\n"
        return f"---\n{frontmatter}\n---\n\n{body}\n"

    def _filename(self, task: dict[str, Any]) -> str:
        title = re.sub(r'[\\/:*?"<>|]+', "-", task["title"]).strip(" .") or "未命名事项"
        title = title[:80]
        due = parse_datetime(task["due_at"])
        return f"{due:%Y-%m-%d} {title}--{task['id'][:8]}.md"

    def _paths(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for path in self.task_dir.glob("*.md"):
            try:
                front, _ = self._split(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValidationError):
                continue
            task_id = _text(front.get("id")).strip()
            if task_id:
                found[task_id] = path
        return found

    def _normalise(self, front: dict[str, Any], body: str, path: Path | None = None) -> dict[str, Any]:
        title = _text(front.get("title")).strip()
        if not title:
            raise ValidationError(f"任务文件缺少 title：{path or '未知文件'}")
        due_raw = front.get("due", front.get("due_at"))
        due = parse_datetime(_text(due_raw))
        if due is None:
            raise ValidationError(f"任务文件缺少 due：{path or title}")
        status = _text(front.get("status"), "open").strip()
        if status not in STATUS_LABELS:
            raise ValidationError(f"任务状态无效：{status}")
        task_id = _text(front.get("id")).strip() or str(uuid.uuid4())
        dingtalk_sync = front.get("dingtalk_sync", front.get("external_sync_enabled", True))
        task = {
            "id": task_id,
            "title": title,
            "details": body.strip(),
            "status": status,
            "workflow_mode": _text(front.get('workflow_mode'), 'staged'),
            "due_at": to_iso(due),
            "scheduled_at": _text(front.get("scheduled", front.get("scheduled_at"))) or None,
            "recipients": [str(x) for x in _as_list(front.get("recipients"))],
            "source_department": _text(front.get("source_department")),
            "category": _text(front.get("category")),
            "source_notice": _text(front.get("source_notice")),
            "related_files": [str(x) for x in _as_list(front.get("related_files"))],
            "semester": _text(front.get("semester")),
            "source_id": _text(front.get("source_id")),
            "recurrence": _text(front.get("recurrence")),
            "external_sync_enabled": int(bool(dingtalk_sync)),
            "external_todo_id": _text(front.get("dingtalk_todo_id", front.get("external_todo_id"))),
            "external_target_status": _text(front.get("dingtalk_target_status", front.get("external_target_status"))),
            "external_last_synced_at": _text(front.get("dingtalk_last_synced_at", front.get("external_last_synced_at"))),
            "external_last_completed_todo_id": _text(front.get("dingtalk_last_completed_todo_id", front.get("external_last_completed_todo_id"))),
            "external_sync_error": _text(front.get("dingtalk_sync_error", front.get("external_sync_error"))),
            "external_reminder_hash": _text(front.get("dingtalk_reminder_hash", front.get("external_reminder_hash"))),
            "metadata": front.get("metadata", {}) if isinstance(front.get("metadata", {}), dict) else {},
            "created_at": _text(front.get("created", front.get("created_at"))) or _stamp(),
            "updated_at": _text(front.get("updated", front.get("updated_at"))) or _stamp(),
            "completed_at": _text(front.get("completed", front.get("completed_at"))) or None,
            "reminders": front.get("reminders", []) if isinstance(front.get("reminders", []), list) else [],
            "path": str(path) if path else "",
        }
        task["status_label"] = STATUS_LABELS[status]
        return task

    def _frontmatter(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": task["id"], "title": task["title"], "status": task["status"], "due": task["due_at"],
            "workflow_mode": task.get('workflow_mode', 'staged'),
            "scheduled": task["scheduled_at"], "recipients": task["recipients"],
            "source_department": task["source_department"], "category": task["category"],
            "source_notice": task["source_notice"], "related_files": task["related_files"],
            "semester": task["semester"], "source_id": task["source_id"], "recurrence": task["recurrence"],
            "dingtalk_sync": bool(task["external_sync_enabled"]), "dingtalk_todo_id": task["external_todo_id"],
            "dingtalk_target_status": task["external_target_status"],
            "dingtalk_last_synced_at": task["external_last_synced_at"],
            "dingtalk_last_completed_todo_id": task["external_last_completed_todo_id"],
            "dingtalk_sync_error": task["external_sync_error"],
            "dingtalk_reminder_hash": task["external_reminder_hash"],
            "reminders": task["reminders"], "metadata": task["metadata"],
            "created": task["created_at"], "updated": task["updated_at"], "completed": task["completed_at"],
        }

    def _write(self, task: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
        paths = self._paths()
        target = path or paths.get(task["id"]) or (self.task_dir / self._filename(task))
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self._render(self._frontmatter(task), task["details"])
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, target)
        task["path"] = str(target)
        task["status_label"] = STATUS_LABELS[task["status"]]
        return task

    def _record(self, event: str, task_id: str | None, detail: Any) -> None:
        line = json.dumps({"at": _stamp(), "event": event, "task_id": task_id, "detail": detail}, ensure_ascii=False)
        with (self.audit_dir / f"{now_local():%Y%m%d}.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")

    def _all(self) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for path in self.task_dir.glob("*.md"):
            try:
                front, body = self._split(path.read_text(encoding="utf-8"))
                tasks.append(self._normalise(front, body, path))
            except (OSError, UnicodeError, ValidationError):
                continue
        return tasks

    # ---- legacy migration -------------------------------------------
    def _migrate_legacy_if_empty(self) -> None:
        if ((self.task_dir / '.initialized').exists() or any(self.task_dir.glob("*.md"))
                or not LEGACY_SQLITE_DB_PATH.exists()):
            return
        try:
            con = sqlite3.connect(LEGACY_SQLITE_DB_PATH)
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM tasks").fetchall()
            reminder_rows = con.execute("SELECT * FROM reminders").fetchall()
        except sqlite3.Error:
            return
        finally:
            try:
                con.close()
            except UnboundLocalError:
                pass
        reminders: dict[str, list[dict[str, Any]]] = {}
        for row in reminder_rows:
            value = dict(row)
            reminders.setdefault(value["task_id"], []).append(value)
        for row in rows:
            value = dict(row)
            for key, default in (("recipients_json", []), ("related_files_json", []), ("metadata_json", {})):
                try:
                    value[key.removesuffix("_json")] = json.loads(value.get(key) or json.dumps(default))
                except json.JSONDecodeError:
                    value[key.removesuffix("_json")] = default
            value["details"] = value.get("details", "")
            value["reminders"] = reminders.get(value["id"], [])
            value["path"] = ""
            value["status_label"] = STATUS_LABELS[value["status"]]
            self._write(value)
        if rows:
            self._record("legacy_sqlite_migrated", None, {"count": len(rows), "source": str(LEGACY_SQLITE_DB_PATH)})

    # ---- public TaskStore-compatible API ----------------------------
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return next((task for task in self._all() if task["id"] == task_id), None)

    def list_tasks(self, query: str = "", status: str | None = None, include_done: bool = True) -> list[dict[str, Any]]:
        if status and status not in STATUS_LABELS:
            raise ValidationError("未知状态")
        token = query.strip().lower()
        tasks = []
        for task in self._all():
            if status and task["status"] != status:
                continue
            if not include_done and task["status"] == "done":
                continue
            haystack = " ".join([task["title"], task["details"], task["source_department"], task["category"], task["source_id"], *task["recipients"]]).lower()
            if token and token not in haystack:
                continue
            tasks.append(task)
        return sorted(tasks, key=lambda task: (task["status"] == "done", task["due_at"], task["updated_at"]), reverse=False)

    def find_possible_duplicates(self, title: str, due_at: str | datetime | None, source_department: str = "", source_id: str = "") -> list[dict[str, Any]]:
        due = parse_datetime(due_at)
        return [task for task in self._all() if (
            task["source_id"] == source_id.strip() if source_id.strip() else
            task["title"] == title.strip() and task["due_at"] == to_iso(due) and task["source_department"] == source_department.strip()
        )]

    def _stage_reminder_at(self, status: str, due: datetime) -> datetime | None:
        current = now_local()
        if status == "open":
            return current
        if status == "notified":
            return max((due - timedelta(days=3)).replace(hour=8, minute=0, second=0, microsecond=0), current)
        if status == "followed-up":
            return max(due, current)
        return None

    def _replace_stage_reminder(self, task: dict[str, Any]) -> None:
        # Preserve prior stage reminders as closed audit records, matching the
        # TaskNotes workflow and making the Markdown file self-explanatory.
        for item in task["reminders"]:
            if str(item.get("kind", "")).startswith("stage:") and item.get("state") in {"pending", "snoozed", "shown"}:
                item["state"] = "closed"
        at = self._stage_reminder_at(task["status"], parse_datetime(task["due_at"]))
        if at:
            task["reminders"].append({"id": str(uuid.uuid4()), "task_id": task["id"], "kind": f"stage:{task['status']}", "remind_at": to_iso(at), "state": "pending", "created_at": _stamp()})

    def create_task(self, payload: dict[str, Any], reject_duplicate: bool = False) -> dict[str, Any]:
        title = _text(payload.get("title")).strip()
        due = parse_datetime(payload.get("due_at") or payload.get("due"))
        if not title or due is None:
            raise ValidationError("必须填写任务标题和最终截止时间")
        status = _text(payload.get("status"), "open")
        mode = payload.get('workflow_mode', 'staged')
        if mode not in {'staged', 'notice'}:
            raise ValidationError('处理方式必须为完整流程或一次性通知')
        if mode == 'notice' and status not in {'open', 'done'}:
            raise ValidationError('一次性通知只使用待传达和已完成状态')
        if status not in STATUS_LABELS:
            raise ValidationError("状态不合法")
        duplicates = self.find_possible_duplicates(title, due, _text(payload.get("source_department")), _text(payload.get("source_id")))
        if duplicates and reject_duplicate:
            raise ValidationError("发现可能重复事项，请先确认现有任务")
        task = {
            "id": str(uuid.uuid4()), "title": title, "details": _text(payload.get("details")), "status": status,
            "workflow_mode": mode,
            "due_at": to_iso(due), "scheduled_at": to_iso(parse_datetime(payload["scheduled_at"])) if payload.get("scheduled_at") else None,
            "recipients": [str(x) for x in _as_list(payload.get("recipients"))], "source_department": _text(payload.get("source_department")).strip(),
            "category": _text(payload.get("category")).strip(), "source_notice": _text(payload.get("source_notice")).strip(),
            "related_files": [str(x) for x in _as_list(payload.get("related_files"))], "semester": _text(payload.get("semester")).strip(),
            "source_id": _text(payload.get("source_id")).strip(), "recurrence": _text(payload.get("recurrence")).strip(),
            "external_sync_enabled": int(bool(payload.get("external_sync_enabled", True))), "external_todo_id": "", "external_target_status": "",
            "external_last_synced_at": "", "external_last_completed_todo_id": "", "external_sync_error": "", "external_reminder_hash": "",
            "metadata": payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), dict) else {}, "created_at": _stamp(), "updated_at": _stamp(), "completed_at": None, "reminders": [], "path": "",
        }
        if 'recipient_progress' in payload:
            self._apply_recipient_progress(task, payload['recipient_progress'])
        self._replace_stage_reminder(task)
        self._write(task)
        self._record("task_created", task["id"], {"status": status, "possible_duplicates": [item["id"] for item in duplicates]})
        return task

    def update_task(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValidationError("未找到任务")
        aliases = {"due": "due_at", "external_sync_enabled": "external_sync_enabled"}
        allowed = {"title", "details", "due_at", "scheduled_at", "recipients", "source_department", "category", "source_notice", "related_files", "semester", "source_id", "recurrence", "external_sync_enabled", "external_todo_id", "metadata"}
        previous_due = task['due_at']
        if 'workflow_mode' in patch:
            if patch['workflow_mode'] not in {'staged', 'notice'}:
                raise ValidationError('处理方式无效')
            task['workflow_mode'] = patch['workflow_mode']
            task['external_target_status'] = next_task_status(task) or ''
        for key, value in patch.items():
            key = aliases.get(key, key)
            if key not in allowed:
                continue
            if key in {"due_at", "scheduled_at"} and value:
                task[key] = to_iso(parse_datetime(value))
            elif key in {"recipients", "related_files"}:
                task[key] = [str(item) for item in _as_list(value)]
            elif key == "external_sync_enabled":
                task[key] = int(bool(value))
            else:
                task[key] = value
        if 'recipient_progress' in patch:
            self._apply_recipient_progress(task, patch['recipient_progress'])
        task["updated_at"] = _stamp()
        if task['due_at'] != previous_due and task["status"] != "done":
            self._replace_stage_reminder(task)
            # A changed deadline is the one intentional reason to replace an
            # already-written DingTalk reminder rule on the next sync.
            task["external_reminder_hash"] = ""
        self._write(task, Path(task["path"]))
        self._record("task_updated", task_id, list(patch))
        return task

    @staticmethod
    def _apply_recipient_progress(task, progress):
        if not isinstance(progress, dict):
            raise ValidationError('按人进度必须是姓名到进度的键值对象')
        previous = task['metadata'].get('recipient_progress', {})
        result = {}
        for name in dict.fromkeys(task['recipients']):
            value = progress.get(name, previous.get(name, {}))
            if not isinstance(value, dict):
                raise ValidationError('每人的通知和反馈进度必须为键值对象')
            result[name] = {}
            for field in ('notified', 'feedback'):
                flag = value.get(field, False)
                if not isinstance(flag, bool):
                    raise ValidationError('通知和反馈进度只能为 true 或 false')
                result[name][field] = flag
                result[name][field+'_at'] = (previous.get(name, {}).get(field+'_at') or _stamp()) if flag else ''
        task['metadata']['recipient_progress'] = result

    def advance_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task or task["status"] == "done":
            raise ValidationError("未找到任务，或任务已完成")
        previous = task["status"]
        task["status"] = next_task_status(task)
        task["completed_at"] = _stamp() if task["status"] == "done" else None
        task["updated_at"] = _stamp()
        self._replace_stage_reminder(task)
        self._write(task, Path(task["path"]))
        self._record("task_advanced", task_id, {"from": previous, "to": task["status"]})
        return task

    def set_external_state(self, task_id: str, *, todo_id: str | None = None, target_status: str | None = None, error: str | None = None, completed_todo_id: str | None = None, reminder_hash: str | None = None, enabled: bool | None = None, close_stage_reminder: bool = False) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValidationError("未找到任务")
        updates = {"external_todo_id": todo_id, "external_target_status": target_status, "external_sync_error": error, "external_last_completed_todo_id": completed_todo_id, "external_reminder_hash": reminder_hash}
        for key, value in updates.items():
            if value is not None:
                task[key] = value
        if enabled is not None:
            task["external_sync_enabled"] = int(enabled)
        task["external_last_synced_at"] = _stamp()
        if close_stage_reminder:
            for reminder in task["reminders"]:
                if reminder.get("kind") == f"stage:{task['status']}" and reminder.get("state") in {"pending", "snoozed", "shown"}:
                    reminder["state"] = "closed"
        task["updated_at"] = _stamp()
        self._write(task, Path(task["path"]))
        self._record("external_state_updated", task_id, {key: value for key, value in updates.items() if value is not None})
        return task

    def set_status(self, task_id: str, status: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task or next_task_status(task) != status:
            raise ValidationError("状态只能按 待传达→待催办→待反馈→已完成 逐步推进")
        return self.advance_task(task_id)

    def move_task(self, task_id: str, status: str) -> dict[str, Any]:
        """Explicit board moves may skip stages or reopen; automatic advances may not."""
        task = self.get_task(task_id)
        if not task or status not in STATUS_LABELS:
            raise ValidationError('未找到事项或目标状态无效')
        previous = task['status']
        if task.get('workflow_mode') == 'notice' and status not in {'open', 'done'}:
            raise ValidationError('一次性通知只需待传达→已完成，可拖回待传达重新开启')
        if previous == status:
            return task
        retired = task['metadata'].setdefault('retired_todo_ids', [])
        if task['external_todo_id'] and task['external_todo_id'] not in retired:
            retired.append(task['external_todo_id'])
        task['external_todo_id'] = ''
        task['external_target_status'] = ''
        task['external_reminder_hash'] = ''
        task['external_sync_error'] = ''
        task['external_sync_enabled'] = int(status != 'done' and (task['external_sync_enabled'] or previous == 'done'))
        task['status'] = status
        task['completed_at'] = _stamp() if status == 'done' else None
        task['updated_at'] = _stamp()
        self._replace_stage_reminder(task)
        self._write(task, Path(task['path']))
        self._record('task_moved', task_id, {'from': previous, 'to': status})
        return task

    def delete_task(self, task_id: str) -> Path:
        """Recoverable deletion; keep remote IDs in Trash for offline cleanup retries."""
        task = self.get_task(task_id)
        if not task:
            raise ValidationError('未找到事项')
        task['metadata']['deleted_at'] = _stamp()
        self._write(task, Path(task['path']))
        source = Path(task['path'])
        trash = self.task_dir.parent / 'Trash'
        trash.mkdir(parents=True, exist_ok=True)
        target = trash / f'{now_local():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}-{source.name}'
        os.replace(source, target)
        self._record('task_deleted', task_id, {'trash_path': str(target)})
        return target

    def deleted_tasks(self) -> list[dict[str, Any]]:
        tasks = []
        for path in (self.task_dir.parent / 'Trash').glob('*.md'):
            try:
                front, body = self._split(path.read_text(encoding='utf-8'))
                task = self._normalise(front, body, path)
                task['deleted_at'] = task['metadata'].get('deleted_at') or to_iso(datetime.fromtimestamp(path.stat().st_mtime, TZ))
                tasks.append(task)
            except (OSError, UnicodeError, ValidationError):
                continue
        return sorted(tasks, key=lambda task: task['deleted_at'], reverse=True)

    def restore_task(self, task_id: str) -> dict[str, Any]:
        task = next((item for item in self.deleted_tasks() if item['id'] == task_id), None)
        if not task:
            raise ValidationError('回收站中未找到事项')
        if self.get_task(task_id):
            raise ValidationError('此事项已在任务目录中，不能重复恢复')
        source = Path(task['path'])
        retired = task['metadata'].setdefault('retired_todo_ids', [])
        if task['external_todo_id'] and task['external_todo_id'] not in retired:
            retired.append(task['external_todo_id'])
        # Never reuse a remote ID which may already have been deleted.
        task['external_todo_id'] = ''
        task['external_target_status'] = ''
        task['external_reminder_hash'] = ''
        task['external_sync_error'] = ''
        task['metadata'].pop('deleted_at', None)
        task['metadata']['restored_at'] = _stamp()
        task['updated_at'] = _stamp()
        task['external_sync_enabled'] = int(task['status'] != 'done')
        self._replace_stage_reminder(task)
        target = self.task_dir / self._filename(task)
        self._write(task, target)
        source.unlink()
        self._record('task_restored', task_id, {'from': str(source), 'to': str(target)})
        return task

    def clear_retired_todo(self, task_id: str, todo_id: str, deleted: bool = False) -> None:
        task = (next((t for t in self.deleted_tasks() if t['id'] == task_id), None)
                if deleted else self.get_task(task_id))
        if not task:
            return
        retired = task['metadata'].get('retired_todo_ids', [])
        task['metadata']['retired_todo_ids'] = [tid for tid in retired if tid != todo_id]
        if deleted and task['external_todo_id'] == todo_id:
            task['external_todo_id'] = ''
            task['external_sync_enabled'] = 0
        self._write(task, Path(task['path']))
        self._record('retired_todo_deleted', task_id, {'todo_id': todo_id, 'deleted_task': deleted})

    def add_custom_reminder(self, task_id: str, remind_at: str | datetime) -> str:
        task = self.get_task(task_id)
        if not task:
            raise ValidationError("未找到任务")
        reminder_id = str(uuid.uuid4())
        task["reminders"].append({"id": reminder_id, "task_id": task_id, "kind": "custom", "remind_at": to_iso(parse_datetime(remind_at)), "state": "pending", "created_at": _stamp()})
        task["updated_at"] = _stamp()
        self._write(task, Path(task["path"]))
        self._record("custom_reminder_added", task_id, {"reminder_id": reminder_id})
        return reminder_id

    def due_reminders(self, at: datetime | None = None) -> list[Any]:
        from core import Reminder
        current = at or now_local()
        due = []
        for task in self._all():
            if task["status"] == "done":
                continue
            for reminder in task["reminders"]:
                if reminder.get("state") in {"pending", "snoozed"} and parse_datetime(_text(reminder.get("remind_at"))) <= current:
                    due.append(Reminder(_text(reminder.get("id")), task["id"], _text(reminder.get("kind")), _text(reminder.get("remind_at")), _text(reminder.get("state")), task["title"], task["status"]))
        return sorted(due, key=lambda item: item.remind_at)

    def _change_reminder(self, reminder_id: str, action: str, minutes: int = 0) -> None:
        for task in self._all():
            for reminder in task["reminders"]:
                if reminder.get("id") == reminder_id:
                    if action == "acknowledged":
                        reminder["state"], reminder["acknowledged_at"] = "acknowledged", _stamp()
                    elif action == "snoozed":
                        reminder["state"], reminder["remind_at"] = "snoozed", to_iso(now_local() + timedelta(minutes=minutes))
                    else:
                        reminder["state"], reminder["last_notified_at"] = action, _stamp()
                    task["updated_at"] = _stamp()
                    self._write(task, Path(task["path"]))
                    return

    def mark_reminder_shown(self, reminder_id: str) -> None: self._change_reminder(reminder_id, "shown")
    def acknowledge_reminder(self, reminder_id: str) -> None: self._change_reminder(reminder_id, "acknowledged")
    def snooze_reminder(self, reminder_id: str, minutes: int = 10) -> None:
        if minutes <= 0: raise ValidationError("暂缓时间必须大于 0")
        self._change_reminder(reminder_id, "snoozed", minutes)

    def reminders_for_task(self, task_id: str) -> list[dict[str, Any]]:
        task = self.get_task(task_id)
        return sorted(task["reminders"], key=lambda item: _text(item.get("remind_at")), reverse=True) if task else []

    def daily_report(self, report_day: date | None = None) -> str:
        report_day = report_day or now_local().date()
        start, end = datetime.combine(report_day, time.min, TZ), datetime.combine(report_day + timedelta(days=1), time.min, TZ)
        tasks = self.list_tasks(include_done=False)
        overdue = [task for task in tasks if parse_datetime(task["due_at"]) < start]
        today = [task for task in tasks if start <= parse_datetime(task["due_at"]) < end]
        lines = [f"# 每日工作清单｜{report_day.isoformat()}", "", "## 逾期事项"]
        lines += [f"- [{task['status_label']}] {task['title']}（截止：{task['due_at']}）" for task in overdue] or ["- 无"]
        lines += ["", "## 今日截止"] + [f"- [{task['status_label']}] {task['title']}（{task['due_at']}）" for task in today] or ["- 无"]
        return "\n".join(lines) + "\n"

    def weekly_report(self, week_start: date | None = None) -> str:
        today = now_local().date(); week_start = week_start or today - timedelta(days=today.weekday()); week_end = week_start + timedelta(days=7)
        tasks = self.list_tasks(include_done=True)
        completed = [task for task in tasks if task["completed_at"] and week_start <= parse_datetime(task["completed_at"]).date() < week_end]
        upcoming = [task for task in tasks if task["status"] != "done" and week_start <= parse_datetime(task["due_at"]).date() < week_end]
        lines = [f"# 每周工作总结｜{week_start.isoformat()} 至 {(week_end - timedelta(days=1)).isoformat()}", "", "## 已完成"]
        lines += [f"- {task['title']}" for task in completed] or ["- 无"]
        lines += ["", "## 本周待办"] + [f"- [{task['status_label']}] {task['title']}（{task['due_at']}）" for task in upcoming] or ["- 无"]
        return "\n".join(lines) + "\n"

    def export_json(self, path: str | Path) -> Path:
        target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"tasks": self._all()}, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def backup(self, backup_dir: str | Path | None = None) -> Path:
        target = Path(backup_dir or self.task_dir.parent / "Backups") / f"tasks-{now_local():%Y%m%d-%H%M%S-%f}"
        shutil.copytree(self.task_dir, target)
        for name in ('Trash', 'Templates', 'Undo', 'SyncStatus'):
            source = self.task_dir.parent / name
            if source.exists():
                shutil.copytree(source, target / name)
        return target

    def revision(self) -> tuple[tuple[str, int, int], ...]:
        """Cheap file-set fingerprint for GUI live reload."""
        paths = [*self.task_dir.glob('*.md'), *(self.task_dir.parent / 'Trash').glob('*.md')]
        fingerprint = []
        for path in paths:
            try:
                stat = path.stat()
                fingerprint.append((str(path.relative_to(self.task_dir.parent)), stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                continue
        return tuple(sorted(fingerprint))
