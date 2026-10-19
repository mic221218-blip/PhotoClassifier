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
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

from src.database import PhotoDatabase
from src.duplicates import DuplicateResult
from src.exif_reader import read_photo_info
from src.filtering import FilterSelection, matches as selection_matches
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
from src.tag_controller import TagController
from src.tag_store import Tag, TagStore
from src.ui_detail import DetailMixin
from src.ui_panels import PanelsMixin
from src.ui_tag_dialogs import TagDialogsMixin
from src.tags import TagConflict, TagError, chip_colors, normalize_tag_name
from src.scanner import scan_folder
from src.shell import open_in_explorer, open_with_default_app

from src.ui_theme import (  # noqa: E402  (主题常量集中管理)
    ACCENT,
    ACCENT_DIM,
    BORDER_SOFT,
    CARD_BG,
    DEFAULT_DB,
    DETAIL_FIELDS as _DETAIL_FIELDS,
    GREEN,
    ICON_PATH,
    INPUT_BG,
    ORANGE,
    PANEL_BG,
    PREVIEW_COLORS as _PREVIEW_COLORS,
    PROJECT_ROOT,
    QUALITY_SHORT as _QUALITY_SHORT,
    RESULT_COLUMNS as _RESULT_COLUMNS,
    TEXT,
    TEXT_FAINT,
    TEXT_SUB,
    WIN_BG,
    category_label,
)


