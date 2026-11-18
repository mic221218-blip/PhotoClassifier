"""Desktop controls for optional AI analysis."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from src.database import PhotoDatabase
from src.person_ai import analyze_selection as analyze_person_selection
from src.photo_info import PhotoInfo
from src.photo_scope import supported_photos
from src.scene_ai import ROLE_NAMES, SUBJECTS, Role, analyze_selection as analyze_scene_selection
from src.sky_ai import analyze_selection as analyze_sky_selection
from src.ui_theme import ACCENT, INPUT_BG, PANEL_BG, TEXT_FAINT, TEXT_SUB


class AIAnalysisMixin:
    def _init_ai_analysis(self) -> None:
        self.ai_filters = {key: tk.BooleanVar(value=False) for key in SUBJECTS}
        self.person_filter = self.ai_filters["person"]
        self.sky_filter = self.ai_filters["sky"]
        self.person_results: dict[str, bool] = {}
        self.sky_results: dict[str, bool] = {}
        self.scene_results: dict[str, dict[str, int]] = {}
        self.scene_current_paths: set[str] = set()
        self._ai_scope_photos: list[PhotoInfo] = []
        self._ai_scope_count = self._ai_scope_ready = 0
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
        count = len(supported_photos(self.infos_by_iid[iid] for iid in self._selected_iids))
        label = f"分析（{count:,} 张）" if count and count < len(self._selected_iids) else "分析"
        self.btn_ai.config(text=f"{label} ▾", bg=ACCENT if count else INPUT_BG)
        busy = self._analysis_busy()
        for entry in ("六类题材主次", "人物和天空", "人物", "天空"):
            self.ai_menu.entryconfigure(entry, state="normal" if count and not busy else "disabled")
        self.ai_menu.entryconfigure(
            self.ai_target_entry, label=f"已选照片 · {count:,} 张" if count else "多选照片后可单独分析",
        )
        self._update_scope_analysis_button()

    def _analysis_busy(self) -> bool:
        return any(worker is not None for worker in (self.ai_thread, self.scan_thread, self.analyze_thread))

    def _update_ai_coverage(self, photos: list[PhotoInfo]) -> None:
        self._ai_scope_photos = [] if self.opened_pick_list else supported_photos(photos)
        self._ai_scope_count = len(self._ai_scope_photos)
        self._ai_scope_ready = sum(str(info.path) in self.scene_current_paths for info in self._ai_scope_photos)
        pending = self._ai_scope_count - self._ai_scope_ready
        legacy = sum(str(info.path) in self.scene_results for info in self._ai_scope_photos) - self._ai_scope_ready
        older = f"（含旧版 {legacy:,} 张）" if legacy else ""
        coverage = (f"本版已分析 {self._ai_scope_ready:,}/{self._ai_scope_count:,} 张\n待分析 {pending:,} 张{older}"
                    if self._ai_scope_count else "此范围暂无可分析照片")
        self.ai_coverage.config(text=coverage, justify="left")
        self._update_scope_analysis_button()

    def _update_scope_analysis_button(self) -> None:
        scope = "当前文件夹" if self._browse_folder else ("当前项目" if self.active_project else "全部图库")
        text = f"分析{scope}"
        state = "normal" if self._ai_scope_count and not self.opened_pick_list and not self._analysis_busy() else "disabled"
        self.btn_analyze_scope.config(text=text, state=state)
        self.ai_menu.entryconfigure(self.ai_scope_entry, label=f"{text}…", state=state)

    def start_scope_scene_analysis(self) -> None:
        if self._analysis_busy() or not self._ai_scope_photos or self.opened_pick_list:
            return
        if self._browse_folder:
            label = f"{self._browse_folder}（含子文件夹）"
        else:
            label = f"项目「{self.active_project.name}」" if self.active_project else "全部图库"
        infos = tuple(self._ai_scope_photos)
        if messagebox.askyesno(
            "六类题材分析",
            f"{label}\n\n共有 {len(infos):,} 张可分析照片，已有 {self._ai_scope_ready:,} 张六类题材结果。\n"
            "已有结果会复用。分析期间可以继续选片。\n\n开始分析？",
            parent=self.root,
        ):
            self._start_ai_analysis((("六类题材主次", analyze_scene_selection),), infos)

    def start_person_analysis(self) -> None:
        self._start_ai_analysis((("人物", analyze_person_selection),))

    def start_sky_analysis(self) -> None:
        self._start_ai_analysis((("天空", analyze_sky_selection),))

    def start_people_and_sky_analysis(self) -> None:
        self._start_ai_analysis((("人物", analyze_person_selection), ("天空", analyze_sky_selection)))

    def start_scene_analysis(self) -> None:
        self._start_ai_analysis((("六类题材主次", analyze_scene_selection),))

    def _start_ai_analysis(self, analyses, infos=None) -> None:
        if self._analysis_busy():
            return
        if infos is None:
            if not self._selecting_results:
                return
            infos = supported_photos(self._selected_infos())
        if not infos:
            return
        label = "和".join(name for name, _ in analyses)
        self.ai_cancel.clear()
        self.ai_thread = threading.Thread(target=self._ai_worker, args=(analyses, infos), daemon=True)
        self._set_ai_progress(True, f"{label} · {len(infos):,} 张")
        self.ai_thread.start()
        self.root.after(100, self._poll_ai_queue, label)

    def _build_ai_progress(self, parent) -> None:
        self.ai_progress = bar = tk.Frame(parent, bg=PANEL_BG)
        self.ai_progress_text = tk.Label(bar, bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9))
        self.ai_progress_text.pack(side="left", padx=(8, 6))
        self.ai_progress_bar = ttk.Progressbar(bar, mode="determinate")
        self.ai_progress_bar.pack(side="left", fill="x", expand=True, padx=6)
        self.btn_cancel_ai = self._button(bar, "取消分析", self.cancel_ai_analysis)
        self.btn_cancel_ai.pack(side="right", padx=(6, 8), pady=4)

    def _set_ai_progress(self, running: bool, detail: str = "") -> None:
        if running:
            self.ai_progress_text.config(text=detail)
            self.ai_progress_bar.config(value=0)
            self.btn_cancel_ai.config(state="normal")
            self.ai_progress.pack(side="bottom", fill="x", padx=12, pady=4)
        else:
            self.ai_progress.pack_forget()
        self._update_scan_controls()

    def cancel_ai_analysis(self) -> None:
        self.ai_cancel.set()
        self.ai_progress_text.config(text="正在取消，已完成的结果会保留")
        self.btn_cancel_ai.config(state="disabled")

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
                    if not self.ai_cancel.is_set():
                        self.ai_progress_text.config(text=f"{text}{name} · 0 / {count:,} 张")
                    continue
                if kind == "progress":
                    total_done, total_work, done, count, _name = message
                    self.ai_progress_bar.config(value=total_done * 100 / total_work)
                    if not self.ai_cancel.is_set():
                        self.ai_progress_text.config(text=f"{label} · {done:,} / {count:,} 张")
                    continue

                focus = self._focus_iid if self._focus_iid in self.result_tree.get_children() else ""
                grid_scroll, list_scroll = self.gallery.yview()[0], self.result_tree.yview()[0]
                self.ai_thread = None
                self._set_ai_progress(False)
                self.refresh_results()
                if focus in self._result_iids:
                    self._show_focused_result_page(focus)
                self._restore_result_position(focus, grid_scroll, list_scroll)
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
