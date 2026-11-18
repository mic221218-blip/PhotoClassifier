"""Export settings, progress and results for the desktop photo library."""

from __future__ import annotations

import json
import tkinter as tk
from itertools import islice
from pathlib import Path
from queue import Empty, SimpleQueue
from tempfile import NamedTemporaryFile
from threading import Event, Thread
from tkinter import filedialog, messagebox, ttk

from src.formatting import format_size
from src.photo_export import (
    ExportGroup, ExportOutcome, ExportResult, export_photos, group_export_sources, name_export_groups, valid_export_name,
)
from src.photo_info import PhotoInfo
from src.shell import open_with_default_app
from src.ui_theme import CARD_BG, GREEN, INPUT_BG, ORANGE, PANEL_BG, TEXT, TEXT_SUB


def _load_settings(path: Path) -> tuple[Path | None, str]:
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ""
    if not isinstance(settings, dict):
        return None, ""
    folder = settings.get("folder")
    prefix = settings.get("prefix")
    base = Path(folder) if isinstance(folder, str) and "\x00" not in folder else None
    return (base if base and base.is_absolute() else None,
            prefix if isinstance(prefix, str) and valid_export_name(f"{prefix}_001.JPG") else "")


def _save_settings(path: Path, folder: Path, prefix: str) -> None:
    staged = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
            json.dump({"folder": str(folder), "prefix": prefix}, stream, ensure_ascii=False)
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


