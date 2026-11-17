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
from src.filtering import FilterSelection, matches as selection_matches
from src.formatting import format_shutter, format_size
from src.person_ai import MODEL_VERSION as PERSON_MODEL_VERSION
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
from src.quality_sort import SORT_DEFAULT, SORT_SHARPNESS, sharpness_key, sharpness_score
from src.quality_store import QualityStore
from src.rules import RULE_REGISTRY, RuleEngine, RuleMatch
from src.sky_ai import MODEL_VERSION as SKY_MODEL_VERSION
from src.tag_controller import TagController
from src.tag_store import Tag, TagStore
from src.ui_detail import DetailMixin
from src.ui_panels import PanelsMixin
from src.ui_ai import AIAnalysisMixin
from src.ui_thumbnail import ThumbnailMixin
from src.ui_tag_dialogs import TagDialogsMixin
from src.tags import (
    PICK_TAG_NAME,
    TagConflict,
    TagError,
    chip_colors,
    normalize_tag_name,
)
from src.scan_catalog import scan_catalog
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

RESULT_PAGE_SIZE = 250

class PhotoClassifierApp(PanelsMixin, DetailMixin, TagDialogsMixin, AIAnalysisMixin, ThumbnailMixin):
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
        self._rule_cache: dict[str, list[RuleMatch]] = {}
        self._matched_iids: list[str] = []
        self._selected_iids: set[str] = set()
        self._selecting_results = False
        self._selection_anchor = ""
        self._result_page = 0
        self._catalog_total = 0
        self.current: PhotoInfo | None = None
        self.scan_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.msg_queue: queue.Queue = queue.Queue()

        self.analyze_thread: threading.Thread | None = None
        self.analyze_cancel = threading.Event()
        self.analyze_queue: queue.Queue = queue.Queue()
        self.analyze_report = None
        self._init_ai_analysis()

        self.rule_engine = RuleEngine()
        self.quality_engine = QualityEngine()
        self.active_rules: set[str] = set()
        self.active_quality: set[str] = set()
        self.active_tags: set[int] = set()
        self.and_mode = tk.BooleanVar(value=True)
        self.gps_only = tk.BooleanVar(value=False)
        self.quality_ok_var = tk.BooleanVar(value=False)
        self.sort_mode = SORT_DEFAULT
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
        self._build_status_bar()
        self._build_left_panel()
        # 先放右侧详情面板、再放中间（中间 expand 会吃掉剩余空间）
        self._build_detail_panel()
        self._build_center()

        self.refresh_library()
        self.refresh_tags()
        self.refresh_results()
        self.refresh_status_bar()
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<Control-z>", lambda _event: self.undo_last_tag_operation())
        self.root.bind("<Escape>", self._on_result_selection_escape)
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
            command=command,
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
        iids = self._selected_iids if self._selecting_results else self.result_tree.selection()
        return [self.infos_by_iid[iid] for iid in sorted(iids) if iid in self.infos_by_iid]

    def start_result_selection(self) -> None:
        if self._selecting_results or not self.result_tree.get_children():
            return
        self._selecting_results = True
        self.btn_select_results.pack_forget()
        self.selection_actions.pack(fill="x")
        self.result_tree.configure(show="tree headings", selectmode="none")
        self.result_tree.xview_moveto(0)
        self.result_tree.focus_set()
        self._sync_result_selection()

    def cancel_result_selection(self) -> None:
        self._selecting_results = False
        self._selected_iids.clear()
        self._selection_anchor = ""
        self.result_tree.focus("")
        self.result_tree.configure(show="headings", selectmode="extended")
        self.selection_actions.pack_forget()
        self.btn_select_results.pack(side="left", padx=8, pady=5)
        self._sync_result_selection()

    def _on_result_selection_escape(self, _event) -> str | None:
        if not self._selecting_results or not self.result_frame.winfo_ismapped():
            return None
        self.cancel_result_selection()
        return "break"

    def toggle_result_page_selection(self) -> None:
        if not self._selecting_results:
            return
        page = set(self.result_tree.get_children())
        if page <= self._selected_iids:
            self._selected_iids.difference_update(page)
        else:
            self._selected_iids.update(page)
        self._selection_anchor = ""
        self._sync_result_selection()

    def _sync_result_selection(self) -> None:
        tree = self.result_tree
        page = tree.get_children()
        selection = tuple(iid for iid in page if iid in self._selected_iids)
        if tree.selection() != selection:
            tree.selection_set(selection)
        for iid in page:
            mark = "☑" if iid in self._selected_iids else "☐"
            if tree.item(iid, "text") != mark:
                tree.item(iid, text=mark)
        count = len(self._selected_iids)
        self.selection_count.config(text=f"已选 {count:,} 张")
        self.btn_select_page.config(
            text="取消本页选择" if page and len(selection) == len(page) else "全选本页",
            state="normal" if page else "disabled",
        )
        self._update_ai_button()
        self._update_pick_button()

    def _on_delete_key(self, _event=None) -> str:
        self.remove_selected_photos()
        return "break"

    def remove_selected_photos(self) -> None:
        """从照片库移除选中记录；只删数据库，不删除磁盘文件。"""
        infos = self._selected_infos()
        if not infos:
            return
        count = len(infos)
        preview = "\n".join(info.file_name for info in infos[:6])
        if count > 6:
            preview += f"\n……另外 {count - 6} 张"
        if not messagebox.askyesno(
            "从照片库移除",
            f"将从 PhotoClassifier 移除 {count} 张照片：\n\n{preview}\n\n"
            "只删除数据库记录，不会删除磁盘文件；重新扫描该文件夹会再次加入。\n\n继续？",
        ):
            return
        removed_paths = {str(info.path) for info in infos}
        removed = self.db.remove_photos([info.path for info in infos])
        if removed <= 0:
            self.status_left.config(text="没有可移除的照片记录")
            return
        if self.current is not None and str(self.current.path) in removed_paths:
            self.current = None
            self.set_detail(None)
        self._quality_revision += 1
        self.tag_controller.mark_dirty()
        self.refresh_library()
        self.refresh_tags()
        self.refresh_results()
        self.refresh_status_bar()
        self.status_left.config(text=f"已从照片库移除 {removed} 张（磁盘文件保留）")

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

    def refresh_results(self, *, skip_duplicate_rebuild: bool = False) -> None:
        folders = sorted(self.checked_folders) or [f.path for f in self.db.list_folders()]
        photos = self.db.find_photos_under(folders)
        previous_infos = self.infos_by_iid
        previous_rules = self._rule_cache
        self.infos_by_iid = {str(info.path): info for info in photos}
        self.matches_by_iid = {}
        self.quality_by_iid = {}
        self._rule_cache = {}
        self.person_results = self.db.person_results(PERSON_MODEL_VERSION)
        self.sky_results = self.db.sky_results(SKY_MODEL_VERSION)
        self._ensure_quality_cache(photos, skip_duplicates=skip_duplicate_rebuild)
        self._ensure_tag_cache()
        if self.sort_mode == SORT_SHARPNESS:
            photos.sort(
                key=lambda info: sharpness_key(
                    info, self.measurements_by_path.get(str(info.path))
                )
            )

        selected_filters = self._filter_selection()
        self._matched_iids = []
        for info in photos:
            iid = str(info.path)
            matches = previous_rules.get(iid) if previous_infos.get(iid) == info else None
            if matches is None:
                matches = self.rule_engine.evaluate(info)
            self._rule_cache[iid] = matches
            quality_flags = self._quality_flags_for(info)
            user_tags = self.tag_controller.tags_by_path.get(iid, [])
            if not self._passes_filter(info, matches, quality_flags, user_tags, selected_filters):
                continue
            self.matches_by_iid[iid] = matches
            self.quality_by_iid[iid] = quality_flags
            self._matched_iids.append(iid)
        self._selected_iids.intersection_update(self.matches_by_iid)
        self._catalog_total = self.db.count_photos()
        self._result_page = min(self._result_page, self._page_count() - 1)
        self._render_result_page()
        self._update_sort_button()
        if self.current is not None:
            keep = self.infos_by_iid.get(str(self.current.path))
            self.set_detail(keep if keep is not None and str(keep.path) in self.matches_by_iid else None)

    def _page_count(self) -> int:
        return max(1, (len(self._matched_iids) + RESULT_PAGE_SIZE - 1) // RESULT_PAGE_SIZE)

    def change_result_page(self, step: int) -> None:
        self._result_page = max(0, min(self._result_page + step, self._page_count() - 1))
        self._selection_anchor = ""
        self._render_result_page()
        self.result_tree.yview_moveto(0)

    def _render_result_page(self) -> None:
        start = self._result_page * RESULT_PAGE_SIZE
        page = self._matched_iids[start:start + RESULT_PAGE_SIZE]
        previous = self.result_tree.get_children()
        removed = set(previous).difference(page)
        if removed:
            self.result_tree.delete(*removed)
        existing = set(previous).difference(removed)
        for iid in page:
            info = self.infos_by_iid[iid]
            matches = self.matches_by_iid[iid]
            quality_flags = self.quality_by_iid[iid]
            user_tags = self.tag_controller.tags_by_path.get(iid, [])
            shutter = format_shutter(info.exposure_time_s) if info.exposure_time_s else ""
            labels = " · ".join(match.label for match in matches)
            if user_tags:
                user_text = " · ".join(f"★{tag.name}" for tag in user_tags)
                labels = f"{user_text} ｜ {labels}" if labels else user_text
            ai_tags = [name for name, hit in (("人物", self.has_ai_person(info)), ("天空", self.has_ai_sky(info))) if hit]
            if ai_tags:
                ai_text = "AI · " + " · ".join(ai_tags)
                labels = f"{labels} ｜ {ai_text}" if labels else ai_text
            measurement = self.measurements_by_path.get(str(info.path))
            quality_text = " · ".join(
                _QUALITY_SHORT.get(flag.flag_id, flag.label) for flag in quality_flags
            )
            if not quality_text:
                if measurement is None:
                    quality_text = ""
                elif measurement.error:
                    quality_text = "失败"
                else:
                    quality_text = "正常"
            score = sharpness_score(measurement)
            sharpness_text = "—" if score is None else f"{score:.0f}"
            values = (
                info.file_name,
                category_label(info.category),
                info.taken_at or "",
                info.camera or "",
                str(info.iso) if info.iso is not None else "",
                shutter,
                labels,
                quality_text,
                sharpness_text,
                format_size(info.size_bytes),
            )
            tags = []
            if info.exif_status == EXIF_ERROR:
                tags.append("error")
            elif info.category in ("video", "unsupported"):
                tags.append("dim")
            if iid in existing:
                if (tuple(map(str, self.result_tree.item(iid, "values"))) != tuple(map(str, values))
                        or self.result_tree.item(iid, "tags") != tuple(tags)):
                    self.result_tree.item(iid, values=values, tags=tags)
            else:
                self.result_tree.insert("", "end", iid=iid, values=values, tags=tags)
        if tuple(page) != self.result_tree.get_children():
            self.result_tree.set_children("", *page)
        if self._selecting_results:
            self._sync_result_selection()
        else:
            self._update_pick_button()
        self._update_page_controls()

    def _update_page_controls(self) -> None:
        pages = self._page_count()
        self.btn_result_prev.config(state="normal" if self._result_page else "disabled")
        self.btn_result_next.config(state="normal" if self._result_page + 1 < pages else "disabled")
        self.page_status.config(text=f"第 {self._result_page + 1} / {pages} 页")
        self.btn_select_results.config(state="normal" if self._matched_iids else "disabled")
        filter_note = ""
        if (
            self.active_rules
            or self.active_quality
            or self.active_tags
            or self.gps_only.get()
            or self.quality_ok_var.get()
            or self.person_filter.get()
            or self.sky_filter.get()
        ):
            filter_note = " · 已筛选"
        sort_note = " · 按清晰度排序" if self.sort_mode == SORT_SHARPNESS else ""
        self.count_label.config(
            text=f"库中共 {self._catalog_total:,} 个文件 · 符合 {len(self._matched_iids):,} 张{filter_note}{sort_note}"
        )

    def _selected_all_picked(self, infos: list[PhotoInfo] | None = None) -> bool:
        infos = infos if infos is not None else self._selected_infos()
        if not infos:
            return False
        tag = self.tag_controller.get_by_name(PICK_TAG_NAME)
        if tag is None:
            return False
        return all(
            tag.id in self.tag_controller.tag_ids_by_path.get(str(info.path), set())
            for info in infos
        )

    def _update_pick_button(self) -> None:
        button = getattr(self, "btn_pick", None)
        if button is None:
            return
        infos = self._selected_infos()
        if not infos:
            button.config(text="标记精选", state="disabled", fg=TEXT_FAINT)
            return
        if self._selected_all_picked(infos):
            button.config(text="取消精选", state="normal", fg=GREEN)
        else:
            button.config(text="标记精选", state="normal", fg=TEXT)

    def _on_pick_key(self, _event=None) -> str:
        self.toggle_picked()
        return "break"

    def toggle_picked(self) -> None:
        """把选中照片标记为“精选”；全部已精选时则取消。"""
        infos = self._selected_infos()
        if not infos:
            return
        tag = self.tag_controller.get_by_name(PICK_TAG_NAME)
        if tag is not None and self._selected_all_picked(infos):
            removed = self.tag_controller.apply_remove(
                [str(info.path) for info in infos], [tag.id]
            )
            message = f"已取消 {len(infos)} 张照片的精选（{len(removed)} 条）"
        else:
            tag = self.tag_controller.ensure_tag(PICK_TAG_NAME)
            added, unknown = self.tag_controller.apply_add(
                [str(info.path) for info in infos], [tag.id]
            )
            message = f"已标记 {len(infos)} 张照片为精选（新增 {len(added)} 条）"
            if unknown:
                message += f"，跳过 {len(unknown)} 个未知路径"
        self._after_tag_change(message)

    def toggle_quality_sort(self) -> None:
        """在“默认顺序”和“清晰度优先”之间切换。"""
        if self.sort_mode == SORT_SHARPNESS:
            self.sort_mode = SORT_DEFAULT
            self.refresh_results()
            return
        if not any(
            sharpness_score(measurement) is not None
            for measurement in self.measurements_by_path.values()
        ):
            self.status_left.config(text="还没有清晰度数据，请先点“质量分析”")
            return
        self.sort_mode = SORT_SHARPNESS
        self.refresh_results()

    def _update_sort_button(self) -> None:
        button = getattr(self, "btn_sort_quality", None)
        if button is None:
            return
        if self.sort_mode == SORT_SHARPNESS:
            button.config(text="清晰度优先 ✓", bg=ACCENT, fg="#ffffff")
        else:
            button.config(text="清晰度优先", bg=INPUT_BG, fg=TEXT)

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

    def _ensure_quality_cache(self, photos: list[PhotoInfo], *, skip_duplicates: bool = False) -> None:
        """测量值与重复分组只在质量数据变化后重算，避免每次筛选都全量重跑。"""
        if self._quality_cache_revision == self._quality_revision:
            return
        measurements = list(self.quality_store.iter_measurements())
        self.measurements_by_path = {measurement.path: measurement for measurement in measurements}
        self.duplicates = DuplicateResult() if skip_duplicates else rebuild_duplicates(self.quality_store, photos)
        self._quality_cache_revision = self._quality_revision

    def _filter_selection(self) -> FilterSelection:
        return FilterSelection(
            rule_ids=frozenset(self.active_rules),
            quality_ids=frozenset(self.active_quality),
            tag_ids=frozenset(self.active_tags),
            and_mode=self.and_mode.get(),
            gps_only=self.gps_only.get(),
            quality_ok=self.quality_ok_var.get(),
            ai_person_only=self.person_filter.get(),
            ai_sky_only=self.sky_filter.get(),
        )

    def _passes_filter(
        self, info: PhotoInfo, matches: list[RuleMatch], quality_flags: list[QualityFlag],
        user_tags: list[Tag] | None = None, selection: FilterSelection | None = None,
    ) -> bool:
        selection = selection if selection is not None else self._filter_selection()
        if selection.is_empty:
            return True
        quality_hits = {flag.flag_id for flag in quality_flags}
        measurement = self.measurements_by_path.get(str(info.path))
        if measurement is None or measurement.error:
            quality_hits.add("__unanalyzed__")
        return selection_matches(
            info,
            rule_hits={match.rule_id for match in matches},
            quality_hits=quality_hits,
            tag_hits={tag.id for tag in (user_tags or [])},
            selection=selection,
            ai_person=self.has_ai_person(info),
            ai_sky=self.has_ai_sky(info),
        )

    def _on_quality_flag_changed(self) -> None:
        """勾选具体问题标记时，自动取消“只看质量正常”。"""
        if any(var.get() for var in self.quality_vars.values()):
            self.quality_ok_var.set(False)
        self._on_filter_changed()

    def _on_quality_ok_changed(self) -> None:
        """勾选“质量正常”时清掉具体问题标记，避免语义冲突。"""
        if self.quality_ok_var.get():
            for var in self.quality_vars.values():
                var.set(False)
        self._on_filter_changed()

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
        self.quality_ok_var.set(False)
        self.person_filter.set(False)
        self.sky_filter.set(False)
        self.active_rules = set()
        self.active_quality = set()
        self.active_tags = set()
        self._refresh_tag_list()
        self.refresh_results()

    @staticmethod
    def _select_target() -> PhotoInfo | None:
        return None

    def _on_result_click(self, event, *, toggle: bool = False, extend: bool = False) -> str | None:
        tree = self.result_tree
        if tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return None
        iid = tree.identify_row(event.y)
        if not iid:
            return None
        if not self._selecting_results:
            if not (toggle or extend):
                return None
            previous = tree.selection()
            self.start_result_selection()
            if previous:
                self._selection_anchor = previous[0]
                if toggle:
                    self._selected_iids.add(previous[0])
        tree.focus_set()
        self._select_result_row(iid, extend=extend)
        return "break"

    def _select_result_row(self, iid: str, *, extend: bool = False) -> None:
        tree = self.result_tree
        page = tree.get_children()
        if extend and self._selection_anchor in page:
            first, last = sorted((page.index(self._selection_anchor), page.index(iid)))
            self._selected_iids.difference_update(page)
            self._selected_iids.update(page[first:last + 1])
        else:
            self._selected_iids.symmetric_difference_update((iid,))
            self._selection_anchor = iid
        tree.focus(iid)
        self.set_detail(self.infos_by_iid[iid])
        self._sync_result_selection()

    def _on_result_space(self, _event) -> str:
        iid = self.result_tree.focus()
        if iid:
            self.start_result_selection()
            self._select_result_row(iid)
        return "break"

    def _on_result_double_click(self, event) -> str | None:
        """双击结果行：用默认程序打开图片（不是打开所在文件夹）。"""
        if self._selecting_results:
            return self._on_result_click(event)
        if self.result_tree.identify_region(event.x, event.y) == "cell":
            self.open_selected()
        return None

    def _on_result_select(self, _event=None) -> None:
        if self._selecting_results:
            return
        selection = self.result_tree.selection()
        if selection:
            self.set_detail(self.infos_by_iid[selection[0]])
        self._update_pick_button()

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

    def start_scan(self, *, full: bool = False) -> None:
        if self.ai_thread is not None:
            return
        if self.scan_thread and self.scan_thread.is_alive():
            return
        if self.analyze_thread and self.analyze_thread.is_alive():
            return
        folders = [f.path for f in self.db.list_folders() if str(f.path) in self.checked_folders]
        if not folders:
            messagebox.showinfo("开始扫描", "请先在左侧文件夹库勾选至少一个要扫描的文件夹。")
            return

        self.cancel_event.clear()
        self._set_scanning_ui(True)
        self.scan_thread = threading.Thread(
            target=self._scan_worker, args=(folders, full), daemon=True
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
            self.btn_cancel.config(command=self.cancel_scan)
            self.progress_folder.config(text="")
            self.progress_detail.config(text="")
            self.progress_current.config(text="")
            self.btn_scan.config(text="扫描中…", state="disabled", bg=INPUT_BG, fg=TEXT_FAINT)
            self.btn_analyze.config(state="disabled", fg=TEXT_FAINT)
            for btn in (self.btn_browse, self.btn_add, self.btn_remove, self.btn_rescan):
                btn.config(state="disabled")
        else:
            self.progress_frame.pack_forget()
            self.result_frame.pack(fill="both", expand=True)
            self.btn_scan.config(text="开始扫描", state="normal", bg=ACCENT, fg="#ffffff")
            if PILLOW_AVAILABLE:
                self.btn_analyze.config(state="normal", fg=TEXT)
            for btn in (self.btn_browse, self.btn_add, self.btn_remove, self.btn_rescan):
                btn.config(state="normal")

    def _scan_worker(self, folders, full=False) -> None:  # 在后台线程中运行
        # SQLite 连接不能跨线程使用：工作线程必须自己开一个连接。
        try:
            written_total = reused_total = 0
            with PhotoDatabase.open(self.db_path) as database:
                for index, folder in enumerate(folders, start=1):
                    if self.cancel_event.is_set():
                        break
                    self.msg_queue.put(("folder", index, len(folders), folder))
                    result, infos, written = scan_catalog(
                        database, folder, full=full, cancelled=self.cancel_event.is_set,
                        progress=lambda done, n, name: self.msg_queue.put(("progress", done, n, name)),
                    )
                    written_total += written
                    reused_total += len(infos) - written
                    if not self.cancel_event.is_set() and not any(issue.path == result.root for issue in result.issues):
                        database.mark_folder_scanned(folder)
            self.msg_queue.put(("done", written_total, reused_total))
        except Exception as exc:  # 任何异常都必须回到 UI，避免进度条永远停在“扫描中”
            self.msg_queue.put(("error", str(exc)))

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
                elif kind == "error":
                    self.scan_thread = None
                    self._set_scanning_ui(False)
                    self._quality_revision += 1
                    self.refresh_library()
                    self.refresh_results()
                    self.refresh_status_bar()
                    messagebox.showerror("扫描失败", msg[1])
                    break
                elif kind == "done":
                    cancelled = self.cancel_event.is_set()
                    self.scan_thread = None
                    self._set_scanning_ui(False)
                    self._quality_revision += 1
                    self.refresh_library()
                    self.refresh_results()
                    self.refresh_status_bar()
                    if cancelled:
                        self.status_left.config(text="扫描已取消（已处理的部分已写入数据库）")
                    else:
                        _, written, reused = msg
                        self.status_left.config(text=f"扫描完成 · 读取 {written:,} · 复用 {reused:,}")
                    break
        except queue.Empty:
            pass
        if self.scan_thread is not None:
            self.root.after(100, self._poll_scan_queue)

    # ------------------------------------------------------------------ #
    # 质量分析流程（V2.5，后台线程 + 队列轮询，可取消）
    # ------------------------------------------------------------------ #

    def start_analysis(self) -> None:
        if self.ai_thread is not None:
            return
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
            self.btn_cancel.config(command=self.cancel_analysis)
            self.progress_folder.config(text="只读分析 · 不会改动原始文件")
            self.progress_detail.config(text="")
            self.progress_current.config(text="准备中……")
            self.btn_analyze.config(text="分析中…", state="disabled", bg=INPUT_BG, fg=TEXT_FAINT)
            self.btn_scan.config(state="disabled", bg=INPUT_BG, fg=TEXT_FAINT)
            for btn in (self.btn_browse, self.btn_add, self.btn_remove, self.btn_rescan):
                btn.config(state="disabled")
        else:
            self.progress_frame.pack_forget()
            self.result_frame.pack(fill="both", expand=True)
            self.btn_analyze.config(text="质量分析", state="normal", bg=INPUT_BG, fg=TEXT)
            self.btn_scan.config(text="开始扫描", state="normal", bg=ACCENT, fg="#ffffff")
            self.btn_cancel.config(command=self.cancel_scan)
            for btn in (self.btn_browse, self.btn_add, self.btn_remove, self.btn_rescan):
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
                    if not self.analyze_cancel.is_set():
                        self.progress_current.config(text=f"当前文件：{name}")
                elif kind == "done":
                    report = msg[1]
                    self.analyze_thread = None
                    self.analyze_report = report
                    self._set_analyzing_ui(False)
                    self._quality_revision += 1
                    self.refresh_results(skip_duplicate_rebuild=report.cancelled)
                    self.refresh_status_bar()
                    self.status_left.config(text=_analysis_summary(report))
                    break
                elif kind == "error":
                    self.analyze_thread = None
                    self._set_analyzing_ui(False)
                    messagebox.showerror("质量分析失败", msg[1])
                    break
        except queue.Empty:
            pass
        if self.analyze_thread is not None:
            self.root.after(120, self._poll_analyze_queue)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def _on_close(self) -> None:
        self.cancel_event.set()
        self.analyze_cancel.set()
        self.ai_cancel.set()
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
    if report.cancelled:
        return f"质量分析已取消 · 已完成 {report.analyzed_now:,} 张 · 未更新重复统计"
    parts = [f"质量分析完成：本次 {report.analyzed_now:,} 张"]
    counts = report.flag_counts
    parts.append(f"模糊 {counts.get('blurry', 0):,}")
    parts.append(f"过曝 {counts.get('overexposed', 0):,}")
    parts.append(f"欠曝 {counts.get('underexposed', 0):,}")
    parts.append(f"重复 {report.duplicate_groups:,} 组")
    if report.sequence_groups:
        parts.append(f"序列 {report.sequence_groups:,} 组")
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