class PhotoClassifierApp(PanelsMixin, DetailMixin, TagDialogsMixin):
    """主窗口应用。``root`` 由调用方创建，方便测试与自检。"""

    def __init__(self, root: tk.Tk, db_path: str | Path = DEFAULT_DB):
        self.root = root
        self.db = PhotoDatabase.open(db_path)
        self.db_path = Path(db_path)
        self.quality_store = QualityStore.open(db_path)
        self.tag_store = TagStore.open(db_path)
        self.tag_store.prune()  # 清理已不在照片库里的历史关联
        self.tag_controller = TagController(self.tag_store)

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
        self.active_tags: set[int] = set()
        self.and_mode = tk.BooleanVar(value=True)
        self.gps_only = tk.BooleanVar(value=False)
        self.rule_vars: dict[str, tk.BooleanVar] = {}
        self.quality_vars: dict[str, tk.BooleanVar] = {}
        self.measurements_by_path: dict[str, QualityMeasurement] = {}
        self.duplicates = DuplicateResult()
        self._quality_revision = 0
        self._quality_cache_revision = -1
        self._tag_entry_var: tk.StringVar | None = None

        self._setup_window()
        self._build_styles()
        self._build_toolbar()
        self._build_left_panel()
        # 先放右侧详情面板、再放中间（中间 expand 会吃掉剩余空间）
        self._build_detail_panel()
        self._build_center()
        self._build_status_bar()

        self.refresh_library()
        self.refresh_tags()
        self.refresh_results()
        self.refresh_status_bar()
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<Control-z>", lambda _event: self.undo_last_tag_operation())
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

    def _selected_infos(self) -> list[PhotoInfo]:
        return [
            self.infos_by_iid[iid]
            for iid in self.result_tree.selection()
            if iid in self.infos_by_iid
        ]

    def undo_last_tag_operation(self) -> None:
        message = self.tag_controller.undo_last()
        if message is None:
            return
        self._after_tag_change(message)

    # ------------------------------------------------------------------ #
    # 底部状态栏
    # ------------------------------------------------------------------ #

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
        self.refresh_tags()
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
        self.refresh_tags()
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
        self._ensure_tag_cache()

        self.result_tree.delete(*self.result_tree.get_children())
        visible = 0
        for info in photos:
            matches = self.rule_engine.evaluate(info)
            quality_flags = self._quality_flags_for(info)
            user_tags = self.tag_controller.tags_by_path.get(str(info.path), [])
            if not self._passes_filter(info, matches, quality_flags, user_tags):
                continue
            iid = str(info.path)
            self.matches_by_iid[iid] = matches
            self.quality_by_iid[iid] = quality_flags
            shutter = format_shutter(info.exposure_time_s) if info.exposure_time_s else ""
            labels = " · ".join(match.label for match in matches)
            if user_tags:
                user_text = " · ".join(f"★{tag.name}" for tag in user_tags)
                labels = f"{user_text} ｜ {labels}" if labels else user_text
            quality_text = " · ".join(
                _QUALITY_SHORT.get(flag.flag_id, flag.label) for flag in quality_flags
            )
            if not quality_text:
                measurement = self.measurements_by_path.get(str(info.path))
                if measurement is not None and measurement.error:
                    quality_text = "失败"
            values = (
                info.file_name,
                category_label(info.category),
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
        if self.active_rules or self.active_quality or self.active_tags or self.gps_only.get():
            filter_note = " · 已筛选"
        self.count_label.config(
            text=f"库中共 {total:,} 个文件 · 当前显示 {visible:,} 张{filter_note}"
        )
        if self.current is not None:
            keep = self.infos_by_iid.get(str(self.current.path))
            if keep is not None and str(keep.path) in self.matches_by_iid:
                self.set_detail(keep)

    def _ensure_tag_cache(self) -> None:
        self.tag_controller.ensure_loaded()

    def refresh_tags(self) -> None:
        self._ensure_tag_cache()
        self._refresh_tag_list()

    def _refresh_tag_list(self) -> None:
        if not hasattr(self, "tag_tree"):
            return
        self._ensure_tag_cache()
        self.tag_tree.delete(*self.tag_tree.get_children())
        self.active_tags &= set(self.tag_controller.tags_by_id)
        query = self.tag_search_var.get().strip().casefold()
        for tag in self.tag_controller.sorted_tags():
            if query and query not in tag.name.casefold():
                continue
            mark = "☑" if tag.id in self.active_tags else "☐"
            count = self.tag_controller.tag_counts.get(tag.id, 0)
            self.tag_tree.insert(
                "", "end", iid=str(tag.id), text=f"{mark}  {tag.name}    {count:,}"
            )

    def _on_tag_click(self, event) -> None:
        iid = self.tag_tree.identify_row(event.y)
        if not iid:
            return
        tag_id = int(iid)
        if tag_id in self.active_tags:
            self.active_tags.discard(tag_id)
        else:
            self.active_tags.add(tag_id)
        self._refresh_tag_list()
        self.refresh_results()

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
        user_tags: list[Tag] | None = None,
    ) -> bool:
        """规则、质量、用户标签共用同一个筛选器（见 src/filtering.py）。"""
        selection = FilterSelection(
            rule_ids=frozenset(self.active_rules),
            quality_ids=frozenset(self.active_quality),
            tag_ids=frozenset(self.active_tags),
            and_mode=self.and_mode.get(),
            gps_only=self.gps_only.get(),
        )
        if selection.is_empty:
            return True
        return selection_matches(
            info,
            rule_hits={match.rule_id for match in matches},
            quality_hits={flag.flag_id for flag in quality_flags},
            tag_hits={tag.id for tag in (user_tags or [])},
            selection=selection,
        )

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
        self.active_tags = set()
        self._refresh_tag_list()
        self.refresh_results()

    @staticmethod
    def _select_target() -> PhotoInfo | None:
        return None

    def _on_result_double_click(self, _event=None) -> None:
        """双击结果行：用默认程序打开图片（不是打开所在文件夹）。"""
        self.open_selected()

    def _on_result_select(self, _event=None) -> None:
        selection = self.result_tree.selection()
        if not selection:
            return
        info = self.infos_by_iid.get(selection[0])
        if info is not None:
            self.set_detail(info)

    def open_selected(self) -> None:
        """用系统默认程序打开当前照片（双击结果行也会走这里）。"""
        if self.current is None:
            return
        ok, error = open_with_default_app(self.current.path)
        if ok:
            self.status_left.config(text=f"已打开：{self.current.file_name}")
        else:
            messagebox.showwarning("打开图片", error)

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
                try:
                    self.tag_store.close()
                finally:
                    self.root.destroy()


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
                app.tag_store.close()
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
