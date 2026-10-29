from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from core import TaskStore  # noqa: E402
from dws_integration import DwsWorkflow  # noqa: E402


class FakeDws:
    def __init__(self) -> None:
        self.counter = 0
        self.done: dict[str, bool] = {}
        self.reminders: dict[str, list[dict]] = {}
        self.reminder_writes: list[tuple[str, list[dict]]] = []

    def auth_status(self):
        return {"authenticated": True, "user_id": "test-user"}

    def current_user_id(self, _auth):
        return "test-user"

    def create_todo(self, _title, _due, _executor):
        self.counter += 1
        task_id = f"todo-{self.counter}"
        self.done[task_id] = False
        return task_id

    def update_todo(self, *_args):
        return None

    def delete_todo(self, todo_id):
        self.done.pop(todo_id, None)
        self.reminders.pop(todo_id, None)

    def reset_reminders(self, todo_id, rules):
        copied = [dict(rule) for rule in rules]
        self.reminders[todo_id] = copied
        self.reminder_writes.append((todo_id, copied))

    def get_todo(self, todo_id):
        return {"isDone": self.done[todo_id]}

    def todo_done(self, value):
        return bool(value["isDone"])


class DwsWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "tasks.sqlite3")
        self.dws = FakeDws()
        self.workflow = DwsWorkflow(self.store, self.dws)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_three_dingtalk_completions_advance_one_stage_each(self) -> None:
        task = self.store.create_task({"title": "培训心得", "due": "2099-10-31 10:00"})
        first = self.workflow.publish(task["id"])
        self.assertEqual(first["external_todo_id"], "todo-1")
        self.assertEqual(first["external_target_status"], "notified")
        self.dws.done["todo-1"] = True
        self.workflow.sync_all()
        second = self.store.get_task(task["id"])
        self.assertEqual(second["status"], "notified")
        self.assertEqual(second["external_todo_id"], "todo-2")
        self.dws.done["todo-2"] = True
        self.workflow.sync_all()
        third = self.store.get_task(task["id"])
        self.assertEqual(third["status"], "followed-up")
        self.assertEqual(third["external_todo_id"], "todo-3")
        self.assertEqual(self.dws.reminders["todo-3"], [{"baseTime": "dueTime", "dueDateOffset": 0}])
        self.dws.done["todo-3"] = True
        self.workflow.sync_all()
        completed = self.store.get_task(task["id"])
        self.assertEqual(completed["status"], "done")
        self.assertEqual(completed["external_todo_id"], "")
        self.assertFalse(completed["external_sync_enabled"])

    def test_repeated_open_sync_preserves_the_first_immediate_reminder(self) -> None:
        task = self.store.create_task({"title": "首次传达", "due": "2099-10-31 10:00"})
        self.workflow.publish(task["id"])
        first_rule = self.dws.reminders["todo-1"]
        self.assertEqual(len(self.dws.reminder_writes), 1)

        self.workflow.sync_all()

        self.assertEqual(len(self.dws.reminder_writes), 1)
        self.assertEqual(self.dws.reminders["todo-1"], first_rule)

    def test_repeated_catch_up_sync_preserves_existing_stage_reminder(self) -> None:
        task = self.store.create_task({"title": "催办测试", "due": "2026-10-24 17:00"})
        self.workflow.publish(task["id"])
        self.dws.done["todo-1"] = True
        self.workflow.sync_all()
        second = self.store.get_task(task["id"])
        second_rule = self.dws.reminders[second["external_todo_id"]]
        writes = len(self.dws.reminder_writes)

        self.workflow.sync_all()

        self.assertEqual(len(self.dws.reminder_writes), writes)
        self.assertEqual(self.dws.reminders[second["external_todo_id"]], second_rule)

    def test_one_time_notice_completes_once_without_followup(self):
        task = self.store.create_task({'title': '转发通知', 'due': '2099-10-31', 'workflow_mode': 'notice'})
        first = self.workflow.publish(task['id'])
        self.assertEqual(first['external_target_status'], 'done')
        self.dws.done[first['external_todo_id']] = True
        self.workflow.sync_all()
        self.workflow.sync_all()
        result = self.store.get_task(task['id'])
        self.assertEqual(result['status'], 'done')
        self.assertEqual(self.dws.counter, 1)
        self.assertEqual(self.store.due_reminders(), [])

    def test_manual_move_does_not_consume_old_completion(self):
        task = self.store.create_task({'title': '拖动', 'due': '2099-10-31'})
        self.workflow.publish(task['id'])
        self.dws.done['todo-1'] = True
        self.store.move_task(task['id'], 'followed-up')
        self.workflow.sync_all()
        result = self.store.get_task(task['id'])
        self.assertEqual(result['status'], 'followed-up')
        self.assertNotIn('todo-1', self.dws.done)
        self.assertEqual(result['external_todo_id'], 'todo-2')
        self.store.move_task(task['id'], 'done')
        self.workflow.sync_all()
        self.assertFalse(self.dws.done)
        self.store.move_task(task['id'], 'open')
        self.workflow.sync_all()
        self.assertEqual(self.store.get_task(task['id'])['external_todo_id'], 'todo-3')

    def test_deleted_task_stays_deleted_and_retries_remote_cleanup(self):
        from unittest.mock import patch
        from dws_integration import DwsError
        task = self.store.create_task({'title': '删除', 'due': '2099-10-31'})
        self.workflow.publish(task['id'])
        trash = self.store.delete_task(task['id'])
        self.assertTrue(trash.exists())
        self.assertIsNone(self.store.get_task(task['id']))
        with patch.object(self.dws, 'delete_todo', side_effect=DwsError('offline')):
            self.assertEqual(self.workflow.sync_all()[0]['action'], 'error')
        self.workflow.sync_all()
        self.assertNotIn('todo-1', self.dws.done)
        reopened = TaskStore(self.store.task_dir)
        self.assertEqual(reopened.list_tasks(), [])
        self.assertTrue(trash.exists())

    def test_restore_before_and_after_remote_deletion_recreates_one_todo(self):
        for cleaned_before_restore in (False, True):
            with self.subTest(cleaned_before_restore=cleaned_before_restore):
                task = self.store.create_task({'title': f'恢复通知-{cleaned_before_restore}',
                                              'due': '2099-10-31', 'workflow_mode': 'notice'})
                published = self.workflow.publish(task['id'])
                old_id = published['external_todo_id']
                trash = self.store.delete_task(task['id'])
                if cleaned_before_restore:
                    self.workflow.sync_all()
                restored = self.store.restore_task(task['id'])
                self.assertEqual(restored['workflow_mode'], 'notice')
                self.assertEqual(restored['status'], 'open')
                self.assertFalse(trash.exists())
                self.workflow.sync_all()
                active = self.store.get_task(task['id'])
                self.assertNotEqual(active['external_todo_id'], old_id)
                self.assertNotIn(old_id, self.dws.done)
                self.assertEqual(active['external_target_status'], 'done')


if __name__ == "__main__":
    unittest.main()
