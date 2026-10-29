"""Local-first core for the teaching-secretary reminder application.

This module has no runtime dependency on Obsidian or a cloud database.
The legacy SQLite implementation remains for read-only migration compatibility;
the active TaskStore is rebound to MarkdownTaskStore at the end of this module.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from calendar import monthrange
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
APP_NAME = "TeachingSecretaryReminder"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / APP_NAME
DEFAULT_DB_PATH = APP_DIR / "data" / "tasks.sqlite3"
# Preserved strictly as a read-only migration source.  The active TaskStore is
# rebound to the Markdown implementation at the bottom of this module.
LEGACY_SQLITE_DB_PATH = DEFAULT_DB_PATH

STATUS_LABELS = {
    "open": "待传达",
    "notified": "待催办",
    "followed-up": "待反馈",
    "done": "已完成",
}
NEXT_STATUS = {"open": "notified", "notified": "followed-up", "followed-up": "done"}


def next_task_status(task: dict[str, Any]) -> str | None:
    if task.get('workflow_mode') == 'notice':
        return 'done' if task['status'] != 'done' else None
    return NEXT_STATUS.get(task['status'])


class ValidationError(ValueError):
    """Raised for invalid user-entered task data."""


def now_local() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(TZ).replace(microsecond=0).isoformat()


def parse_datetime(value: str | datetime | None, default_hour: int = 17) -> datetime | None:
    """Parse a date/time in common Chinese data-entry forms.

    A date without a time is intentionally interpreted as 17:00.  This is the
    agreed fallback for feedback-stage deadlines, and makes the stored deadline
    unambiguous for reminders after a reboot.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip().replace("/", "-").replace(" ", "T")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            try:
                result = datetime.combine(date.fromisoformat(text), time(default_hour, 0))
            except ValueError as exc:
                raise ValidationError("截止时间应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM") from exc
        else:
            try:
                result = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValidationError("截止时间应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=TZ)
    return result.astimezone(TZ).replace(second=0, microsecond=0)


def json_load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Reminder:
    id: str
    task_id: str
    kind: str
    remind_at: str
    state: str
    title: str
    task_status: str


class TaskStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterable[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self) -> None:
        with self.connection() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN ('open','notified','followed-up','done')),
                    due_at TEXT NOT NULL,
                    scheduled_at TEXT,
                    recipients_json TEXT NOT NULL DEFAULT '[]',
                    source_department TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    source_notice TEXT NOT NULL DEFAULT '',
                    related_files_json TEXT NOT NULL DEFAULT '[]',
                    semester TEXT NOT NULL DEFAULT '',
                    source_id TEXT NOT NULL DEFAULT '',
                    recurrence TEXT NOT NULL DEFAULT '',
                    external_sync_enabled INTEGER NOT NULL DEFAULT 0,
                    external_todo_id TEXT NOT NULL DEFAULT '',
                    external_target_status TEXT NOT NULL DEFAULT '',
                    external_last_synced_at TEXT NOT NULL DEFAULT '',
                    external_last_completed_todo_id TEXT NOT NULL DEFAULT '',
                    external_sync_error TEXT NOT NULL DEFAULT '',
                    external_reminder_hash TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_source_id ON tasks(source_id);
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','snoozed','shown','acknowledged','closed')),
                    last_notified_at TEXT,
                    acknowledged_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(state, remind_at);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id TEXT PRIMARY KEY,
                    task_id TEXT,
                    event TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            # Keep early prototype databases compatible with the DWS-enabled
            # edition without requiring users to recreate local data.
            columns = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
            for name in (
                "external_target_status",
                "external_last_synced_at",
                "external_last_completed_todo_id",
                "external_sync_error",
                "external_reminder_hash",
            ):
                if name not in columns:
                    con.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

    def _record(self, con: sqlite3.Connection, event: str, task_id: str | None, detail: Any) -> None:
        con.execute(
            "INSERT INTO audit_log(id,task_id,event,detail_json,created_at) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), task_id, event, json_dump(detail), to_iso(now_local())),
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        task = dict(row)
        for key, default in (("recipients_json", []), ("related_files_json", []), ("metadata_json", {})):
            task[key.removesuffix("_json")] = json_load(task.pop(key), default)
        task["status_label"] = STATUS_LABELS[task["status"]]
        return task

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as con:
            return self._task_from_row(con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def list_tasks(
        self, query: str = "", status: str | None = None, include_done: bool = True
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        args: list[Any] = []
        if status:
            if status not in STATUS_LABELS:
                raise ValidationError("未知状态")
            sql += " AND status=?"
            args.append(status)
        elif not include_done:
            sql += " AND status <> 'done'"
        if query.strip():
            token = f"%{query.strip()}%"
            sql += " AND (title LIKE ? OR details LIKE ? OR source_department LIKE ? OR category LIKE ? OR source_id LIKE ? OR recipients_json LIKE ?)"
            args.extend([token] * 6)
        sql += " ORDER BY CASE WHEN status='done' THEN 1 ELSE 0 END, due_at, updated_at DESC"
        with self.connection() as con:
            return [self._task_from_row(r) for r in con.execute(sql, args).fetchall()]

    def find_possible_duplicates(
        self, title: str, due_at: str | datetime | None, source_department: str = "", source_id: str = ""
    ) -> list[dict[str, Any]]:
        due = parse_datetime(due_at)
        with self.connection() as con:
            if source_id.strip():
                rows = con.execute("SELECT * FROM tasks WHERE source_id=? ORDER BY updated_at DESC", (source_id.strip(),)).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM tasks WHERE title=? AND due_at=? AND source_department=? ORDER BY updated_at DESC",
                    (title.strip(), to_iso(due) if due else "", source_department.strip()),
                ).fetchall()
            return [self._task_from_row(r) for r in rows]

    def create_task(self, payload: dict[str, Any], reject_duplicate: bool = False) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        if not title:
            raise ValidationError("任务标题不能为空")
        due = parse_datetime(payload.get("due_at") or payload.get("due"))
        if due is None:
            raise ValidationError("必须填写最终截止时间")
        status = str(payload.get("status") or "open")
        if status not in STATUS_LABELS:
            raise ValidationError("状态不合法")
        dupes = self.find_possible_duplicates(
            title, due, str(payload.get("source_department", "")), str(payload.get("source_id", ""))
        )
        if dupes and reject_duplicate:
            raise ValidationError("发现可能重复事项，请先确认现有任务")
        stamp = to_iso(now_local())
        task_id = str(uuid.uuid4())
        values = {
            "id": task_id,
            "title": title,
            "details": str(payload.get("details", "")),
            "status": status,
            "due_at": to_iso(due),
            "scheduled_at": to_iso(parse_datetime(payload["scheduled_at"])) if payload.get("scheduled_at") else None,
            "recipients_json": json_dump(payload.get("recipients", [])),
            "source_department": str(payload.get("source_department", "")).strip(),
            "category": str(payload.get("category", "")).strip(),
            "source_notice": str(payload.get("source_notice", "")).strip(),
            "related_files_json": json_dump(payload.get("related_files", [])),
            "semester": str(payload.get("semester", "")).strip(),
            "source_id": str(payload.get("source_id", "")).strip(),
            "recurrence": str(payload.get("recurrence", "")).strip(),
            "external_sync_enabled": int(bool(payload.get("external_sync_enabled", True))),
            "external_todo_id": "",
            "external_target_status": "",
            "external_last_synced_at": "",
            "external_last_completed_todo_id": "",
            "external_sync_error": "",
            "external_reminder_hash": "",
            "metadata_json": json_dump(payload.get("metadata", {})),
            "created_at": stamp,
            "updated_at": stamp,
            "completed_at": None,
        }
        with self.connection() as con:
            con.execute(
                "INSERT INTO tasks(%s) VALUES(%s)" % (",".join(values), ",".join("?" for _ in values)),
                tuple(values.values()),
            )
            self._replace_stage_reminder(con, task_id, status, due)
            self._record(con, "task_created", task_id, {"status": status, "possible_duplicates": [d["id"] for d in dupes]})
        return self.get_task(task_id)  # type: ignore[return-value]

    def update_task(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        old = self.get_task(task_id)
        if not old:
            raise ValidationError("未找到任务")
        allowed = {
            "title", "details", "scheduled_at", "recipients", "source_department", "category", "source_notice",
            "related_files", "semester", "source_id", "recurrence", "external_sync_enabled", "external_todo_id", "metadata",
        }
        fields: dict[str, Any] = {}
        for key in allowed & patch.keys():
            db_key = f"{key}_json" if key in {"recipients", "related_files", "metadata"} else key
            fields[db_key] = json_dump(patch[key]) if db_key.endswith("_json") else patch[key]
        due_changed = "due_at" in patch or "due" in patch
        if due_changed:
            fields["due_at"] = to_iso(parse_datetime(patch.get("due_at") or patch.get("due")))
        if "scheduled_at" in fields and fields["scheduled_at"]:
            fields["scheduled_at"] = to_iso(parse_datetime(fields["scheduled_at"]))
        if not fields:
            return old
        fields["updated_at"] = to_iso(now_local())
        with self.connection() as con:
            con.execute("UPDATE tasks SET %s WHERE id=?" % ",".join(f"{k}=?" for k in fields), (*fields.values(), task_id))
            if due_changed and old["status"] != "done":
                due = parse_datetime(fields["due_at"])
                self._replace_stage_reminder(con, task_id, old["status"], due)
            self._record(con, "task_updated", task_id, list(fields))
        return self.get_task(task_id)  # type: ignore[return-value]

    def _stage_reminder_at(self, status: str, due: datetime) -> datetime | None:
        current = now_local()
        if status == "open":
            return current
        if status == "notified":
            at = (due - timedelta(days=3)).replace(hour=8, minute=0, second=0, microsecond=0)
            return max(at, current)
        if status == "followed-up":
            return max(due, current)
        return None

    def _replace_stage_reminder(self, con: sqlite3.Connection, task_id: str, status: str, due: datetime) -> None:
        con.execute(
            "UPDATE reminders SET state='closed' WHERE task_id=? AND kind LIKE 'stage:%' AND state IN ('pending','snoozed','shown')",
            (task_id,),
        )
        at = self._stage_reminder_at(status, due)
        if at is None:
            return
        con.execute(
            "INSERT INTO reminders(id,task_id,kind,remind_at,state,created_at) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), task_id, f"stage:{status}", to_iso(at), "pending", to_iso(now_local())),
        )

    def add_custom_reminder(self, task_id: str, remind_at: str | datetime) -> str:
        if not self.get_task(task_id):
            raise ValidationError("未找到任务")
        reminder_id = str(uuid.uuid4())
        with self.connection() as con:
            con.execute(
                "INSERT INTO reminders(id,task_id,kind,remind_at,state,created_at) VALUES(?,?,?,?,?,?)",
                (reminder_id, task_id, "custom", to_iso(parse_datetime(remind_at)), "pending", to_iso(now_local())),
            )
            self._record(con, "custom_reminder_added", task_id, {"reminder_id": reminder_id})
        return reminder_id

    def advance_task(self, task_id: str) -> dict[str, Any]:
        with self.connection() as con:
            row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            task = self._task_from_row(row)
            if not task:
                raise ValidationError("未找到任务")
            current = task["status"]
            if current == "done":
                raise ValidationError("任务已完成，不能再推进")
            next_status = NEXT_STATUS[current]
            stamp = to_iso(now_local())
            completed_at = stamp if next_status == "done" else None
            con.execute("UPDATE tasks SET status=?,updated_at=?,completed_at=? WHERE id=?", (next_status, stamp, completed_at, task_id))
            due = parse_datetime(task["due_at"])
            self._replace_stage_reminder(con, task_id, next_status, due)  # type: ignore[arg-type]
            self._record(con, "task_advanced", task_id, {"from": current, "to": next_status})
        return self.get_task(task_id)  # type: ignore[return-value]

    def set_external_state(
        self,
        task_id: str,
        *,
        todo_id: str | None = None,
        target_status: str | None = None,
        error: str | None = None,
        completed_todo_id: str | None = None,
        reminder_hash: str | None = None,
        enabled: bool | None = None,
        close_stage_reminder: bool = False,
    ) -> dict[str, Any]:
        """Persist only adapter-owned metadata; task status remains local-owned."""
        task = self.get_task(task_id)
        if not task:
            raise ValidationError("未找到任务")
        values: dict[str, Any] = {"external_last_synced_at": to_iso(now_local())}
        if todo_id is not None:
            values["external_todo_id"] = todo_id
        if target_status is not None:
            values["external_target_status"] = target_status
        if error is not None:
            values["external_sync_error"] = error
        if completed_todo_id is not None:
            values["external_last_completed_todo_id"] = completed_todo_id
        if reminder_hash is not None:
            values["external_reminder_hash"] = reminder_hash
        if enabled is not None:
            values["external_sync_enabled"] = int(enabled)
        with self.connection() as con:
            con.execute("UPDATE tasks SET %s,updated_at=? WHERE id=?" % ",".join(f"{key}=?" for key in values), (*values.values(), to_iso(now_local()), task_id))
            if close_stage_reminder:
                con.execute(
                    "UPDATE reminders SET state='closed' WHERE task_id=? AND kind=? AND state IN ('pending','snoozed','shown')",
                    (task_id, f"stage:{task['status']}"),
                )
            self._record(con, "external_state_updated", task_id, {"fields": list(values)})
        return self.get_task(task_id)  # type: ignore[return-value]

    def set_status(self, task_id: str, status: str) -> dict[str, Any]:
        """Set only the next legal state; prevents accidental stage skipping."""
        task = self.get_task(task_id)
        if not task:
            raise ValidationError("未找到任务")
        if status == task["status"]:
            return task
        if NEXT_STATUS.get(task["status"]) != status:
            raise ValidationError("状态只能按 待传达→待催办→待反馈→已完成 逐步推进")
        return self.advance_task(task_id)

    def due_reminders(self, at: datetime | None = None) -> list[Reminder]:
        at = at or now_local()
        with self.connection() as con:
            rows = con.execute(
                """
                SELECT r.*,t.title,t.status AS task_status FROM reminders r
                JOIN tasks t ON t.id=r.task_id
                WHERE r.state IN ('pending','snoozed') AND r.remind_at <= ? AND t.status <> 'done'
                ORDER BY r.remind_at
                """,
                (to_iso(at),),
            ).fetchall()
            return [Reminder(r["id"], r["task_id"], r["kind"], r["remind_at"], r["state"], r["title"], r["task_status"]) for r in rows]

    def mark_reminder_shown(self, reminder_id: str) -> None:
        with self.connection() as con:
            con.execute("UPDATE reminders SET state='shown',last_notified_at=? WHERE id=?", (to_iso(now_local()), reminder_id))

    def acknowledge_reminder(self, reminder_id: str) -> None:
        with self.connection() as con:
            con.execute("UPDATE reminders SET state='acknowledged',acknowledged_at=? WHERE id=?", (to_iso(now_local()), reminder_id))

    def snooze_reminder(self, reminder_id: str, minutes: int = 10) -> None:
        if minutes <= 0:
            raise ValidationError("暂缓时间必须大于 0")
        with self.connection() as con:
            con.execute("UPDATE reminders SET state='snoozed',remind_at=? WHERE id=?", (to_iso(now_local() + timedelta(minutes=minutes)), reminder_id))

    def reminders_for_task(self, task_id: str) -> list[dict[str, Any]]:
        with self.connection() as con:
            return [dict(r) for r in con.execute("SELECT * FROM reminders WHERE task_id=? ORDER BY remind_at DESC", (task_id,)).fetchall()]

    def daily_report(self, report_day: date | None = None) -> str:
        report_day = report_day or now_local().date()
        start = datetime.combine(report_day, time.min, TZ)
        end = start + timedelta(days=1)
        tasks = self.list_tasks(include_done=False)
        overdue = [t for t in tasks if parse_datetime(t["due_at"]) < start]
        today = [t for t in tasks if start <= parse_datetime(t["due_at"]) < end]
        lines = [f"# 每日工作清单｜{report_day.isoformat()}", "", "## 逾期事项"]
        lines += [f"- [{t['status_label']}] {t['title']}（截止：{t['due_at']}）" for t in overdue] or ["- 无"]
        lines += ["", "## 今日截止"]
        lines += [f"- [{t['status_label']}] {t['title']}（{t['due_at']}）" for t in today] or ["- 无"]
        lines += ["", "## 待处理总览"]
        for code, label in STATUS_LABELS.items():
            if code == "done":
                continue
            lines.append(f"- {label}：{sum(t['status'] == code for t in tasks)} 项")
        return "\n".join(lines) + "\n"

    def weekly_report(self, week_start: date | None = None) -> str:
        today = now_local().date()
        week_start = week_start or today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=7)
        tasks = self.list_tasks(include_done=True)
        completed = [t for t in tasks if t["completed_at"] and week_start <= parse_datetime(t["completed_at"]).date() < week_end]
        upcoming = [t for t in tasks if t["status"] != "done" and week_start <= parse_datetime(t["due_at"]).date() < week_end]
        lines = [f"# 每周工作总结｜{week_start.isoformat()} 至 {(week_end - timedelta(days=1)).isoformat()}", "", "## 已完成"]
        lines += [f"- {t['title']}" for t in completed] or ["- 无"]
        lines += ["", "## 本周待办"]
        lines += [f"- [{t['status_label']}] {t['title']}（截止：{t['due_at']}）" for t in upcoming] or ["- 无"]
        return "\n".join(lines) + "\n"

    def export_json(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            data = {name: [dict(row) for row in con.execute(f"SELECT * FROM {name}").fetchall()] for name in ("tasks", "reminders", "audit_log")}
        destination.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return destination

    def backup(self, backup_dir: str | Path | None = None) -> Path:
        folder = Path(backup_dir or (self.db_path.parent / "backups"))
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"tasks-{now_local().strftime('%Y%m%d-%H%M%S')}.sqlite3"
        shutil.copy2(self.db_path, target)
        return target


def add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


# The independent application now has a TaskNotes-compatible Markdown source
# of truth.  Keeping the legacy implementation above lets existing SQLite
# data be migrated safely on first launch without touching the Obsidian vault.
from markdown_store import DEFAULT_TASK_DIR, MarkdownTaskStore  # noqa: E402

DEFAULT_DB_PATH = DEFAULT_TASK_DIR
TaskStore = MarkdownTaskStore
