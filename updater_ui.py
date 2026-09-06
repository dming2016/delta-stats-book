"""Native update prompt and progress window for the desktop launcher."""

from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from ctypes import windll
from pathlib import Path
from tkinter import ttk
from typing import Callable, TypeVar

from app_version import APP_DISPLAY_NAME


T = TypeVar("T")
BACKGROUND = "#f3f5f2"
SURFACE = "#ffffff"
TEXT = "#172019"
MUTED = "#69736b"
LINE = "#dce2da"
ACCENT = "#246743"
ACCENT_HOVER = "#1b5536"
ERROR = "#b93f3f"


try:
    windll.shcore.SetProcessDpiAwareness(1)
except (AttributeError, OSError):
    try:
        windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


class UpdateCancelled(RuntimeError):
    """Raised when the user cancels an in-progress download."""


def format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            digits = 0 if unit == "B" else 1
            return f"{size:.{digits}f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, math.ceil(seconds))
    if total_seconds < 60:
        return f"{total_seconds} 秒"
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes} 分钟 {remaining_seconds} 秒" if remaining_seconds else f"{minutes} 分钟"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours} 小时 {remaining_minutes} 分钟" if remaining_minutes else f"{hours} 小时"


def format_download_progress(completed: int, total: int, elapsed: float) -> str:
    summary = f"{format_bytes(completed)} / {format_bytes(total)}"
    if completed <= 0 or elapsed <= 0:
        return summary
    average_speed = completed / elapsed
    summary += f" · 全程平均 {format_bytes(round(average_speed))}/s"
    if completed < total:
        remaining = (total - completed) / average_speed
        summary += f" · 预计剩余 {format_duration(remaining)}"
    return summary


def _center(window: tk.Tk, width: int, height: int) -> None:
    window.update_idletasks()
    left = max(0, (window.winfo_screenwidth() - width) // 2)
    top = max(0, (window.winfo_screenheight() - height) // 2)
    window.geometry(f"{width}x{height}+{left}+{top}")


def _configure_window(window: tk.Tk, icon_path: Path | None) -> None:
    window.configure(background=BACKGROUND)
    window.resizable(False, False)
    if icon_path and icon_path.is_file():
        try:
            window.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass
    window.attributes("-topmost", True)
    window.after(350, lambda: window.attributes("-topmost", False))


def _configure_styles(window: tk.Tk) -> ttk.Style:
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure(
        "Update.Horizontal.TProgressbar",
        troughcolor="#e2e7e1",
        background=ACCENT,
        bordercolor="#e2e7e1",
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        thickness=10,
    )
    style.configure(
        "Error.Horizontal.TProgressbar",
        troughcolor="#eee1e1",
        background=ERROR,
        bordercolor="#eee1e1",
        lightcolor=ERROR,
        darkcolor=ERROR,
        thickness=10,
    )
    return style


def _button(parent: tk.Misc, text: str, command: Callable[[], None], *, primary: bool = False) -> tk.Button:
    background = ACCENT if primary else SURFACE
    foreground = "#ffffff" if primary else TEXT
    active_background = ACCENT_HOVER if primary else "#edf0ed"
    outline = ACCENT if primary else "#b9c3ba"
    return tk.Button(
        parent,
        text=text,
        command=command,
        background=background,
        foreground=foreground,
        activebackground=active_background,
        activeforeground=foreground,
        disabledforeground="#8c958e",
        borderwidth=0,
        relief="flat",
        highlightthickness=1,
        highlightbackground=outline,
        highlightcolor=outline,
        font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"),
        padx=16,
        pady=7,
        cursor="hand2",
    )


def _load_logo(path: Path | None, size: int = 54) -> tk.PhotoImage | None:
    if not path or not path.is_file():
        return None
    try:
        source = tk.PhotoImage(file=str(path))
        factor = max(1, round(max(source.width(), source.height()) / size))
        return source.subsample(factor, factor)
    except tk.TclError:
        return None


def ask_for_update(
    version: str,
    download_size: int,
    *,
    icon_path: Path | None = None,
    logo_path: Path | None = None,
) -> bool:
    window = tk.Tk()
    window.title(f"{APP_DISPLAY_NAME}更新")
    _configure_window(window, icon_path)
    _configure_styles(window)
    _center(window, 540, 286)
    result = {"accepted": False}

    main = tk.Frame(window, background=SURFACE, padx=28, pady=24)
    main.pack(fill="both", expand=True)
    logo = _load_logo(logo_path)
    if logo:
        logo_label = tk.Label(main, image=logo, background=SURFACE)
        logo_label.image = logo
        logo_label.grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, 18))

    content_column = 1 if logo else 0
    tk.Label(
        main,
        text=f"发现新版本 v{version}",
        background=SURFACE,
        foreground=TEXT,
        font=("Microsoft YaHei UI", 16, "bold"),
    ).grid(row=0, column=content_column, sticky="w")
    tk.Label(
        main,
        text="建议立即更新。完成后程序会自动打开，你的本地战绩和登录态不会受影响。",
        background=SURFACE,
        foreground=MUTED,
        font=("Microsoft YaHei UI", 10),
        wraplength=390,
        justify="left",
    ).grid(row=1, column=content_column, sticky="w", pady=(9, 0))
    tk.Label(
        main,
        text=f"需要下载 {format_bytes(download_size)}",
        background=SURFACE,
        foreground=ACCENT,
        font=("Microsoft YaHei UI", 9, "bold"),
    ).grid(row=2, column=content_column, sticky="w", pady=(12, 0))
    main.grid_columnconfigure(content_column, weight=1)

    footer = tk.Frame(window, background=BACKGROUND, padx=22, pady=15, highlightthickness=1, highlightbackground=LINE)
    footer.pack(fill="x")

    def finish(accepted: bool) -> None:
        result["accepted"] = accepted
        window.destroy()

    _button(footer, "稍后", lambda: finish(False)).pack(side="right")
    _button(footer, "立即更新", lambda: finish(True), primary=True).pack(side="right", padx=(0, 10))
    window.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    window.bind("<Escape>", lambda _event: finish(False))
    window.mainloop()
    return result["accepted"]


