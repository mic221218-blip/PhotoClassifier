"""Confirmation, progress and results for copying selected photos."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
from tkinter import messagebox, ttk

from src.photo_export import ExportResult, export_photos
from src.shell import open_with_default_app
from src.ui_theme import CARD_BG, GREEN, PANEL_BG, TEXT, TEXT_SUB


class PhotoExportDialog(tk.Toplevel):
    def __init__(self, app, sources: list[Path], destination: Path) -> None:
        super().__init__(app.root)
        self._sources = sources
        self._destination = destination
        self._updates: SimpleQueue[tuple[int, str] | ExportResult | Exception] = SimpleQueue()
        self._running = False

        self.title("导出所选照片")
        self.transient(app.root)
        self.configure(bg=PANEL_BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", self._close)

        body = tk.Frame(self, bg=PANEL_BG, width=500)
        body.pack(fill="both", expand=True, padx=20, pady=18)
        self._heading = tk.Label(
            body, text=f"将复制 {len(sources):,} 个照片文件", bg=PANEL_BG, fg=TEXT, font=app._font(15, bold=True),
        )
        self._heading.pack(anchor="w")
        tk.Label(
            body, text=f"导出到  {destination}", bg=PANEL_BG, fg=TEXT_SUB,
            font=app._font(10), wraplength=460, justify="left",
        ).pack(anchor="w", pady=(6, 0))

        self._progress = ttk.Progressbar(body, maximum=len(sources))
        self._detail = tk.Label(body, bg=PANEL_BG, fg=TEXT, font=app._font(10), wraplength=460, justify="left")
        self._failures = tk.Frame(body, bg=PANEL_BG)
        self._failure_text = tk.Text(
            self._failures, width=55, height=6, wrap="word", bg=CARD_BG, fg=TEXT,
            font=app._font(10), padx=8, pady=6, relief="flat", undo=False,
        )
        scroll = ttk.Scrollbar(self._failures, orient="vertical", command=self._failure_text.yview)
        self._failure_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._failure_text.pack(side="left", fill="both", expand=True)

        footer = tk.Frame(body, bg=PANEL_BG)
        footer.pack(side="bottom", fill="x", pady=(16, 0))
        self._action = app._button(footer, "开始导出", self._start, primary=True)
        self._action.pack(side="right")
        self._close_button = app._button(footer, "取消", self._close)
        self._close_button.pack(side="right", padx=(0, 8))

        self.minsize(500, 1)
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_reqwidth()) // 2
        y = app.root.winfo_rooty() + (app.root.winfo_height() - self.winfo_reqheight()) // 2
        self.geometry(f"{x:+d}{y:+d}")
        self.grab_set()
        self._action.focus_set()

    def _close(self, _event=None) -> None:
        if not self._running:
            self.destroy()

    def _start(self) -> None:
        self._running = True
        self._action.config(state="disabled")
        self._close_button.config(state="disabled")
        self._heading.config(text="正在导出照片")
        self._progress.pack(fill="x", pady=(16, 0))
        self._detail.config(text=f"已处理 0 / {len(self._sources):,} 个文件")
        self._detail.pack(anchor="w", pady=(6, 0))

        sources, destination, updates = self._sources, self._destination, self._updates

        def copy() -> None:
            try:
                result = export_photos(sources, destination, progress=lambda done, name: updates.put((done, name)))
            except Exception as exc:
                updates.put(exc)
            else:
                updates.put(result)

        Thread(target=copy, name="photo-export", daemon=False).start()
        self.after(100, self._poll)

    def _poll(self) -> None:
        progress = None
        try:
            while True:
                update = self._updates.get_nowait()
                if isinstance(update, tuple):
                    progress = update
                else:
                    self._finish(update)
                    return
        except Empty:
            pass
        if progress is not None:
            done, name = progress
            self._progress.config(value=done)
            self._detail.config(text=f"已处理 {done:,} / {len(self._sources):,} 个文件  ·  {name}")
        self.after(100, self._poll)

    def _finish(self, result: ExportResult | Exception) -> None:
        self._running = False
        self._progress.pack_forget()
        self._close_button.config(text="关闭", state="normal")
        self._action.config(text="打开导出文件夹", command=self._open_folder, state="normal")
        if isinstance(result, Exception):
            self._heading.config(text="导出中断")
            self._detail.config(text=str(result))
            return

        failed = len(result.failures)
        self._heading.config(text="导出完成", fg=TEXT if failed else GREEN)
        self._detail.config(text=f"成功 {result.copied:,}  ·  跳过 {result.skipped:,}  ·  失败 {failed:,}")
        if result.failures:
            self._failure_text.insert("1.0", "\n\n".join(f"{path}\n{reason}" for path, reason in result.failures))
            self._failure_text.config(state="disabled")
            self._failures.pack(fill="both", expand=True, pady=(8, 0))

    def _open_folder(self) -> None:
        opened, error = open_with_default_app(self._destination)
        if not opened:
            messagebox.showwarning("打开导出文件夹", error, parent=self)
