"""Command interface intended for DeepSeek Agent or any local automation.

All responses are JSON on stdout.  Errors are JSON on stderr with a non-zero
exit code, so an agent can distinguish a failed action from an empty result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from core import DEFAULT_DB_PATH, TaskStore, ValidationError
from deepseek import DeepSeekError, parse_notice
from dws_integration import DwsClient, DwsError, DwsWorkflow, PublishLock
from productivity import ActionHistory, Templates, focus_tasks, recipient_progress


def emit(value: object, code: int = 0) -> None:
    stream = sys.stdout if code == 0 else sys.stderr
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str), file=stream)
    raise SystemExit(code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Teaching Secretary Reminder agent interface")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="任务 Markdown 目录；--db 保留用于兼容旧 Agent 调用")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")

    listing = sub.add_parser("list")
    listing.add_argument("--query", default="")
    listing.add_argument("--status", choices=("open", "notified", "followed-up", "done"))
    listing.add_argument("--include-done", action="store_true")

    create = sub.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--due", required=True, help="YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    create.add_argument("--details", default="")
    create.add_argument("--recipients", default="", help="Comma-separated")
    create.add_argument("--department", default="")
    create.add_argument("--category", default="")
    create.add_argument("--source-notice", default="")
    create.add_argument("--source-id", default="")
    create.add_argument("--semester", default="")
    create.add_argument('--workflow-mode', choices=('staged', 'notice'), default='staged',
                        help='staged: 完整流程；notice: 一次性通知，完成一次即结束')
    create.add_argument("--reject-duplicate", action="store_true")
    create.add_argument("--publish", action="store_true", help="Create the first DingTalk stage through DWS after saving locally")

    get = sub.add_parser("get")
    get.add_argument("task_id")
    advance = sub.add_parser("advance")
    advance.add_argument("task_id")
    move = sub.add_parser('move')
    move.add_argument('task_id')
    move.add_argument('--status', required=True, choices=('open', 'notified', 'followed-up', 'done'))
    delete = sub.add_parser('delete')
    delete.add_argument('task_id')
    delete.add_argument('--confirm', action='store_true', help='仅在用户明确要求删除后使用')
    sub.add_parser('trash')
    restore = sub.add_parser('restore')
    restore.add_argument('task_id')
    sub.add_parser('undo')
    focus = sub.add_parser('focus')
    focus.add_argument('view', choices=('today', 'overdue'))
    sub.add_parser('templates')
    progress = sub.add_parser('progress')
    progress.add_argument('task_id')
    progress.add_argument('--recipient', required=True)
    progress.add_argument('--notified', choices=('yes', 'no'))
    progress.add_argument('--feedback', choices=('yes', 'no'))
    sync_task = sub.add_parser('sync-task')
    sync_task.add_argument('task_id')
    duplicate = sub.add_parser("duplicates")
    duplicate.add_argument("--title", required=True)
    duplicate.add_argument("--due", required=True)
    duplicate.add_argument("--department", default="")
    duplicate.add_argument("--source-id", default="")
    report = sub.add_parser("report")
    report.add_argument("kind", choices=("daily", "weekly"))
    report.add_argument("--output")
    backup = sub.add_parser("backup")
    backup.add_argument("--output-dir")
    export = sub.add_parser("export")
    export.add_argument("--output", required=True)
    parse = sub.add_parser("parse-notice")
    parse.add_argument("--text", required=True)
    parse.add_argument("--model", default="deepseek-chat")
    parse.add_argument("--base-url", default="https://api.deepseek.com")
    sub.add_parser("dws-status")
    sub.add_parser("sync-dws")
    publish = sub.add_parser("publish-dws")
    publish.add_argument("task_id")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    store = TaskStore(args.db)
    if args.command == "health":
        emit({"ok": True, "database": str(Path(args.db).resolve()), "app": "TeachingSecretaryReminder"})
    if args.command == "list":
        emit(store.list_tasks(args.query, args.status, args.include_done))
    if args.command == "get":
        task = store.get_task(args.task_id)
        if not task:
            emit({"error": "task_not_found", "task_id": args.task_id}, 2)
        emit(task)
    if args.command == "duplicates":
        emit(store.find_possible_duplicates(args.title, args.due, args.department, args.source_id))
    if args.command == "create":
        task = store.create_task(
                {
                    "title": args.title,
                    "details": args.details,
                    "due": args.due,
                    "recipients": [x.strip() for x in args.recipients.split(",") if x.strip()],
                    "source_department": args.department,
                    "category": args.category,
                    "source_notice": args.source_notice,
                    "source_id": args.source_id,
                    "semester": args.semester,
                    "workflow_mode": args.workflow_mode,
                },
                reject_duplicate=args.reject_duplicate,
            )
        if args.publish:
            task = DwsWorkflow(store).publish(task["id"])
        emit(task)
    if args.command == "advance":
        emit(store.advance_task(args.task_id))
    if args.command == 'move':
        with PublishLock(store.task_dir):
            task = store.move_task(args.task_id, args.status)
        emit(task)
    if args.command == 'delete':
        if not args.confirm:
            raise ValidationError('删除前需用户明确要求，并提供 --confirm')
        with PublishLock(store.task_dir):
            path = store.delete_task(args.task_id)
        emit({'deleted': args.task_id, 'trash_path': str(path), 'remote_cleanup': 'pending_sync'})
    if args.command == 'trash':
        emit(store.deleted_tasks())
    if args.command == 'restore':
        with PublishLock(store.task_dir):
            task = store.restore_task(args.task_id)
        emit(task)
    if args.command == 'undo':
        with PublishLock(store.task_dir):
            label = ActionHistory(store).undo()
        emit({'undone': label})
    if args.command == 'focus':
        emit(focus_tasks(store.list_tasks(include_done=False), args.view))
    if args.command == 'templates':
        emit(Templates(store).list())
    if args.command == 'progress':
        with PublishLock(store.task_dir):
            task = store.get_task(args.task_id)
            if not task:
                raise ValidationError('未找到事项')
            values = recipient_progress(task)
            if args.recipient not in values:
                raise ValidationError('该姓名不在事项的通知对象中')
            for key in ('notified', 'feedback'):
                flag = getattr(args, key)
                if flag is not None:
                    values[args.recipient][key] = flag == 'yes'
            task = ActionHistory(store).perform('记录按人进度', [args.task_id],
                lambda: store.update_task(args.task_id, {'recipient_progress':values}))
        emit(task)
    if args.command == 'sync-task':
        emit(DwsWorkflow(store).sync_all({args.task_id}))
    if args.command == "report":
        text = store.daily_report() if args.kind == "daily" else store.weekly_report()
        if args.output:
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            emit({"output": str(target.resolve()), "kind": args.kind})
        emit({"kind": args.kind, "markdown": text})
    if args.command == "backup":
        emit({"backup": str(store.backup(args.output_dir))})
    if args.command == "export":
        emit({"export": str(store.export_json(args.output))})
    if args.command == "parse-notice":
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        emit(parse_notice(args.text, key, args.model, args.base_url))
    if args.command == "dws-status":
        emit(DwsClient().auth_status())
    if args.command == "sync-dws":
        emit(DwsWorkflow(store).sync_all())
    if args.command == "publish-dws":
        emit(DwsWorkflow(store).publish(args.task_id))


if __name__ == "__main__":
    try:
        main()
    except (ValidationError, DeepSeekError, DwsError) as exc:
        emit({"error": type(exc).__name__, "message": str(exc)}, 2)
    except KeyboardInterrupt:
        emit({"error": "interrupted"}, 130)
    except Exception as exc:  # Avoid a stack trace being mistaken for successful JSON by an agent.
        emit({"error": type(exc).__name__, "message": str(exc)}, 1)