class PhotoExportDialog(tk.Toplevel):
    def __init__(
        self, app, selected: list[PhotoInfo], results: list[PhotoInfo], *, selected_first: bool,
    ) -> None:
        super().__init__(app.root)
        self._settings_file = app.db_path.with_name(f"{app.db_path.name}.export.json")
        self._folder, prefix = _load_settings(self._settings_file)
        self._groups = {scope: group_export_sources(info.path for info in photos)
                        for scope, photos in (("selected", selected), ("results", results))}
        self._counts = {"selected": len(selected), "results": len(results)}
        self._sizes = {"selected": sum(info.size_bytes for info in selected),
                       "results": sum(info.size_bytes for info in results)}
        self._scope = tk.StringVar(self, value="selected" if selected_first else "results")
        self._subfolder = tk.StringVar(self)
        self._prefix = tk.StringVar(self, value=prefix)
        self._naming = tk.StringVar(self, value="numbered" if prefix else "original")
        self._collision = tk.StringVar(self, value="rename")
        self._updates: SimpleQueue[tuple[int, str] | ExportResult | Exception] = SimpleQueue()
        self._outcomes: dict[Path, ExportOutcome] = {}
        self._failed: tuple[ExportGroup, ...] = ()
        self._cancel = Event()
        self._destination: Path | None = None
        self._running = False
        self._settings_saved = True

        self.title("导出照片")
        self.transient(app.root)
        self.configure(bg=PANEL_BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", self._close)
        self.bind("<Destroy>", self._on_destroy)

        body = tk.Frame(self, bg=PANEL_BG)
        body.pack(fill="both", expand=True, padx=20, pady=18)
        self._heading = self._label(body, app, size=15, bold=True, text="导出照片")
        self._heading.pack(anchor="w")
        self._form = tk.Frame(body, bg=PANEL_BG)
        self._form.pack(fill="x")
        self._build_form(app)
        self._detail = self._label(body, app, wraplength=540)
        self._progress = ttk.Progressbar(body)
        self._failures = tk.Frame(body, bg=PANEL_BG)
        self._failure_text = tk.Text(self._failures, width=61, height=6, wrap="word", bg=CARD_BG, fg=TEXT,
                                     font=app._font(10), padx=8, pady=6, relief="flat", state="disabled")
        scroll = ttk.Scrollbar(self._failures, orient="vertical", command=self._failure_text.yview)
        self._failure_text.config(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._failure_text.pack(side="left", fill="both", expand=True)

        self._footer = tk.Frame(body, bg=PANEL_BG)
        self._footer.pack(fill="x", pady=(16, 0))
        self._action = app._button(self._footer, "开始导出", self._start, primary=True)
        self._action.pack(side="right")
        self._close_button = app._button(self._footer, "关闭", self._close)
        self._close_button.pack(side="right", padx=(0, 8))
        self._retry_button = app._button(self._footer, "重试失败项", self._retry)
        for variable in (self._scope, self._naming, self._prefix, self._subfolder):
            variable.trace_add("write", self._update_preview)
        self._update_preview()
        self.minsize(590, 1)
        self.update_idletasks()
        root = app.root
        x = max(0, root.winfo_rootx() + (root.winfo_width() - self.winfo_width()) // 2)
        y = max(0, root.winfo_rooty() + (root.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self._browse_button.focus_set()

    @staticmethod
    def _label(parent, app, *, size=10, bold=False, **kwargs):
        return tk.Label(parent, bg=PANEL_BG, fg=TEXT, font=app._font(size, bold=bold), justify="left", **kwargs)

    def _build_form(self, app) -> None:
        form = self._form
        form.columnconfigure(1, weight=1)

        def label(row, text):
            self._label(form, app, text=text, bold=True).grid(row=row, column=0, sticky="nw", pady=(12, 0), padx=(0, 14))

        def controls(row):
            frame = tk.Frame(form, bg=PANEL_BG)
            frame.grid(row=row, column=1, sticky="ew", pady=(12, 0))
            return frame

        def radio(parent, variable, value, text, *, enabled=True):
            tk.Radiobutton(parent, variable=variable, value=value, text=text, bg=PANEL_BG, fg=TEXT,
                           activebackground=PANEL_BG, activeforeground=TEXT, selectcolor=INPUT_BG,
                           font=app._font(10), state="normal" if enabled else "disabled").pack(side="left")

        def entry(parent, variable):
            return tk.Entry(parent, textvariable=variable, width=23, bg=INPUT_BG, fg=TEXT, insertbackground=TEXT,
                            disabledbackground=PANEL_BG, disabledforeground=TEXT_SUB, relief="flat", font=app._font(10))

        label(0, "导出范围")
        scope = controls(0)
        radio(scope, self._scope, "selected", f"已选照片（{self._counts['selected']:,}）", enabled=bool(self._counts['selected']))
        view_label = "当前延时叠" if app._opened_timelapse_id is not None else "当前筛选结果（所有页）"
        radio(scope, self._scope, "results", f"{view_label} · {self._counts['results']:,}")

        label(1, "保存位置")
        folder = controls(1)
        self._browse_button = app._button(folder, "选择…", self._choose_folder)
        self._browse_button.pack(side="left", padx=(0, 8))
        self._path_preview = self._label(folder, app, wraplength=340)
        self._path_preview.pack(side="left", fill="x", expand=True, anchor="w")
        label(2, "子文件夹")
        subfolder = controls(2)
        entry(subfolder, self._subfolder).pack(side="left", ipady=4)
        optional = self._label(subfolder, app, text="可选")
        optional.config(fg=TEXT_SUB)
        optional.pack(side="left", padx=(8, 0))

        label(3, "文件命名")
        naming = controls(3)
        radio(naming, self._naming, "original", "保留原名")
        radio(naming, self._naming, "numbered", "前缀 + 序号")
        self._prefix_entry = entry(naming, self._prefix)
        self._prefix_entry.pack(side="left", padx=(8, 0), ipady=4)

        label(4, "遇到同名")
        collision = controls(4)
        radio(collision, self._collision, "rename", "自动加编号")
        radio(collision, self._collision, "skip", "跳过同名文件")

        label(5, "导出预览")
        preview = controls(5)
        self._summary = self._label(preview, app)
        self._summary.pack(anchor="w")
        self._example_names = self._label(preview, app, size=9, wraplength=390)
        self._example_names.config(fg=TEXT_SUB)
        self._example_names.pack(anchor="w", pady=(4, 0))
        self._form_error = self._label(form, app, wraplength=540)
        self._form_error.config(fg=ORANGE)
        self._form_error.grid(row=6, column=0, columnspan=2, sticky="w")

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="选择导出文件夹", initialdir=str(self._folder or Path.home()),
                                         mustexist=True)
        if chosen:
            self._folder = Path(chosen)
            self._update_preview()

    def _update_preview(self, *_args) -> None:
        scope = self._scope.get()
        numbered = self._naming.get() == "numbered"
        prefix = self._prefix.get().strip() if numbered else ""
        subfolder = self._subfolder.get()
        self._prefix_entry.config(state="normal" if numbered else "disabled")
        error = ""
        if subfolder and not valid_export_name(subfolder):
            error = "请输入有效的子文件夹名称。"
        elif numbered and (not prefix or not valid_export_name(f"{prefix}_001.JPG")):
            error = "请输入有效的文件名前缀。"
        self._destination = self._folder / subfolder if self._folder and not error else None
        self._path_preview.config(text=str(self._destination or self._folder or "请选择目标文件夹"))
        self._summary.config(text=f"{self._counts[scope]:,} 个照片文件  ·  约 {format_size(self._sizes[scope])}")
        examples = (file.name for group in name_export_groups(self._groups[scope], prefix) for file in group)
        self._example_names.config(text="\n".join(islice(examples, 3)) if not error else "")
        self._form_error.config(text=error)
        self._action.config(state="normal" if self._destination and self._counts[scope] else "disabled")

    def _close(self, _event=None) -> str:
        if self._running:
            self._cancel.set()
            self._heading.config(text="正在取消，处理完当前文件组后停止")
            self._close_button.config(state="disabled")
        else:
            self.destroy()
        return "break"

    def _on_destroy(self, event) -> None:
        if event.widget is self:
            self._cancel.set()

    def _start(self) -> None:
        if self._destination is None or self._folder is None:
            return
        prefix = self._prefix.get().strip() if self._naming.get() == "numbered" else ""
        scope = self._scope.get()
        self._total_files = self._counts[scope]
        self._create_subfolder = bool(self._subfolder.get())
        self._skip_existing = self._collision.get() == "skip"
        self._form.pack_forget()
        try:
            _save_settings(self._settings_file, self._folder, prefix)
        except OSError:
            self._settings_saved = False
        self._run(tuple(name_export_groups(self._groups[scope], prefix)))

    def _retry(self) -> None:
        self._run(self._failed, keep_names=True)

    def _run(self, groups: tuple[ExportGroup, ...], *, keep_names: bool = False) -> None:
        self._running = True
        self._cancel.clear()
        self._batch_size = sum(map(len, groups))
        self._heading.config(text="正在重试失败项" if keep_names else "正在导出照片", fg=TEXT)
        self._action.pack_forget()
        self._retry_button.pack_forget()
        self._failures.pack_forget()
        self._close_button.config(text="取消导出", state="normal")
        self._progress.config(value=0, maximum=self._batch_size)
        self._progress.pack(fill="x", pady=(12, 0), before=self._footer)
        self._detail.config(text=f"已处理 0 / {self._batch_size:,} 个文件")
        self._detail.pack(anchor="w", pady=(6, 0), before=self._footer)

        destination, updates, cancel = self._destination, self._updates, self._cancel
        create_subfolder, skip_existing = self._create_subfolder, self._skip_existing

        def copy() -> None:
            try:
                result = export_photos(groups, destination, create_subfolder=create_subfolder, skip_existing=skip_existing,
                                       keep_names=keep_names, progress=lambda done, name: updates.put((done, name)),
                                       cancelled=cancel.is_set)
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
            self._detail.config(text=f"已处理 {done:,} / {self._batch_size:,} 个文件  ·  {name}")
        self.after(100, self._poll)

    def _finish(self, result: ExportResult | Exception) -> None:
        self._running = False
        self._progress.pack_forget()
        self._close_button.config(text="关闭", state="normal")
        self._action.config(text="打开导出文件夹", command=self._open_folder, state="normal")
        self._action.pack(side="right", before=self._close_button)
        if isinstance(result, ExportResult):
            self._outcomes.update((outcome.file.source, outcome) for outcome in result.outcomes)

        copied = sum(outcome.status == "copied" for outcome in self._outcomes.values())
        skipped = sum(outcome.status == "skipped" for outcome in self._outcomes.values())
        failed = [outcome for outcome in self._outcomes.values() if outcome.status == "failed"]
        self._failed = tuple((outcome.file,) for outcome in failed)
        cancelled = isinstance(result, ExportResult) and result.cancelled
        heading = "导出中断" if isinstance(result, Exception) else ("导出已取消" if cancelled else "导出完成")
        self._heading.config(text=heading, fg=TEXT if failed or heading != "导出完成" else GREEN)
        summary = f"成功 {copied:,}  ·  跳过 {skipped:,}  ·  失败 {len(failed):,}"
        if remaining := self._total_files - len(self._outcomes):
            summary += f"  ·  未处理 {remaining:,}"
        if not self._settings_saved:
            summary += "\n本次导出设置未能保存。"
        if isinstance(result, Exception):
            summary += f"\n{result}"
        self._detail.config(text=f"{self._destination}\n\n{summary}")
        if failed:
            self._failure_text.config(state="normal")
            self._failure_text.delete("1.0", "end")
            self._failure_text.insert("1.0", "\n\n".join(f"{item.file.source}\n{item.reason}" for item in failed))
            self._failure_text.config(state="disabled")
            self._failures.pack(fill="both", expand=True, pady=(8, 0), before=self._footer)
            self._retry_button.pack(side="left")

    def _open_folder(self) -> None:
        opened, error = open_with_default_app(self._destination)
        if not opened:
            messagebox.showwarning("打开导出文件夹", error, parent=self)
