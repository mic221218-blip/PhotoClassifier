"""详情面板的刷新 / 编辑逻辑（Tk 界面层）。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from src.formatting import format_size
from src.photo_info import EXIF_ERROR, EXIF_NO_EXIF, EXIF_OK, PhotoInfo
from src.quality import FLAG_LABELS, QualityFlag
from src.tag_store import Tag
from src.tags import TagError, chip_colors, normalize_tag_name
from src.ui_theme import (
    ACCENT,
    BORDER_SOFT,
    CARD_BG,
    DETAIL_FIELDS,
    GREEN,
    INPUT_BG,
    ORANGE,
    PANEL_BG,
    TEXT,
    TEXT_FAINT,
    TEXT_SUB,
)


class DetailMixin:
    """依赖宿主提供详情面板控件、缓存与引擎。"""

    def set_detail(self, info: PhotoInfo | None) -> None:
        """刷新右侧详情面板。"""
        self.current = info
        self._draw_preview(info)
        for child in self.rules_frame.winfo_children():
            child.destroy()
        for child in self.quality_frame.winfo_children():
            child.destroy()
        for child in self.tag_chips_frame.winfo_children():
            child.destroy()
        self.tag_suggestions.pack_forget()
        if info is None:
            self.detail_name.config(text="未选择照片")
            self.badge_format.config(text="")
            self.badge_status.config(text="")
            self.detail_meta.config(text="")
            for child in self.detail_rows.winfo_children():
                child.destroy()
            tk.Label(
                self.rules_frame, text="未命中任何规则", bg=PANEL_BG, fg=TEXT_FAINT, font=self._font(8)
            ).pack(anchor="w")
            tk.Label(
                self.quality_frame, text="—", bg=PANEL_BG, fg=TEXT_FAINT, font=self._font(8)
            ).pack(anchor="w")
            self.tag_entry.config(state="disabled")
            self.btn_add_tag.config(state="disabled", fg=TEXT_FAINT)
            self.btn_open.config(state="disabled", command=None)
            self.btn_reveal.config(state="disabled", command=None)
            self.btn_copy.config(state="disabled", command=None)
            return

        self.detail_name.config(text=info.file_name)
        self.badge_format.config(text=info.category.upper())
        if info.exif_status == EXIF_OK:
            self.badge_status.config(text="已读取", fg=GREEN)
        elif info.exif_status == EXIF_ERROR:
            self.badge_status.config(text="读取失败", fg=ORANGE)
        elif info.exif_status == EXIF_NO_EXIF:
            self.badge_status.config(text="无 EXIF", fg=TEXT_SUB)
        else:
            self.badge_status.config(text="未解析", fg=TEXT_SUB)
        self.detail_meta.config(text=f"{format_size(info.size_bytes)} · 修改于 {info.modified_at or '—'}")

        for child in self.detail_rows.winfo_children():
            child.destroy()
        rows = []
        for label, getter in DETAIL_FIELDS:
            value = getter(info)
            rows.append((label, value))
        if info.exif_status == EXIF_ERROR:
            rows.append(("错误信息", info.exif_error))
        for label, value in rows:
            row = tk.Frame(self.detail_rows, bg=PANEL_BG)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9)).pack(side="left")
            tk.Label(
                row, text=value if value else "—", bg=PANEL_BG, fg=TEXT if value else TEXT_FAINT,
                font=self._font(9), anchor="e",
            ).pack(side="right")

        path = info.path
        exists = path.exists()
        self.btn_open.config(
            state="normal" if exists else "disabled",
            command=self.open_selected,
        )
        self.btn_reveal.config(
            state="normal" if exists else "disabled",
            command=self.reveal_selected,
        )
        self.btn_copy.config(state="normal", command=self.copy_selected_path)

        # 规则命中原因（V2）
        matches = self.rule_engine.evaluate(info)
        if not matches:
            tk.Label(
                self.rules_frame, text="未命中任何规则", bg=PANEL_BG, fg=TEXT_FAINT, font=self._font(8)
            ).pack(anchor="w")
        for match in matches:
            row = tk.Frame(self.rules_frame, bg=PANEL_BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{match.label}：", bg=PANEL_BG, fg=ACCENT, font=self._font(8, bold=True)).pack(
                side="left", anchor="n"
            )
            tk.Label(
                row,
                text=match.reason,
                bg=PANEL_BG,
                fg=TEXT_SUB,
                font=self._font(8),
                wraplength=280,
                justify="left",
            ).pack(side="left", anchor="w")

        self._fill_tag_editor(info)
        self._fill_quality_detail(info)

    # ------------------------------------------------------------------ #
    # 质量分析：筛选、详情与分析流程（V2.5）
    # ------------------------------------------------------------------ #

    def _quality_flags_for(self, info: PhotoInfo) -> list[QualityFlag]:
        """一张照片当前的质量标记（含重复）。"""
        key = str(info.path)
        measurement = self.measurements_by_path.get(key)
        if measurement is None or not measurement.analyzed:
            return []
        flags = self.quality_engine.flags(measurement, info)
        reason = self.duplicates.flag_reason(info.path)
        if reason:
            flags.append(QualityFlag("duplicate", FLAG_LABELS["duplicate"], reason))
        return flags

    def _fill_quality_detail(self, info: PhotoInfo) -> None:
        key = str(info.path)
        measurement = self.measurements_by_path.get(key)
        if measurement is None:
            tk.Label(
                self.quality_frame,
                text="未分析（点击工具栏「质量分析」）",
                bg=PANEL_BG,
                fg=TEXT_FAINT,
                font=self._font(8),
                anchor="w",
            ).pack(fill="x")
            return
        if measurement.error or not measurement.analyzed:
            tk.Label(
                self.quality_frame,
                text=measurement.error or "没有可用的测量值",
                bg=PANEL_BG,
                fg=ORANGE,
                font=self._font(8),
                wraplength=305,
                justify="left",
                anchor="w",
            ).pack(fill="x")
            return

        flags = self._quality_flags_for(info)
        source = {
            "embedded_jpeg": "内嵌预览",
            "full_image": "原图",
            "none": "无预览",
        }.get(measurement.preview_source, measurement.preview_source)
        summary = (
            f"清晰度 {measurement.blur_score:.0f} · 对比度 {measurement.contrast:.0f} · "
            f"亮度 {measurement.mean_lum:.0f} · "
            f"高光 {measurement.clip_high_frac:.1%} / 暗部 {measurement.clip_low_frac:.1%}"
        )
        tk.Label(
            self.quality_frame,
            text=f"{source} {measurement.preview_width or '—'}×{measurement.preview_height or '—'}",
            bg=PANEL_BG,
            fg=TEXT_FAINT,
            font=self._font(8),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            self.quality_frame,
            text=summary,
            bg=PANEL_BG,
            fg=TEXT_SUB,
            font=self._font(8),
            wraplength=305,
            justify="left",
            anchor="w",
        ).pack(fill="x")
        if not flags:
            tk.Label(
                self.quality_frame, text="未发现异常", bg=PANEL_BG, fg=GREEN, font=self._font(8), anchor="w"
            ).pack(fill="x")
            return
        for flag in flags:
            row = tk.Frame(self.quality_frame, bg=PANEL_BG)
            row.pack(fill="x", pady=1)
            tk.Label(
                row, text=f"{flag.label}：", bg=PANEL_BG, fg=ORANGE, font=self._font(8, bold=True)
            ).pack(side="left", anchor="n")
            tk.Label(
                row,
                text=flag.reason,
                bg=PANEL_BG,
                fg=TEXT_SUB,
                font=self._font(8),
                wraplength=250,
                justify="left",
            ).pack(side="left", anchor="w")

    # ------------------------------------------------------------------ #
    # 我的标签：单张编辑、批量、管理与撤销（V3）
    # ------------------------------------------------------------------ #

    def _fill_tag_editor(self, info: PhotoInfo | None) -> None:
        for child in self.tag_chips_frame.winfo_children():
            child.destroy()
        if info is None:
            self.tag_entry.delete(0, "end")
            self.tag_entry.config(state="disabled")
            self.btn_add_tag.config(state="disabled", fg=TEXT_FAINT)
            self.tag_suggestions.pack_forget()
            return

        self.tag_entry.config(state="normal")
        self.btn_add_tag.config(state="normal", fg=TEXT)
        tags = self.tag_controller.tags_by_path.get(str(info.path), [])
        if not tags:
            tk.Label(
                self.tag_chips_frame,
                text="（还没有标签）",
                bg=PANEL_BG,
                fg=TEXT_FAINT,
                font=self._font(8),
            ).grid(row=0, column=0, sticky="w")
        for index, tag in enumerate(tags):
            background, foreground = chip_colors(tag.color)
            chip = tk.Frame(self.tag_chips_frame, bg=background)
            chip.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 4), pady=1)
            tk.Label(
                chip,
                text=tag.name,
                bg=background,
                fg=foreground,
                font=self._font(8),
                padx=6,
                pady=1,
            ).pack(side="left")
            tk.Button(
                chip,
                text="×",
                command=lambda tag_id=tag.id: self._remove_tag_from_current(tag_id),
                bg=background,
                fg=foreground,
                activebackground=background,
                activeforeground=foreground,
                relief="flat",
                bd=0,
                font=self._font(8),
                cursor="hand2",
                padx=2,
            ).pack(side="left")
        self._update_tag_suggestions()

    def _update_tag_suggestions(self) -> None:
        if self._tag_entry_var is None or self.current is None:
            return
        query = self._tag_entry_var.get().strip().casefold()
        if not query:
            self.tag_suggestions.pack_forget()
            return
        current_ids = self.tag_controller.tag_ids_by_path.get(str(self.current.path), set())
        matches = [
            tag
            for tag in self.tag_controller.tags_by_id.values()
            if tag.id not in current_ids and query in tag.name.casefold()
        ]
        matches.sort(key=lambda tag: tag.name.casefold())
        self.tag_suggestions.delete(0, "end")
        for tag in matches[:6]:
            self.tag_suggestions.insert("end", tag.name)
        if matches:
            self.tag_suggestions.pack(fill="x", padx=18, pady=(2, 0))
        else:
            self.tag_suggestions.pack_forget()

    def _on_tag_suggestion_clicked(self, _event=None) -> None:
        selection = self.tag_suggestions.curselection()
        if not selection:
            return
        self._tag_entry_var.set(self.tag_suggestions.get(selection[0]))
        self.tag_suggestions.pack_forget()
        self._add_tag_from_entry()

    def _add_tag_from_entry(self) -> None:
        if self.current is None:
            return
        try:
            name = normalize_tag_name(self._tag_entry_var.get())
        except TagError as exc:
            self.status_left.config(text=str(exc))
            return

        tag = self.tag_store.get_by_name(name)
        if tag is None:
            if not messagebox.askyesno(
                "新建标签", f"标签「{name}」不存在，创建并添加到当前照片？"
            ):
                return
            try:
                tag = self.tag_store.create_tag(name)
            except TagError as exc:
                self.status_left.config(text=str(exc))
                return

        current_ids = self.tag_controller.tag_ids_by_path.get(str(self.current.path), set())
        if tag.id in current_ids:
            self.status_left.config(text=f"这张照片已经有标签「{tag.name}」")
            self._tag_entry_var.set("")
            return

        self.tag_controller.add_single(str(self.current.path), tag.id)
        self._tag_entry_var.set("")
        self._after_tag_change(f"已添加标签「{tag.name}」")

    def _remove_tag_from_current(self, tag_id: int) -> None:
        if self.current is None:
            return
        tags = {tag.id: tag for tag in self.tag_controller.tags_by_id.values()}
        removed = self.tag_controller.remove_single(str(self.current.path), tag_id)
        name = tags[tag_id].name if tag_id in tags else str(tag_id)
        if removed:
            self._after_tag_change(f"已移除标签「{name}」")

    def _after_tag_change(self, message: str) -> None:
        self.tag_controller.mark_dirty()
        self.refresh_tags()
        self.refresh_results()
        if self.current is not None:
            self.set_detail(self.current)
        self.refresh_status_bar()
        self.status_left.config(text=message)

