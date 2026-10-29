"""Packaged GUI acceptance in a temporary repository, without network calls."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace


def run(report_path):
    import app
    from core import TaskStore
    from design_ui import DesignApp

    with tempfile.TemporaryDirectory(prefix='tsr-startup-') as folder:
        store = TaskStore(Path(folder)/'Tasks')
        first = store.create_task({'title': '启动验收：完整流程', 'due': '2099-10-31 10:00'})
        store.create_task({'title': '启动验收：一次性通知', 'due': '2099-10-31', 'workflow_mode': 'notice'})
        app.TaskStore = lambda: store
        app.DwsWorkflow = lambda _: None
        app.ensure_sync_service = lambda _: None
        DesignApp._start_tray = lambda _: None
        DesignApp.check_reminders = lambda _: None
        DesignApp.refresh_periodically = lambda _: None
        DesignApp.queue_auto_sync = lambda _: None
        window = DesignApp()
        result = {'ok': False, 'isolated': True, 'network': False}
        def fail(_kind, error, _traceback):
            result['error'] = str(error)
            finish()
        window.report_callback_exception = fail
        def finish():
            for job in window.tk.call('after', 'info'):
                window.after_cancel(job)
            window.destroy()
        def verify():
            try:
                assert window.winfo_viewable()
                window.actions_menu()
                window.update_idletasks()
                assert window._popover.winfo_viewable()
                window._popover.destroy()
                panel = window.columns['done'][0]
                window.begin_drag(SimpleNamespace(x_root=20, y_root=20), first['id'])
                event = SimpleNamespace(x_root=panel.winfo_rootx()+30, y_root=panel.winfo_rooty()+55)
                window.drag_motion(event)
                window.update_idletasks()
                assert window._drag_preview.winfo_viewable()
                window.end_drag(event)
                assert store.get_task(first['id'])['status'] == 'done'
                window.navigate('done')
                window.update_idletasks()
                assert len(window.completed_cards) == 1
                store.delete_task(first['id'])
                window.navigate('trash')
                window.update_idletasks()
                assert window.trash.winfo_viewable()
                assert len(store.deleted_tasks()) == 1
                window.restore_task(first['id'])
                assert store.get_task(first['id'])['status'] == 'done'
                assert not store.deleted_tasks()
                window.open_template(window.templates.list()[0])
                window.editor_vars['title'].set('启动验收：模板与进度')
                window.editor_vars['recipients'].set('张老师，李老师')
                window.progress_vars['张老师']['notified'].set(True)
                window.save_editor()
                created_id = window.selected_id
                created = store.get_task(created_id)
                assert created['workflow_mode'] == 'notice'
                assert created['metadata']['recipient_progress']['张老师']['notified']
                window.undo_latest()
                assert store.get_task(created_id) is None
                window.navigate('board')
                window.enter_selection()
                window.select_visible()
                from tkinter import messagebox
                messagebox.askyesno = lambda *args, **kwargs: True
                window.bulk_apply('done')
                assert all(task['status'] == 'done' for task in store.list_tasks())
                window.undo_latest()
                assert any(task['status'] == 'open' for task in store.list_tasks())
                window.leave_selection()
                window.navigate('today')
                assert window.heading.cget('text') == '今日待办'
                window.navigate('overdue')
                assert window.heading.cget('text') == '已逾期'
                result.update(ok=True, title=window.title(), drag=True, popup=True,
                              compact_cards=True, floating_drag=True, trash_restore=True,
                              templates=True, recipient_progress=True, undo=True, bulk=True, focus_views=True,
                              geometry=window.geometry())
            except Exception as exc:
                import traceback
                result['error'] = traceback.format_exc()
            finally:
                finish()
        window.after(700, verify)
        window.mainloop()
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if result['ok'] else 1