ProgressReporter = Callable[[str, int, int, str, bool], None]
UpdateWorker = Callable[[ProgressReporter, threading.Event], T]


class UpdateProgressWindow:
    def __init__(
        self,
        version: str,
        total_bytes: int,
        *,
        allow_cancel: bool,
        icon_path: Path | None = None,
        logo_path: Path | None = None,
    ) -> None:
        self.version = version
        self.total_bytes = max(1, total_bytes)
        self.allow_cancel = allow_cancel
        self.cancel_event = threading.Event()
        self.events: queue.Queue[tuple] = queue.Queue()
        self.result: T | None = None
        self.error: Exception | None = None
        self.cancelled = False
        self.started_at = time.monotonic()
        self.close_allowed = False

        self.window = tk.Tk()
        self.window.title(f"{APP_DISPLAY_NAME}更新")
        _configure_window(self.window, icon_path)
        _configure_styles(self.window)
        _center(self.window, 560, 312)

        main = tk.Frame(self.window, background=SURFACE, padx=28, pady=24)
        main.pack(fill="both", expand=True)
        logo = _load_logo(logo_path)
        if logo:
            logo_label = tk.Label(main, image=logo, background=SURFACE)
            logo_label.image = logo
            logo_label.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 17))
        content_column = 1 if logo else 0

        tk.Label(
            main,
            text=f"正在更新{APP_DISPLAY_NAME}",
            background=SURFACE,
            foreground=TEXT,
            font=("Microsoft YaHei UI", 16, "bold"),
        ).grid(row=0, column=content_column, sticky="w")
        tk.Label(
            main,
            text=f"目标版本 v{version}",
            background=SURFACE,
            foreground=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).grid(row=1, column=content_column, sticky="w", pady=(4, 0))
        main.grid_columnconfigure(content_column, weight=1)

        self.stage_label = tk.Label(
            main,
            text="准备更新…",
            background=SURFACE,
            foreground=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.stage_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(25, 9))

        self.percent_label = tk.Label(
            main,
            text="0%",
            background=SURFACE,
            foreground=ACCENT,
            font=("Segoe UI", 10, "bold"),
        )
        self.percent_label.grid(row=2, column=0, columnspan=2, sticky="e", pady=(25, 9))

        self.progress = ttk.Progressbar(
            main,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            style="Update.Horizontal.TProgressbar",
        )
        self.progress.grid(row=3, column=0, columnspan=2, sticky="ew")

        self.detail_label = tk.Label(
            main,
            text=f"0 B / {format_bytes(self.total_bytes)}",
            background=SURFACE,
            foreground=MUTED,
            font=("Segoe UI", 9),
        )
        self.detail_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(9, 0))

        footer = tk.Frame(self.window, background=BACKGROUND, padx=22, pady=15, highlightthickness=1, highlightbackground=LINE)
        footer.pack(fill="x")
        self.hint_label = tk.Label(
            footer,
            text="更新完成后程序会自动打开",
            background=BACKGROUND,
            foreground=MUTED,
            font=("Microsoft YaHei UI", 9),
        )
        self.hint_label.pack(side="left")
        self.cancel_button = _button(footer, "取消", self._request_cancel)
        self.cancel_button.pack(side="right")
        if not allow_cancel:
            self.cancel_button.configure(state="disabled")

        self.window.protocol("WM_DELETE_WINDOW", self._close_window)

    def report(self, stage: str, completed: int, total: int, detail: str = "", cancelable: bool = True) -> None:
        self.events.put(("progress", stage, completed, total, detail, cancelable))

    def run(self, worker: UpdateWorker[T]) -> T | None:
        thread = threading.Thread(target=self._run_worker, args=(worker,), name="delta-updater", daemon=True)
        thread.start()
        self.window.after(40, self._poll)
        self.window.mainloop()
        if self.error:
            raise self.error
        if self.cancelled:
            return None
        return self.result

    def _run_worker(self, worker: UpdateWorker[T]) -> None:
        try:
            self.result = worker(self.report, self.cancel_event)
        except UpdateCancelled:
            self.events.put(("cancelled",))
        except Exception as exc:
            self.events.put(("error", exc))
        else:
            self.events.put(("done",))

    def _poll(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "progress":
                self._apply_progress(*event[1:])
            elif kind == "done":
                self.close_allowed = True
                self.stage_label.configure(text="更新完成", foreground=ACCENT)
                self.detail_label.configure(text=f"正在打开{APP_DISPLAY_NAME}…")
                self.progress.configure(value=100)
                self.percent_label.configure(text="100%")
                self.cancel_button.configure(state="disabled")
                self.window.after(500, self.window.destroy)
                return
            elif kind == "cancelled":
                self.cancelled = True
                self.close_allowed = True
                self.window.after(120, self.window.destroy)
                return
            elif kind == "error":
                self.error = event[1]
                self.close_allowed = True
                self.stage_label.configure(text="更新失败", foreground=ERROR)
                self.detail_label.configure(text=str(self.error), foreground=ERROR, wraplength=500, justify="left")
                self.progress.configure(style="Error.Horizontal.TProgressbar")
                self.hint_label.configure(text="当前版本不会被删除")
                self.cancel_button.configure(text="关闭")
                self.cancel_button.configure(state="normal")
        if self.window.winfo_exists():
            self.window.after(40, self._poll)

    def _apply_progress(
        self,
        stage: str,
        completed: int,
        total: int,
        detail: str,
        cancelable: bool,
    ) -> None:
        total = max(1, total)
        completed = min(max(0, completed), total)
        now = time.monotonic()
        percent = completed / total * 100
        self.stage_label.configure(text=stage, foreground=TEXT)
        self.percent_label.configure(text=f"{percent:.0f}%", foreground=ACCENT)
        self.progress.configure(value=percent, style="Update.Horizontal.TProgressbar")
        if detail:
            detail_text = detail
        else:
            detail_text = format_download_progress(completed, total, now - self.started_at)
        self.detail_label.configure(text=detail_text, foreground=MUTED, wraplength=500, justify="left")
        can_cancel_now = self.allow_cancel and cancelable and not self.cancel_event.is_set()
        self.cancel_button.configure(state="normal" if can_cancel_now else "disabled")

    def _request_cancel(self) -> None:
        if self.close_allowed:
            self.window.destroy()
            return
        if not self.allow_cancel or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.stage_label.configure(text="正在取消更新…")
        self.detail_label.configure(text="正在保留当前版本，请稍候。")

    def _close_window(self) -> None:
        if self.close_allowed:
            self.window.destroy()
        elif self.allow_cancel:
            self._request_cancel()
        else:
            self.window.bell()


def run_update_window(
    version: str,
    total_bytes: int,
    worker: UpdateWorker[T],
    *,
    allow_cancel: bool,
    icon_path: Path | None = None,
    logo_path: Path | None = None,
) -> T | None:
    return UpdateProgressWindow(
        version,
        total_bytes,
        allow_cancel=allow_cancel,
        icon_path=icon_path,
        logo_path=logo_path,
    ).run(worker)
