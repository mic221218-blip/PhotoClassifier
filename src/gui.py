"""PhotoClassifier GUI 外壳（GUI V1.5，Tkinter 实现）。

按已确认的 UI 设计稿（docs/ui-design.md）实现第一版界面：

- 顶部工具栏：文件夹路径 + 浏览… + 开始扫描（V1.5 能力）；
- 左侧：文件夹库（多文件夹、勾选参与扫描，V1.5 能力）
  + 筛选面板（V3/V4 占位，禁用）；
- 中部：扫描结果列表（文件信息，V1.5 能力）+ 扫描进度视图（可取消）；
- 右侧：详情面板（完整照片信息；打开文件位置 / 复制路径可用，
  导出与导入到 Lightroom 为 V5 占位）；
- 底部状态栏：扫描报告摘要 + 数据库位置。

技术说明：设计文档原定 PySide6，但当前环境无法访问 PyPI，
故第一版用标准库 Tkinter 实现同样的布局与交互；模块按"界面构建 + 数据刷新"
组织，后续如迁移 PySide6 可以复用数据库层与扫描流程。

安全原则：GUI 全程只读原始照片，绝不删除 / 移动 / 修改任何文件。
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from src.database import PhotoDatabase
from src.duplicates import DuplicateResult
from src.exif_reader import read_photo_info
from src.formatting import format_shutter, format_size
from src.photo_info import EXIF_ERROR, EXIF_NO_EXIF, EXIF_OK, PhotoInfo
from src.preview import PILLOW_AVAILABLE, is_supported
from src.quality import (
    FLAG_IDS,
    FLAG_LABELS,
    QualityEngine,
    QualityFlag,
    QualityMeasurement,
)
from src.quality_scan import AnalyzeOptions, analyze_photos, rebuild_duplicates
from src.quality_store import QualityStore
from src.rules import RULE_REGISTRY, RuleEngine, RuleMatch
from src.scanner import scan_folder
from src.shell import open_in_explorer

#: 项目根目录（src/ 的上一级）。数据库默认位置与其解耦于当前工作目录。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "photos.db"
ICON_PATH = PROJECT_ROOT / "assets" / "photoclassifier.ico"

# --------------------------------------------------------------------------- #
# 深色主题调色板（与设计稿一致）
# --------------------------------------------------------------------------- #

WIN_BG = "#1b1c21"
PANEL_BG = "#202127"
CARD_BG = "#23242b"
INPUT_BG = "#26272e"
BORDER = "#2f313b"
BORDER_SOFT = "#2a2b33"
ACCENT = "#4c8dff"
ACCENT_DIM = "#31415e"
GREEN = "#3fb27f"
ORANGE = "#e8a33d"
TEXT = "#e9eaf0"
TEXT_SUB = "#9a9dab"
TEXT_FAINT = "#6e717e"

#: 结果列表的列定义：(键, 标题, 宽度, 锚)
_RESULT_COLUMNS = [
    ("name", "文件名", 190, "w"),
    ("category", "类型", 55, "w"),
    ("taken_at", "拍摄时间", 150, "w"),
    ("camera", "相机", 120, "w"),
    ("iso", "ISO", 50, "e"),
    ("shutter", "快门", 80, "e"),
    ("rules", "自动标签", 150, "w"),
    ("quality", "质量", 95, "w"),
    ("size", "大小", 85, "e"),
]

#: 质量列的紧凑写法（空间有限，详情面板里显示完整原因）
_QUALITY_SHORT = {
    "blurry": "模糊",
    "motion_blur": "运动",
    "overexposed": "过曝",
    "underexposed": "欠曝",
    "black_frame": "黑屏",
    "duplicate": "重复",
}

#: 详情面板展示的字段（与 V1.5 数据库字段一一对应）
_DETAIL_FIELDS = [
    ("拍摄时间", lambda i: i.taken_at),
    ("相机", lambda i: i.camera),
    ("镜头", lambda i: i.lens_model),
    ("焦距", lambda i: _mm(i.focal_length_mm)),
    ("光圈", lambda i: _f_number(i.f_number)),
    ("快门", lambda i: format_shutter(i.exposure_time_s) if i.exposure_time_s else None),
    ("ISO", lambda i: str(i.iso) if i.iso is not None else None),
    ("曝光补偿", lambda i: _ev(i.exposure_bias_ev)),
    ("GPS", lambda i: f"{i.gps_latitude:.6f}, {i.gps_longitude:.6f}" if i.has_gps else None),
    ("尺寸", lambda i: f"{i.width_px} x {i.height_px}" if i.width_px else None),
    ("修改时间", lambda i: i.modified_at),
]

#: 分类对应的预览渐变色（顶部色, 底部色）
_PREVIEW_COLORS = {
    "nef": ((58, 48, 92), (206, 120, 74)),
    "jpeg": ((30, 52, 84), (120, 160, 150)),
    "png": ((28, 44, 60), (104, 170, 130)),
    "video": ((40, 41, 48), (64, 66, 76)),
    "unsupported": ((40, 41, 48), (64, 66, 76)),
}


def _mm(value):
    return f"{value:g} mm" if value is not None else None


def _f_number(value):
    return f"f/{value:g}" if value is not None else None


def _ev(value):
    if value is None:
        return None
    return "0 EV" if value == 0 else f"{value:+.1f} EV"


class PhotoClassifierApp:
    """主窗口应用。``root`` 由调用方创建，方便测试与自检。"""

    def __init__(self, root: tk.Tk, db_path: str | Path = DEFAULT_DB):
        self.root = root
        self.db = PhotoDatabase.open(db_path)
        self.db_path = Path(db_path)
        self.quality_store = QualityStore.open(db_path)

        self.checked_folders: set[str] = set()
        self.infos_by_iid: dict[str, PhotoInfo] = {}
        self.matches_by_iid: dict[str, list[RuleMatch]] = {}
        self.quality_by_iid: dict[str, list[QualityFlag]] = {}
        self.current: PhotoInfo | None = None
        self.scan_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.msg_queue: queue.Queue = queue.Queue()

        self.analyze_thread: threading.Thread | None = None
        self.analyze_cancel = threading.Event()
        self.analyze_queue: queue.Queue = queue.Queue()
        self.analyze_report = None

        self.rule_engine = RuleEngine()
        self.quality_engine = QualityEngine()
        self.active_rules: set[str] = set()
        self.active_quality: set[str] = set()
        self.and_mode = tk.BooleanVar(value=True)
        self.gps_only = tk.BooleanVar(value=False)
        self.rule_vars: dict[str, tk.BooleanVar] = {}
        self.quality_vars: dict[str, tk.BooleanVar] = {}
        self.measurements_by_path: dict[str, QualityMeasurement] = {}
        self.duplicates = DuplicateResult()
        self._quality_revision = 0
        self._quality_cache_revision = -1

        self._setup_window()
        self._build_styles()
        self._build_toolbar()
        self._build_left_panel()
        # 先放右侧详情面板、再放中间（中间 expand 会吃掉剩余空间）
        self._build_detail_panel()
        self._build_center()
        self._build_status_bar()

        self.refresh_library()
        self.refresh_results()
        self.refresh_status_bar()
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_mousewheel(self, event) -> None:
        """鼠标在左侧筛选栏 / 右侧详情栏上时滚动对应面板。"""
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget is getattr(self, "left_canvas", None):
                self.left_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return
            if widget is getattr(self, "detail_canvas", None):
                self.detail_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return
            widget = widget.master

    # ------------------------------------------------------------------ #
    # 窗口与样式
    # ------------------------------------------------------------------ #

    def _setup_window(self) -> None:
        self.root.title("PhotoClassifier")
        self.root.geometry("1660x900")
        self.root.minsize(1200, 700)
        self.root.configure(bg=WIN_BG)
        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass

    def _font(self, size=10, bold=False):
        return tkfont.Font(family="Microsoft YaHei", size=size, weight="bold" if bold else "normal")

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Treeview",
            background=CARD_BG,
            fieldbackground=CARD_BG,
            foreground=TEXT,
            rowheight=28,
            borderwidth=0,
            font=self._font(10),
        )
        style.map("Treeview", background=[("selected", ACCENT_DIM)], foreground=[("selected", TEXT)])
        style.configure(
            "Treeview.Heading",
            background=PANEL_BG,
            foreground=TEXT_SUB,
            relief="flat",
            font=self._font(9),
        )
        style.map("Treeview.Heading", background=[("active", PANEL_BG)])
        style.configure(
            "Horizontal.TProgressbar",
            background=ACCENT,
            troughcolor=INPUT_BG,
            borderwidth=0,
            thickness=10,
        )
        style.configure("Vertical.TScrollbar", background=INPUT_BG, troughcolor=WIN_BG, borderwidth=0)
        style.configure("Horizontal.TScrollbar", background=INPUT_BG, troughcolor=WIN_BG, borderwidth=0)

    def _button(self, parent, text, command=None, *, primary=False, disabled=False, width=None):
        btn = tk.Button(
            parent,
            text=text,
            command=command if not disabled else None,
            font=self._font(10, bold=primary),
            bg=ACCENT if primary and not disabled else INPUT_BG,
            fg="#ffffff" if primary and not disabled else (TEXT_FAINT if disabled else TEXT),
            disabledforeground=TEXT_FAINT,
            activebackground=ACCENT_DIM if not primary else ACCENT,
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=14,
            pady=5,
            cursor="arrow" if disabled else "hand2",
            state="disabled" if disabled else "normal",
            width=width,
        )
        return btn

    # ------------------------------------------------------------------ #
    # 顶部工具栏
    # ------------------------------------------------------------------ #

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
        cols = [key for key, *_ in _RESULT_COLUMNS]
        self.result_tree = ttk.Treeview(self.result_frame, columns=cols, show="headings")
        for key, title, width, anchor in _RESULT_COLUMNS:
            self.result_tree.heading(key, text=title)
            self.result_tree.column(key, width=width, anchor=anchor, stretch=(key == "name"))
        vsb = ttk.Scrollbar(self.result_frame, orient="vertical", command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=vsb.set)
        self.result_tree.pack(side="left", fill="both", expand=True, padx=(0, 0))
        vsb.pack(side="left", fill="y", padx=(0, 8))
        self.result_tree.tag_configure("error", foreground=ORANGE)
        self.result_tree.tag_configure("dim", foreground=TEXT_SUB)
        self.result_tree.bind("<<TreeviewSelect>>", self._on_result_select)
        self.result_tree.bind("<Double-1>", lambda _e: self.reveal_selected())

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
        self.btn_reveal = self._button(actions, "打开文件位置", self.reveal_selected)
        self.btn_reveal.pack(side="left", padx=(0, 8), ipady=2)
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
        top, bottom = _PREVIEW_COLORS.get(info.category if info else None, ((40, 41, 48), (64, 66, 76)))
        for y in range(height):
            t = y / max(height - 1, 1)
            color = "#%02x%02x%02x" % tuple(round(a * (1 - t) + b * t) for a, b in zip(top, bottom))
            canvas.create_line(0, y, width, y, fill=color)
        label = info.category.upper() if info else "—"
        canvas.create_text(
            width / 2, height / 2, text=label, fill="#c9cbd6", font=("Consolas", 18, "bold")
        )

    def set_detail(self, info: PhotoInfo | None) -> None:
        """刷新右侧详情面板。"""
        self.current = info
        self._draw_preview(info)
        for child in self.rules_frame.winfo_children():
            child.destroy()
        for child in self.quality_frame.winfo_children():
            child.destroy()
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
        for label, getter in _DETAIL_FIELDS:
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
        self.btn_reveal.config(
            state="normal" if path.exists() else "disabled",
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

        self._fill_quality_detail(info)

    # ------------------------------------------------------------------ #
    # 质量分析：筛选、详情与分析流程（V2.5）
    # ------------------------------------------------------------------ #

    def _quality_flags_for(self, info: PhotoInfo) -> list[QualityFlag]:
        """一张照片当前的质量标记（含重复）。"""
        key = str(info.path.resolve())
        measurement = self.measurements_by_path.get(key)
        if measurement is None or not measurement.analyzed:
            return []
        flags = self.quality_engine.flags(measurement, info)
        reason = self.duplicates.flag_reason(info.path)
        if reason:
            flags.append(QualityFlag("duplicate", FLAG_LABELS["duplicate"], reason))
        return flags

    def _fill_quality_detail(self, info: PhotoInfo) -> None:
        key = str(info.path.resolve())
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
    # 底部状态栏
    # ------------------------------------------------------------------ #

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

    def refresh_status_bar(self) -> None:
        total = self.db.count_photos()
        err = self.db.count_by_exif_status().get(EXIF_ERROR, 0)
        folders = self.db.list_folders()
        last_scan = max((f.last_scanned_at for f in folders if f.last_scanned_at), default=None)
        parts = []
        if last_scan:
            parts.append(f"上次扫描 {last_scan}")
        parts.append(f"库中总文件 {total:,}")
        parts.append(f"成功 {total - err:,}")
        parts.append(f"异常 {err:,}")
        try:
            analyzable = sum(
                count
                for category, count in self.db.count_by("category").items()
                if is_supported(category)
            )
            stats = self.quality_store.stats()
            quality_note = f"质量已分析 {stats.analyzed:,}/{analyzable:,}"
            parts.append(quality_note)
            if self.duplicates.groups:
                parts.append(f"重复 {len(self.duplicates.groups):,} 组")
        except Exception:
            pass
        self.status_left.config(text=" · ".join(parts))

    # ------------------------------------------------------------------ #
    # 文件夹库
    # ------------------------------------------------------------------ #

    def refresh_library(self) -> None:
        self.library_tree.delete(*self.library_tree.get_children())
        for folder in self.db.list_folders():
            key = str(folder.path)
            mark = "☑" if key in self.checked_folders else "☐"
            count = f"{folder.photo_count:,}"
            scan_note = f" · 扫描于 {folder.last_scanned_at}" if folder.last_scanned_at else ""
            self.library_tree.insert("", "end", iid=key, text=f"{mark}  {folder.path}", values=(f"{count} 张{scan_note}",))

    def _on_library_click(self, event) -> None:
        iid = self.library_tree.identify_row(event.y)
        if not iid:
            return
        if str(iid) in self.checked_folders:
            self.checked_folders.discard(str(iid))
        else:
            self.checked_folders.add(str(iid))
        self.refresh_library()
        # 点击同时选中该行：这样「移除」按钮知道要移除哪一个（之前只切换勾选状态）
        if iid in self.library_tree.get_children():
            self.library_tree.selection_set(iid)
            self.library_tree.focus(iid)
        self.refresh_results()

    def add_folder_dialog(self) -> None:
        chosen = filedialog.askdirectory(title="选择要加入文件夹库的文件夹", initialdir=self.folder_var.get() or str(PROJECT_ROOT))
        if not chosen:
            return
        self.db.add_folder(chosen)
        self.checked_folders.add(str(Path(chosen).resolve()))
        self.folder_var.set(str(Path(chosen).resolve()))
        self.refresh_library()
        self.refresh_results()
        self.refresh_status_bar()

    def remove_selected_folder(self) -> None:
        selection = self.library_tree.selection()
        if not selection:
            focused = self.library_tree.focus()
            selection = (focused,) if focused else ()
        if not selection:
            messagebox.showinfo("移除文件夹", "请先在文件夹库中点击选择一个文件夹。")
            return
        folder = selection[0]
        if not messagebox.askyesno(
            "移除文件夹",
            "只从文件夹库移除该条目，不删除照片记录，更不会删除原片。\n\n继续移除？\n" + folder,
        ):
            return
        self.db.remove_folder(folder)
        self.checked_folders.discard(folder)
        self.refresh_library()
        self.refresh_results()
        self.refresh_status_bar()

    # ------------------------------------------------------------------ #
    # 结果列表与详情联动
    # ------------------------------------------------------------------ #

    def refresh_results(self) -> None:
        folders = sorted(self.checked_folders) or [f.path for f in self.db.list_folders()]
        photos = self.db.find_photos_under(folders)
        self.infos_by_iid = {str(info.path): info for info in photos}
        self.matches_by_iid = {}
        self.quality_by_iid = {}
        self._ensure_quality_cache(photos)

        self.result_tree.delete(*self.result_tree.get_children())
        visible = 0
        for info in photos:
            matches = self.rule_engine.evaluate(info)
            quality_flags = self._quality_flags_for(info)
            if not self._passes_filter(info, matches, quality_flags):
                continue
            iid = str(info.path)
            self.matches_by_iid[iid] = matches
            self.quality_by_iid[iid] = quality_flags
            shutter = format_shutter(info.exposure_time_s) if info.exposure_time_s else ""
            labels = " · ".join(match.label for match in matches)
            quality_text = " · ".join(
                _QUALITY_SHORT.get(flag.flag_id, flag.label) for flag in quality_flags
            )
            if not quality_text:
                measurement = self.measurements_by_path.get(str(info.path.resolve()))
                if measurement is not None and measurement.error:
                    quality_text = "失败"
            values = (
                info.file_name,
                _category_label(info.category),
                info.taken_at or "",
                info.camera or "",
                str(info.iso) if info.iso is not None else "",
                shutter,
                labels,
                quality_text,
                format_size(info.size_bytes),
            )
            tags = []
            if info.exif_status == EXIF_ERROR:
                tags.append("error")
            elif info.category in ("video", "unsupported"):
                tags.append("dim")
            self.result_tree.insert("", "end", iid=iid, values=values, tags=tags)
            visible += 1

        total = self.db.count_photos()
        filter_note = ""
        if self.active_rules or self.active_quality or self.gps_only.get():
            filter_note = " · 已筛选"
        self.count_label.config(
            text=f"库中共 {total:,} 个文件 · 当前显示 {visible:,} 张{filter_note}"
        )
        if self.current is not None:
            keep = self.infos_by_iid.get(str(self.current.path))
            if keep is not None and str(keep.path) in self.matches_by_iid:
                self.set_detail(keep)

    def _ensure_quality_cache(self, photos: list[PhotoInfo]) -> None:
        """测量值与重复分组只在质量数据变化后重算，避免每次筛选都全量重跑。"""
        if self._quality_cache_revision == self._quality_revision:
            return
        measurements = list(self.quality_store.iter_measurements())
        self.measurements_by_path = {measurement.path: measurement for measurement in measurements}
        self.duplicates = rebuild_duplicates(self.quality_store, photos)
        self._quality_cache_revision = self._quality_revision

    def _passes_filter(
        self,
        info: PhotoInfo,
        matches: list[RuleMatch],
        quality_flags: list[QualityFlag],
    ) -> bool:
        """当前筛选条件是否接受这张照片（自动标签与质量标记共用 AND/OR）。"""
        if self.gps_only.get() and not info.has_gps:
            return False
        selected = self.active_rules | self.active_quality
        if selected:
            hit = {match.rule_id for match in matches}
            hit |= {flag.flag_id for flag in quality_flags}
            if self.and_mode.get():
                return selected.issubset(hit)
            return bool(selected & hit)
        return True

    def _on_filter_changed(self) -> None:
        self.active_rules = {
            rule_id
            for rule_id, var in self.rule_vars.items()
            if var.get()
        }
        self.active_quality = {
            flag_id
            for flag_id, var in self.quality_vars.items()
            if var.get()
        }
        self.refresh_results()

    def clear_filters(self) -> None:
        for var in self.rule_vars.values():
            var.set(False)
        for var in self.quality_vars.values():
            var.set(False)
        self.gps_only.set(False)
        self.active_rules = set()
        self.active_quality = set()
        self.refresh_results()

    @staticmethod
    def _select_target() -> PhotoInfo | None:
        return None

    def _on_result_select(self, _event=None) -> None:
        selection = self.result_tree.selection()
        if not selection:
            return
        info = self.infos_by_iid.get(selection[0])
        if info is not None:
            self.set_detail(info)

    def reveal_selected(self) -> None:
        if self.current is None:
            return
        target = self.current.path
        ok, error = open_in_explorer(target)
        if ok:
            self.status_left.config(text=f"已在资源管理器中定位：{target.name}")
            return
        # 失败时不弹系统错误框：复制路径，让用户手动粘贴
        self.root.clipboard_clear()
        self.root.clipboard_append(str(target))
        messagebox.showwarning(
            "打开文件位置",
            f"{error}\n\n路径已复制到剪贴板，可以粘贴到资源管理器地址栏。",
        )

    def copy_selected_path(self) -> None:
        if self.current is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(str(self.current.path))
        self.status_left.config(text=f"已复制路径：{self.current.path}")

    # ------------------------------------------------------------------ #
    # 扫描流程（后台线程 + 队列轮询）
    # ------------------------------------------------------------------ #

    def start_scan(self) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            return
        folders = [f.path for f in self.db.list_folders() if str(f.path) in self.checked_folders]
        if not folders:
            messagebox.showinfo("开始扫描", "请先在左侧文件夹库勾选至少一个要扫描的文件夹。")
            return

        self.cancel_event.clear()
        self._set_scanning_ui(True)
        self.scan_thread = threading.Thread(
            target=self._scan_worker, args=(folders,), daemon=True
        )
        self.scan_thread.start()
        self.root.after(100, self._poll_scan_queue)

    def cancel_scan(self) -> None:
        self.cancel_event.set()
        self.progress_current.config(text="正在取消……（等待当前文件处理完成）")

    def _set_scanning_ui(self, scanning: bool) -> None:
        if scanning:
            self.result_frame.pack_forget()
            self.progress_frame.pack(fill="both", expand=True)
            self.progress_bar.config(value=0)
            self.progress_title.config(text="正在扫描")
            self.progress_folder.config(text="")
            self.progress_detail.config(text="")
            self.progress_current.config(text="")
            self.btn_scan.config(text="扫描中…", state="disabled", bg=INPUT_BG, fg=TEXT_FAINT)
            self.btn_analyze.config(state="disabled", fg=TEXT_FAINT)
            for btn in (self.btn_browse, self.btn_add, self.btn_remove):
                btn.config(state="disabled")
        else:
            self.progress_frame.pack_forget()
            self.result_frame.pack(fill="both", expand=True)
            self.btn_scan.config(text="开始扫描", state="normal", bg=ACCENT, fg="#ffffff")
            if PILLOW_AVAILABLE:
                self.btn_analyze.config(state="normal", fg=TEXT)
            for btn in (self.btn_browse, self.btn_add, self.btn_remove):
                btn.config(state="normal")

    def _scan_worker(self, folders) -> None:  # 在后台线程中运行
        for index, folder in enumerate(folders, start=1):
            if self.cancel_event.is_set():
                break
            self.msg_queue.put(("folder", index, len(folders), folder))
            result = scan_folder(folder)
            infos: list[PhotoInfo] = []
            n = len(result.files)
            for done, item in enumerate(result.files, start=1):
                if self.cancel_event.is_set():
                    break
                infos.append(read_photo_info(item.path, item.category))
                if done % 5 == 0 or done == n:
                    self.msg_queue.put(("progress", done, n, item.path.name))
            if infos:
                self.db.upsert_many(infos)
            if not self.cancel_event.is_set():
                self.db.mark_folder_scanned(folder)
        self.msg_queue.put(("done",))

    def _poll_scan_queue(self) -> None:
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "folder":
                    _, index, total, folder = msg
                    self.progress_title.config(text=f"正在扫描（文件夹 {index} / {total}）")
                    self.progress_folder.config(text=str(folder))
                    self.progress_bar.config(value=0)
                    self.progress_detail.config(text="")
                elif kind == "progress":
                    _, done, n, name = msg
                    percent = done * 100.0 / max(n, 1)
                    self.progress_bar.config(value=percent)
                    self.progress_detail.config(text=f"{done:,} / {n:,} · {percent:.0f}%")
                    self.progress_current.config(text=f"当前文件：{name}")
                elif kind == "done":
                    cancelled = self.cancel_event.is_set()
                    self._set_scanning_ui(False)
                    self._quality_revision += 1
                    self.refresh_library()
                    self.refresh_results()
                    self.refresh_status_bar()
                    if cancelled:
                        self.status_left.config(text="扫描已取消（已处理的部分已写入数据库）")
                    break
        except queue.Empty:
            pass
        if self.scan_thread and self.scan_thread.is_alive():
            self.root.after(100, self._poll_scan_queue)

    # ------------------------------------------------------------------ #
    # 质量分析流程（V2.5，后台线程 + 队列轮询，可取消）
    # ------------------------------------------------------------------ #

    def start_analysis(self) -> None:
        if not PILLOW_AVAILABLE:
            messagebox.showinfo(
                "质量分析",
                "需要安装 Pillow 才能分析图像质量：\n\n"
                "pip install -r requirements-quality.txt\n\n"
                "未安装时扫描、EXIF、规则筛选仍然可用。",
            )
            return
        if self.analyze_thread and self.analyze_thread.is_alive():
            return
        if self.scan_thread and self.scan_thread.is_alive():
            return
        folders = [f.path for f in self.db.list_folders() if str(f.path) in self.checked_folders]
        if not folders:
            messagebox.showinfo("质量分析", "请先在左侧文件夹库勾选至少一个要分析的文件夹。")
            return
        infos = self.db.find_photos_under(folders)
        if not infos:
            messagebox.showinfo("质量分析", "勾选的文件夹里还没有照片记录，请先扫描。")
            return

        if not messagebox.askyesno(
            "质量分析",
            "将对勾选文件夹中的照片分析图像质量（清晰度 / 曝光 / 重复）。\n\n"
            "· 只读分析，不会改动任何原始文件\n"
            "· 只处理未分析或已变更的照片，可以随时取消\n\n继续？",
        ):
            return

        self.analyze_cancel.clear()
        self._set_analyzing_ui(True)
        self.analyze_thread = threading.Thread(
            target=self._analyze_worker, args=(infos,), daemon=True
        )
        self.analyze_thread.start()
        self.root.after(120, self._poll_analyze_queue)

    def cancel_analysis(self) -> None:
        self.analyze_cancel.set()
        self.progress_current.config(text="正在取消……（等待当前批次写入完成）")

    def _set_analyzing_ui(self, analyzing: bool) -> None:
        if analyzing:
            self.result_frame.pack_forget()
            self.progress_frame.pack(fill="both", expand=True)
            self.progress_bar.config(value=0)
            self.progress_title.config(text="正在分析质量")
            self.progress_folder.config(text="只读分析 · 不会改动原始文件")
            self.progress_detail.config(text="")
            self.progress_current.config(text="准备中……")
            self.btn_analyze.config(text="分析中…", state="disabled", bg=INPUT_BG, fg=TEXT_FAINT)
            self.btn_scan.config(state="disabled", bg=INPUT_BG, fg=TEXT_FAINT)
            for btn in (self.btn_browse, self.btn_add, self.btn_remove):
                btn.config(state="disabled")
        else:
            self.progress_frame.pack_forget()
            self.result_frame.pack(fill="both", expand=True)
            self.btn_analyze.config(text="质量分析", state="normal", bg=INPUT_BG, fg=TEXT)
            self.btn_scan.config(text="开始扫描", state="normal", bg=ACCENT, fg="#ffffff")
            for btn in (self.btn_browse, self.btn_add, self.btn_remove):
                btn.config(state="normal")

    def _analyze_worker(self, infos) -> None:  # 在后台线程中运行
        try:
            with QualityStore.open(self.db_path) as store:
                report = analyze_photos(
                    infos,
                    store,
                    options=AnalyzeOptions(workers=4),
                    progress=lambda done, total, name: self.analyze_queue.put(
                        ("progress", done, total, name)
                    ),
                    is_cancelled=self.analyze_cancel.is_set,
                )
            self.analyze_queue.put(("done", report))
        except Exception as exc:  # 包括缺少 Pillow / 数据库错误
            self.analyze_queue.put(("error", str(exc)))

    def _poll_analyze_queue(self) -> None:
        try:
            while True:
                msg = self.analyze_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, done, total, name = msg
                    percent = done * 100.0 / max(total, 1)
                    self.progress_bar.config(value=percent)
                    self.progress_detail.config(text=f"{done:,} / {total:,} · {percent:.0f}%")
                    self.progress_current.config(text=f"当前文件：{name}")
                elif kind == "done":
                    report = msg[1]
                    self.analyze_report = report
                    self._set_analyzing_ui(False)
                    self._quality_revision += 1
                    self.refresh_results()
                    self.refresh_status_bar()
                    self.status_left.config(text=_analysis_summary(report))
                    break
                elif kind == "error":
                    self._set_analyzing_ui(False)
                    messagebox.showerror("质量分析失败", msg[1])
                    break
        except queue.Empty:
            pass
        if self.analyze_thread and self.analyze_thread.is_alive():
            self.root.after(120, self._poll_analyze_queue)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def _on_close(self) -> None:
        self.cancel_event.set()
        self.analyze_cancel.set()
        try:
            self.db.close()
        finally:
            try:
                self.quality_store.close()
            finally:
                self.root.destroy()


def _category_label(category: str) -> str:
    return {"nef": "NEF", "jpeg": "JPEG", "png": "PNG", "video": "视频", "unsupported": "其他"}.get(
        category, category
    )


def _analysis_summary(report) -> str:
    """一行状态栏摘要。"""
    parts = [f"质量分析完成：本次 {report.analyzed_now:,} 张"]
    counts = report.flag_counts
    parts.append(f"模糊 {counts.get('blurry', 0):,}")
    parts.append(f"过曝 {counts.get('overexposed', 0):,}")
    parts.append(f"欠曝 {counts.get('underexposed', 0):,}")
    parts.append(f"重复 {report.duplicate_groups:,} 组")
    if report.sequence_groups:
        parts.append(f"序列 {report.sequence_groups:,} 组")
    if report.cancelled:
        parts.append("已取消")
    return " · ".join(parts)


def main(argv: list[str] | None = None) -> int:
    """GUI 入口。``--selftest`` 仅构建窗口后立即退出（供自动化验证）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    root = tk.Tk()
    try:
        if "--selftest" in args:
            root.withdraw()
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                app = PhotoClassifierApp(root, Path(tmp) / "selftest.db")
                root.update_idletasks()
                root.update()
                app.quality_store.close()
                app.db.close()  # 关闭数据库，允许临时目录被清理
            root.destroy()
            print("GUI selftest OK")
            return 0
        PhotoClassifierApp(root)
        root.mainloop()
        return 0
    except Exception:
        root.destroy()
        raise


if __name__ == "__main__":
    sys.exit(main())
