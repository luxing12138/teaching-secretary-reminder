"""Card workspace: presentation layer over the existing reminder workflow."""
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import timedelta

from app import ReminderApp, PastelCard, MonthlyCalendar, NoticeParser, PALETTE, STATUS_COLORS
from core import STATUS_LABELS, ValidationError, now_local, parse_datetime
from dws_integration import PublishLock
from productivity import focus_tasks
from productivity_ui import ProductivityUI

BG = '#FAFAF8'
INK = '#202A40'
MUTED = '#87909E'
GREEN = '#668F70'
COLORS = {'open': ('#EAF4EE', '#548765'), 'notified': ('#FFF3E3', '#BA853E'),
          'followed-up': ('#EAF2FF', '#537EBA'), 'done': ('#F2ECFC', '#9273BE')}
WORKFLOW_LABELS = {'staged': '完整流程', 'notice': '一次性通知'}


class ScrollArea(tk.Frame):
    def __init__(self, parent, color=BG):
        super().__init__(parent, bg=color)
        self.canvas = tk.Canvas(self, bg=color, highlightthickness=0, width=100)
        bar = ttk.Scrollbar(self, command=self.canvas.yview, style='Soft.Vertical.TScrollbar')
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.body = tk.Frame(self.canvas, bg=color)
        self.window = self.canvas.create_window(0, 0, anchor='nw', window=self.body)
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.body.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<MouseWheel>', self.wheel)

    def wheel(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), 'units')


class SoftButton(tk.Canvas):
    def __init__(self, parent, text, command, primary=False, width=110):
        super().__init__(parent, width=width, height=38, bg=parent.cget('bg'), highlightthickness=0, cursor='hand2', takefocus=True)
        fill = GREEN if primary else '#EEF3EF'
        self.shape = PastelCard._rounded(self, 1, 1, width-1, 37, 12, fill=fill, outline='')
        self.text_item = self.create_text(width/2, 19, text=text, fill='white' if primary else '#465B50', font=('Microsoft YaHei UI', 10))
        self.command = command
        self.bind('<ButtonRelease-1>', lambda e: command())
        self.bind('<Return>', lambda e: command())
        self.bind('<Enter>', lambda e: self.itemconfigure(self.shape, fill='#79A183' if primary else '#E2ECE5'))
        self.bind('<Leave>', lambda e: self.itemconfigure(self.shape, fill=fill))


class DragPreview(tk.Toplevel):
    """A light floating copy; the original card retains its place until drop."""
    def __init__(self, parent, task, width=272):
        super().__init__(parent, takefocus=False)
        self.withdraw()
        self.overrideredirect(True)
        self.transient(parent)
        self.attributes('-topmost', True)
        self.attributes('-alpha', 0.95)
        transparent = '#FF00FF'
        try:
            self.attributes('-transparentcolor', transparent)
        except tk.TclError:
            transparent = BG
        canvas = tk.Canvas(self, width=width, height=144, bg=transparent,
                           bd=0, highlightthickness=0)
        canvas.pack()
        for offset, color in [(12, '#DCE2DD'), (8, '#E5EBE6'), (4, '#EFF3EF')]:
            PastelCard._rounded(canvas, 5+offset, 3+offset, width-5, 138, 18,
                                fill=color, outline='')
        PastelCard._rounded(canvas, 3, 2, width-14, 128, 16, fill='white', outline='#D9E4DC')
        canvas.create_text(19, 17, anchor='nw', text=task.get('title', '事项'),
                           width=width-52, fill=INK, font=('Microsoft YaHei UI', 10, 'bold'))
        label = WORKFLOW_LABELS.get(task.get('workflow_mode'), '完整流程')
        canvas.create_text(19, 82, anchor='nw', text=label, fill=MUTED,
                           font=('Microsoft YaHei UI', 8))
        self.destination = canvas.create_text(19, 105, anchor='nw', text='拖到目标分区后松开',
                                              fill=GREEN, font=('Microsoft YaHei UI', 9))
        self.canvas = canvas

    def follow(self, x, y):
        self.geometry(f'+{max(0, int(x))}+{max(0, int(y))}')
        self.deiconify()

    def set_destination(self, text):
        self.canvas.itemconfigure(self.destination, text=text)


