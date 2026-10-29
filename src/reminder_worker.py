"""Background reminder runner.

Run with --once from Windows Task Scheduler every five minutes and at user
logon.  Due reminders remain pending until the user acknowledges or snoozes
them, so a sleep/restart cannot silently discard a reminder.
"""

from __future__ import annotations

import argparse
import tkinter as tk
from tkinter import messagebox

from core import STATUS_LABELS, TaskStore
from dws_integration import DwsWorkflow


def show_due(store: TaskStore) -> int:
    # DWS owns the remote completion poll.  Local stage reminders are closed
    # after a successful DWS publish and remain as a fallback only on errors.
    try:
        DwsWorkflow(store).sync_all()
    except Exception:
        pass
    due = store.due_reminders()
    if not due:
        return 0
    root = tk.Tk()
    root.withdraw()
    try:
        for reminder in due:
            answer = messagebox.askyesno(
                "教学秘书提醒",
                f"[{STATUS_LABELS[reminder.task_status]}] {reminder.title}\n\n"
                f"提醒时间：{reminder.remind_at}\n\n"
                "选择“是”确认知晓；选择“否”暂缓 10 分钟。",
                parent=root,
            )
            if answer:
                store.acknowledge_reminder(reminder.id)
            else:
                store.snooze_reminder(reminder.id, 10)
    finally:
        root.destroy()
    return len(due)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--once", action="store_true", help="Kept for scheduler readability; the worker always exits after one pass.")
    args = parser.parse_args()
    raise SystemExit(0 if show_due(TaskStore(args.db)) >= 0 else 1)
