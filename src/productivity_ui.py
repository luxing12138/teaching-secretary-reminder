"""Optional UI controls layered over the existing card workspace."""
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from core import STATUS_LABELS, ValidationError, now_local
from dws_integration import PublishLock
from productivity import ActionHistory, Templates, SyncStatus, recipient_progress


class ProductivityUI:
    def init_productivity(self):
        self.history = ActionHistory(self.store)
        self.templates = Templates(self.store)
        self.sync_status = SyncStatus(self.store)
        self._sync_status_revision = self.sync_status.revision()
        self._focus_minute = None
        self.selection_mode = False
        self.selected_ids = set()
        self.visible_tasks = []
        self.progress_vars = {}

    def build_productivity_controls(self, filters):
        from design_ui import SoftButton, BG, MUTED
        self.undo_button = SoftButton(filters, '撤销', self.undo_latest, width=68)
        self.undo_button.pack(side='left', padx=4)
        self.bulkbar = tk.Frame(self.main, bg=BG)
        self.bulk_count = tk.Label(self.bulkbar, text='已选 0 项', bg=BG, fg=MUTED)
        self.bulk_count.pack(side='left', padx=8)
        for label, fn, width in [('全选当前', self.select_visible, 90), ('移至…', self.bulk_move_menu, 80),
                                 ('删除所选', self.bulk_delete, 90), ('退出批量', self.leave_selection, 90)]:
            SoftButton(self.bulkbar, label, fn, width=width).pack(side='left', padx=4)
        self.bind('<Control-z>', self.undo_shortcut)

    def refresh_periodically(self):
        try:
            revision = self.store.revision()
            status_revision = self.sync_status.revision()
            minute = now_local().strftime('%Y-%m-%d %H:%M')
            if revision != self._store_revision:
                self.refresh()
                self.queue_auto_sync()
            elif status_revision != self._sync_status_revision or minute != self._focus_minute:
                self.refresh()
        finally:
            self.after(1000, self.refresh_periodically)

    def update_productivity_controls(self, tasks):
        self.visible_tasks = tasks
        self.selected_ids.intersection_update(task['id'] for task in tasks)
        self.bulk_count.configure(text=f'已选 {len(self.selected_ids)} 项')
        self._sync_status_revision = self.sync_status.revision()
        self._focus_minute = now_local().strftime('%Y-%m-%d %H:%M')

    def undo_shortcut(self, event):
        if isinstance(self.focus_get(), (tk.Text, tk.Entry, ttk.Entry, ttk.Combobox)):
            return
        self.undo_latest()
        return 'break'

    def mutation_ready(self):
        if self._syncing:
            messagebox.showinfo('正在同步', '请等当前同步结束后重试。', parent=self)
            return False
        if self.detail.winfo_ismapped() and self.is_dirty():
            messagebox.showinfo('请先保存', '请先保存或取消右侧修改。', parent=self)
            return False
        return True

    def undo_latest(self):
        if not self.mutation_ready():
            return
        try:
            with PublishLock(self.store.task_dir, timeout=0.2):
                label = self.history.undo()
            if self.detail.winfo_ismapped():
                self.detail.pack_forget()
                self.editor_vars = {}
                self.editor_task = None
            self.refresh()
            self.status_line.configure(text=f'已撤销：{label}')
            self.queue_auto_sync()
        except (ValidationError, OSError, RuntimeError) as exc:
            messagebox.showinfo('无法撤销', str(exc), parent=self)

    def enter_selection(self):
        if self.page in {'trash', 'calendar'}:
            self.navigate('board')
        self.selection_mode = True
        self.bulkbar.pack(fill='x', padx=24, pady=(0,10), before=self.workspace)
        self.refresh()

    def leave_selection(self):
        self.selection_mode = False
        self.selected_ids.clear()
        self.bulkbar.pack_forget()
        self.refresh()

    def toggle_selection(self, task_id):
        if task_id in self.selected_ids:
            self.selected_ids.remove(task_id)
        else:
            self.selected_ids.add(task_id)
        self.after_idle(self.refresh)

    def select_visible(self):
        self.selected_ids = {task['id'] for task in self.visible_tasks}
        self.refresh()

    def bulk_move_menu(self):
        self.show_menu([(label, lambda code=code: self.bulk_apply(code))
                        for code, label in STATUS_LABELS.items()])

    def bulk_delete(self):
        self.bulk_apply('delete')

    def bulk_apply(self, target):
        if not self.selected_ids:
            messagebox.showinfo('尚未选择', '请勾选需要处理的事项。', parent=self)
            return
        if not self.mutation_ready():
            return
        ids = sorted(self.selected_ids)
        label = '批量删除' if target == 'delete' else '批量移至'+STATUS_LABELS[target]
        if not messagebox.askyesno(label, f'{label}，共 {len(ids)} 项？'+
                                  ('\n本地文件可在回收站恢复，对应钉钉待办同步删除。' if target == 'delete' else ''), parent=self):
            return
        try:
            with PublishLock(self.store.task_dir, timeout=0.2):
                tasks = [self.store.get_task(tid) for tid in ids]
                if any(task is None for task in tasks):
                    raise ValidationError('部分事项已被其他程序更改，请刷新后重新选择')
                if target in {'notified', 'followed-up'} and any(t.get('workflow_mode') == 'notice' for t in tasks):
                    raise ValidationError('所选包含一次性通知，它只使用待传达和已完成状态')
                def operation():
                    for tid in ids:
                        self.store.delete_task(tid) if target == 'delete' else self.store.move_task(tid, target)
                self.history.perform(label, ids, operation)
            self.selected_ids.clear()
            if self.editor_task and self.editor_task['id'] in ids:
                self.detail.pack_forget()
                self.editor_vars = {}
                self.editor_task = None
            self.refresh()
            self.queue_auto_sync()
        except (ValidationError, OSError, RuntimeError) as exc:
            self.refresh()
            messagebox.showerror('批量操作未全部完成', str(exc)+'\n已成功的操作可用“撤销”恢复。', parent=self)
            self.queue_auto_sync()

    def template_menu(self):
        self.show_menu([(item['name'], lambda item=item: self.open_template(item)) for item in self.templates.list()])

    def open_template(self, template):
        self.open_editor(None, self.templates.draft(template))

    def save_template(self):
        if self.detail.winfo_ismapped():
            payload = self.editor_payload()
        else:
            payload = self.selected_task()
            if not payload:
                return
        name = simpledialog.askstring('保存为模板', '模板名称：', initialvalue=payload.get('title', ''), parent=self)
        if name:
            try:
                self.templates.save(name, payload)
                self.status_line.configure(text='模板已保存；从“更多→从模板新建”使用')
            except (ValidationError, OSError) as exc:
                messagebox.showerror('无法保存模板', str(exc), parent=self)

    def setup_progress_editor(self, parent, data):
        from design_ui import MUTED
        self.progress_vars = {}
        self._progress_seed = recipient_progress(data)
        self.progress_frame = tk.Frame(parent, bg='white')
        self.progress_frame.pack(fill='x', pady=(14,4))
        self.editor_vars['recipients'].trace_add('write', lambda *_: self.render_progress_editor())
        self.workflow_var.trace_add('write', lambda *_: self.render_progress_editor())
        self.render_progress_editor()
        tk.Label(parent, text='勾选仅记录进度，保存后生效，不自动推进事项。', bg='white', fg=MUTED,
                 wraplength=260, justify='left', font=('Microsoft YaHei UI', 8)).pack(anchor='w')

    def render_progress_editor(self):
        from design_ui import INK, MUTED
        previous = {name: {field: var.get() for field, var in values.items()} for name, values in self.progress_vars.items()}
        names = list(dict.fromkeys(name.strip() for name in self.editor_vars['recipients'].get().replace('，', ',').split(',') if name.strip()))
        for child in self.progress_frame.winfo_children():
            child.destroy()
        self.progress_vars = {}
        tk.Label(self.progress_frame, text='按人记录进度', bg='white', fg=MUTED, anchor='w').pack(fill='x')
        for name in names:
            row = tk.Frame(self.progress_frame, bg='white')
            row.pack(fill='x', pady=3)
            tk.Label(row, text=name, bg='white', fg=INK, anchor='w', wraplength=90, width=9).pack(side='left')
            values = previous.get(name, self._progress_seed.get(name, {}))
            self.progress_vars[name] = {}
            for field, label in [('notified', '已通知'), ('feedback', '已反馈')]:
                var = tk.BooleanVar(value=values.get(field, False))
                self.progress_vars[name][field] = var
                if field == 'feedback' and self.workflow_var.get() == '一次性通知':
                    continue
                tk.Checkbutton(row, text=label, variable=var, bg='white', activebackground='white',
                               fg=INK, selectcolor='#EAF4EE', font=('Microsoft YaHei UI', 8)).pack(side='left')
        if not names:
            tk.Label(self.progress_frame, text='填写通知对象后可逐人勾选', bg='white', fg=MUTED,
                     font=('Microsoft YaHei UI', 8)).pack(anchor='w', pady=5)

    def progress_payload(self):
        return {name: {field: var.get() for field, var in values.items()} for name, values in self.progress_vars.items()}

    def add_card_sync(self, card, task):
        from design_ui import MUTED, GREEN
        values = recipient_progress(task)
        if values:
            notified = sum(value['notified'] for value in values.values())
            feedback = sum(value['feedback'] for value in values.values())
            text = f'已通知 {notified}/{len(values)}'
            if task.get('workflow_mode') != 'notice':
                text += f' · 已反馈 {feedback}/{len(values)}'
            tk.Label(card.content, text=text, bg='white', fg=MUTED, anchor='w',
                     font=('Microsoft YaHei UI', 8)).pack(fill='x', padx=8, pady=(5,0))
        status = self.sync_status.get(task)
        pending_cleanup = task.get('metadata', {}).get('retired_todo_ids')
        if not task.get('external_sync_enabled', 1) and not pending_cleanup:
            text, color = ('已完成 · 无提醒' if task['status']=='done' else '本地事项'), MUTED
        elif self._syncing and getattr(self, '_sync_task_id', None) in (None, task['id']):
            text, color = '正在同步…', MUTED
        elif status['state'] == 'error':
            text, color = '同步失败 · 点击重试', '#B36D70'
        elif status['state'] == 'ok':
            text, color = '钉钉已同步', GREEN
        else:
            text, color = '等待同步', MUTED
        widget = tk.Label(card.content, text=text, bg='white', fg=color, anchor='w',
                          font=('Microsoft YaHei UI', 8), cursor='hand2')
        widget.pack(fill='x', padx=8, pady=(4,0))
        widget.bind('<ButtonPress-1>', lambda e: 'break')
        def retry(_event):
            self.sync_dingtalk(silent=True, task_id=task['id'])
            return 'break'
        widget.bind('<ButtonRelease-1>', retry)
        def explain(_event):
            if status.get('error'):
                self.status_line.configure(text='同步失败：'+str(status['error']).replace('\n',' ')[:240])
            elif status.get('checked_at'):
                self.status_line.configure(text='上次同步确认：'+status['checked_at'])
        widget.bind('<Enter>', explain)
        return widget
