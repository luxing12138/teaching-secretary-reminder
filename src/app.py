"""Minimal Windows desktop UI for the independent reminder application."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import calendar as calendar_module
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from core import DEFAULT_DB_PATH, STATUS_LABELS, TaskStore, ValidationError, parse_datetime
from deepseek import DeepSeekError, parse_notice
from dws_integration import DwsError, DwsWorkflow


PALETTE = {
    "background": "#FFF9F5",
    "surface": "#FFFFFF",
    "ink": "#4B4642",
    "muted": "#857D77",
    "border": "#E9DFD8",
    "shadow": "#E9E0DA",
    "mint": "#B9DEC7",
    "blue": "#BBDCF2",
    "lavender": "#D5C6ED",
    "peach": "#F7C8AF",
    "rose": "#F3C2CE",
    "butter": "#F8E4A4",
}

STATUS_COLORS = {
    "open": (PALETTE["blue"], "#345A77"),
    "notified": (PALETTE["lavender"], "#5B4675"),
    "followed-up": (PALETTE["peach"], "#7B4F3A"),
    "done": (PALETTE["mint"], "#3E6D4D"),
}


class PastelCard(tk.Canvas):
    """A light rounded card with a simple painted drop shadow."""

    def __init__(self, parent: tk.Misc, fill: str, *, width: int = 145, height: int = 104) -> None:
        super().__init__(parent, width=width, height=height, bg=PALETTE["background"], highlightthickness=0, bd=0)
        self.fill = fill
        self.content = tk.Frame(self, bg=fill, highlightthickness=0, bd=0)
        self._content_window = self.create_window(10, 8, anchor="nw", window=self.content)
        self.bind("<Configure>", self._resize)
        self._draw(width, height)

    @staticmethod
    def _rounded(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs: Any) -> int:
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=20, **kwargs)

    def _draw(self, width: int, height: int) -> None:
        self.delete("card")
        self._rounded(self, 7, 8, max(8, width - 2), max(9, height - 1), 16, fill=PALETTE["shadow"], outline="", tags="card")
        self._rounded(self, 2, 2, max(3, width - 7), max(3, height - 7), 16, fill=self.fill, outline=PALETTE["border"], width=1, tags="card")
        self.tag_lower("card")

    def _resize(self, event: tk.Event[Any]) -> None:
        self._draw(event.width, event.height)
        self.coords(self._content_window, 11, 10)
        self.itemconfigure(self._content_window, width=max(1, event.width - 23), height=max(1, event.height - 22))


class MonthlyCalendar(ttk.Frame):
    """Month view backed by the same Markdown task store as the board."""

    def __init__(self, parent: tk.Misc, app: "ReminderApp") -> None:
        super().__init__(parent, padding=(4, 8, 4, 4))
        self.app = app
        self.visible_month = date.today().replace(day=1)
        header = ttk.Frame(self)
        header.pack(fill="x", padx=4, pady=(0, 8))
        ttk.Button(header, text="‹ 上月", style="Soft.TButton", command=lambda: self.shift_month(-1)).pack(side="left")
        ttk.Button(header, text="今天", style="Soft.TButton", command=self.show_today).pack(side="left", padx=6)
        ttk.Button(header, text="下月 ›", style="Soft.TButton", command=lambda: self.shift_month(1)).pack(side="left")
        self.month_label = ttk.Label(header, style="ViewTitle.TLabel", anchor="center")
        self.month_label.pack(side="left", fill="x", expand=True)
        ttk.Label(header, text="点击任意日期即可新建事项", style="Hint.TLabel").pack(side="right")

        self.week_header = ttk.Frame(self)
        self.week_header.pack(fill="x", padx=4)
        for column, name in enumerate(("周一", "周二", "周三", "周四", "周五", "周六", "周日")):
            label = ttk.Label(self.week_header, text=name, anchor="center", style="CalendarHead.TLabel")
            label.grid(row=0, column=column, sticky="ew", padx=3, pady=(0, 5))
            self.week_header.columnconfigure(column, weight=1, uniform="calendar")

        self.days = ttk.Frame(self)
        self.days.pack(fill="both", expand=True, padx=4)
        for column in range(7):
            self.days.columnconfigure(column, weight=1, uniform="calendar")
        for row in range(6):
            self.days.rowconfigure(row, weight=1, uniform="calendar")

    def show_today(self) -> None:
        self.visible_month = date.today().replace(day=1)
        self.refresh(self.app.store.list_tasks(include_done=True))

    def shift_month(self, amount: int) -> None:
        month = self.visible_month.month + amount
        year = self.visible_month.year
        if month == 0:
            month, year = 12, year - 1
        elif month == 13:
            month, year = 1, year + 1
        self.visible_month = date(year, month, 1)
        self.refresh(self.app.store.list_tasks(include_done=True))

    @staticmethod
    def _shorten(text: str, limit: int = 18) -> str:
        return text if len(text) <= limit else f"{text[:limit - 1]}…"

    def _new_task(self, selected_day: date) -> None:
        TaskEditor(self.app, draft={"due_at": f"{selected_day.isoformat()}T17:00"})

    def _edit_task(self, task_id: str) -> None:
        self.app.edit_task_by_id(task_id)

    def _bind_new_task(self, widget: tk.Misc, selected_day: date) -> None:
        widget.bind("<Button-1>", lambda _event: self._new_task(selected_day))

    def refresh(self, tasks: list[dict[str, Any]]) -> None:
        for child in self.days.winfo_children():
            child.destroy()
        self.month_label.configure(text=f"{self.visible_month.year} 年 {self.visible_month.month} 月")
        buckets: dict[date, list[dict[str, Any]]] = {}
        for task in tasks:
            if task.get("status") == "done":
                continue
            due = parse_datetime(task.get("due_at"))
            if due:
                buckets.setdefault(due.date(), []).append(task)
        for items in buckets.values():
            items.sort(key=lambda item: (item["status"] == "done", item["due_at"]))

        weeks = calendar_module.Calendar(firstweekday=0).monthdatescalendar(self.visible_month.year, self.visible_month.month)
        while len(weeks) < 6:
            start = weeks[-1][-1]
            weeks.append([start.fromordinal(start.toordinal() + offset) for offset in range(1, 8)])
        today = date.today()
        for row, week in enumerate(weeks[:6]):
            for column, selected_day in enumerate(week):
                fill = PALETTE["butter"] if selected_day == today else ("#F5F0EC" if selected_day.month != self.visible_month.month else PALETTE["surface"])
                card = PastelCard(self.days, fill)
                card.grid(row=row, column=column, sticky="nsew", padx=3, pady=3)
                self._bind_new_task(card, selected_day)
                self._bind_new_task(card.content, selected_day)
                date_text = str(selected_day.day)
                if selected_day == today:
                    date_text += "  今天"
                date_label = tk.Label(card.content, text=date_text, bg=fill, fg=PALETTE["ink"] if selected_day.month == self.visible_month.month else PALETTE["muted"], font=("Microsoft YaHei UI", 9, "bold"), anchor="w")
                date_label.pack(fill="x", anchor="w")
                self._bind_new_task(date_label, selected_day)
                daily_tasks = buckets.get(selected_day, [])
                for task in daily_tasks[:3]:
                    background, foreground = STATUS_COLORS[task["status"]]
                    label = tk.Label(card.content, text=f"{task['status_label']} · {self._shorten(task['title'])}", bg=background, fg=foreground, font=("Microsoft YaHei UI", 8), anchor="w", padx=5, pady=2, cursor="hand2")
                    label.pack(fill="x", pady=(4, 0))
                    label.bind("<Button-1>", lambda _event, task_id=task["id"]: self._edit_task(task_id))
                if len(daily_tasks) > 3:
                    more = tk.Label(card.content, text=f"另有 {len(daily_tasks) - 3} 项", bg=fill, fg=PALETTE["muted"], font=("Microsoft YaHei UI", 8), anchor="w")
                    more.pack(fill="x", pady=(3, 0))
                    self._bind_new_task(more, selected_day)


def ensure_sync_service(task_dir: str) -> None:
    """Start one hidden sync service unless its active lock is fresh."""
    lock_path = Path(task_dir).parent / ".dingtalk-sync-service.lock"
    try:
        if lock_path.exists() and time.time() - lock_path.stat().st_mtime < 45:
            return
    except OSError:
        return
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--sync-service"]
    else:
        command = [sys.executable, str(Path(__file__).with_name("sync_service.py"))]
    options: dict[str, Any] = {"close_fds": True}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(command, **options)
    except OSError:
        # Manual sync remains available if Windows blocks a background launch.
        pass


class TaskEditor(tk.Toplevel):
    def __init__(self, parent: "ReminderApp", task: dict[str, Any] | None = None, draft: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.parent = parent
        self.task = task
        data = {**(task or {}), **(draft or {})}
        self.title("编辑事项" if task else "新建事项")
        self.geometry("720x620")
        self.transient(parent)
        self.grab_set()
        self.vars = {key: tk.StringVar(value=str(data.get(key, ""))) for key in ("title", "due_at", "recipients", "source_department", "category", "source_id", "semester")}
        if isinstance(data.get("recipients"), list):
            self.vars["recipients"].set("，".join(data["recipients"]))
        if not self.vars["due_at"].get() and data.get("due"):
            self.vars["due_at"].set(str(data["due"]))
        form = ttk.Frame(self, padding=12)
        form.pack(fill="both", expand=True)
        labels = [
            ("事项标题*", "title"), ("最终截止时间*", "due_at"), ("通知对象（逗号分隔）", "recipients"),
            ("来源部门", "source_department"), ("事项类别", "category"), ("来源编号 / 去重键", "source_id"), ("学期", "semester"),
        ]
        for row, (label, key) in enumerate(labels):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=self.vars[key], width=70).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Label(form, text="原通知 / 处理备注").grid(row=len(labels), column=0, sticky="nw", pady=4)
        self.details = tk.Text(form, height=15, wrap="word")
        self.details.grid(row=len(labels), column=1, sticky="nsew", pady=4)
        self.details.insert("1.0", str(data.get("details", "")))
        ttk.Label(form, text="日期可填 2026-10-31；系统将补为 17:00。明确时刻请填 2026-10-31 10:00。", foreground="#666").grid(row=len(labels) + 1, column=1, sticky="w")
        form.columnconfigure(1, weight=1)
        form.rowconfigure(len(labels), weight=1)
        buttons = ttk.Frame(form)
        buttons.grid(row=len(labels) + 2, column=1, sticky="e", pady=8)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="保存", command=self.save).pack(side="right", padx=4)

    def save(self) -> None:
        payload = {key: variable.get().strip() for key, variable in self.vars.items()}
        payload["recipients"] = [x.strip() for x in payload["recipients"].replace("，", ",").split(",") if x.strip()]
        payload["details"] = self.details.get("1.0", "end-1c").strip()
        try:
            if self.task:
                self.parent.store.update_task(self.task["id"], payload)
                self.parent.queue_auto_sync()
            else:
                duplicates = self.parent.store.find_possible_duplicates(payload["title"], payload["due_at"], payload["source_department"], payload["source_id"])
                if duplicates and not messagebox.askyesno("可能重复", f"发现 {len(duplicates)} 个可能重复事项，仍要创建吗？", parent=self):
                    return
                created = self.parent.store.create_task(payload)
                self.parent.publish_created_task(created)
        except ValidationError as exc:
            messagebox.showerror("无法保存", str(exc), parent=self)
            return
        self.parent.refresh()
        self.destroy()


class NoticeParser(tk.Toplevel):
    def __init__(self, parent: "ReminderApp") -> None:
        super().__init__(parent)
        self.parent = parent
        self.title("从通知文本提取事项")
        self.geometry("760x500")
        self.transient(parent)
        ttk.Label(self, text="粘贴通知原文。DeepSeek 只生成草稿，保存前仍可修改。", padding=10).pack(anchor="w")
        self.text = tk.Text(self, wrap="word")
        self.text.pack(fill="both", expand=True, padx=10, pady=4)
        self.button = ttk.Button(self, text="用 DeepSeek 提取", command=self.parse)
        self.button.pack(anchor="e", padx=10, pady=10)

    def parse(self) -> None:
        notice = self.text.get("1.0", "end-1c").strip()
        key = self.parent.api_key.get().strip() or os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            messagebox.showwarning("缺少 Key", "请先在“设置”中输入本次运行使用的 DeepSeek API Key。", parent=self)
            return
        self.button.configure(state="disabled", text="正在提取…")

        def work() -> None:
            try:
                draft = parse_notice(notice, key, self.parent.model.get().strip() or "deepseek-chat")
                draft["details"] = draft.get("details") or notice
                self.after(0, lambda: self.finish(draft, None))
            except Exception as exc:
                self.after(0, lambda: self.finish(None, exc))

        threading.Thread(target=work, daemon=True).start()

    def finish(self, draft: dict[str, Any] | None, error: Exception | None) -> None:
        self.button.configure(state="normal", text="用 DeepSeek 提取")
        if error:
            messagebox.showerror("提取失败", str(error), parent=self)
            return
        self.destroy()
        TaskEditor(self.parent, draft=draft)


class ReminderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self._configure_theme()
        self.store = TaskStore()
        self.dws = DwsWorkflow(self.store)
        ensure_sync_service(str(self.store.task_dir))
        self.api_key = tk.StringVar(value=os.environ.get("DEEPSEEK_API_KEY", ""))
        self.model = tk.StringVar(value="deepseek-chat")
        self.search = tk.StringVar()
        self.status_filter = tk.StringVar(value="全部")
        self.board_trees: dict[str, ttk.Treeview] = {}
        self.board_headers: dict[str, ttk.Label] = {}
        self._selected_board: ttk.Treeview | None = None
        self._store_revision: object | None = None
        self._auto_sync_job: str | None = None
        self._syncing = False
        self._tray: Any | None = None
        self._tray_events: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._quitting = False
        self.title("教学秘书提醒器（独立版）")
        self.geometry("1180x760")
        self.minsize(960, 620)
        self.configure(bg=PALETTE["background"])
        self._build()
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._start_tray()
        self.bind("<FocusIn>", getattr(self, "refresh_on_focus", lambda _event: None))
        self.after(1500, self.check_reminders)
        self.after(1_000, self.refresh_periodically)
        self.after(200, self._process_tray_events)

    def _configure_theme(self) -> None:
        """Apply the shared pastel treatment before creating any widgets."""
        style = ttk.Style(self)
        style.theme_use("clam")
        base_font = ("Microsoft YaHei UI", 9)
        style.configure(".", font=base_font, background=PALETTE["background"], foreground=PALETTE["ink"])
        style.configure("TFrame", background=PALETTE["background"])
        style.configure("TLabel", background=PALETTE["background"], foreground=PALETTE["ink"])
        style.configure("Hint.TLabel", foreground=PALETTE["muted"], font=("Microsoft YaHei UI", 8))
        style.configure("ViewTitle.TLabel", font=("Microsoft YaHei UI", 13, "bold"), foreground=PALETTE["ink"])
        style.configure("CalendarHead.TLabel", font=("Microsoft YaHei UI", 9, "bold"), foreground=PALETTE["muted"])
        style.configure("TButton", background=PALETTE["surface"], foreground=PALETTE["ink"], borderwidth=0, padding=(10, 6), relief="flat")
        style.map("TButton", background=[("active", "#F8EEE8"), ("pressed", "#F0E3DC")])
        style.configure("Primary.TButton", background=PALETTE["mint"], foreground="#305C40", font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Primary.TButton", background=[("active", "#A9D2B6"), ("pressed", "#98C6A7")])
        style.configure("Soft.TButton", background="#F7EFEA", foreground=PALETTE["ink"], padding=(9, 5))
        style.map("Soft.TButton", background=[("active", "#EDE1D8")])
        style.configure("TEntry", fieldbackground=PALETTE["surface"], bordercolor=PALETTE["border"], padding=5)
        style.configure("TCombobox", fieldbackground=PALETTE["surface"], background=PALETTE["surface"], padding=4)
        style.configure("TNotebook", background=PALETTE["background"], borderwidth=0)
        style.configure("TNotebook.Tab", background="#F4ECE7", foreground=PALETTE["muted"], padding=(18, 8), font=("Microsoft YaHei UI", 9, "bold"))
        style.map("TNotebook.Tab", background=[("selected", PALETTE["surface"]), ("active", "#F8EEE8")], foreground=[("selected", PALETTE["ink"])])
        style.configure("Board.TLabelframe", background=PALETTE["surface"], bordercolor=PALETTE["border"], relief="flat", borderwidth=1)
        style.configure("Board.TLabelframe.Label", background=PALETTE["surface"], foreground=PALETTE["ink"], font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", background=PALETTE["surface"], fieldbackground=PALETTE["surface"], foreground=PALETTE["ink"], rowheight=30, borderwidth=0)
        style.map("Treeview", background=[("selected", "#E9F2F8")], foreground=[("selected", PALETTE["ink"])])
        style.configure("Treeview.Heading", background="#F5ECE7", foreground=PALETTE["muted"], font=("Microsoft YaHei UI", 8, "bold"), relief="flat", padding=6)

    @staticmethod
    def _tray_image() -> Any:
        """Create a small self-contained pastel icon; no external image file is needed."""
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((7, 9, 57, 57), radius=15, fill="#D5C6ED", outline="#B9A6D5", width=2)
        draw.rounded_rectangle((15, 17, 49, 49), radius=10, fill="#FFF9F5")
        draw.line((22, 27, 42, 27), fill="#8B789F", width=3)
        draw.line((22, 35, 38, 35), fill="#8B789F", width=3)
        draw.ellipse((22, 41, 27, 46), fill="#B9DEC7")
        draw.ellipse((31, 41, 36, 46), fill="#F7C8AF")
        return image

    def _start_tray(self) -> None:
        """Run the tray backend off the Tk event loop, degrading safely if unavailable."""
        try:
            import pystray

            def enqueue(action: str) -> Any:
                return lambda _icon, _item: self._tray_events.put(action)

            menu = pystray.Menu(
                pystray.MenuItem("打开提醒器", enqueue("show"), default=True),
                pystray.MenuItem("同步钉钉", enqueue("sync")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出提醒器", enqueue("exit")),
            )
            self._tray = pystray.Icon("TeachingSecretaryReminder", self._tray_image(), "教学秘书提醒器", menu)
            self._tray.run_detached()
        except Exception:
            # The normal window keeps all functions if a restricted desktop
            # session does not expose the Windows notification area.
            self._tray = None

    def hide_to_tray(self) -> None:
        """Closing the window keeps reminders and the tray menu available."""
        self.withdraw()

    def show_from_tray(self) -> None:
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()
        self.refresh()

    def _process_tray_events(self) -> None:
        try:
            while True:
                action = self._tray_events.get_nowait()
                if action == "show":
                    self.show_from_tray()
                elif action == "sync":
                    self.show_from_tray()
                    self.sync_dingtalk()
                elif action == "exit":
                    self.exit_application()
                    return
        except queue.Empty:
            pass
        if not self._quitting:
            self.after(200, self._process_tray_events)

    def exit_application(self) -> None:
        """The explicit tray command is the only path that terminates the GUI process."""
        self._quitting = True
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
        self.destroy()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="新建事项", style="Primary.TButton", command=lambda: TaskEditor(self)).pack(side="left", padx=(0, 5))
        ttk.Button(toolbar, text="AI 提取通知", command=lambda: NoticeParser(self)).pack(side="left", padx=5)
        ttk.Button(toolbar, text="同步钉钉", style="Primary.TButton", command=self.sync_dingtalk).pack(side="left", padx=5)
        ttk.Button(toolbar, text="月历", style="Soft.TButton", command=self.open_calendar).pack(side="left", padx=5)
        ttk.Button(toolbar, text="推进当前阶段", command=self.advance_selected).pack(side="left", padx=5)
        ttk.Button(toolbar, text="添加提醒", command=self.add_reminder).pack(side="left", padx=5)
        ttk.Button(toolbar, text="详情 / 编辑", command=self.edit_selected).pack(side="left", padx=5)
        ttk.Button(toolbar, text="打开 Markdown", command=self.open_markdown).pack(side="left", padx=5)
        ttk.Button(toolbar, text="刷新", command=self.refresh).pack(side="left", padx=5)
        ttk.Button(toolbar, text="设置", command=self.open_settings).pack(side="right")
        ttk.Button(toolbar, text="备份", command=self.backup).pack(side="right", padx=5)
        ttk.Button(toolbar, text="导出", command=self.export).pack(side="right", padx=5)
        ttk.Button(toolbar, text="日报", command=lambda: self.show_report("daily")).pack(side="right", padx=5)
        ttk.Button(toolbar, text="周报", command=lambda: self.show_report("weekly")).pack(side="right", padx=5)

        filter_box = ttk.Frame(outer)
        filter_box.pack(fill="x", pady=(0, 8))
        ttk.Label(filter_box, text="检索").pack(side="left")
        entry = ttk.Entry(filter_box, textvariable=self.search, width=42)
        entry.pack(side="left", padx=5)
        entry.bind("<Return>", lambda _e: self.refresh())
        ttk.Label(filter_box, text="状态").pack(side="left", padx=(12, 0))
        options = ["全部", *STATUS_LABELS.values()]
        ttk.Combobox(filter_box, textvariable=self.status_filter, values=options, state="readonly", width=10).pack(side="left", padx=5)
        ttk.Button(filter_box, text="筛选", command=self.refresh).pack(side="left")

        self.views = ttk.Notebook(outer)
        self.views.pack(fill="both", expand=True)
        kanban_tab = ttk.Frame(self.views, padding=(4, 8, 4, 4))
        calendar_tab = ttk.Frame(self.views, padding=(2, 2, 2, 2))
        self.views.add(kanban_tab, text="  事项看板  ")
        self.views.add(calendar_tab, text="  月度日历  ")
        self.board = ttk.Frame(kanban_tab)
        self.board.pack(fill="both", expand=True)
        for index, status in enumerate(STATUS_LABELS):
            row, column = divmod(index, 2)
            panel = ttk.LabelFrame(self.board, padding=8, style="Board.TLabelframe")
            panel.grid(row=row, column=column, sticky="nsew", padx=(0, 6) if column == 0 else (6, 0), pady=(0, 6) if row == 0 else (6, 0))
            header = ttk.Label(panel, text=STATUS_LABELS[status], font=("Microsoft YaHei UI", 10, "bold"))
            header.pack(anchor="w", pady=(0, 4))
            tree = ttk.Treeview(panel, columns=("title", "due"), show="headings", selectmode="browse", height=8)
            tree.heading("title", text="事项")
            tree.heading("due", text="最终截止")
            tree.column("title", width=360, minwidth=180, anchor="w")
            tree.column("due", width=170, minwidth=130, anchor="w")
            tree.pack(fill="both", expand=True)
            tree.bind("<<TreeviewSelect>>", lambda _event, current=tree: self._select_board(current))
            tree.bind("<Double-1>", lambda _event: self.edit_selected())
            self.board_trees[status] = tree
            self.board_headers[status] = header
        self.board.columnconfigure(0, weight=1)
        self.board.columnconfigure(1, weight=1)
        self.board.rowconfigure(0, weight=1)
        self.board.rowconfigure(1, weight=1)
        self.calendar = MonthlyCalendar(calendar_tab, self)
        self.calendar.pack(fill="both", expand=True)
        self.status_line = ttk.Label(outer, text="", anchor="w")
        self.status_line.pack(fill="x", pady=(8, 0))

    def _select_board(self, selected: ttk.Treeview) -> None:
        """Keep one active card selection across the four kanban columns."""
        if not selected.selection():
            return
        self._selected_board = selected
        for tree in self.board_trees.values():
            if tree is not selected and tree.selection():
                tree.selection_remove(tree.selection())

    def selected_task(self) -> dict[str, Any] | None:
        tree = self._selected_board
        chosen = tree.selection() if tree else ()
        if not chosen:
            for candidate in self.board_trees.values():
                if candidate.selection():
                    chosen = candidate.selection()
                    break
        if not chosen:
            messagebox.showinfo("请选择事项", "请先选择一条事项。", parent=self)
            return None
        return self.store.get_task(chosen[0])

    def refresh(self) -> None:
        label_to_status = {v: k for k, v in STATUS_LABELS.items()}
        status = label_to_status.get(self.status_filter.get())
        rows = self.store.list_tasks(self.search.get(), status, include_done=True)
        all_rows = self.store.list_tasks(include_done=True)
        for tree in self.board_trees.values():
            for item in tree.get_children():
                tree.delete(item)
        for task in rows:
            self.board_trees[task["status"]].insert("", "end", iid=task["id"], values=(task["title"], task["due_at"]))
        for code, label in STATUS_LABELS.items():
            count = sum(row["status"] == code for row in all_rows)
            self.board_headers[code].configure(text=f"{label}（{count}）")
        self.calendar.refresh(all_rows)
        active = sum(row["status"] != "done" for row in all_rows)
        shown = "全部" if status is None else STATUS_LABELS[status]
        self.status_line.configure(text=f"当前显示：{shown}；共 {len(rows)} 条，未完成 {active} 条。数据保存在：{self.store.db_path}")
        revision = getattr(self.store, "revision", None)
        self._store_revision = revision() if revision else None

    def refresh_periodically(self) -> None:
        """Show tasks created by the CLI or another app instance without a restart."""
        try:
            revision = getattr(self.store, "revision", None)
            current = revision() if revision else None
            if current != self._store_revision:
                self.refresh()
                self.queue_auto_sync()
        finally:
            self.after(1_000, self.refresh_periodically)

    def edit_selected(self) -> None:
        task = self.selected_task()
        if task:
            TaskEditor(self, task=task)

    def edit_task_by_id(self, task_id: str) -> None:
        """Open a calendar item without requiring it to be selected on the board."""
        task = self.store.get_task(task_id)
        if task:
            TaskEditor(self, task=task)

    def open_calendar(self) -> None:
        self.views.select(1)

    def open_markdown(self) -> None:
        task = self.selected_task()
        if not task:
            return
        path = task.get("path", "")
        if not path or not os.path.exists(path):
            messagebox.showerror("文件不存在", "未找到该事项对应的 Markdown 文件。", parent=self)
            return
        os.startfile(path)  # type: ignore[attr-defined]  # Windows-only desktop app

    def advance_selected(self) -> None:
        task = self.selected_task()
        if not task:
            return
        if task["status"] == "done":
            messagebox.showinfo("已完成", "该事项已完成，无需继续推进。", parent=self)
            return
        if task["external_todo_id"]:
            messagebox.showinfo("请先同步钉钉", "该任务已关联钉钉待办。请在钉钉点击完成后，使用“同步钉钉”推进一个阶段。", parent=self)
            return
        next_label = STATUS_LABELS[{"open": "notified", "notified": "followed-up", "followed-up": "done"}[task["status"]]]
        if not messagebox.askyesno("确认推进", f"“{task['title']}”将从 {task['status_label']} 推进到 {next_label}。", parent=self):
            return
        try:
            updated = self.store.advance_task(task["id"])
            messagebox.showinfo("已推进", f"当前状态：{updated['status_label']}", parent=self)
            self.refresh()
        except ValidationError as exc:
            messagebox.showerror("无法推进", str(exc), parent=self)

    def publish_created_task(self, task: dict[str, Any]) -> None:
        """Create the first DingTalk stage without losing the local task on failure."""
        if not task["external_sync_enabled"]:
            return
        try:
            self.dws.publish(task["id"])
        except Exception as exc:
            messagebox.showwarning("本地任务已创建", f"钉钉待办暂未创建：{exc}\n\n可稍后点击“同步钉钉”重试；本地提醒仍会保留。", parent=self)

    def queue_auto_sync(self) -> None:
        """Debounce local Markdown edits before publishing them to DingTalk."""
        if self._syncing:
            return
        if self._auto_sync_job:
            self.after_cancel(self._auto_sync_job)
        self._auto_sync_job = self.after(1_000, lambda: self.sync_dingtalk(silent=True))

    def sync_dingtalk(self, silent: bool = False, task_id: str | None = None) -> None:
        if self._syncing:
            return
        self._auto_sync_job = None
        self._syncing = True
        self._sync_task_id = task_id
        def work() -> None:
            try:
                result = self.dws.sync_all({task_id}) if task_id else self.dws.sync_all()
                self.after(0, lambda: self.finish_dingtalk_sync(result, None, silent))
            except Exception as exc:
                self.after(0, lambda error=exc: self.finish_dingtalk_sync(None, error, silent))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()
        self.status_line.configure(text="正在同步钉钉待办…")

    def finish_dingtalk_sync(self, result: list[dict[str, Any]] | None, error: Exception | None, silent: bool = False) -> None:
        self._syncing = False
        self._sync_task_id = None
        self.refresh()
        if error:
            self.status_line.configure(text=f"钉钉自动同步失败：{error}")
            if not silent:
                messagebox.showerror("钉钉同步失败", str(error), parent=self)
            return
        advanced = sum(item["action"] in {"advanced", "completed"} for item in result or [])
        created = sum(item["action"] == "created" for item in result or [])
        failures = [item for item in result or [] if item["action"] == "error"]
        summary = f"检查 {len(result or [])} 项；新建 {created} 项；推进/完成 {advanced} 项；失败 {len(failures)} 项。"
        self.status_line.configure(text=f"钉钉同步完成：{summary}")
        if not silent:
            messagebox.showinfo("钉钉同步完成", summary, parent=self)

    def add_reminder(self) -> None:
        task = self.selected_task()
        if not task:
            return
        at = simpledialog.askstring("添加额外提醒", "请输入提醒时间（例：2026-10-30 08:00）：", parent=self)
        if not at:
            return
        try:
            self.store.add_custom_reminder(task["id"], at)
            messagebox.showinfo("已添加", "额外提醒已加入本地提醒队列。", parent=self)
        except ValidationError as exc:
            messagebox.showerror("无法添加", str(exc), parent=self)

    def check_reminders(self) -> None:
        try:
            reminders = self.store.due_reminders()
            for reminder in reminders:
                answer = messagebox.askyesno(
                    "教学秘书提醒",
                    f"[{STATUS_LABELS[reminder.task_status]}] {reminder.title}\n\n提醒时间：{reminder.remind_at}\n\n选择“是”确认知晓；选择“否”暂缓 10 分钟。",
                    parent=self,
                )
                if answer:
                    self.store.acknowledge_reminder(reminder.id)
                else:
                    self.store.snooze_reminder(reminder.id, 10)
        finally:
            self.after(30_000, self.check_reminders)

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("本次运行设置")
        dialog.geometry("590x180")
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="DeepSeek API Key（仅保存在本次运行内存；不写入磁盘）").grid(row=0, column=0, sticky="w")
        key = ttk.Entry(frame, textvariable=self.api_key, show="•", width=65)
        key.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Label(frame, text="模型").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.model, width=30).grid(row=3, column=0, sticky="w", pady=4)
        ttk.Label(frame, text="也可在启动前设置环境变量 DEEPSEEK_API_KEY。", foreground="#666").grid(row=4, column=0, sticky="w")
        ttk.Button(frame, text="关闭", command=dialog.destroy).grid(row=5, column=0, sticky="e", pady=8)
        frame.columnconfigure(0, weight=1)

    def show_report(self, kind: str) -> None:
        text = self.store.daily_report() if kind == "daily" else self.store.weekly_report()
        dialog = tk.Toplevel(self)
        dialog.title("每日工作清单" if kind == "daily" else "每周工作总结")
        dialog.geometry("760x520")
        box = tk.Text(dialog, wrap="word")
        box.pack(fill="both", expand=True, padx=8, pady=8)
        box.insert("1.0", text)

    def backup(self) -> None:
        path = self.store.backup()
        messagebox.showinfo("备份完成", str(path), parent=self)

    def export(self) -> None:
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            self.store.export_json(path)
            messagebox.showinfo("已导出", path, parent=self)


if __name__ == "__main__":
    if '--smoke-test' in sys.argv:
        from ui_smoke import run as smoke_run
        raise SystemExit(smoke_run(sys.argv[sys.argv.index('--smoke-test')+1]))
    if "--sync-service" in sys.argv:
        from sync_service import run
        raise SystemExit(run())
    from design_ui import DesignApp
    DesignApp().mainloop()
