"""UI regression checks using only an in-memory store and disabled services."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import date
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from design_ui import DesignApp
from core import STATUS_LABELS


class DesignTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = MagicMock()
        self.store.task_dir = Path(temporary.name)/'Tasks'
        self.tasks = [dict(id=str(i), title=f'UI-CARD-{i}', status=state,
                           status_label=STATUS_LABELS[state],
                           due_at=date.today().isoformat()+'T17:00:00+08:00')
                      for i, state in enumerate(['open']+['done']*30)]
        self.store.list_tasks.return_value = self.tasks
        self.store.get_task.side_effect = lambda tid: next((t for t in self.tasks if t['id']==tid), None)
        for target in ['app.TaskStore', 'app.DwsWorkflow', 'app.ensure_sync_service',
                       'design_ui.DesignApp._start_tray', 'design_ui.DesignApp.check_reminders',
                       'design_ui.DesignApp.refresh_periodically']:
            p = patch(target, return_value=self.store if target=='app.TaskStore' else None)
            p.start()
            self.addCleanup(p.stop)
        self.ui = DesignApp()
        self.addCleanup(self.close_ui)
        self.ui.update()

    def close_ui(self):
        for job in self.ui.tk.call('after', 'info'):
            self.ui.after_cancel(job)
        self.ui.destroy()

    def texts(self, widget):
        result = []
        if 'text' in widget.keys():
            result.append(str(widget.cget('text')))
        for child in widget.winfo_children():
            result.extend(self.texts(child))
        return result

    def test_calendar_excludes_completed_on_all_refresh_routes(self):
        self.ui.navigate('calendar')
        for action in [self.ui.calendar.show_today,
                       lambda: self.ui.calendar.shift_month(1),
                       lambda: self.ui.calendar.shift_month(-1)]:
            action()
            text = ' '.join(self.texts(self.ui.calendar.days))
            self.assertNotIn('已完成', text)
        self.assertIn('UI-CARD-0', text)

    def test_completed_cards_compact_and_scrollable(self):
        self.ui.navigate('done')
        self.ui.update()
        self.assertEqual(len(self.ui.completed_cards), 30)
        card = self.ui.completed_cards[0]
        self.assertEqual(card.winfo_width(), 286)
        self.assertEqual(card.winfo_height(), 126)
        self.assertGreater(int(self.ui.completed_cards[1].grid_info()['column']), 0)
        self.ui.completed.canvas.yview_moveto(1)
        self.ui.update()
        self.assertGreater(self.ui.completed.canvas.yview()[0], 0)
        self.ui.geometry('1120x680')
        self.ui.update()
        self.assertLessEqual(max(c.winfo_x()+c.winfo_width() for c in self.ui.completed_cards),
                             self.ui.completed.body.winfo_width())

    def test_menu_actions_and_scroll_style(self):
        callback = MagicMock()
        self.ui.show_menu([('测试菜单', callback)])
        self.ui.update()
        self.ui._popover.winfo_children()[0].content.winfo_children()[0].invoke()
        self.ui.update()
        callback.assert_called_once()
        self.assertFalse(self.ui._popover.winfo_exists())
        bar = self.ui.completed.winfo_children()[1]
        self.assertEqual(bar.cget('style'), 'Soft.Vertical.TScrollbar')

    def test_real_menu_click_and_no_clipped_rows(self):
        with patch.object(self.ui, 'show_report') as report:
            self.ui.more_button.event_generate('<ButtonPress-1>', x=30, y=18)
            self.ui.more_button.event_generate('<ButtonRelease-1>', x=30, y=18)
            self.ui.update()
            menu = self.ui._popover
            self.assertTrue(menu.winfo_viewable())
            buttons = menu.winfo_children()[0].content.winfo_children()
            self.assertTrue(all(b.winfo_height() >= b.winfo_reqheight() for b in buttons))
            button = next(b for b in buttons if b.cget('text') == '每日清单')
            button.event_generate('<Enter>')
            button.event_generate('<ButtonPress-1>', x=20, y=15)
            button.event_generate('<ButtonRelease-1>', x=20, y=15)
            self.ui.update()
            report.assert_called_once_with('daily')

    def test_search_debounce_accepts_programmatic_and_paste_updates(self):
        self.store.list_tasks.side_effect = lambda q='', status=None, include_done=True: [
            t for t in self.tasks if q.lower() in t['title'].lower()]
        self.ui.search.set('UI-CARD-0')
        self.ui.after_cancel(self.ui._search_job)
        self.ui.run_search()
        text = ' '.join(self.texts(self.ui.board))
        self.assertIn('UI-CARD-0', text)
        self.assertNotIn('UI-CARD-1', text)
        self.ui.clear_search_button.event_generate('<ButtonRelease-1>', x=20, y=18)
        self.assertEqual(self.ui.search.get(), '')

    def test_drag_release_calls_target_and_plain_click_opens_editor(self):
        self.ui.update()
        from types import SimpleNamespace
        target = self.ui.columns['done'][0]
        event = SimpleNamespace(x_root=target.winfo_rootx()+25, y_root=target.winfo_rooty()+50)
        with patch.object(self.ui, 'move_task_to') as move, patch.object(self.ui, 'edit_task_by_id') as edit:
            self.ui.begin_drag(SimpleNamespace(x_root=50, y_root=50), '0')
            self.ui.drag_motion(event)
            self.ui.refresh()  # A service refresh must not destroy the dragged card.
            self.ui.end_drag(event)
            move.assert_called_once_with('0', 'done')
            edit.assert_not_called()
            self.ui.begin_drag(event, '0')
            self.ui.end_drag(event)
            edit.assert_called_once_with('0')

    def test_editor_default_and_one_time_workflow(self):
        self.ui.new_task()
        self.assertEqual(self.ui.editor_payload()['workflow_mode'], 'staged')
        self.ui.workflow_menu()
        self.ui.update()
        buttons = self.ui._popover.winfo_children()[0].content.winfo_children()
        next(b for b in buttons if b.cget('text') == '一次性通知').invoke()
        self.ui.update()
        self.assertEqual(self.ui.editor_payload()['workflow_mode'], 'notice')

    def test_drag_preview_follows_pointer_and_escape_keeps_task(self):
        from types import SimpleNamespace
        source = self.ui.columns['open'][2].body.winfo_children()[0]
        self.ui.begin_drag(SimpleNamespace(x_root=source.winfo_rootx()+25, y_root=source.winfo_rooty()+25),
                           '0', source, self.tasks[0])
        start = SimpleNamespace(x_root=600, y_root=350)
        self.ui.drag_motion(start)
        self.ui.update_idletasks()
        preview = self.ui._drag_preview
        self.assertTrue(preview.winfo_viewable())
        position = (preview.winfo_x(), preview.winfo_y())
        self.ui.drag_motion(SimpleNamespace(x_root=680, y_root=400))
        self.ui.update_idletasks()
        self.assertEqual((preview.winfo_x()-position[0], preview.winfo_y()-position[1]), (80, 50))
        self.ui.cancel_drag()
        self.assertFalse(preview.winfo_exists())
        self.assertIsNone(self.ui._drag_scroll_job)
        self.store.move_task.assert_not_called()

    def test_trash_navigation_restoration_and_drag_target(self):
        deleted = {**self.tasks[0], 'deleted_at': date.today().isoformat()+'T10:00:00+08:00'}
        self.store.deleted_tasks.return_value = [deleted]
        self.ui.navigate('trash')
        self.ui.update()
        self.assertTrue(self.ui.trash.winfo_ismapped())
        self.assertIn('UI-CARD-0', ' '.join(self.texts(self.ui.trash)))
        restore_button = self.ui.trash.body.winfo_children()[1].content.winfo_children()[0]
        self.assertEqual(restore_button.itemcget(restore_button.text_item, 'text'), '恢复事项')
        self.store.restore_task.return_value = deleted
        with patch('design_ui.PublishLock'), patch.object(self.ui, 'queue_auto_sync') as sync:
            restore_button.event_generate('<ButtonRelease-1>', x=20, y=18)
            self.store.restore_task.assert_called_once_with('0')
            sync.assert_called_once()
        button = self.ui.nav['trash']
        self.assertEqual(self.ui.drop_target(button.winfo_rootx()+20, button.winfo_rooty()+20), 'trash')
        self.ui.navigate('board')

    def test_backup_and_export_menu_actions(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, patch('design_ui.messagebox.showinfo'), patch('design_ui.filedialog.asksaveasfilename', return_value=str(Path(tmp)/'export.json')):
            for label in ['备份所有事项', '导出 JSON']:
                self.ui.storage_menu()
                self.ui.update()
                buttons = self.ui._popover.winfo_children()[0].content.winfo_children()
                next(b for b in buttons if b.cget('text') == label).invoke()
                self.ui.update()
            self.store.backup.assert_called_once()
            self.store.export_json.assert_called_once()


if __name__ == '__main__':
    unittest.main()
