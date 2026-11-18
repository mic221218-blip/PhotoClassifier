"""Desktop controls for optional AI analysis."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox

from src.database import PhotoDatabase
from src.person_ai import analyze_selection as analyze_person_selection
from src.photo_info import PhotoInfo
from src.preview import is_supported
from src.scene_ai import ROLE_NAMES, SUBJECTS, Role, analyze_selection as analyze_scene_selection
from src.sky_ai import analyze_selection as analyze_sky_selection
from src.ui_theme import ACCENT, INPUT_BG, TEXT_FAINT


class AIAnalysisMixin:
    def _init_ai_analysis(self) -> None:
        self.ai_filters = {key: tk.BooleanVar(value=False) for key in SUBJECTS}
        self.person_filter = self.ai_filters["person"]
        self.sky_filter = self.ai_filters["sky"]
        self.person_results: dict[str, bool] = {}
        self.sky_results: dict[str, bool] = {}
        self.scene_results: dict[str, dict[str, int]] = {}
        self.ai_thread: threading.Thread | None = None
        self.ai_cancel = threading.Event()
        self.ai_queue: queue.Queue = queue.Queue()

    def _ai_roles(self, iid: str) -> dict[str, int]:
        roles = self.scene_results.get(iid)
        if roles is not None:
            return roles
        return {key: 0 for key, results in (("person", self.person_results), ("sky", self.sky_results))
                if results.get(iid)}

    def _ai_sort_key(self, iid: str, selected):
        roles = self._ai_roles(iid)
        # Unranked legacy detections follow background matches. Stable sorting
        # preserves the requested sharpness or catalog order within equal roles.
        counts = [0, 0, 0, 0]
        for key in selected.intersection(roles):
            counts[roles[key]] += 1
        return counts[::-1]

    def _fill_ai_detail(self, info: PhotoInfo | None) -> None:
        anchor = self.detail_meta
        roles = self.scene_results.get(str(info.path)) if info is not None else None
        for label in self.scene_details.values():
            label.pack_forget()
        if roles is not None:
            for label in self.ai_details.values():
                label.pack_forget()
            for role, title in ROLE_NAMES.items():
                names = [SUBJECTS[key] for key, value in roles.items() if value == role]
                if names or (not roles and role == Role.PRIMARY):
                    label = self.scene_details[role]
                    label.config(text=f"{title} · {'、'.join(names)}" if names else "六类题材均无明显命中")
                    label.pack(fill="x", padx=18, pady=(5, 2), after=anchor)
                    anchor = label
            return
        for name, results in (("人物", self.person_results), ("天空", self.sky_results)):
            detail = self.ai_details[name]
            result = results.get(str(info.path)) if info is not None else None
            if result is None:
                detail.pack_forget()
            else:
                text = f"AI 建议 · {name}" if result else f"{name} AI · 没有建议"
                detail.config(text=text, fg=ACCENT if result else TEXT_FAINT)
                detail.pack(fill="x", padx=18, pady=(5, 2), after=anchor)
                anchor = detail

    def _update_ai_button(self) -> None:
        count = sum(is_supported(self.infos_by_iid[iid].category) for iid in self._selected_iids)
        label = f"AI 分析（{count:,} 张）" if count and count < len(self._selected_iids) else "AI 分析"
        self.btn_ai.config(
            text=f"{label} ▾", state="normal" if count else "disabled", bg=ACCENT if count else INPUT_BG,
        )

    def start_person_analysis(self) -> None:
        self._start_ai_analysis((("人物", analyze_person_selection),))

    def start_sky_analysis(self) -> None:
        self._start_ai_analysis((("天空", analyze_sky_selection),))

    def start_people_and_sky_analysis(self) -> None:
        self._start_ai_analysis((("人物", analyze_person_selection), ("天空", analyze_sky_selection)))

    def start_scene_analysis(self) -> None:
        self._start_ai_analysis((("六类题材主次", analyze_scene_selection),))

    def _start_ai_analysis(self, analyses) -> None:
        if not self._selecting_results or any(
            worker is not None for worker in (self.ai_thread, self.scan_thread, self.analyze_thread)
        ):
            return
        infos = [info for info in self._selected_infos() if is_supported(info.category)]
        if not infos:
            return
        label = "和".join(name for name, _ in analyses)
        self.ai_cancel.clear()
        self._set_ai_progress(True, f"{label} · {len(infos):,} 张")
        self.ai_thread = threading.Thread(target=self._ai_worker, args=(analyses, infos), daemon=True)
        self.ai_thread.start()
        self.root.after(100, self._poll_ai_queue, label)

    def _set_ai_progress(self, running: bool, detail: str = "") -> None:
        if running:
            self.result_frame.pack_forget()
            self.progress_frame.pack(fill="both", expand=True)
            self.progress_bar.config(value=0)
            self.progress_title.config(text="正在分析选中的照片")
            self.progress_folder.config(text=detail)
            self.progress_detail.config(text="")
            self.progress_current.config(text="正在准备照片……")
            self.btn_cancel.config(command=self.cancel_ai_analysis)
            self._update_scan_controls("AI 分析中…")
        else:
            self.progress_frame.pack_forget()
            self.result_frame.pack(fill="both", expand=True)
            self.btn_cancel.config(command=self.cancel_scan)
            self._update_scan_controls()

    def cancel_ai_analysis(self) -> None:
        self.ai_cancel.set()
        self.progress_current.config(text="正在取消，已完成的结果会保留。")

    def _ai_worker(self, analyses, infos) -> None:
        reports = []
        errors = []
        photo_count = len(infos)
        try:
            with PhotoDatabase.open(self.db_path) as db:
                for index, (label, analyze) in enumerate(analyses):
                    if self.ai_cancel.is_set():
                        break
                    offset = index * photo_count
                    self.ai_queue.put(("stage", label, index + 1, len(analyses), photo_count))
                    try:
                        report = analyze(
                            infos, db,
                            progress=lambda done, total, name: self.ai_queue.put(
                                ("progress", offset + done, photo_count * len(analyses), done, total, name)
                            ),
                            cancelled=self.ai_cancel.is_set,
                        )
                    except Exception as exc:
                        errors.append(f"{label} · {exc}")
                        continue
                    reports.append(report)
                    if report[3]:
                        errors.append(f"{label} · {report[3]}")
            self.ai_queue.put(("done", reports, errors))
        except Exception as exc:
            self.ai_queue.put(("error", str(exc)))

    def _poll_ai_queue(self, label: str) -> None:
        try:
            while True:
                kind, *message = self.ai_queue.get_nowait()
                if kind == "stage":
                    name, stage, stages, count = message
                    text = f"第 {stage} / {stages} 项 · " if stages > 1 else ""
                    self.progress_folder.config(text=f"{text}{name} AI · {count:,} 张")
                    self.progress_detail.config(text=f"0 / {count:,}")
                    if not self.ai_cancel.is_set():
                        self.progress_current.config(text="正在准备照片……")
                    continue
                if kind == "progress":
                    total_done, total_work, done, count, name = message
                    self.progress_bar.config(value=total_done * 100 / total_work)
                    self.progress_detail.config(text=f"{done:,} / {count:,}")
                    if not self.ai_cancel.is_set():
                        self.progress_current.config(text=name)
                    continue

                self.ai_thread = None
                self._set_ai_progress(False)
                self.refresh_results()
                if kind == "error":
                    messagebox.showerror(f"{label}分析失败", message[0])
                    return

                reports, errors = message
                analyzed = sum(report[0] for report in reports)
                reused = sum(report[1] for report in reports)
                failed = sum(report[2] for report in reports)
                if self.ai_cancel.is_set():
                    status = f"{label}分析已取消"
                elif errors:
                    status = f"{label}分析有错误" if reports else f"{label}分析失败"
                else:
                    status = f"{label}分析完成"
                self.status_left.config(text=f"{status} · 新分析 {analyzed:,} 项 · "
                                             f"已分析 {reused:,} 项 · 失败 {failed:,} 项")
                if errors:
                    messagebox.showwarning("AI 分析有错误", "\n\n".join(errors))
                return
        except queue.Empty:
            self.root.after(100, self._poll_ai_queue, label)
