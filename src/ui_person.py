"""Desktop controls for the optional person analysis."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox

from src.database import PhotoDatabase
from src.person_ai import analyze_selection
from src.photo_info import PhotoInfo
from src.preview import PILLOW_AVAILABLE, is_supported
from src.ui_theme import ACCENT, TEXT_FAINT


class PersonAIMixin:
    def _init_person_analysis(self) -> None:
        self.person_filter = tk.BooleanVar(value=False)
        self.person_results: dict[str, bool] = {}
        self.person_thread: threading.Thread | None = None
        self.person_cancel = threading.Event()
        self.person_queue: queue.Queue = queue.Queue()

    def has_ai_person(self, info: PhotoInfo) -> bool:
        return self.person_results.get(str(info.path), False)

    def _fill_person_detail(self, info: PhotoInfo | None) -> None:
        result = self.person_results.get(str(info.path)) if info is not None else None
        if result is None:
            self.person_detail.pack_forget()
        else:
            text = "AI 建议 · 人物" if result else "人物 AI · 没有建议"
            self.person_detail.config(text=text, fg=ACCENT if result else TEXT_FAINT)
            self.person_detail.pack(fill="x", padx=18, pady=(5, 2), after=self.detail_meta)

    def start_person_analysis(self) -> None:
        if self.person_thread is not None or any(
            worker is not None and worker.is_alive() for worker in (self.scan_thread, self.analyze_thread)
        ):
            return
        selected = self._selected_infos()
        infos = [info for info in selected if is_supported(info.category)]
        if not infos:
            messagebox.showinfo("人物 AI", "请在结果列表里选中一张或多张照片，再点击「人物 AI」。")
            return
        self.person_cancel.clear()
        self._set_person_progress(True)
        self.person_thread = threading.Thread(
            target=self._person_worker, args=(infos, len(selected) - len(infos)), daemon=True
        )
        self.person_thread.start()
        self.root.after(100, self._poll_person_queue)

    def _set_person_progress(self, running: bool) -> None:
        if running:
            self.result_frame.pack_forget()
            self.progress_frame.pack(fill="both", expand=True)
            self.progress_bar.config(value=0)
            self.progress_title.config(text="正在分析选中的照片")
            self.progress_folder.config(text="人物 AI · 本地分析")
            self.progress_detail.config(text="")
            self.progress_current.config(text="正在准备照片……")
            self.btn_cancel.config(command=self.cancel_person_analysis)
            for button in (self.btn_person, self.btn_scan, self.btn_analyze,
                           self.btn_browse, self.btn_add, self.btn_remove, self.btn_rescan):
                button.config(state="disabled")
        else:
            self.progress_frame.pack_forget()
            self.result_frame.pack(fill="both", expand=True)
            self.btn_cancel.config(command=self.cancel_scan)
            for button in (self.btn_person, self.btn_scan, self.btn_browse, self.btn_add, self.btn_remove, self.btn_rescan):
                button.config(state="normal")
            self.btn_analyze.config(state="normal" if PILLOW_AVAILABLE else "disabled")

    def cancel_person_analysis(self) -> None:
        self.person_cancel.set()
        self.progress_current.config(text="正在取消，已完成的结果会保留。")

    def _person_worker(self, infos, skipped: int) -> None:
        try:
            with PhotoDatabase.open(self.db_path) as db:
                result = analyze_selection(
                    infos, db,
                    progress=lambda done, total, name: self.person_queue.put(("progress", done, total, name)),
                    cancelled=self.person_cancel.is_set,
                )
            self.person_queue.put(("done", result, skipped))
        except Exception as exc:
            self.person_queue.put(("error", str(exc)))

    def _poll_person_queue(self) -> None:
        try:
            while True:
                kind, *message = self.person_queue.get_nowait()
                if kind == "progress":
                    done, total, name = message
                    self.progress_bar.config(value=done * 100 / total)
                    self.progress_detail.config(text=f"{done:,} / {total:,}")
                    if not self.person_cancel.is_set():
                        self.progress_current.config(text=name)
                    continue

                selected = self.result_tree.selection()
                self.person_thread = None
                self._set_person_progress(False)
                self.refresh_results()
                visible = [iid for iid in selected if self.result_tree.exists(iid)]
                if visible:
                    self.result_tree.selection_set(visible)
                    self.result_tree.see(visible[0])
                if kind == "error":
                    messagebox.showerror("人物分析失败", message[0])
                    return

                (analyzed, reused, failed, first_error), skipped = message
                status = "人物分析已取消" if self.person_cancel.is_set() else "人物分析完成"
                self.status_left.config(text=f"{status} · 新分析 {analyzed:,} · 已分析 {reused:,} · "
                                             f"失败 {failed:,} · 跳过 {skipped:,}")
                if first_error and failed:
                    messagebox.showwarning("部分照片未能分析", first_error)
                return
        except queue.Empty:
            self.root.after(100, self._poll_person_queue)
