import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from core import TaskStore, ValidationError, parse_datetime
from productivity import ActionHistory, Templates, SyncStatus, focus_tasks, recipient_progress
from dws_integration import DwsWorkflow, DwsError
from tests.test_dws_workflow import FakeDws


class ProductivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TaskStore(Path(self.temp.name)/'Tasks')
        self.history = ActionHistory(self.store)
        self.client = FakeDws()
        self.workflow = DwsWorkflow(self.store, self.client)

    def task(self, **fields):
        return self.store.create_task({'title': '测试事项', 'due': '2099-10-31 10:00', **fields})

    def test_edit_undo_retains_adapter_state_and_reminder(self):
        task = self.task()
        current = self.workflow.publish(task['id'])
        self.history.perform('编辑', [task['id']], lambda: self.store.update_task(task['id'],
                             {'title': '新标题', 'due_at': current['due_at']}))
        self.store.set_external_state(task['id'], error='temporary error')
        ActionHistory(self.store).undo()  # Survives restart; sync metadata isn't user data.
        result = self.store.get_task(task['id'])
        self.assertEqual(result['title'], '测试事项')
        self.assertEqual(result['external_todo_id'], current['external_todo_id'])
        self.assertEqual(result['external_reminder_hash'], current['external_reminder_hash'])
        self.assertEqual(len(result['reminders']), len(current['reminders']))

    def test_undo_move_after_sync_cannot_restore_old_todo_id(self):
        task = self.task()
        self.workflow.publish(task['id'])
        self.history.perform('移动', [task['id']], lambda: self.store.move_task(task['id'], 'done'))
        self.workflow.sync_all()
        self.history.undo()
        self.workflow.sync_all()
        result = self.store.get_task(task['id'])
        self.assertEqual(result['status'], 'open')
        self.assertEqual(result['external_todo_id'], 'todo-2')
        self.assertNotIn('todo-1', self.client.done)

    def test_undo_rejects_later_dingtalk_completion(self):
        task = self.task()
        self.history.perform('编辑', [task['id']], lambda: self.store.update_task(task['id'], {'title':'改名'}))
        self.workflow.publish(task['id'])
        self.client.done['todo-1'] = True
        self.workflow.sync_all()
        with self.assertRaises(ValidationError):
            self.history.undo()
        self.assertEqual(self.store.get_task(task['id'])['status'], 'notified')

    def test_batch_undo_and_partial_failure_preserve_success(self):
        tasks = [self.task(title=f'事项{i}') for i in range(2)]
        ids = [t['id'] for t in tasks]
        def partial():
            self.store.delete_task(ids[0])
            raise OSError('simulate second file locked')
        with self.assertRaises(OSError):
            self.history.perform('批量删除', ids, partial)
        self.history.undo()
        self.assertEqual(len(self.store.list_tasks()), 2)
        self.history.perform('批量完成', ids, lambda: [self.store.move_task(tid, 'done') for tid in ids])
        self.history.undo()
        self.assertTrue(all(t['status']=='open' for t in self.store.list_tasks()))

    def test_new_task_and_restore_can_be_undone(self):
        task = self.task()
        self.history.created(task['id'])
        self.history.undo()
        self.assertIsNone(self.store.get_task(task['id']))
        self.history.perform('恢复', [task['id']], lambda: self.store.restore_task(task['id']))
        self.history.undo()
        self.assertIsNone(self.store.get_task(task['id']))

    def test_per_person_progress_does_not_advance_or_reset_reminders(self):
        task = self.task(recipients=['张老师', '李老师'])
        published = self.workflow.publish(task['id'])
        progress = {'张老师': {'notified':True, 'feedback':True}, '李老师':{'notified':True, 'feedback':False}}
        result = self.store.update_task(task['id'], {'due_at': published['due_at'], 'recipient_progress': progress})
        result = self.store.get_task(task['id'])
        self.assertEqual(result['status'], 'open')
        self.assertEqual(recipient_progress(result), progress)
        self.assertEqual(result['external_todo_id'], published['external_todo_id'])
        self.assertEqual(result['external_reminder_hash'], published['external_reminder_hash'])
        self.assertEqual(len(result['reminders']), len(published['reminders']))

    def test_templates_keep_time_but_not_old_identity_or_deadline(self):
        task = self.task(source_id='old-notice-id', workflow_mode='notice', recipients=['张老师'])
        templates = Templates(self.store)
        templates.save('自定义通知', task)
        item = templates.list()[-1]
        draft = templates.draft(item, parse_datetime('2030-01-02 09:00'))
        self.assertEqual(draft['due_at'], '2030-01-02T10:00')
        self.assertEqual(draft['workflow_mode'], 'notice')
        self.assertNotIn('id', draft)
        self.assertNotIn('source_id', draft)
        self.assertNotIn('external_todo_id', draft)

    def test_sync_indicator_is_separate_and_does_not_dirty_markdown(self):
        task = self.task()
        current = self.workflow.publish(task['id'])
        revision = self.store.revision()
        status = SyncStatus(self.store)
        self.assertEqual(status.get(current)['state'], 'ok')
        status.record(current)
        self.assertEqual(self.store.revision(), revision)
        changed = self.store.update_task(task['id'], {'title':'修改标题'})
        self.assertEqual(status.get(changed)['state'], 'pending')
        with patch.object(self.client, 'auth_status', side_effect=DwsError('offline')):
            with self.assertRaises(DwsError):
                self.workflow.sync_all()
        self.assertEqual(status.get(self.store.get_task(task['id']))['state'], 'error')

    def test_targeted_retry_does_not_publish_other_tasks(self):
        first, second = self.task(title='A'), self.task(title='B')
        self.workflow.sync_all({first['id']})
        self.assertTrue(self.store.get_task(first['id'])['external_todo_id'])
        self.assertFalse(self.store.get_task(second['id'])['external_todo_id'])

    def test_today_and_overdue_boundaries(self):
        tasks = [self.task(title='昨日', due='2030-01-01 17:00'),
                 self.task(title='今日已过点', due='2030-01-02 08:00'),
                 self.task(title='今日稍后', due='2030-01-02 17:00'),
                 self.task(title='已完成', due='2030-01-02', status='done')]
        at = parse_datetime('2030-01-02 09:00')
        self.assertEqual([t['title'] for t in focus_tasks(tasks, 'today', at)], ['今日已过点','今日稍后'])
        self.assertEqual([t['title'] for t in focus_tasks(tasks, 'overdue', at)], ['昨日','今日已过点'])

    def test_multiple_changes_in_same_second_undo_in_reverse_order(self):
        task = self.task()
        with patch('productivity.now_local', return_value=parse_datetime('2030-01-02 09:00')):
            for i in range(6):
                self.history.perform(str(i), [task['id']], lambda i=i: self.store.update_task(task['id'], {'title':str(i)}))
            for i in range(5, -1, -1):
                self.assertEqual(self.history.undo(), str(i))
        self.assertEqual(self.store.get_task(task['id'])['title'], '测试事项')


if __name__ == '__main__':
    unittest.main()
