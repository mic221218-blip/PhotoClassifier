"""V2/V3 主窗口的布局构建（Tk 面板），由 PhotoClassifierApp 继承使用。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from src.photo_info import PhotoInfo
from src.preview import PILLOW_AVAILABLE
from src.quality import FLAG_IDS, FLAG_LABELS
from src.rules import RULE_REGISTRY
from src.ui_theme import (
    ACCENT,
    ACCENT_DIM,
    BORDER_SOFT,
    CARD_BG,
    DEFAULT_DB,
    DETAIL_FIELDS,
    GREEN,
    ICON_PATH,
    INPUT_BG,
    ORANGE,
    PANEL_BG,
    PREVIEW_COLORS,
    PROJECT_ROOT,
    QUALITY_SHORT,
    RESULT_COLUMNS,
    TEXT,
    TEXT_FAINT,
    TEXT_SUB,
    WIN_BG,
    category_label,
)


class PanelsMixin:
    """只负责构建控件；状态与业务动作仍在主窗口里。"""

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self.root, bg=WIN_BG, height=60)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        tk.Label(bar, text="文件夹", bg=WIN_BG, fg=TEXT_SUB, font=self._font(10)).pack(
            side="left", padx=(20, 8)
        )
        self.folder_var = tk.StringVar(value="")
        entry = tk.Entry(
            bar,
            textvariable=self.folder_var,
            bg=INPUT_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Consolas", 10),
            state="readonly",
            readonlybackground=INPUT_BG,
        )
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10), ipady=6)
        self.btn_browse = self._button(bar, "浏览…", self.add_folder_dialog)
        self.btn_browse.pack(side="left", padx=(0, 10))
        self.btn_scan = self._button(bar, "开始扫描", self.start_scan, primary=True)
        self.btn_scan.pack(side="left", padx=(0, 8))
        self.btn_analyze = self._button(bar, "质量分析", self.start_analysis)
        self.btn_analyze.pack(side="left", padx=(0, 12))
        if not PILLOW_AVAILABLE:
            self.btn_analyze.config(state="disabled", fg=TEXT_FAINT)
        tk.Label(
            bar, text="只读扫描 · 不会改动原始文件", bg=WIN_BG, fg=TEXT_FAINT, font=self._font(9)
        ).pack(side="left", padx=(0, 16))

    # ------------------------------------------------------------------ #
    # 左侧：文件夹库 + 筛选占位
    # ------------------------------------------------------------------ #

    def _build_left_panel(self) -> None:
        outer = tk.Frame(self.root, bg=PANEL_BG, width=270)
        outer.pack(side="left", fill="y")
        outer.pack_propagate(False)

        # 面板内容比窗口高时（小屏幕 / 系统缩放），用滚动条保证所有控件可达
        canvas = tk.Canvas(outer, bg=PANEL_BG, highlightthickness=0, width=258)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        panel = tk.Frame(canvas, bg=PANEL_BG)
        window = canvas.create_window((0, 0), window=panel, anchor="nw", width=258)
        panel.bind(
            "<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.left_canvas = canvas

        tk.Label(panel, text="文件夹库", bg=PANEL_BG, fg=TEXT, font=self._font(11, bold=True)).pack(
            anchor="w", padx=16, pady=(14, 6)
        )
        self.library_tree = ttk.Treeview(panel, show="tree", selectmode="browse", height=5)
        self.library_tree.pack(fill="x", padx=12)
        self.library_tree.bind("<ButtonRelease-1>", self._on_library_click)

        btn_row = tk.Frame(panel, bg=PANEL_BG)
        btn_row.pack(fill="x", padx=12, pady=(8, 4))
        self.btn_add = self._button(btn_row, "+ 添加文件夹", self.add_folder_dialog)
        self.btn_add.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_remove = self._button(btn_row, "移除", self.remove_selected_folder)
        self.btn_remove.pack(side="left")

        tk.Frame(panel, bg=BORDER_SOFT, height=1).pack(fill="x", padx=12, pady=10)

        tk.Label(panel, text="筛选", bg=PANEL_BG, fg=TEXT, font=self._font(11, bold=True)).pack(
            anchor="w", padx=16, pady=(0, 2)
        )
        tk.Label(
            panel, text="自动标签（EXIF 规则）· 勾选后实时筛选", bg=PANEL_BG, fg=TEXT_FAINT, font=self._font(8)
        ).pack(anchor="w", padx=16, pady=(0, 6))

        rules = tk.Frame(panel, bg=PANEL_BG)
        rules.pack(fill="x", padx=12)
        rule_entries = [(rule_id, zh) for rule_id, zh, _en, _fn in RULE_REGISTRY]
        for i, (rule_id, label) in enumerate(rule_entries):
            var = tk.BooleanVar(value=False)
            self.rule_vars[rule_id] = var
            tk.Checkbutton(
                rules,
                text=label,
                variable=var,
                command=self._on_filter_changed,
                bg=PANEL_BG,
                fg=TEXT,
                activebackground=PANEL_BG,
                activeforeground=TEXT,
                selectcolor=INPUT_BG,
                font=self._font(9),
            ).grid(row=i // 2, column=i % 2, sticky="w", pady=1, padx=(0, 6))

        gps_row = tk.Frame(panel, bg=PANEL_BG)
        gps_row.pack(fill="x", padx=16, pady=(2, 0))
        tk.Checkbutton(
            gps_row,
            text="只看有 GPS 坐标的照片",
            variable=self.gps_only,
            command=self._on_filter_changed,
            bg=PANEL_BG,
            fg=TEXT_SUB,
            activebackground=PANEL_BG,
            activeforeground=TEXT_SUB,
            selectcolor=INPUT_BG,
            font=self._font(9),
        ).pack(anchor="w")

        tk.Frame(panel, bg=BORDER_SOFT, height=1).pack(fill="x", padx=12, pady=(8, 6))
        tk.Label(
            panel, text="质量分析（V2.5）", bg=PANEL_BG, fg=TEXT, font=self._font(10, bold=True)
        ).pack(anchor="w", padx=16)
        tk.Label(
            panel, text="模糊 / 曝光 / 重复 · 勾选后实时筛选", bg=PANEL_BG, fg=TEXT_FAINT, font=self._font(8)
        ).pack(anchor="w", padx=16, pady=(0, 4))

        quality = tk.Frame(panel, bg=PANEL_BG)
        quality.pack(fill="x", padx=12)
        for i, flag_id in enumerate(FLAG_IDS):
            var = tk.BooleanVar(value=False)
            self.quality_vars[flag_id] = var
            tk.Checkbutton(
                quality,
                text=FLAG_LABELS[flag_id],
                variable=var,
                command=self._on_filter_changed,
                bg=PANEL_BG,
                fg=TEXT,
                activebackground=PANEL_BG,
                activeforeground=TEXT,
                selectcolor=INPUT_BG,
                font=self._font(9),
            ).grid(row=i // 2, column=i % 2, sticky="w", pady=1, padx=(0, 6))

        tk.Frame(panel, bg=BORDER_SOFT, height=1).pack(fill="x", padx=12, pady=(8, 6))
        tk.Label(
            panel, text="我的标签", bg=PANEL_BG, fg=TEXT, font=self._font(10, bold=True)
        ).pack(anchor="w", padx=16)
        tk.Label(
            panel, text="勾选后与规则 / 质量共用 并且/或者", bg=PANEL_BG, fg=TEXT_FAINT, font=self._font(8)
        ).pack(anchor="w", padx=16, pady=(0, 4))

        tk.Label(panel, text="搜索标签", bg=PANEL_BG, fg=TEXT_FAINT, font=self._font(8)).pack(
            anchor="w", padx=16
        )
        self.tag_search_var = tk.StringVar()
        search = tk.Entry(
            panel,
            textvariable=self.tag_search_var,
            bg=INPUT_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=self._font(9),
        )
        search.pack(fill="x", padx=16, ipady=4)
        self.tag_search_var.trace_add("write", lambda *_args: self._refresh_tag_list())

        tag_list_frame = tk.Frame(panel, bg=PANEL_BG)
        tag_list_frame.pack(fill="x", padx=12, pady=(6, 0))
        self.tag_tree = ttk.Treeview(
            tag_list_frame, show="tree", selectmode="browse", height=6
        )
        tag_scroll = ttk.Scrollbar(
            tag_list_frame, orient="vertical", command=self.tag_tree.yview
        )
        self.tag_tree.configure(yscrollcommand=tag_scroll.set)
        self.tag_tree.pack(side="left", fill="x", expand=True)
        tag_scroll.pack(side="right", fill="y")
        self.tag_tree.bind("<ButtonRelease-1>", self._on_tag_click)

        self.btn_manage_tags = self._button(panel, "管理标签…", self.open_tag_manager)
        self.btn_manage_tags.pack(fill="x", padx=16, pady=(6, 2), ipady=1)

        logic_row = tk.Frame(panel, bg=PANEL_BG)
        logic_row.pack(fill="x", padx=16, pady=(8, 0))
        tk.Label(logic_row, text="条件组合", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9)).pack(anchor="w")
        radios = tk.Frame(logic_row, bg=PANEL_BG)
        radios.pack(anchor="w", pady=(2, 0))
        for text, value in (("并且 AND", True), ("或者 OR", False)):
            tk.Radiobutton(
                radios,
                text=text,
                value=value,
                variable=self.and_mode,
                command=self._on_filter_changed,
                bg=PANEL_BG,
                fg=TEXT_SUB,
                activebackground=PANEL_BG,
                activeforeground=TEXT_SUB,
                selectcolor=INPUT_BG,
                font=self._font(9),
            ).pack(anchor="w")

        self.btn_clear = self._button(panel, "清除筛选", self.clear_filters)
        self.btn_clear.pack(fill="x", padx=16, pady=(10, 2), ipady=2)

        tk.Frame(panel, bg=BORDER_SOFT, height=1).pack(fill="x", padx=12, pady=8)

        tk.Label(panel, text="拍摄参数（V4 接入）", bg=PANEL_BG, fg=TEXT_FAINT, font=self._font(8)).pack(
            anchor="w", padx=16, pady=(0, 4)
        )
        params = tk.Frame(panel, bg=PANEL_BG)
        params.pack(fill="x", padx=16)
        for i, label in enumerate(("ISO ≥", "焦距 ≥", "快门 ≤")):
            tk.Label(params, text=label, bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9)).grid(
                row=i, column=0, sticky="w", pady=2
            )
            tk.Entry(
                params, bg=INPUT_BG, fg=TEXT_FAINT, relief="flat", width=12, state="disabled",
                disabledbackground=INPUT_BG, disabledforeground=TEXT_FAINT, font=self._font(9),
            ).grid(row=i, column=1, sticky="w", padx=(12, 0), pady=2, ipady=3)

    # ------------------------------------------------------------------ #
    # 中部：结果列表 + 扫描进度
    # ------------------------------------------------------------------ #

    def _build_center(self) -> None:
        center = tk.Frame(self.root, bg=WIN_BG)
        center.pack(side="left", fill="both", expand=True)

        header = tk.Frame(center, bg=WIN_BG)
        header.pack(fill="x", padx=18, pady=(12, 6))
        tk.Label(header, text="扫描结果", bg=WIN_BG, fg=TEXT, font=self._font(11, bold=True)).pack(
            side="left"
        )
        self.count_label = tk.Label(header, text="", bg=WIN_BG, fg=TEXT_SUB, font=self._font(9))
        self.count_label.pack(side="left", padx=(12, 0))

        # 扫描进度视图（扫描时显示，平时隐藏）
        self.progress_frame = tk.Frame(center, bg=WIN_BG)
        self.progress_title = tk.Label(self.progress_frame, text="", bg=WIN_BG, fg=TEXT, font=self._font(12, bold=True))
        self.progress_title.pack(pady=(70, 8))
        self.progress_folder = tk.Label(self.progress_frame, text="", bg=WIN_BG, fg=TEXT_SUB, font=("Consolas", 10))
        self.progress_folder.pack()
        self.progress_bar = ttk.Progressbar(self.progress_frame, length=560, mode="determinate")
        self.progress_bar.pack(pady=16)
        self.progress_detail = tk.Label(self.progress_frame, text="", bg=WIN_BG, fg=TEXT, font=self._font(10))
        self.progress_detail.pack()
        self.progress_current = tk.Label(self.progress_frame, text="", bg=WIN_BG, fg=TEXT_SUB, font=self._font(9))
        self.progress_current.pack(pady=(6, 0))
        self.btn_cancel = self._button(self.progress_frame, "取消", self.cancel_scan)
        self.btn_cancel.pack(pady=20, ipady=2)

        # 结果列表
        self.result_frame = tk.Frame(center, bg=WIN_BG)
        self.result_frame.pack(fill="both", expand=True)
        cols = [key for key, *_ in RESULT_COLUMNS]
        self.result_tree = ttk.Treeview(self.result_frame, columns=cols, show="headings")
        for key, title, width, anchor in RESULT_COLUMNS:
            self.result_tree.heading(key, text=title)
            self.result_tree.column(key, width=width, anchor=anchor, stretch=(key == "name"))
        vsb = ttk.Scrollbar(self.result_frame, orient="vertical", command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=vsb.set)
        self.result_tree.pack(side="left", fill="both", expand=True, padx=(0, 0))
        vsb.pack(side="left", fill="y", padx=(0, 8))
        self.result_tree.tag_configure("error", foreground=ORANGE)
        self.result_tree.tag_configure("dim", foreground=TEXT_SUB)
        self.result_tree.bind("<<TreeviewSelect>>", self._on_result_select)
        self.result_tree.bind("<Double-1>", self._on_result_double_click)
        self.result_tree.bind("<Button-3>", self._show_result_menu)

    # ------------------------------------------------------------------ #
    # 右侧：详情面板
    # ------------------------------------------------------------------ #

    def _build_detail_panel(self) -> None:
        panel = tk.Frame(self.root, bg=PANEL_BG, width=360)
        panel.pack(side="right", fill="y")
        panel.pack_propagate(False)
        self.detail_panel = panel

        # ---- 固定在底部的操作区（不随内容滚动） ----
        actions_area = tk.Frame(panel, bg=PANEL_BG)
        actions_area.pack(side="bottom", fill="x", pady=(4, 10))
        tk.Frame(actions_area, bg=BORDER_SOFT, height=1).pack(fill="x", padx=18, pady=(0, 10))
        actions = tk.Frame(actions_area, bg=PANEL_BG)
        actions.pack(fill="x", padx=18)
        self.btn_open = self._button(actions, "打开图片", self.open_selected, primary=True)
        self.btn_open.pack(side="left", padx=(0, 4), ipady=2)
        self.btn_reveal = self._button(actions, "打开文件位置", self.reveal_selected)
        self.btn_reveal.pack(side="left", padx=(0, 4), ipady=2)
        self.btn_copy = self._button(actions, "复制路径", self.copy_selected_path)
        self.btn_copy.pack(side="left", ipady=2)
        actions2 = tk.Frame(actions_area, bg=PANEL_BG)
        actions2.pack(fill="x", padx=18, pady=(8, 0))
        self._button(actions2, "导出…", None, disabled=True).pack(side="left", padx=(0, 8), ipady=2)
        self._button(actions2, "导入到 Lightroom", None, disabled=True).pack(side="left", ipady=2)
        tk.Label(
            actions_area,
            text="导出只复制不移动；交给 Lightroom 前需确认（V5 接入）",
            bg=PANEL_BG,
            fg=TEXT_FAINT,
            font=self._font(8),
            wraplength=320,
            justify="left",
        ).pack(fill="x", padx=18, pady=(8, 0))

        # ---- 可滚动的内容区 ----
        canvas = tk.Canvas(panel, bg=PANEL_BG, highlightthickness=0, width=344)
        scrollbar = ttk.Scrollbar(panel, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        content = tk.Frame(canvas, bg=PANEL_BG)
        window = canvas.create_window((0, 0), window=content, anchor="nw", width=344)
        content.bind(
            "<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.detail_canvas = canvas
        self.detail_content = content

        self.preview_canvas = tk.Canvas(content, bg=CARD_BG, height=112, highlightthickness=0)
        self.preview_canvas.pack(fill="x", padx=14, pady=(14, 0))

        name_row = tk.Frame(content, bg=PANEL_BG)
        name_row.pack(fill="x", padx=18, pady=(10, 0))
        self.detail_name = tk.Label(name_row, text="未选择照片", bg=PANEL_BG, fg=TEXT, font=self._font(12, bold=True))
        self.detail_name.pack(side="left")
        self.badge_format = tk.Label(name_row, text="", bg=INPUT_BG, fg=ACCENT, font=self._font(8, bold=True))
        self.badge_format.pack(side="left", padx=(8, 4), ipadx=5, ipady=1)
        self.badge_status = tk.Label(name_row, text="", bg=INPUT_BG, fg=GREEN, font=self._font(8, bold=True))
        self.badge_status.pack(side="left", ipadx=5, ipady=1)

        self.detail_meta = tk.Label(content, text="", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(8))
        self.detail_meta.pack(fill="x", padx=18, pady=(4, 8))
        tk.Frame(content, bg=BORDER_SOFT, height=1).pack(fill="x", padx=18)

        self.detail_rows = tk.Frame(content, bg=PANEL_BG)
        self.detail_rows.pack(fill="x", padx=18, pady=(8, 0))

        # 我的标签（V3）：chips + 输入 + 自动补全
        tk.Label(content, text="我的标签", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(8, bold=True)).pack(
            anchor="w", padx=18, pady=(8, 2)
        )
        self.tag_chips_frame = tk.Frame(content, bg=PANEL_BG)
        self.tag_chips_frame.pack(fill="x", padx=18)
        tag_entry_row = tk.Frame(content, bg=PANEL_BG)
        tag_entry_row.pack(fill="x", padx=18, pady=(4, 0))
        self._tag_entry_var = tk.StringVar()
        self.tag_entry = tk.Entry(
            tag_entry_row,
            textvariable=self._tag_entry_var,
            bg=INPUT_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=self._font(9),
        )
        self.tag_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.tag_entry.bind("<Return>", lambda _event: self._add_tag_from_entry())
        self.btn_add_tag = self._button(tag_entry_row, "添加", self._add_tag_from_entry)
        self.btn_add_tag.pack(side="left", padx=(6, 0), ipady=1)
        self._tag_entry_var.trace_add("write", lambda *_args: self._update_tag_suggestions())
        self.tag_suggestions = tk.Listbox(
            content,
            height=3,
            bg=INPUT_BG,
            fg=TEXT,
            selectbackground=ACCENT_DIM,
            selectforeground=TEXT,
            relief="flat",
            font=self._font(9),
            highlightthickness=0,
        )
        self.tag_suggestions.bind("<ButtonRelease-1>", self._on_tag_suggestion_clicked)

        # 质量分析（V2.5）：紧凑摘要 + 标记原因（放在规则之前，V2.5 的重点）
        tk.Label(content, text="质量分析", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(8, bold=True)).pack(
            anchor="w", padx=18, pady=(8, 2)
        )
        self.quality_frame = tk.Frame(content, bg=PANEL_BG)
        self.quality_frame.pack(fill="x", padx=18)

        # 规则命中（V2）：自动标签与原因
        tk.Label(content, text="规则命中", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(8, bold=True)).pack(
            anchor="w", padx=18, pady=(10, 2)
        )
        self.rules_frame = tk.Frame(content, bg=PANEL_BG)
        self.rules_frame.pack(fill="x", padx=18, pady=(0, 14))

        self.set_detail(None)

    def _draw_preview(self, info: PhotoInfo | None) -> None:
        canvas = self.preview_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 100)
        height = int(canvas["height"])
        top, bottom = PREVIEW_COLORS.get(info.category if info else None, ((40, 41, 48), (64, 66, 76)))
        for y in range(height):
            t = y / max(height - 1, 1)
            color = "#%02x%02x%02x" % tuple(round(a * (1 - t) + b * t) for a, b in zip(top, bottom))
            canvas.create_line(0, y, width, y, fill=color)
        label = info.category.upper() if info else "—"
        canvas.create_text(
            width / 2, height / 2, text=label, fill="#c9cbd6", font=("Consolas", 18, "bold")
        )

    def _build_status_bar(self) -> None:
        bar = tk.Frame(self.root, bg=PANEL_BG, height=40)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status_left = tk.Label(bar, text="", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9))
        self.status_left.pack(side="left", padx=16)
        self.status_right = tk.Label(
            bar, text=f"数据库 {self.db_path}", bg=PANEL_BG, fg=TEXT_FAINT, font=("Consolas", 8)
        )
        self.status_right.pack(side="right", padx=16)

