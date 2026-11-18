"""PhotoClassifier 主窗口的控件布局。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from src.quality import FLAG_IDS, FLAG_LABELS
from src.rules import RULE_REGISTRY
from src.scene_ai import ROLE_NAMES, SUBJECTS, Role
from src.ui_gallery import PhotoGallery
from src.ui_theme import (
    ACCENT,
    ACCENT_DIM,
    ACCENT_SOFT,
    BORDER_SOFT,
    CARD_BG,
    GREEN,
    INPUT_BG,
    ORANGE,
    PANEL_BG,
    RESULT_COLUMNS,
    TEXT,
    TEXT_FAINT,
    TEXT_SUB,
    WIN_BG,
)


class PanelsMixin:
    """只负责构建控件；状态与业务动作仍在主窗口里。"""

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self.root, bg=PANEL_BG, height=52)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        tk.Label(bar, text="照片库", bg=PANEL_BG, fg=TEXT, font=self._font(11, bold=True)).pack(
            side="left", padx=16,
        )
        self.library_menu_button, self.library_menu = self._menu_button(bar, "更多 ▾")
        self.library_menu.add_command(label="扫描范围…", command=self.choose_scan_folders)
        self.library_menu.add_command(label="全量重扫…", command=lambda: self.start_scan(full=True))
        self.library_menu.add_command(label="质量分析…", command=self.start_analysis)
        self.library_menu.add_separator()
        self.library_menu.add_command(label="从库中移除当前文件夹…", command=self.remove_selected_folder)
        self.library_menu_button.pack(side="right", padx=(0, 14))
        self.btn_scan = self._button(bar, "添加文件夹", self.add_folder_dialog, primary=True)
        self.btn_scan.pack(side="right", padx=(8, 6))
        self.scan_scope_label = tk.Label(bar, bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9))
        self.scan_scope_label.pack(side="right")
        self.btn_detail = self._button(bar, "照片详情", self.toggle_detail_panel)
        self.btn_detail.pack(side="left", padx=(0, 6))

    def _menu_button(self, parent, text):
        button = tk.Menubutton(
            parent, text=text, font=self._font(10), bg=INPUT_BG, fg=TEXT,
            activebackground=ACCENT_DIM, activeforeground=TEXT, disabledforeground=TEXT_FAINT,
            relief="flat", bd=0, padx=10, pady=5, takefocus=True,
        )
        menu = tk.Menu(button, tearoff=0, bg=CARD_BG, fg=TEXT, activebackground=ACCENT_DIM)
        button.configure(menu=menu)
        return button, menu

    def _scrolling_panel(self, parent):
        canvas = tk.Canvas(parent, bg=PANEL_BG, highlightthickness=0, yscrollincrement=16)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        content = tk.Frame(canvas, bg=PANEL_BG)
        window = canvas.create_window((0, 0), window=content, anchor="nw")

        def update_scrollregion(_event=None):
            box = canvas.bbox(window)
            canvas.configure(scrollregion=box)
            if box[3] > canvas.winfo_height():
                scrollbar.pack(side="right", fill="y", before=canvas)
            else:
                scrollbar.pack_forget()

        def resize(event):
            canvas.itemconfigure(window, width=event.width)
            update_scrollregion()

        content.bind("<Configure>", update_scrollregion)
        canvas.bind("<Configure>", resize)
        return canvas, content

    # ------------------------------------------------------------------ #
    # 左侧：文件夹库与可展开的筛选
    # ------------------------------------------------------------------ #

    def _build_left_panel(self) -> None:
        outer = tk.Frame(self.root, bg=PANEL_BG, width=224)
        outer.pack(side="left", fill="y")
        outer.pack_propagate(False)

        self.left_canvas, panel = self._scrolling_panel(outer)

        library_header = tk.Frame(panel, bg=PANEL_BG)
        library_header.pack(fill="x", padx=12, pady=(12, 8))
        tk.Label(library_header, text="文件夹", bg=PANEL_BG, fg=TEXT, font=self._font(11, bold=True)).pack(
            side="left",
        )
        self.btn_add = self._button(library_header, "+ 添加", self.add_folder_dialog)
        self.btn_add.pack(side="right")
        self.btn_library_all = self._button(panel, "全部照片", lambda: self.browse_folder(None))
        self.btn_library_all.pack(fill="x", padx=12, pady=(0, 4))
        library_rows = tk.Frame(panel, bg=PANEL_BG)
        library_rows.pack(fill="x", padx=12)
        self.library_tree = ttk.Treeview(library_rows, show="tree", selectmode="browse", height=4)
        self.library_scroll = ttk.Scrollbar(library_rows, orient="vertical", command=self.library_tree.yview)
        self.library_tree.configure(yscrollcommand=self.library_scroll.set)
        self.library_tree.pack(side="left", fill="both", expand=True)
        self.library_tree.column("#0", width=190, minwidth=120)
        self.library_tree.bind("<<TreeviewSelect>>", self._on_library_select)
        self.library_tree.bind("<MouseWheel>", self._on_sidebar_wheel)
        self._build_filters(panel)

    def _build_filters(self, parent) -> None:
        self.filter_panel = panel = tk.Frame(parent, bg=PANEL_BG)
        tk.Label(panel, text="筛选", bg=PANEL_BG, fg=TEXT, font=self._font(11, bold=True)).pack(
            anchor="w", padx=16, pady=(12, 6)
        )
        self.raw_filter_check = tk.Checkbutton(
            panel, text="只看 RAW", variable=self.raw_only, command=self._on_filter_changed,
            bg=ACCENT_SOFT, fg=TEXT, activebackground=ACCENT_DIM, activeforeground=TEXT,
            selectcolor=INPUT_BG, highlightbackground=ACCENT_DIM, highlightcolor=ACCENT,
            highlightthickness=1, relief="flat", bd=0, anchor="w", padx=8, pady=5,
            font=self._font(10, bold=True),
        )
        self.raw_filter_check.pack(fill="x", padx=12, pady=(0, 10))
        tk.Label(
            panel, text="自动标签（EXIF 规则）", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9, bold=True)
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
            panel, text="质量分析", bg=PANEL_BG, fg=TEXT, font=self._font(10, bold=True)
        ).pack(anchor="w", padx=16)
        tk.Label(
            panel,
            text="勾选问题以查看坏片，或直接看“质量正常”",
            bg=PANEL_BG,
            fg=TEXT_FAINT,
            font=self._font(8),
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
                command=self._on_quality_flag_changed,
                bg=PANEL_BG,
                fg=TEXT,
                activebackground=PANEL_BG,
                activeforeground=TEXT,
                selectcolor=INPUT_BG,
                font=self._font(9),
            ).grid(row=i // 2, column=i % 2, sticky="w", pady=1, padx=(0, 6))

        tk.Checkbutton(
            panel,
            text="只看质量正常",
            variable=self.quality_ok_var,
            command=self._on_quality_ok_changed,
            bg=PANEL_BG,
            fg=GREEN,
            activebackground=PANEL_BG,
            activeforeground=GREEN,
            selectcolor=INPUT_BG,
            font=self._font(9, bold=True),
        ).pack(anchor="w", padx=16, pady=(4, 0))

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
        self.tag_tree.bind("<MouseWheel>", self._on_sidebar_wheel)

        self.btn_manage_tags = self._button(panel, "管理标签…", self.open_tag_manager)
        self.btn_manage_tags.pack(fill="x", padx=16, pady=(6, 2), ipady=1)

        for key, variable in self.ai_filters.items():
            tk.Checkbutton(
                panel, text=f"AI 建议 · {SUBJECTS[key]}", variable=variable, command=self.refresh_results,
                bg=PANEL_BG, fg=TEXT, activebackground=PANEL_BG, activeforeground=TEXT,
                selectcolor=INPUT_BG, font=self._font(9),
            ).pack(anchor="w", padx=16, pady=(6, 0))

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

    # ------------------------------------------------------------------ #
    # 中部：结果列表 + 扫描进度
    # ------------------------------------------------------------------ #

    def _build_center(self) -> None:
        center = tk.Frame(self.root, bg=WIN_BG)
        center.pack(side="left", fill="both", expand=True)
        self.center = center

        header = tk.Frame(center, bg=WIN_BG)
        header.pack(fill="x", padx=14, pady=(12, 8))
        self.btn_leave_timelapse = self._button(header, "返回浏览", self.leave_timelapse)
        self.gallery_title = tk.Label(header, bg=WIN_BG, fg=TEXT, font=self._font(16, bold=True))
        self.gallery_title.pack(side="left")
        self.count_label = tk.Label(header, text="", bg=WIN_BG, fg=TEXT_SUB, font=self._font(9))
        self.count_label.pack(side="left", padx=(12, 0))
        self.btn_export_results = self._button(header, "导出…", self.open_export_dialog)
        self.btn_export_results.pack(side="right")
        self.btn_sort_quality = self._button(header, "清晰度优先", self.toggle_quality_sort)
        self.btn_sort_quality.pack(side="right", padx=(0, 4))

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

        self.result_frame = tk.Frame(center, bg=WIN_BG)
        self.result_frame.pack(fill="both", expand=True)
        self._build_gallery_controls()
        pages = tk.Frame(self.result_frame, bg=WIN_BG)
        self.page_controls = pages
        pages.pack(side="bottom", fill="x", padx=12, pady=4)
        self.btn_result_prev = self._button(pages, "上一页", lambda: self.change_result_page(-1))
        self.btn_result_prev.pack(side="left")
        self.btn_result_next = self._button(pages, "下一页", lambda: self.change_result_page(1))
        self.btn_result_next.pack(side="left", padx=(5, 10))
        self.page_status = tk.Label(pages, bg=WIN_BG, fg=TEXT_SUB, font=self._font(9))
        self.page_status.pack(side="left")
        self._build_selection_bar()

        viewport = tk.Frame(self.result_frame, bg=WIN_BG)
        viewport.pack(fill="both", expand=True)
        self.grid_frame = tk.Frame(viewport, bg=WIN_BG)
        self.list_frame = tk.Frame(viewport, bg=WIN_BG)
        self.grid_frame.pack(fill="both", expand=True)
        self.gallery = PhotoGallery(self.grid_frame, self._font(9))
        grid_scroll = ttk.Scrollbar(self.grid_frame, orient="vertical", command=self.gallery.scroll)
        self.gallery.configure(yscrollcommand=grid_scroll.set)
        grid_scroll.pack(side="right", fill="y")
        self.gallery.pack(side="left", fill="both", expand=True)
        self.gallery.bind("<Button-1>", self._on_gallery_click)
        self.gallery.bind("<Control-Button-1>", lambda event: self._on_gallery_click(event, toggle=True))
        self.gallery.bind("<Shift-Button-1>", lambda event: self._on_gallery_click(event, extend=True))
        self.gallery.bind("<Double-Button-1>", self._on_gallery_double_click)
        self.gallery.bind("<Button-3>", self._on_gallery_menu)
        for key, row, column in (("Left", 0, -1), ("Right", 0, 1), ("Up", -1, 0), ("Down", 1, 0)):
            self.gallery.bind(
                f"<{key}>", lambda _event, row=row, column=column: self._navigate_gallery(row, column),
            )
        self.gallery.bind("<space>", lambda _event: self.open_gallery_preview())
        self.gallery.bind("<Return>", lambda _event: self.open_gallery_preview())
        self.gallery.bind("<Delete>", self._on_delete_key)
        self.gallery.bind("<p>", self._on_pick_key)
        self.gallery.bind("<P>", self._on_pick_key)

        cols = [key for key, *_ in RESULT_COLUMNS]
        self.result_tree = ttk.Treeview(self.list_frame, columns=cols, show="headings")
        self.result_tree.heading("#0", text="选择")
        self.result_tree.column("#0", width=54, minwidth=54, stretch=False)
        for key, title, width, anchor in RESULT_COLUMNS:
            self.result_tree.heading(key, text=title, anchor=anchor)
            self.result_tree.column(key, width=width, anchor=anchor, stretch=False)
        vsb = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.result_tree.yview)
        hsb = ttk.Scrollbar(self.list_frame, orient="horizontal", command=self.result_tree.xview)
        self.result_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.list_frame.columnconfigure(0, weight=1)
        self.list_frame.rowconfigure(0, weight=1)
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns", padx=(0, 8))
        hsb.grid(row=1, column=0, sticky="ew")
        self.result_tree.tag_configure("error", foreground=ORANGE)
        self.result_tree.tag_configure("dim", foreground=TEXT_SUB)
        self.result_tree.bind("<<TreeviewSelect>>", self._on_result_select)
        self.result_tree.bind("<Button-1>", self._on_result_click)
        for click in ("Button-1", "Double-Button-1"):
            self.result_tree.bind(f"<Control-{click}>", lambda event: self._on_result_click(event, toggle=True))
            self.result_tree.bind(f"<Shift-{click}>", lambda event: self._on_result_click(event, extend=True))
        self.result_tree.bind("<space>", self._on_result_space)
        self.result_tree.bind("<Double-1>", self._on_result_double_click)
        self.result_tree.bind("<Button-3>", self._show_result_menu)
        self.result_tree.bind("<Delete>", self._on_delete_key)
        self.result_tree.bind("<p>", self._on_pick_key)
        self.result_tree.bind("<P>", self._on_pick_key)

    def _build_gallery_controls(self) -> None:
        controls = tk.Frame(self.result_frame, bg=WIN_BG)
        controls.pack(fill="x", padx=14, pady=(0, 8))
        self.quick_filter_buttons = {}
        for key, label in (
            ("all", "全部"), ("picked", "精选"), ("issues", "质量问题"), ("duplicates", "重复组"),
            ("timelapse", "延时叠"), ("cull", "筛选组"),
        ):
            button = self._button(controls, label, lambda key=key: self.set_quick_filter(key))
            button.configure(padx=8)
            button.pack(side="left", padx=(0, 4))
            self.quick_filter_buttons[key] = button
        self.view_buttons = {}
        for key, label in (("list", "列表"), ("grid", "网格")):
            button = self._button(controls, label, lambda key=key: self.set_result_view(key))
            button.configure(padx=8)
            button.pack(side="right", padx=(3, 0))
            self.view_buttons[key] = button
        self.btn_filters = self._button(controls, "筛选", self.toggle_filters)
        self.btn_filters.configure(padx=8)
        self.btn_filters.pack(side="right", padx=(4, 5))
        self.btn_fold_timelapse = self._button(controls, "折叠延时", self.toggle_timelapse_folding)
        self.btn_fold_timelapse.configure(padx=8)
        self.btn_fold_timelapse.pack(side="right", padx=(0, 4))
        self.active_filter_row = tk.Frame(self.result_frame, bg=WIN_BG)
        self.filter_summary = tk.Label(self.active_filter_row, bg=WIN_BG, fg=TEXT_SUB, font=self._font(9))
        self.filter_summary.pack(side="left", padx=(0, 4))
        self.filter_chips = tk.Frame(self.active_filter_row, bg=WIN_BG)
        self.filter_chips.pack(side="left")
        self.btn_clear_active = self._button(self.active_filter_row, "清除", self.clear_filters)
        self.btn_clear_active.pack(side="right")
        self.gallery_controls = controls

    def _build_selection_bar(self) -> None:
        self.btn_select_results = self._button(self.page_controls, "多选", self.start_result_selection)
        self.btn_select_results.pack(side="right")
        self.btn_select_page = self._button(
            self.page_controls, "全选本页", self.toggle_result_page_selection,
        )
        self.btn_leave_selection = self._button(self.page_controls, "退出多选", self.cancel_result_selection)
        self.selection_actions = tk.Frame(self.result_frame, bg=PANEL_BG)
        self.selection_count = tk.Label(self.selection_actions, bg=PANEL_BG, fg=TEXT, font=self._font(10, bold=True))
        self.selection_count.pack(side="left", padx=(12, 10))
        self.btn_batch_tag = self._button(self.selection_actions, "添加标签", self.batch_add_tags, disabled=True)
        self.btn_batch_tag.pack(side="left", padx=(0, 4), pady=5)
        self.btn_ai, self.ai_menu = self._menu_button(self.selection_actions, "AI 分析 ▾")
        self.btn_ai.pack(side="left", padx=(0, 4), pady=5)
        self.btn_pick = self._button(self.selection_actions, "标记精选", self.toggle_picked, disabled=True)
        self.btn_pick.pack(side="left", padx=(0, 4), pady=5)
        self.btn_export = self._button(
            self.selection_actions, "导出所选照片…", lambda: self.open_export_dialog(selected_first=True),
        )
        self.btn_export.pack(side="left", pady=5)
        self.btn_cancel_selection = self._button(self.selection_actions, "清除选择", self.cancel_result_selection)
        self.btn_cancel_selection.pack(side="right", padx=(0, 8), pady=5)
        self.ai_menu.add_command(label="人物", command=self.start_person_analysis)
        self.ai_menu.add_command(label="天空", command=self.start_sky_analysis)
        self.ai_menu.add_command(label="人物和天空", command=self.start_people_and_sky_analysis)
        self.ai_menu.add_command(label="六类题材主次", command=self.start_scene_analysis)

    # ------------------------------------------------------------------ #
    # 右侧：详情面板
    # ------------------------------------------------------------------ #

    def _build_detail_panel(self) -> None:
        panel = tk.Frame(self.root, bg=PANEL_BG, width=326)
        panel.pack(side="right", fill="y")
        panel.pack_propagate(False)
        self.detail_panel = panel
        header = tk.Frame(panel, bg=PANEL_BG)
        header.pack(fill="x", padx=14, pady=(10, 7))
        tk.Label(header, text="照片详情", bg=PANEL_BG, fg=TEXT, font=self._font(11, bold=True)).pack(side="left")
        self._button(header, "收起", self.toggle_detail_panel).pack(side="right")

        # ---- 固定在底部的操作区（不随内容滚动） ----
        actions_area = tk.Frame(panel, bg=PANEL_BG)
        actions_area.pack(side="bottom", fill="x", pady=(4, 10))
        tk.Frame(actions_area, bg=BORDER_SOFT, height=1).pack(fill="x", padx=18, pady=(0, 10))
        self.timelapse_actions = tk.Frame(actions_area, bg=PANEL_BG)
        for column, (label, command) in enumerate((
            ("展开", self.toggle_focused_timelapse),
            ("选中整叠", self.select_focused_timelapse),
            ("打开原文件夹", self.open_focused_timelapse_folder),
        )):
            self.timelapse_actions.columnconfigure(column, weight=1)
            button = self._button(self.timelapse_actions, label, command)
            button.configure(padx=4)
            button.grid(row=0, column=column, sticky="ew", padx=1, ipady=2)
        actions = tk.Frame(actions_area, bg=PANEL_BG)
        actions.pack(fill="x", padx=8)
        self.btn_open = self._button(actions, "打开图片", self.open_selected)
        self.btn_reveal = self._button(actions, "文件位置", self.reveal_selected)
        self.btn_copy = self._button(actions, "复制路径", self.copy_selected_path)
        for column, button in enumerate((self.btn_open, self.btn_reveal, self.btn_copy)):
            actions.columnconfigure(column, weight=1)
            button.configure(padx=4)
            button.grid(row=0, column=column, sticky="ew", padx=1, ipady=2)

        # ---- 可滚动的内容区 ----
        self.detail_canvas, content = self._scrolling_panel(panel)
        self.detail_content = content

        self.preview_canvas = tk.Canvas(content, bg=CARD_BG, height=202, highlightthickness=0)
        self.preview_canvas.pack(fill="x", padx=14)
        self._init_thumbnail()
        self.preview_canvas.bind("<Double-1>", lambda _event: self.open_gallery_preview())

        name_row = tk.Frame(content, bg=PANEL_BG)
        name_row.pack(fill="x", padx=18, pady=(10, 0))
        self.detail_name = tk.Label(
            name_row, text="未选择照片", bg=PANEL_BG, fg=TEXT,
            font=self._font(11, bold=True), wraplength=170, justify="left",
        )
        self.detail_name.pack(side="left")
        self.badge_format = tk.Label(name_row, text="", bg=INPUT_BG, fg=ACCENT, font=self._font(8, bold=True))
        self.badge_format.pack(side="left", padx=(8, 4), ipadx=5, ipady=1)
        self.badge_status = tk.Label(name_row, text="", bg=INPUT_BG, fg=GREEN, font=self._font(8, bold=True))
        self.badge_status.pack(side="left", ipadx=5, ipady=1)

        self.detail_meta = tk.Label(
            content, text="", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9), anchor="w", justify="left",
        )
        self.detail_meta.pack(fill="x", padx=18, pady=(4, 8))
        self.ai_details = {
            label: tk.Label(content, text="", bg=PANEL_BG, anchor="w", font=self._font(9, bold=True))
            for label in ("人物", "天空")
        }
        self.scene_details = {
            role: tk.Label(content, bg=PANEL_BG, anchor="w", fg=ACCENT if role == Role.PRIMARY else TEXT_SUB,
                           font=self._font(9, bold=role == Role.PRIMARY))
            for role in ROLE_NAMES
        }
        tk.Frame(content, bg=BORDER_SOFT, height=1).pack(fill="x", padx=18)

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

        self.btn_current_pick = self._button(
            content, "标记精选", lambda: self.toggle_picked([self.current] if self.current else []),
            disabled=True,
        )
        self.btn_current_pick.pack(anchor="w", padx=18, pady=(10, 6))
        self.btn_parameters = self._button(content, "拍摄参数与分析 ▸", self.toggle_detail_parameters)
        self.btn_parameters.pack(fill="x", padx=18, pady=(2, 8))
        self.detail_parameters = tk.Frame(content, bg=PANEL_BG)
        self.detail_rows = tk.Frame(self.detail_parameters, bg=PANEL_BG)
        self.detail_rows.pack(fill="x", padx=18, pady=(0, 8))

        # 质量分析（V2.5）：紧凑摘要 + 标记原因（放在规则之前，V2.5 的重点）
        tk.Label(self.detail_parameters, text="质量分析", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(8, bold=True)).pack(
            anchor="w", padx=18, pady=(8, 2)
        )
        self.quality_frame = tk.Frame(self.detail_parameters, bg=PANEL_BG)
        self.quality_frame.pack(fill="x", padx=18)

        # 规则命中（V2）：自动标签与原因
        tk.Label(self.detail_parameters, text="规则命中", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(8, bold=True)).pack(
            anchor="w", padx=18, pady=(10, 2)
        )
        self.rules_frame = tk.Frame(self.detail_parameters, bg=PANEL_BG)
        self.rules_frame.pack(fill="x", padx=18, pady=(0, 14))

        self.set_detail(None)

    def _build_status_bar(self) -> None:
        bar = tk.Frame(self.root, bg=PANEL_BG, height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status_left = tk.Label(bar, text="", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9))
        self.status_left.pack(side="left", padx=16)
        self.status_shortcuts = tk.Label(bar, bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9))
        self.status_shortcuts.pack(side="right", padx=16)