class DesignApp(ProductivityUI, ReminderApp):
    def _configure_theme(self):
        PALETTE.update(background=BG, ink=INK, muted=MUTED, border='#E8ECEB', shadow='#F0F1F0')
        STATUS_COLORS.update(COLORS)
        super()._configure_theme()
        self.option_add('*Font', ('Microsoft YaHei UI', 10))
        self.option_add('*Text.background', '#FFFFFF')
        self.option_add('*Text.foreground', INK)
        style = ttk.Style(self)
        style.layout('Soft.Vertical.TScrollbar', [('Vertical.Scrollbar.trough', {
            'sticky': 'ns', 'children': [('Vertical.Scrollbar.thumb', {'sticky': 'nswe'})]})])
        style.configure('Soft.Vertical.TScrollbar', background='#CBDACF',
                        troughcolor='#F3F5F3', bordercolor='#F3F5F3',
                        lightcolor='#CBDACF', darkcolor='#CBDACF', borderwidth=0,
                        width=9, arrowsize=9, gripcount=0)
        style.map('Soft.Vertical.TScrollbar', background=[('pressed', '#87A78F'), ('active', '#ABC2B1')])

    def _build(self):
        self.geometry('1480x860')
        self.minsize(1120, 680)
        self.page = 'board'
        self.week_only = False
        self.selected_id = None
        self.editor_task = None
        self.editor_vars = {}
        self.init_productivity()
        self._drag = None
        self._drag_preview = None
        self._drop_hint = None
        self._source_placeholder = None
        self._drag_scroll_job = None
        self._search_job = None
        self.sidebar = tk.Frame(self, bg='#FFFFFF', width=185)
        self.sidebar.pack(side='left', fill='y')
        self.sidebar.pack_propagate(False)
        tk.Label(self.sidebar, text='▦  教学秘书', bg='white', fg=INK, font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w', padx=22, pady=(30, 36))
        self.nav = {}
        for code, title in [('board', '▦   事项看板'), ('today', '今日待办'), ('overdue', '已逾期'), ('calendar', '▣   月度日历'), ('done', '✓   已完成'), ('trash', '回收站')]:
            button = tk.Button(self.sidebar, text=title, anchor='w', relief='flat', bd=0, bg='white', fg=INK, activebackground='#EAF4EE', padx=18, pady=14, command=lambda c=code: self.navigate(c))
            button.pack(fill='x', padx=12, pady=4)
            self.nav[code] = button
        tk.Label(self.sidebar, text='本地事项 · 自动同步', bg='white', fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(side='bottom', pady=24)
        for title, command in [('设置', self.open_settings), ('备份与导出', self.storage_menu)]:
            tk.Button(self.sidebar, text=title, anchor='w', bg='white', fg='#596578', bd=0, padx=22, pady=13, command=command).pack(side='bottom', fill='x')

        self.main = tk.Frame(self, bg=BG)
        self.main.pack(side='left', fill='both', expand=True)
        top = tk.Frame(self.main, bg=BG)
        top.pack(fill='x', padx=26, pady=(28, 12))
        SoftButton(top, '＋ 新建事项', self.new_task, True, 124).pack(side='right')
        SoftButton(top, '搜索', self.refresh, width=62).pack(side='right', padx=(0, 8))
        self.clear_search_button = SoftButton(top, '清空', lambda: self.search.set(''), width=60)
        self.clear_search_button.pack(side='right')
        entry = self.search_entry = ttk.Entry(top, textvariable=self.search, width=18)
        entry.pack(side='right', padx=16, ipady=3)
        entry.bind('<Return>', lambda e: self.refresh())
        entry.bind('<Escape>', lambda e: self.search.set(''))
        self.search.trace_add('write', self.schedule_search)
        self.heading = tk.Label(top, text='事项看板', bg=BG, fg=INK, font=('Microsoft YaHei UI', 24, 'bold'))
        self.heading.pack(side='left')
        tk.Label(self.main, text='每一件事务，都有清晰的进度', bg=BG, fg=MUTED).pack(anchor='w', padx=28, pady=(0, 22))
        filters = tk.Frame(self.main, bg=BG)
        filters.pack(fill='x', padx=26, pady=(0, 18))
        SoftButton(filters, '全部事项', lambda: self.set_week(False), width=100).pack(side='left')
        SoftButton(filters, '本周截止', lambda: self.set_week(True), width=100).pack(side='left', padx=8)
        self.status_button = SoftButton(filters, '全部 ▾', self.status_menu, width=100)
        self.status_button.pack(side='left', padx=8)
        SoftButton(filters, '同步钉钉', self.sync_dingtalk, width=100).pack(side='right')
        self.more_button = SoftButton(filters, '更多 ···', self.actions_menu, width=90)
        self.more_button.pack(side='right', padx=8)
        self.build_productivity_controls(filters)
        self.workspace = tk.Frame(self.main, bg=BG)
        self.workspace.pack(fill='both', expand=True, padx=22)
        self.board = tk.Frame(self.workspace, bg=BG)
        self.board.pack(fill='both', expand=True)
        self.columns = {}
        for i, (code, label) in enumerate(STATUS_LABELS.items()):
            tint, accent = COLORS[code]
            panel = tk.Frame(self.board, bg=tint, highlightthickness=2, highlightbackground=tint)
            panel.grid(row=0, column=i, sticky='nsew', padx=5)
            self.board.columnconfigure(i, weight=1, uniform='board')
            header = tk.Label(panel, text=label, bg=tint, fg=accent, font=('Microsoft YaHei UI', 11, 'bold'), anchor='w', padx=14, pady=17)
            header.pack(fill='x')
            area = ScrollArea(panel, '#F6F8F7' if code=='open' else '#F9F9FA')
            area.pack(fill='both', expand=True)
            self.columns[code] = (panel, header, area)
        self.board.rowconfigure(0, weight=1)
        self.completed = ScrollArea(self.workspace)
        self.completed_cards = []
        self.completed.body.bind('<Configure>', self.layout_completed, add='+')
        self.trash = ScrollArea(self.workspace)
        self.calendar = MonthlyCalendar(self.workspace, self)
        self.calendar._new_task = lambda day: self.new_task(day.isoformat()+'T17:00')
        self.status_line = tk.Label(self.main, text='', bg=BG, fg=MUTED, anchor='w', font=('Microsoft YaHei UI', 8))
        self.status_line.pack(fill='x', padx=28, pady=12)
        self.detail = tk.Frame(self, width=320, bg='white', highlightbackground='#E8ECEB', highlightthickness=1)
        self.detail.pack_propagate(False)
        self.bind('<Escape>', lambda e: self.cancel_drag() if self._drag else None)
        self.navigate('board')

    def schedule_search(self, *_):
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(180, self.run_search)

    def run_search(self):
        self._search_job = None
        self.refresh()

    def refresh_on_focus(self, event):
        # Child focus changes must not rebuild cards while clicking or dragging.
        if event.widget is self and self.store.revision() != self._store_revision:
            self.refresh()

    def navigate(self, page):
        self.page = page
        if page in {'today', 'overdue'}:
            self.status_filter.set('全部')
            self.status_button.itemconfigure(self.status_button.text_item, text='全部 ▾')
        self.selected_ids.clear()
        if page in {'trash', 'calendar'}:
            self.selection_mode = False
            self.bulkbar.pack_forget()
        for key, button in self.nav.items():
            button.configure(bg='#EAF4EE' if key == page else 'white', fg='#43664E' if key==page else INK)
        self.heading.configure(text={'board':'事项看板', 'today':'今日待办', 'overdue':'已逾期', 'calendar':'月度日历', 'done':'已完成', 'trash':'回收站'}[page])
        self.board.pack_forget()
        self.calendar.pack_forget()
        self.completed.pack_forget()
        self.trash.pack_forget()
        {'calendar': self.calendar, 'done': self.completed, 'board': self.board, 'today':self.board, 'overdue':self.board, 'trash':self.trash}[page].pack(fill='both', expand=True)
        self.refresh()

    def set_week(self, value):
        self.week_only = value
        if self.page in {'today', 'overdue'}:
            self.navigate('board')
        else:
            self.refresh()

    def refresh(self):
        if not hasattr(self, 'columns'):
            return
        if self._drag:
            return
        self.refresh_trash()
        if self.page == 'trash':
            self.update_productivity_controls([])
            self._store_revision = self.store.revision()
            return
        status = {v:k for k,v in STATUS_LABELS.items()}.get(self.status_filter.get())
        tasks = self.store.list_tasks(self.search.get(), status, include_done=True)
        all_tasks = self.store.list_tasks(include_done=True)
        self.nav['today'].configure(text=f'今日待办   {len(focus_tasks(all_tasks, "today"))}')
        self.nav['overdue'].configure(text=f'已逾期   {len(focus_tasks(all_tasks, "overdue"))}')
        if self.page in {'today', 'overdue'}:
            tasks = focus_tasks(tasks, self.page)
        if self.page == 'done':
            tasks = [t for t in tasks if t['status']=='done']
        if self.week_only and self.page not in {'today', 'overdue'}:
            today = now_local().date()
            start = today-timedelta(days=today.weekday())
            tasks = [t for t in tasks if start <= parse_datetime(t['due_at']).date() < start+timedelta(days=7)]
        self.update_productivity_controls(tasks)
        for code, (panel, header, area) in self.columns.items():
            items = [t for t in tasks if t['status']==code]
            header.configure(text=f'●  {STATUS_LABELS[code]}     {len(items)}')
            for child in area.body.winfo_children():
                child.destroy()
            for task in items:
                self.task_card(area.body, task, area.wheel)
            if not items:
                tk.Label(area.body, text='暂无事项', bg=area.body.cget('bg'), fg='#A6AFB9', pady=35).pack(fill='x')
        for child in self.completed.body.winfo_children():
            child.destroy()
        self.completed_cards = [self.task_card(self.completed.body, task, self.completed.wheel, compact=True)
                                for task in tasks if task['status'] == 'done']
        self.layout_completed()
        if not self.completed_cards:
            tk.Label(self.completed.body, text='暂无已完成事项', bg=BG, fg=MUTED, pady=30).grid(row=0, column=0)
        self.calendar.refresh(tasks)
        self.status_line.configure(text=f'当前显示 {len(tasks)} 项  ·  未完成 {sum(t["status"]!="done" for t in tasks)} 项')
        self._store_revision = self.store.revision()

    def refresh_trash(self):
        deleted = self.store.deleted_tasks()
        self.nav['trash'].configure(text=f'回收站   {len(deleted)}')
        if self.page != 'trash':
            return
        for child in self.trash.body.winfo_children():
            child.destroy()
        tk.Label(self.trash.body, text='删除的事项保留在这里，恢复后回到原分类。', bg=BG,
                 fg=MUTED, anchor='w', pady=12).pack(fill='x', padx=10)
        query = self.search.get().strip().lower()
        shown = [task for task in deleted if query in ' '.join(str(task.get(k, ''))
                 for k in ('title', 'details', 'source_department')).lower()]
        for task in shown:
            card = PastelCard(self.trash.body, 'white', height=112)
            card.pack(fill='x', padx=6, pady=5)
            SoftButton(card.content, '恢复事项', lambda tid=task['id']: self.restore_task(tid), width=100).pack(side='right', padx=12)
            text = tk.Frame(card.content, bg='white')
            text.pack(side='left', fill='both', expand=True, padx=10, pady=6)
            title = tk.Label(text, text=task['title'], bg='white', fg=INK, anchor='w',
                             justify='left', font=('Microsoft YaHei UI', 10, 'bold'))
            title.pack(fill='x')
            text.bind('<Configure>', lambda e, label=title: label.configure(wraplength=max(100, e.width-8)))
            deleted_at = parse_datetime(task.get('deleted_at'))
            when = deleted_at.strftime('%m月%d日 %H:%M') if deleted_at else '较早'
            tk.Label(text, text=f'原分类：{task["status_label"]}   ·   {when} 删除',
                     bg='white', fg=MUTED, anchor='w', font=('Microsoft YaHei UI', 9)).pack(fill='x', pady=(8,0))
            for widget in [card, text, *text.winfo_children()]:
                widget.bind('<MouseWheel>', self.trash.wheel)
        if not shown:
            tk.Label(self.trash.body, text='没有匹配的事项' if query else '回收站为空',
                     bg=BG, fg=MUTED, pady=45).pack(fill='x')
        self.status_line.configure(text=f'回收站：{len(shown)} 项  ·  恢复后重新同步，已完成事项不再提醒')

    def restore_task(self, task_id):
        if self._syncing:
            messagebox.showinfo('正在同步', '请等当前同步结束后恢复事项。', parent=self)
            return
        try:
            with PublishLock(self.store.task_dir, timeout=0.2):
                task = self.history.perform('恢复事项', [task_id], lambda: self.store.restore_task(task_id))
            self.refresh()
            self.status_line.configure(text=f'已恢复“{task["title"]}”至{task["status_label"]}')
            self.queue_auto_sync()
        except (ValidationError, OSError, RuntimeError) as exc:
            messagebox.showerror('无法恢复事项', str(exc), parent=self)

    def layout_completed(self, event=None):
        columns = max(1, self.completed.body.winfo_width() // 300)
        for i, card in enumerate(self.completed_cards):
            card.grid(row=i // columns, column=i % columns, padx=6, pady=6, sticky='nw')

    def task_card(self, parent, task, wheel, compact=False):
        tint, accent = COLORS[task['status']]
        height = 190 if task.get('recipients') else 170
        card = PastelCard(parent, 'white', width=286, height=126 if compact else height)
        card.configure(bg=parent.cget('bg'))
        if not compact:
            card.pack(fill='x', padx=6, pady=5)
        title = tk.Label(card.content, text=task['title'], bg='white', fg=INK, font=('Microsoft YaHei UI', 10, 'bold'), anchor='nw', justify='left')
        title.pack(fill='x', padx=7, pady=(4, 5))
        title.configure(height=2)
        card.content.bind('<Configure>', lambda e: title.configure(wraplength=max(70,e.width-(46 if self.selection_mode else 16))))
        if not compact:
            source = task.get('source_department') or '未填写来源'
            if task.get('workflow_mode') == 'notice':
                source = '一次性通知 · '+source
            tk.Label(card.content, text=source, bg='white', fg='#7D8796', anchor='w', font=('Microsoft YaHei UI', 8)).pack(fill='x', padx=8, pady=(0, 5))
        due = parse_datetime(task['due_at']).strftime('%m月%d日  %H:%M')
        tk.Label(card.content, text='已完成' if task['status']=='done' else due, bg=tint, fg=accent, padx=9, pady=5, font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=8)
        if self.selection_mode:
            var = tk.BooleanVar(value=task['id'] in self.selected_ids)
            check = tk.Checkbutton(card.content, variable=var, bg='white', activebackground='white',
                                   selectcolor='#EAF4EE', command=lambda tid=task['id']: self.toggle_selection(tid))
            check.variable = var
            check.place(relx=1, x=-24, y=1, width=24, height=24)
        sync_widget = self.add_card_sync(card, task) if not compact else None
        for widget in [card, card.content, *card.content.winfo_children()]:
            if widget is sync_widget or isinstance(widget, tk.Checkbutton):
                widget.bind('<MouseWheel>', wheel)
                continue
            widget.configure(cursor='hand2')
            widget.bind('<ButtonPress-1>', lambda e, tid=task['id'], c=card, t=task: self.begin_drag(e, tid, c, t))
            widget.bind('<B1-Motion>', self.drag_motion)
            widget.bind('<ButtonRelease-1>', self.end_drag)
            widget.bind('<Button-3>', lambda e, tid=task['id']: self.card_menu(e, tid))
            widget.bind('<MouseWheel>', wheel)
        return card

    def begin_drag(self, event, task_id, source=None, task=None):
        if self.selection_mode:
            self.toggle_selection(task_id)
            return
        self.selected_id = task_id
        if self._drag:
            self.clear_drag_visuals()
        self._drag = {'id': task_id, 'x': event.x_root, 'y': event.y_root,
                      'active': False, 'target': None, 'source': source, 'task': task,
                      'offset_x': event.x_root-source.winfo_rootx() if source else 20,
                      'offset_y': event.y_root-source.winfo_rooty() if source else 20}

    def drop_target(self, x, y):
        button = self.nav['trash']
        if (button.winfo_rootx() <= x < button.winfo_rootx()+button.winfo_width()
                and button.winfo_rooty() <= y < button.winfo_rooty()+button.winfo_height()):
            return 'trash'
        if self.page not in {'board', 'today', 'overdue'}:
            return None
        for code, (panel, _, _) in self.columns.items():
            if (panel.winfo_ismapped() and panel.winfo_rootx() <= x < panel.winfo_rootx()+panel.winfo_width()
                    and panel.winfo_rooty() <= y < panel.winfo_rooty()+panel.winfo_height()):
                return code
        return None

    def drag_motion(self, event):
        if not self._drag:
            return
        if abs(event.x_root-self._drag['x'])+abs(event.y_root-self._drag['y']) < 8:
            return
        if not self._drag['active']:
            self._drag['active'] = True
            source = self._drag['source']
            data = self._drag['task'] or self.store.get_task(self._drag['id'])
            data = data if isinstance(data, dict) else {'title': '事项'}
            self._drag_preview = DragPreview(self, data, max(230, min(300, source.winfo_width())) if source else 272)
            if source:
                self._drag['source_fill'] = source.fill
                source.fill = '#EFF3F0'
                source._draw(source.winfo_width(), source.winfo_height())
                self._source_placeholder = tk.Label(source.content, text='移动中\n原位置保留',
                                                     bg='#EFF3F0', fg='#93A497',
                                                     font=('Microsoft YaHei UI', 10), justify='center')
                self._source_placeholder.place(x=0, y=0, relwidth=1, relheight=1)
            self._drag_scroll_job = self.after(40, self.drag_autoscroll)
        target = self.drop_target(event.x_root, event.y_root)
        changed = target != self._drag['target']
        self._drag['target'] = target
        self._drag['pointer'] = (event.x_root, event.y_root)
        self._drag_preview.follow(event.x_root-self._drag['offset_x']+6, event.y_root-self._drag['offset_y']-6)
        self.configure(cursor='fleur')
        labels = {**STATUS_LABELS, 'trash':'回收站'}
        prompt = f'松开移至{labels[target]}' if target else '拖到目标分区后松开'
        self._drag_preview.set_destination(prompt)
        if changed:
            if self._drop_hint:
                self._drop_hint.destroy()
                self._drop_hint = None
            for code, (panel, header, area) in self.columns.items():
                panel.configure(highlightbackground=COLORS[code][1] if code==target else COLORS[code][0])
                header.configure(bg='#D8E8DC' if code==target else COLORS[code][0])
                if code == target:
                    self._drop_hint = tk.Label(area.canvas, text=prompt, bg=COLORS[code][0],
                                               fg=COLORS[code][1], highlightthickness=1,
                                               highlightbackground=COLORS[code][1], pady=12)
                    self._drop_hint.place(x=8, y=8, relwidth=1, width=-16, height=46)
            self.nav['trash'].configure(bg='#FBECEE' if target=='trash' else ('#EAF4EE' if self.page=='trash' else 'white'))
        self.status_line.configure(text=prompt+'  ·  Esc 取消')

    def drag_autoscroll(self):
        self._drag_scroll_job = None
        if not self._drag or not self._drag['active']:
            return
        target = self._drag['target']
        if target in self.columns:
            canvas = self.columns[target][2].canvas
            _, y = self._drag['pointer']
            offset = y-canvas.winfo_rooty()
            if 0 <= offset < 32:
                canvas.yview_scroll(-1, 'units')
            elif canvas.winfo_height()-32 < offset < canvas.winfo_height():
                canvas.yview_scroll(1, 'units')
        self._drag_scroll_job = self.after(40, self.drag_autoscroll)

    def clear_drag_visuals(self):
        if self._drag_scroll_job:
            self.after_cancel(self._drag_scroll_job)
            self._drag_scroll_job = None
        for attr in ('_drag_preview', '_drop_hint', '_source_placeholder'):
            widget = getattr(self, attr)
            if widget:
                widget.destroy()
                setattr(self, attr, None)
        if self._drag:
            source = self._drag.get('source')
            if source and source.winfo_exists():
                source.fill = self._drag.get('source_fill', 'white')
                source._draw(source.winfo_width(), source.winfo_height())
        self.configure(cursor='')
        for code, (panel, header, _) in self.columns.items():
            panel.configure(highlightbackground=COLORS[code][0])
            header.configure(bg=COLORS[code][0])
        self.nav['trash'].configure(bg='#EAF4EE' if self.page=='trash' else 'white')

    def cancel_drag(self):
        self.clear_drag_visuals()
        self._drag = None
        self.refresh()

    def end_drag(self, event):
        drag = self._drag
        self.clear_drag_visuals()
        self._drag = None
        if not drag:
            return
        if not drag['active']:
            self.edit_task_by_id(drag['id'])
            return
        target = self.drop_target(event.x_root, event.y_root)
        if target == 'trash':
            self.delete_task(drag['id'])
        elif target:
            self.move_task_to(drag['id'], target)
        else:
            self.refresh()

    def move_task_to(self, task_id, status):
        if self._syncing:
            messagebox.showinfo('正在同步', '请等当前同步结束后移动事项。', parent=self)
            return
        task = self.store.get_task(task_id)
        if not task or task['status'] == status:
            self.refresh()
            return
        if self.detail.winfo_ismapped() and self.is_dirty():
            messagebox.showinfo('请先保存', '请先保存或取消右侧修改，再移动事项。', parent=self)
            return
        try:
            with PublishLock(self.store.task_dir, timeout=0.2):
                updated = self.history.perform('移动事项', [task_id], lambda: self.store.move_task(task_id, status))
            self.refresh()
            if self.editor_task and self.editor_task['id'] == task_id:
                self.open_editor(updated)
            self.queue_auto_sync()
        except (ValidationError, OSError, RuntimeError) as exc:
            messagebox.showerror('无法移动事项', str(exc), parent=self)

    def card_menu(self, event, task_id):
        self.selected_id = task_id
        self.show_menu([('编辑事项', lambda: self.edit_task_by_id(task_id)),
                        *[(f'移至{label}', lambda c=code: self.move_task_to(task_id, c))
                          for code, label in STATUS_LABELS.items()],
                        ('删除事项', lambda: self.delete_task(task_id))], position=(event.x_root, event.y_root))

    def delete_task(self, task_id=None):
        task_id = task_id or self.selected_id
        task = self.store.get_task(task_id) if task_id else None
        if not task:
            messagebox.showinfo('请选择事项', '请先点击一张事项卡片。', parent=self)
            return
        if self._syncing:
            messagebox.showinfo('正在同步', '请等当前同步结束后删除事项。', parent=self)
            return
        if not messagebox.askyesno('删除事项', f'删除“{task["title"]}”？\n\nMarkdown 将移入本地 Trash 回收目录。对应钉钉待办也会在同步时删除。', parent=self):
            return
        try:
            with PublishLock(self.store.task_dir, timeout=0.2):
                self.history.perform('删除事项', [task_id], lambda: self.store.delete_task(task_id))
            if self.editor_task and self.editor_task['id'] == task_id:
                self.detail.pack_forget()
                self.editor_vars = {}
                self.editor_task = None
            self.selected_id = None
            self.refresh()
            self.queue_auto_sync()
        except (ValidationError, OSError, RuntimeError) as exc:
            messagebox.showerror('无法删除事项', str(exc), parent=self)

    def selected_task(self):
        task = self.store.get_task(self.selected_id) if self.selected_id else None
        if not task:
            messagebox.showinfo('请选择事项', '请点击一张事项卡片。', parent=self)
        return task

    def advance_selected(self):
        task = self.selected_task()
        if not task:
            return
        from core import next_task_status
        target = next_task_status(task)
        if not target:
            messagebox.showinfo('已完成', '该事项已经完成。', parent=self)
            return
        if messagebox.askyesno('确认推进', f'将“{task["title"]}”移至{STATUS_LABELS[target]}？', parent=self):
            self.move_task_to(task['id'], target)

    def new_task(self, due=None):
        self.open_editor(None, {'due_at': due or now_local().strftime('%Y-%m-%dT17:00')})

    def edit_task_by_id(self, task_id):
        task = self.store.get_task(task_id)
        if task:
            self.selected_id = task_id
            self.open_editor(task)

    def edit_selected(self):
        if self.selected_id:
            self.edit_task_by_id(self.selected_id)

    def open_calendar(self):
        self.navigate('calendar')

    def open_editor(self, task=None, draft=None):
        if self.detail.winfo_ismapped() and self.is_dirty():
            if not messagebox.askyesno('未保存修改', '放弃当前修改并打开另一事项？', parent=self):
                return
        self.editor_task = task
        data = {**(task or {}), **(draft or {})}
        for child in self.detail.winfo_children():
            child.destroy()
        self.detail.pack(side='right', fill='y', padx=(0, 10), pady=16, before=self.main)
        header = tk.Frame(self.detail, bg='white')
        header.pack(fill='x', padx=20, pady=20)
        tk.Label(header, text='事项详情' if task else '新建事项', bg='white', fg=INK, font=('Microsoft YaHei UI', 12, 'bold')).pack(side='left')
        tk.Button(header, text='×', bg='white', fg=MUTED, bd=0, command=self.close_editor).pack(side='right')
        footer = tk.Frame(self.detail, bg='white')
        footer.pack(side='bottom', fill='x', padx=16, pady=16)
        SoftButton(footer, '保存修改' if task else '创建事项', self.save_editor, True, 130).pack(side='right')
        SoftButton(footer, '取消', self.close_editor, width=80).pack(side='left')
        if task:
            tk.Button(self.detail, text='删除此事项', bg='white', fg='#B36D70', relief='flat',
                      command=lambda: self.delete_task(task['id'])).pack(side='bottom', anchor='w', padx=22, pady=5)
        area = ScrollArea(self.detail, 'white')
        area.pack(fill='both', expand=True, padx=18)
        self.editor_vars = {}
        tk.Label(area.body, text='处理方式', bg='white', fg=MUTED, anchor='w').pack(fill='x', pady=(6,5))
        self.workflow_var = tk.StringVar(value=WORKFLOW_LABELS.get(data.get('workflow_mode', 'staged'), '完整流程'))
        self.workflow_button = SoftButton(area.body, self.workflow_var.get()+' ▾', self.workflow_menu, width=220)
        self.workflow_button.pack(anchor='w')
        tk.Label(area.body, text='一次性通知：传达后即完成，无需催办和反馈', bg='white', fg=MUTED,
                 wraplength=260, justify='left', font=('Microsoft YaHei UI', 8)).pack(anchor='w', pady=(5,8))
        if task:
            tint, accent = COLORS[task['status']]
            tk.Label(area.body, text=task['status_label'], bg=tint, fg=accent, padx=12, pady=6).pack(anchor='w', pady=(0,16))
        fields = [('事项标题','title'), ('截止时间','due_at'), ('来源部门','source_department'), ('通知对象','recipients'), ('事项类别','category'), ('来源编号','source_id'), ('学期','semester')]
        for label,key in fields:
            value = data.get(key,'')
            if isinstance(value,list): value = '，'.join(value)
            tk.Label(area.body,text=label,bg='white',fg=MUTED,anchor='w',font=('Microsoft YaHei UI',9)).pack(fill='x',pady=(9,5))
            var = tk.StringVar(value=str(value or ''))
            self.editor_vars[key] = var
            ttk.Entry(area.body,textvariable=var).pack(fill='x', ipady=3)
        self.setup_progress_editor(area.body, data)
        tk.Label(area.body,text='原通知 / 传达记录 / 反馈记录',bg='white',fg=MUTED,anchor='w',font=('Microsoft YaHei UI',9)).pack(fill='x',pady=(22,8))
        self.editor_text = tk.Text(area.body, height=12, wrap='word', relief='flat', bd=0, highlightthickness=1, highlightbackground='#E8ECEB', padx=10, pady=10, font=('Microsoft YaHei UI',10), undo=True)
        self.editor_text.pack(fill='x', pady=(0,16))
        self.editor_text.insert('1.0', data.get('details',''))
        self.editor_snapshot = self.editor_payload()

    def editor_payload(self):
        payload = {k:v.get().strip() for k,v in self.editor_vars.items()}
        payload['workflow_mode'] = {v:k for k,v in WORKFLOW_LABELS.items()}[self.workflow_var.get()]
        payload['recipients'] = [v.strip() for v in payload['recipients'].replace('，',',').split(',') if v.strip()]
        payload['details'] = self.editor_text.get('1.0','end-1c').strip()
        payload['recipient_progress'] = self.progress_payload()
        return payload

    def is_dirty(self):
        return bool(self.editor_vars) and self.editor_payload() != self.editor_snapshot

    def close_editor(self):
        if self.is_dirty() and not messagebox.askyesno('未保存修改','放弃当前未保存的修改？',parent=self): return
        self.detail.pack_forget()
        self.editor_vars = {}

    def save_editor(self):
        payload = self.editor_payload()
        try:
            if not payload['title']: raise ValidationError('请填写事项标题')
            with PublishLock(self.store.task_dir, timeout=0.2):
                if self.editor_task:
                    task_id = self.editor_task['id']
                    self.history.perform('编辑事项', [task_id], lambda: self.store.update_task(task_id, payload))
                else:
                    task = self.store.create_task(payload, reject_duplicate=True)
                    self.selected_id = task['id']
                    self.history.created(task['id'])
            self.editor_snapshot = payload
            self.close_editor()
            self.refresh()
            self.queue_auto_sync()
        except (ValidationError, ValueError, OSError, RuntimeError) as exc:
            messagebox.showerror('无法保存',str(exc),parent=self)

    def actions_menu(self):
        self.show_menu([('从模板新建', self.template_menu), ('保存为模板', self.save_template),
                        ('批量操作', self.enter_selection), ('撤销上一步', self.undo_latest),
                        ('AI 提取通知',lambda: NoticeParser(self)),('推进当前阶段',self.advance_selected),
                        ('添加提醒',self.add_reminder),('打开 Markdown',self.open_markdown),
                        ('每日清单',lambda:self.show_report('daily')),('每周总结',lambda:self.show_report('weekly')),
                        ('删除当前事项',self.delete_task),('刷新',self.refresh)], anchor=self.more_button)

    def storage_menu(self):
        self.show_menu([('备份所有事项', self.backup), ('导出 JSON', self.export)])

    def status_menu(self):
        def select(value):
            self.status_filter.set(value)
            self.status_button.itemconfigure(self.status_button.text_item, text=value+' ▾')
            self.refresh()
        self.show_menu([(value, lambda v=value: select(v)) for value in ['全部', *STATUS_LABELS.values()]], anchor=self.status_button)

    def workflow_menu(self):
        def select(value):
            self.workflow_var.set(value)
            self.workflow_button.itemconfigure(self.workflow_button.text_item, text=value+' ▾')
        self.show_menu([(value, lambda v=value: select(v)) for value in WORKFLOW_LABELS.values()], anchor=self.workflow_button)

    def show_menu(self, entries, anchor=None, position=None):
        old = getattr(self, '_popover', None)
        if old and old.winfo_exists():
            old.destroy()
        menu = self._popover = tk.Toplevel(self)
        menu.withdraw()
        menu.overrideredirect(True)
        menu.transient(self)
        surface = PastelCard(menu, 'white', width=220, height=100)
        surface.pack(fill='both', expand=True)
        buttons = []
        def invoke(fn):
            menu.grab_release()
            menu.destroy()
            self.after_idle(fn)
        for label, fn in entries:
            button = tk.Button(surface.content, text=label, anchor='w', bg='white', fg=INK,
                               activebackground='#EAF4EE', activeforeground='#43664E',
                               relief='flat', bd=0, padx=14, pady=7,
                               command=lambda f=fn: invoke(f))
            button.pack(fill='x')
            button.bind('<Enter>', lambda e: e.widget.configure(bg='#EAF4EE'))
            button.bind('<Leave>', lambda e: e.widget.configure(bg='white'))
            buttons.append(button)
        def move(event, delta):
            current = menu.focus_get()
            index = buttons.index(current) if current in buttons else 0
            buttons[(index+delta) % len(buttons)].focus_set()
            return 'break'
        menu.bind('<Escape>', lambda e: menu.destroy())
        menu.bind('<Down>', lambda e: move(e, 1))
        menu.bind('<Up>', lambda e: move(e, -1))
        menu.bind('<Return>', lambda e: menu.focus_get().invoke() if menu.focus_get() in buttons else None)
        menu.update_idletasks()
        height = sum(b.winfo_reqheight() for b in buttons)+24
        if anchor:
            x, y = anchor.winfo_rootx(), anchor.winfo_rooty()+anchor.winfo_height()+4
        else:
            x, y = position or (self.winfo_pointerx(), self.winfo_pointery())
        x = min(x, self.winfo_screenwidth()-224)
        y = min(y, self.winfo_screenheight()-height)
        menu.geometry(f'220x{height}+{max(0,x)}+{max(0,y)}')
        menu.bind('<ButtonPress-1>', lambda e: menu.destroy() if not (0 <= e.x_root-menu.winfo_rootx() < 220 and 0 <= e.y_root-menu.winfo_rooty() < height) else None)
        menu.deiconify()
        menu.lift()
        menu.attributes('-topmost', True)
        menu.update_idletasks()
        menu.grab_set()
        buttons[0].focus_set()

    def backup(self):
        try:
            path = self.store.backup()
            messagebox.showinfo('备份完成', str(path), parent=self)
        except OSError as exc:
            messagebox.showerror('备份失败', str(exc), parent=self)

    def export(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.json',
                                          initialfile=f'事项-{now_local():%Y%m%d-%H%M%S}.json',
                                          filetypes=[('JSON', '*.json')])
        if path:
            try:
                self.store.export_json(path)
                messagebox.showinfo('已导出', path, parent=self)
            except OSError as exc:
                messagebox.showerror('导出失败', str(exc), parent=self)

    def hide_to_tray(self):
        if self._tray is None:
            self.iconify()
        else:
            self.withdraw()

    def exit_application(self):
        if self.detail.winfo_ismapped() and self.is_dirty():
            if not messagebox.askyesno('未保存修改','放弃修改并退出？',parent=self): return
        super().exit_application()
