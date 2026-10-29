import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from core import TaskStore
from design_ui import DesignApp
from productivity import recipient_progress
from productivity_ui import ProductivityUI


class ProductivityUITests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = TaskStore(Path(folder.name)/'Tasks')
        self.first = self.store.create_task({'title':'材料收集', 'due':'2099-10-31', 'recipients':['张老师','李老师']})
        self.second = self.store.create_task({'title':'一次通知', 'due':'2099-10-31', 'workflow_mode':'notice'})
        for target in ['app.TaskStore','app.ensure_sync_service','app.DwsWorkflow',
                       'design_ui.DesignApp._start_tray','design_ui.DesignApp.check_reminders',
                       'design_ui.DesignApp.refresh_periodically','design_ui.DesignApp.queue_auto_sync']:
            p = patch(target, return_value=self.store if target=='app.TaskStore' else None)
            p.start()
            self.addCleanup(p.stop)
        self.ui = DesignApp()
        self.addCleanup(self.close_ui)
        self.ui.update()

    def close_ui(self):
        for job in self.ui.tk.call('after','info'):
            self.ui.after_cancel(job)
        self.ui.destroy()

    def test_editor_progress_save_and_undo(self):
        self.ui.edit_task_by_id(self.first['id'])
        self.ui.progress_vars['张老师']['notified'].set(True)
        self.ui.save_editor()
        task = self.store.get_task(self.first['id'])
        self.assertTrue(recipient_progress(task)['张老师']['notified'])
        self.assertEqual(task['status'], 'open')
        self.ui.undo_latest()
        self.assertFalse(recipient_progress(self.store.get_task(task['id']))['张老师']['notified'])

    def test_mixed_batch_validation_and_completion_undo(self):
        self.ui.enter_selection()
        self.ui.select_visible()
        with patch('productivity_ui.messagebox.askyesno', return_value=True), patch('productivity_ui.messagebox.showerror') as error:
            self.ui.bulk_apply('notified')
            error.assert_called_once()
        self.assertTrue(all(t['status']=='open' for t in self.store.list_tasks()))
        with patch('productivity_ui.messagebox.askyesno', return_value=True):
            self.ui.bulk_apply('done')
        self.assertTrue(all(t['status']=='done' for t in self.store.list_tasks()))
        self.ui.undo_latest()
        self.assertTrue(all(t['status']=='open' for t in self.store.list_tasks()))

    def test_template_is_a_draft_and_new_task_can_be_undone(self):
        self.ui.open_template(self.ui.templates.list()[0])
        self.assertEqual(self.ui.editor_payload()['workflow_mode'], 'notice')
        self.assertEqual(len(self.store.list_tasks()), 2)
        self.ui.editor_vars['title'].set('模板新建通知')
        self.ui.save_editor()
        self.assertEqual(len(self.store.list_tasks()), 3)
        self.ui.undo_latest()
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_sidecar_refresh_does_not_queue_a_sync_loop(self):
        self.ui.sync_status.record(self.store.get_task(self.first['id']))
        with patch.object(self.ui, 'queue_auto_sync') as queue:
            ProductivityUI.refresh_periodically(self.ui)
            queue.assert_not_called()

    def test_text_editing_does_not_trigger_task_undo(self):
        self.ui.new_task()
        with patch.object(self.ui, 'focus_get', return_value=self.ui.editor_text), patch.object(self.ui, 'undo_latest') as undo:
            self.ui.undo_shortcut(None)
            undo.assert_not_called()


if __name__ == '__main__':
    unittest.main()
