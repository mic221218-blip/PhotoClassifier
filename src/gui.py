"""PhotoClassifier 桌面界面。扫描和标签操作均只读访问原始照片。"""

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
from src.filtering import FilterSelection, matches as selection_matches
from src.formatting import format_shutter, format_size
from src.person_ai import MODEL_VERSION as PERSON_MODEL_VERSION
from src.photo_info import EXIF_ERROR, PhotoInfo
from src.preview import PILLOW_AVAILABLE, is_supported
from src.quality import (
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
from src.timelapse import TimelapseStack, detect_timelapse
from src.ui_ai import AIAnalysisMixin
from src.ui_detail import DetailMixin
from src.ui_gallery import GalleryPhoto
from src.ui_panels import PanelsMixin
from src.ui_tag_dialogs import TagDialogsMixin
from src.ui_thumbnail import PreviewCanvas, ThumbnailMixin
from src.tags import PICK_TAG_NAME
from src.scan_catalog import scan_catalog
from src.shell import open_in_explorer, open_with_default_app

from src.ui_theme import (  # noqa: E402  (主题常量集中管理)
    ACCENT,
    ACCENT_DIM,
    BORDER_SOFT,
    CARD_BG,
    DEFAULT_DB,
    GREEN,
    ICON_PATH,
    INPUT_BG,
    ORANGE,
    PANEL_BG,
    PROJECT_ROOT,
    QUALITY_SHORT as _QUALITY_SHORT,
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
        self._browse_folder: str | None = None
        self._folder_paths: list[Path] = []
        self._result_view = "grid"
        self._quick_filter = "all"
        self._focus_iid = ""
        self._preview_window: tk.Toplevel | None = None
        self._fonts: dict[tuple[int, bool], tkfont.Font] = {}
        self.infos_by_iid: dict[str, PhotoInfo] = {}
        self.matches_by_iid: dict[str, list[RuleMatch]] = {}
        self.quality_by_iid: dict[str, list[QualityFlag]] = {}
        self._rule_cache: dict[str, list[RuleMatch]] = {}
        self._matched_iids: list[str] = []
        self._result_iids: list[str] = []
        self._timelapses: dict[str, TimelapseStack] = {}
        self._timelapse_paths: dict[str, tuple[str, ...]] = {}
        self._timelapse_by_path: dict[str, str] = {}
        self._fold_timelapses = True
        self._opened_timelapse_id: str | None = None
        self._timelapse_return_page = 0
        self._selected_iids: set[str] = set()
        self._selecting_results = False
        self._selection_anchor = ""
        self._result_page = 0
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

        self.set_result_view("grid", focus=False)
        self.btn_detail.config(bg=ACCENT_DIM)
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
        steps = max(1, abs(event.delta) // 120) * (3 if event.delta < 0 else -3)
        while widget is not None:
            if widget in (self.left_canvas, self.detail_canvas):
                widget.yview_scroll(steps, "units")
                return
            widget = widget.master

    def _on_sidebar_wheel(self, event) -> str:
        event.widget.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

    # ------------------------------------------------------------------ #
    # 窗口与样式
    # ------------------------------------------------------------------ #

    def _setup_window(self) -> None:
        self.root.title("PhotoClassifier")
        screen_width = self.root.winfo_screenwidth() - 40
        screen_height = self.root.winfo_screenheight() - 80
        self.root.geometry(f"{min(1660, screen_width)}x{min(900, screen_height)}")
        self.root.minsize(min(1200, screen_width), min(700, screen_height))
        self.root.configure(bg=WIN_BG)
        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass

    def _font(self, size=10, bold=False):
        key = size, bold
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(
                root=self.root, family="Microsoft YaHei", size=size, weight="bold" if bold else "normal",
            )
        return self._fonts[key]

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
            bordercolor=BORDER_SOFT,
            lightcolor=BORDER_SOFT,
            foreground=TEXT,
            rowheight=28,
            borderwidth=0,
            font=self._font(10),
        )
        style.map(
            "Treeview", background=[("selected", ACCENT_DIM)], foreground=[("selected", TEXT)],
            bordercolor=[("focus", ACCENT_DIM)], lightcolor=[("focus", ACCENT_DIM)],
        )
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
        for direction, sticky in (("Vertical", "ns"), ("Horizontal", "we")):
            name = f"{direction}.TScrollbar"
            element = f"{direction}.Scrollbar"
            style.layout(name, [(f"{element}.trough", {
                "sticky": sticky, "children": [(f"{element}.thumb", {"sticky": "nswe"})],
            })])
            style.configure(
                name, background="#454753", troughcolor=WIN_BG, bordercolor=WIN_BG,
                lightcolor="#454753", darkcolor="#454753", gripcount=0, arrowsize=10,
            )

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

    def _paths_for_row(self, iid: str) -> tuple[str, ...]:
        return self._timelapse_paths.get(iid, (iid,))

    def start_result_selection(self) -> None:
        if self._selecting_results or not self.result_tree.get_children():
            return
        self._selecting_results = True
        self.btn_select_results.pack_forget()
        self.btn_select_page.pack(side="right")
        self.result_tree.configure(show="tree headings", selectmode="none")
        self.result_tree.xview_moveto(0)
        self._focus_result_widget()
        self._sync_result_selection()

    def cancel_result_selection(self) -> None:
        self._selecting_results = False
        self._selected_iids.clear()
        self._selection_anchor = ""
        self.result_tree.focus("")
        self.result_tree.configure(show="headings", selectmode="extended")
        self.btn_select_page.pack_forget()
        self.btn_select_results.pack(side="right")
        self._sync_result_selection()
        if self.result_frame.winfo_ismapped():
            self._focus_result_widget()

    def _on_result_selection_escape(self, _event) -> str | None:
        if not self._selecting_results or not self.result_frame.winfo_ismapped():
            return None
        self.cancel_result_selection()
        return "break"

    def toggle_result_page_selection(self) -> None:
        if not self._selecting_results:
            return
        paths = {path for iid in self.result_tree.get_children() for path in self._paths_for_row(iid)}
        if paths <= self._selected_iids:
            self._selected_iids.difference_update(paths)
        else:
            self._selected_iids.update(paths)
        self._selection_anchor = ""
        self._sync_result_selection()

    def _sync_result_selection(self) -> None:
        tree = self.result_tree
        page = tree.get_children()
        selected_rows = []
        partial_rows = set()
        # Stack rows are virtual. Keep selection in terms of the original files.
        for iid in page:
            paths = self._paths_for_row(iid)
            selected_count = sum(path in self._selected_iids for path in paths)
            mark = "☐"
            if selected_count == len(paths):
                selected_rows.append(iid)
                mark = "☑"
            elif selected_count:
                partial_rows.add(iid)
                mark = "⊟"
            if tree.item(iid, "text") != mark:
                tree.item(iid, text=mark)
        selection = tuple(selected_rows)
        if tree.selection() != selection:
            tree.selection_set(selection)
        count = len(self._selected_iids)
        self.selection_count.config(text=f"已选 {count:,} 张")
        if count:
            self.selection_actions.pack(
                side="bottom", fill="x", padx=12, pady=(4, 0), before=self.grid_frame.master,
            )
        else:
            self.selection_actions.pack_forget()
        if self._selecting_results and not count:
            self.btn_leave_selection.pack(side="right", padx=(0, 4))
        else:
            self.btn_leave_selection.pack_forget()
        self.btn_batch_tag.config(state="normal" if count else "disabled", fg=TEXT if count else TEXT_FAINT)
        self.btn_select_page.config(
            text="取消本页选择" if page and len(selection) == len(page) else "全选本页",
            state="normal" if page else "disabled",
        )
        self._update_ai_button()
        self._update_pick_button()
        self.gallery.show_selection(self._focus_iid, set(selected_rows), partial_rows)

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
        folders = self.db.list_folders()
        self._folder_paths = [folder.path for folder in folders]
        keys = {str(path) for path in self._folder_paths}
        self.checked_folders.intersection_update(keys)
        if self._browse_folder not in keys:
            self._browse_folder = None
        tree = self.library_tree
        removed = set(tree.get_children()) - keys
        if removed:
            tree.delete(*removed)
        for folder in folders:
            key = str(folder.path)
            name = self._folder_label(folder.path)
            if tree.exists(key):
                tree.item(key, text=name)
            else:
                tree.insert("", "end", iid=key, text=name)
        tree.set_children("", *(str(path) for path in self._folder_paths))
        tree.configure(height=max(1, min(7, len(folders))))
        if len(folders) > 7:
            self.library_scroll.pack(side="right", fill="y", before=tree)
        else:
            self.library_scroll.pack_forget()
        self._sync_library_selection()
        self._update_scan_controls()

    def _folder_label(self, path: Path) -> str:
        name = path.name or str(path)
        if sum(other.name == path.name for other in self._folder_paths) > 1:
            return f"{name} · {path.parent}"
        return name

    def _on_library_select(self, _event=None) -> None:
        selected = self.library_tree.selection()
        if selected:
            self.browse_folder(selected[0])

    def _sync_library_selection(self) -> None:
        selection = (self._browse_folder,) if self._browse_folder is not None else ()
        if self.library_tree.selection() != selection:
            self.library_tree.selection_set(selection)
        self.library_tree.focus(self._browse_folder or "")
        self.btn_library_all.config(bg=INPUT_BG if self._browse_folder else ACCENT_DIM)
        self.library_menu.entryconfigure(
            "从库中移除当前文件夹…", state="normal" if self._browse_folder else "disabled",
        )

    def browse_folder(self, folder: str | None) -> None:
        if folder == self._browse_folder:
            return
        self._browse_folder = folder
        self._sync_library_selection()
        self._result_page = 0
        self._opened_timelapse_id = None
        if self._selecting_results:
            self.cancel_result_selection()
        self.refresh_results()

    def _scan_paths(self, folders: list[Path]) -> list[Path]:
        return [path for path in folders if not self.checked_folders or str(path) in self.checked_folders]

    def _update_scan_controls(self, busy: str = "") -> None:
        has_folders = bool(self._folder_paths)
        scope = self._scan_paths(self._folder_paths)
        if self.checked_folders:
            label = self._folder_label(scope[0]) if len(scope) == 1 else f"{len(scope)} 个文件夹"
        else:
            label = "全部文件夹"
        if len(label) > 28:
            label = label[:27] + "…"
        self.scan_scope_label.config(text=f"扫描范围 · {label}" if has_folders else "")
        self.btn_scan.config(
            text=busy or ("扫描新文件" if has_folders else "添加文件夹"),
            command=self.start_scan if has_folders else self.add_folder_dialog,
            state="disabled" if busy else "normal",
            bg=INPUT_BG if busy else ACCENT, fg=TEXT_FAINT if busy else "#ffffff",
        )
        for widget in (self.btn_add, self.library_menu_button):
            widget.config(state="disabled" if busy else "normal")
        for entry in ("扫描范围…", "全量重扫…", "质量分析…"):
            enabled = has_folders and (entry != "质量分析…" or PILLOW_AVAILABLE)
            self.library_menu.entryconfigure(entry, state="normal" if enabled else "disabled")

    def choose_scan_folders(self) -> None:
        paths = self._folder_paths
        if not paths:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("扫描范围")
        dialog.transient(self.root)
        dialog.configure(bg=PANEL_BG)
        tk.Label(
            dialog, text="选择扫描和质量分析要处理的文件夹", bg=PANEL_BG, fg=TEXT, font=self._font(),
        ).pack(anchor="w", padx=16, pady=(14, 8))
        body = tk.Frame(dialog, bg=PANEL_BG)
        body.pack(fill="both", expand=True, padx=16)
        choices = tk.Listbox(
            body, selectmode="multiple", exportselection=False, width=62, height=min(len(paths), 12),
            bg=CARD_BG, fg=TEXT, selectbackground=ACCENT_DIM, font=self._font(10), activestyle="none",
        )
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=choices.yview)
        choices.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        choices.pack(side="left", fill="both", expand=True)
        for index, path in enumerate(paths):
            choices.insert("end", str(path))
            if not self.checked_folders or str(path) in self.checked_folders:
                choices.selection_set(index)

        def apply_scope() -> None:
            selected = choices.curselection()
            if not selected:
                return
            self.checked_folders = {str(paths[index]) for index in selected} if len(selected) < len(paths) else set()
            self._update_scan_controls()
            dialog.destroy()

        footer = tk.Frame(dialog, bg=PANEL_BG)
        footer.pack(fill="x", padx=16, pady=12)
        self._button(footer, "全选", lambda: choices.selection_set(0, "end")).pack(side="left")
        self._button(footer, "应用", apply_scope, primary=True).pack(side="right")
        self._button(footer, "取消", dialog.destroy).pack(side="right", padx=(0, 6))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        choices.focus_set()

    def add_folder_dialog(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择要加入文件夹库的文件夹", initialdir=self._browse_folder or str(PROJECT_ROOT),
        )
        if not chosen:
            return
        self.db.add_folder(chosen)
        self._browse_folder = str(Path(chosen).resolve())
        self._result_page = 0
        if self._selecting_results:
            self.cancel_result_selection()
        self._quality_revision += 1
        self.refresh_library()
        self.refresh_tags()
        self.refresh_results()
        self.refresh_status_bar()

    def remove_selected_folder(self) -> None:
        folder = self._browse_folder
        if folder is None:
            messagebox.showinfo("移除文件夹", "请先在文件夹库中点击选择一个文件夹。")
            return
        if not messagebox.askyesno(
            "移除文件夹",
            "只从文件夹库移除该条目，不删除照片记录，更不会删除原片。\n\n继续移除？\n" + folder,
        ):
            return
        self.db.remove_folder(folder)
        self.checked_folders.discard(folder)
        self._result_page = 0
        self._quality_revision += 1
        self.refresh_library()
        self.refresh_tags()
        self.refresh_results()
        self.refresh_status_bar()

    # ------------------------------------------------------------------ #
    # 结果列表与详情联动
    # ------------------------------------------------------------------ #

    def refresh_results(self, *, skip_duplicate_rebuild: bool = False) -> None:
        folders = [self._browse_folder] if self._browse_folder else [f.path for f in self.db.list_folders()]
        photos = self.db.find_photos_under(folders)
        previous_infos = self.infos_by_iid
        previous_rules = self._rule_cache
        self.infos_by_iid = {str(info.path): info for info in photos}
        if previous_infos != self.infos_by_iid:
            self._timelapses = {stack.id: stack for stack in detect_timelapse(photos)}
            self._timelapse_paths = {
                stack.id: tuple(str(info.path) for info in stack.members)
                for stack in self._timelapses.values()
            }
            self._timelapse_by_path = {
                path: stack_id for stack_id, paths in self._timelapse_paths.items() for path in paths
            }
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
        picked = self.tag_controller.get_by_name(PICK_TAG_NAME)
        self._matched_iids = []
        for info in photos:
            iid = str(info.path)
            if self._quick_filter == "timelapse" and iid not in self._timelapse_by_path:
                continue
            if self._quick_filter == "cull" and iid in self._timelapse_by_path:
                continue
            matches = previous_rules.get(iid) if previous_infos.get(iid) == info else None
            if matches is None:
                matches = self.rule_engine.evaluate(info)
            self._rule_cache[iid] = matches
            quality_flags = self._quality_flags_for(info)
            user_tags = self.tag_controller.tags_by_path.get(iid, [])
            if not self._passes_quick_filter(quality_flags, user_tags, picked):
                continue
            if not self._passes_filter(info, matches, quality_flags, user_tags, selected_filters):
                continue
            self.matches_by_iid[iid] = matches
            self.quality_by_iid[iid] = quality_flags
            self._matched_iids.append(iid)
        if self._quick_filter == "duplicates":
            self._matched_iids.sort(key=lambda iid: self.duplicates.by_path[iid].group_id)
        self._refresh_result_rows()
        self._result_page = min(self._result_page, self._page_count() - 1)
        self._render_result_page()
        self._update_sort_button()
        if self._focus_iid:
            if self._focus_iid in self._result_iids:
                self._show_result_detail(self._focus_iid)
            else:
                self.set_detail(None)

    def _refresh_result_rows(self) -> None:
        opened = self._timelapse_paths.get(self._opened_timelapse_id)
        if opened is not None:
            self._result_iids = [path for path in opened if path in self.matches_by_iid]
            self._selected_iids.intersection_update(self._result_iids)
        else:
            self._opened_timelapse_id = None
            self._selected_iids.intersection_update(self.matches_by_iid)
            self._result_iids = self._matched_iids
            if self._fold_timelapses and self._timelapses and self._quick_filter not in ("duplicates", "cull"):
                # Only fold complete stacks so a row never selects files hidden by a filter.
                complete = {
                    stack_id for stack_id, paths in self._timelapse_paths.items()
                    if all(path in self.matches_by_iid for path in paths)
                }
                rows = {}
                for path in self._matched_iids:
                    stack_id = self._timelapse_by_path.get(path)
                    rows[stack_id if stack_id in complete else path] = None
                self._result_iids = list(rows)
        if self._selection_anchor not in self._result_iids:
            self._selection_anchor = ""

    def _passes_quick_filter(
        self, flags: list[QualityFlag], tags: list[Tag], picked: Tag | None,
    ) -> bool:
        if self._quick_filter == "picked":
            return picked is not None and any(tag.id == picked.id for tag in tags)
        if self._quick_filter == "issues":
            return any(flag.flag_id != "duplicate" for flag in flags)
        if self._quick_filter == "duplicates":
            return any(flag.flag_id == "duplicate" for flag in flags)
        return True

    def set_quick_filter(self, key: str) -> None:
        if key != self._quick_filter:
            self._quick_filter = key
            self._opened_timelapse_id = None
            self._result_page = 0
            self.refresh_results()

    def toggle_timelapse_folding(self) -> None:
        self._fold_timelapses = not self._fold_timelapses
        self._result_page = 0
        self.refresh_results()
        self.result_tree.yview_moveto(0)

    def toggle_focused_timelapse(self) -> None:
        if self._focus_iid not in self._timelapses:
            return
        self._opened_timelapse_id = self._focus_iid
        self._timelapse_return_page = self._result_page
        self._result_page = 0
        self.refresh_results()
        self.result_tree.yview_moveto(0)
        self._focus_result_widget()

    def leave_timelapse(self) -> None:
        stack_id = self._opened_timelapse_id
        if stack_id is None:
            return
        self._opened_timelapse_id = None
        self._result_page = self._timelapse_return_page
        self.refresh_results()
        if stack_id in self.result_tree.get_children():
            self._focus_gallery_photo(stack_id)
            self.gallery.see_photo(stack_id)
        self._focus_result_widget()

    def select_focused_timelapse(self) -> None:
        if self._focus_iid not in self._timelapses:
            return
        self.start_result_selection()
        self._selected_iids.update(self._paths_for_row(self._focus_iid))
        self._selection_anchor = self._focus_iid
        self._sync_result_selection()

    def open_focused_timelapse_folder(self) -> None:
        stack = self._timelapses.get(self._focus_iid)
        if stack is not None:
            self._reveal_path(stack.cover.path)

    def _page_count(self) -> int:
        return max(1, (len(self._result_iids) + RESULT_PAGE_SIZE - 1) // RESULT_PAGE_SIZE)

    def change_result_page(self, step: int) -> None:
        page = max(0, min(self._result_page + step, self._page_count() - 1))
        if page == self._result_page:
            return
        self._result_page = page
        self._selection_anchor = ""
        self.set_detail(None)
        self._render_result_page()
        self.result_tree.yview_moveto(0)

    def _render_result_page(self) -> None:
        start = self._result_page * RESULT_PAGE_SIZE
        page = self._result_iids[start:start + RESULT_PAGE_SIZE]
        previous = self.result_tree.get_children()
        removed = set(previous).difference(page)
        if removed:
            self.result_tree.delete(*removed)
        existing = set(previous).difference(removed)
        gallery_photos = []
        picked_tag = self.tag_controller.get_by_name(PICK_TAG_NAME)
        for iid in page:
            stack = self._timelapses.get(iid)
            if stack is not None:
                title = f"延时 · {stack.frame_count:,} 帧"
                cadence = f"每 {stack.cadence_seconds:g} 秒一张"
                gallery_photos.append(GalleryPhoto(stack.cover, status=cadence, iid=iid, title=title))
                values = (
                    title, "延时叠", stack.frames[0].taken_at.isoformat(sep=" "),
                    stack.cover.camera or "", "", "", cadence, "", "", "",
                )
                self._render_result_row(iid, values, [], existing)
                continue
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
            status = quality_text.split(" · ", 1)[0]
            if self._quick_filter == "duplicates":
                status = f"重复组 {self.duplicates.by_path[iid].group_id}"
            if info.exif_status == EXIF_ERROR:
                status = status or "读取失败"
            gallery_photos.append(GalleryPhoto(
                info, picked_tag is not None and any(tag.id == picked_tag.id for tag in user_tags), status,
            ))
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
            self._render_result_row(iid, values, tags, existing)
        if tuple(page) != self.result_tree.get_children():
            self.result_tree.set_children("", *page)
        if not self._folder_paths:
            empty = "添加文件夹，开始整理照片"
        elif self._quick_filter in ("issues", "duplicates") and not self.measurements_by_path:
            empty = "从「更多 → 质量分析」分析照片后，可以在这里复核结果。"
        elif self._quick_filter == "timelapse" and not self._timelapses:
            empty = "这个范围里还没有识别到延时序列"
        elif self._quick_filter != "all" or not self._filter_selection().is_empty:
            empty = "没有符合当前筛选条件的照片"
        elif self._browse_folder and self.checked_folders and self._browse_folder not in self.checked_folders:
            empty = "这个文件夹还没有照片。可在「更多 → 扫描范围」中将它加入扫描。"
        else:
            empty = "这里还没有照片。点击「扫描新文件」读取照片。"
        self.gallery.show_photos(gallery_photos, empty)
        if self._selecting_results:
            self._sync_result_selection()
        else:
            self.gallery.show_selection(self._focus_iid, set())
            self._update_pick_button()
        self._update_page_controls()

    def _render_result_row(self, iid: str, values: tuple, tags: list[str], existing: set[str]) -> None:
        tree = self.result_tree
        if iid not in existing:
            tree.insert("", "end", iid=iid, values=values, tags=tags)
        elif (tuple(map(str, tree.item(iid, "values"))) != tuple(map(str, values))
                or tree.item(iid, "tags") != tuple(tags)):
            tree.item(iid, values=values, tags=tags)

    def _update_page_controls(self) -> None:
        pages = self._page_count()
        self.btn_result_prev.config(state="normal" if self._result_page else "disabled")
        self.btn_result_next.config(state="normal" if self._result_page + 1 < pages else "disabled")
        for button in (self.btn_result_prev, self.btn_result_next):
            if pages == 1:
                button.pack_forget()
        if pages > 1 and not self.btn_result_prev.winfo_manager():
            self.btn_result_prev.pack(side="left", before=self.page_status)
            self.btn_result_next.pack(side="left", padx=(5, 10), before=self.page_status)
        self.page_status.config(text=f"第 {self._result_page + 1} / {pages} 页" if pages > 1 else "")
        self.btn_select_results.config(state="normal" if self._result_iids else "disabled")
        title = self._folder_label(Path(self._browse_folder)) if self._browse_folder else "全部照片"
        stack = self._timelapses.get(self._opened_timelapse_id)
        if stack is not None:
            title = f"延时 · {stack.frame_count:,} 帧"
            count = f"{len(self._result_iids):,} 张"
            self.btn_leave_timelapse.pack(side="left", before=self.gallery_title, padx=(0, 8))
            self.btn_fold_timelapse.config(state="disabled", fg=TEXT_FAINT)
        else:
            count = f"{len(self._matched_iids):,} 张"
            folded = sum(iid in self._timelapses for iid in self._result_iids) if self._timelapses else 0
            if folded:
                count += f" · {folded:,} 叠"
            self.btn_leave_timelapse.pack_forget()
            self.btn_fold_timelapse.config(state="normal", fg=TEXT)
        self.btn_fold_timelapse.config(bg=ACCENT_DIM if self._fold_timelapses else INPUT_BG)
        self.gallery_title.config(text=title if len(title) <= 22 else title[:21] + "…")
        self.count_label.config(text=count)
        self._update_shortcut_hint()
        for key, button in self.quick_filter_buttons.items():
            button.config(bg=ACCENT_DIM if key == self._quick_filter else INPUT_BG)
        self._show_active_filters()

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

    def toggle_picked(self, infos: list[PhotoInfo] | None = None) -> None:
        """把选中照片标记为“精选”；全部已精选时则取消。"""
        infos = self._selected_infos() if infos is None else infos
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
        if self._opened_timelapse_id is not None:
            button.config(text="按拍摄时间", bg=INPUT_BG, fg=TEXT_FAINT, state="disabled")
        elif self.sort_mode == SORT_SHARPNESS:
            button.config(text="清晰度优先 ✓", bg=ACCENT, fg="#ffffff", state="normal")
        else:
            button.config(text="清晰度优先", bg=INPUT_BG, fg=TEXT, state="normal")

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
        if skip_duplicates:
            self.duplicates = DuplicateResult()
        else:
            if self._browse_folder:
                photos = self.db.find_photos_under(f.path for f in self.db.list_folders())
            self.duplicates = rebuild_duplicates(self.quality_store, photos)
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

    def toggle_filters(self) -> None:
        if self.filter_panel.winfo_manager():
            self.filter_panel.pack_forget()
            self.left_canvas.yview_moveto(0)
            self.btn_filters.config(text="筛选")
        else:
            self.filter_panel.pack(fill="x", pady=(0, 10))
            self.btn_filters.config(text="收起筛选")

    def _show_active_filters(self) -> None:
        filters = [
            (label, lambda var=var: self._clear_filter_var(var))
            for label, var in (
                *((label, self.rule_vars[key]) for key, label, _en, _fn in RULE_REGISTRY),
                *((FLAG_LABELS[key], var) for key, var in self.quality_vars.items()),
                ("有 GPS", self.gps_only),
                ("质量正常", self.quality_ok_var),
                ("AI 人物", self.person_filter),
                ("AI 天空", self.sky_filter),
            )
            if var.get()
        ]
        for tag_id in sorted(self.active_tags):
            tag = self.tag_controller.tags_by_id.get(tag_id)
            if tag:
                filters.append((tag.name, lambda tag_id=tag_id: self._clear_tag_filter(tag_id)))
        if not filters:
            self.active_filter_row.pack_forget()
            return
        for child in self.filter_chips.winfo_children():
            child.destroy()
        self.filter_summary.config(text="同时满足" if self.and_mode.get() else "满足其一")
        for label, clear in filters[:2]:
            short_label = label if len(label) <= 12 else label[:11] + "…"
            self._button(self.filter_chips, f"{short_label} ×", clear).pack(side="left", padx=(0, 4))
        if len(filters) > 2:
            self._button(self.filter_chips, f"另 {len(filters) - 2} 项", self._open_filters).pack(side="left")
        self.active_filter_row.pack(fill="x", padx=14, pady=(0, 5), after=self.gallery_controls)

    def _open_filters(self) -> None:
        if not self.filter_panel.winfo_manager():
            self.toggle_filters()

    def _clear_filter_var(self, variable: tk.BooleanVar) -> None:
        variable.set(False)
        self._on_filter_changed()

    def _clear_tag_filter(self, tag_id: int) -> None:
        self.active_tags.discard(tag_id)
        self._refresh_tag_list()
        self.refresh_results()

    def clear_filters(self) -> None:
        self._quick_filter = "all"
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

    def set_result_view(self, mode: str, *, focus: bool = True) -> None:
        self._result_view = mode
        self.grid_frame.pack_forget()
        self.list_frame.pack_forget()
        frame = self.grid_frame if mode == "grid" else self.list_frame
        frame.pack(fill="both", expand=True)
        for key, button in self.view_buttons.items():
            button.config(bg=ACCENT_DIM if key == mode else INPUT_BG)
        if self._focus_iid in self.result_tree.get_children():
            if mode == "grid":
                self.gallery.after_idle(self.gallery.see_photo, self._focus_iid)
            else:
                self.result_tree.focus(self._focus_iid)
                self.result_tree.see(self._focus_iid)
        if focus:
            self._focus_result_widget()
        self._update_shortcut_hint()

    def _update_shortcut_hint(self) -> None:
        hint = ""
        if self._result_view == "grid" and self._result_iids:
            hint = "空格  大图    P  精选" if PILLOW_AVAILABLE else "安装 Pillow 后可查看缩略图"
        self.status_shortcuts.config(text=hint)

    def _focus_result_widget(self) -> None:
        widget = self.gallery if self._result_view == "grid" else self.result_tree
        widget.focus_set()

    def _show_result_detail(self, iid: str) -> None:
        self.set_detail(self.infos_by_iid.get(iid), stack=self._timelapses.get(iid))

    def _focus_gallery_photo(self, iid: str) -> None:
        tree = self.result_tree
        tree.focus(iid)
        if not self._selecting_results:
            if tree.selection() != (iid,):
                tree.selection_set(iid)
        if self._result_view == "list":
            tree.see(iid)
        self._show_result_detail(iid)
        self._update_pick_button()

    def _on_gallery_click(self, event, *, toggle: bool = False, extend: bool = False) -> str:
        iid, checkbox = self.gallery.hit(event.x, event.y)
        if not iid:
            return "break"
        self.gallery.focus_set()
        if not (self._selecting_results or toggle or extend or checkbox):
            self._focus_gallery_photo(iid)
            return "break"
        if not self._selecting_results:
            previous = self._focus_iid
            self.start_result_selection()
            if previous in self.result_tree.get_children():
                self._selection_anchor = previous
                if toggle and previous != iid:
                    self._selected_iids.update(self._paths_for_row(previous))
        self._select_result_row(iid, extend=extend)
        # Clicking the last checked tile returns to regular browsing.
        if checkbox and not self._selected_iids:
            self.cancel_result_selection()
        return "break"

    def _on_gallery_double_click(self, event) -> str:
        iid, checkbox = self.gallery.hit(event.x, event.y)
        if self._selecting_results or checkbox:
            return self._on_gallery_click(event)
        if iid:
            self._focus_gallery_photo(iid)
            if iid in self._timelapses:
                self.toggle_focused_timelapse()
            else:
                self.open_gallery_preview()
        return "break"

    def _on_gallery_menu(self, event) -> None:
        iid, _checkbox = self.gallery.hit(event.x, event.y)
        if iid:
            self._focus_gallery_photo(iid)
            self._post_result_menu(event, iid)

    def _navigate_gallery(self, row: int, column: int) -> str:
        page = self.result_tree.get_children()
        if not page:
            return "break"
        if self._focus_iid in page:
            index = page.index(self._focus_iid) + row * self.gallery.columns + column
        else:
            index = 0
        if index < 0 and self._result_page > 0:
            self.change_result_page(-1)
            page = self.result_tree.get_children()
            index += len(page)
        elif index >= len(page) and self._result_page + 1 < self._page_count():
            index -= len(page)
            self.change_result_page(1)
            page = self.result_tree.get_children()
        iid = page[max(0, min(index, len(page) - 1))]
        self._focus_gallery_photo(iid)
        self.gallery.see_photo(iid)
        return "break"

    def open_gallery_preview(self) -> str:
        page = self.result_tree.get_children()
        if not page:
            return "break"
        if self._focus_iid not in page:
            self._focus_gallery_photo(page[0])
        if self._preview_window is not None:
            self._preview_window.lift()
            return "break"

        viewer = tk.Toplevel(self.root)
        self._preview_window = viewer
        viewer.transient(self.root)
        viewer.configure(bg=WIN_BG)
        width = min(self.root.winfo_screenwidth() - 80, 1060)
        height = min(self.root.winfo_screenheight() - 120, 740)
        viewer.geometry(f"{width}x{height}")
        preview = PreviewCanvas(viewer, (width - 20, height - 70))
        self.large_preview = preview
        controls = tk.Frame(viewer, bg=WIN_BG)
        controls.pack(side="bottom", fill="x", padx=12, pady=8)
        self._button(controls, "上一张", lambda: self._navigate_gallery(0, -1)).pack(side="left", padx=(0, 4))
        self._button(controls, "下一张", lambda: self._navigate_gallery(0, 1)).pack(side="left")
        self._button(controls, "关闭", viewer.destroy).pack(side="right")
        preview.pack(fill="both", expand=True, padx=10)
        viewer.bind("<Left>", lambda _event: self._navigate_gallery(0, -1))
        viewer.bind("<Right>", lambda _event: self._navigate_gallery(0, 1))
        viewer.bind("<p>", lambda _event: self.toggle_picked([self.current] if self.current else []))
        viewer.bind("<P>", lambda _event: self.toggle_picked([self.current] if self.current else []))
        viewer.bind("<Escape>", lambda _event: viewer.destroy())
        viewer.bind("<space>", lambda _event: viewer.destroy())
        viewer.bind("<Destroy>", self._on_preview_destroy)
        stack = self._timelapses.get(self._focus_iid)
        self._update_gallery_preview(stack.cover if stack is not None else self.current)
        viewer.focus_set()
        return "break"

    def _update_gallery_preview(self, info: PhotoInfo | None) -> None:
        if self._preview_window is not None:
            self._preview_window.title(info.file_name if info else "照片预览")
            self.large_preview.show_photo(info)

    def _on_preview_destroy(self, event) -> None:
        if event.widget == self._preview_window:
            self._preview_window = None
            if self.result_frame.winfo_ismapped():
                self._focus_result_widget()

    def toggle_detail_panel(self) -> None:
        if self.detail_panel.winfo_manager():
            self.detail_panel.pack_forget()
        else:
            self.detail_panel.pack(side="right", fill="y", before=self.center)
        self.btn_detail.config(bg=ACCENT_DIM if self.detail_panel.winfo_manager() else INPUT_BG)

    def toggle_detail_parameters(self) -> None:
        if self.detail_parameters.winfo_manager():
            self.detail_parameters.pack_forget()
            self.btn_parameters.config(text="拍摄参数与分析 ▸")
        else:
            self.detail_parameters.pack(fill="x", after=self.btn_parameters)
            self.btn_parameters.config(text="拍摄参数与分析 ▾")

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
                    self._selected_iids.update(self._paths_for_row(previous[0]))
        tree.focus_set()
        self._select_result_row(iid, extend=extend)
        return "break"

    def _select_result_row(self, iid: str, *, extend: bool = False) -> None:
        tree = self.result_tree
        page = tree.get_children()
        if extend and self._selection_anchor in page:
            first, last = sorted((page.index(self._selection_anchor), page.index(iid)))
            self._selected_iids.difference_update(path for row in page for path in self._paths_for_row(row))
            self._selected_iids.update(path for row in page[first:last + 1] for path in self._paths_for_row(row))
        else:
            paths = self._paths_for_row(iid)
            if all(path in self._selected_iids for path in paths):
                self._selected_iids.difference_update(paths)
            else:
                self._selected_iids.update(paths)
            self._selection_anchor = iid
        tree.focus(iid)
        self._show_result_detail(iid)
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
            iid = self.result_tree.identify_row(event.y)
            if iid in self._timelapses:
                self._show_result_detail(iid)
                self.toggle_focused_timelapse()
                return "break"
            self.open_selected()
        return None

    def _on_result_select(self, _event=None) -> None:
        if self._selecting_results:
            return
        selection = self.result_tree.selection()
        if selection and self._focus_iid != selection[0]:
            self._show_result_detail(selection[0])
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
        if self.current is not None:
            self._reveal_path(self.current.path)

    def _reveal_path(self, target: Path) -> None:
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
        folders = self._scan_paths([folder.path for folder in self.db.list_folders()])
        if not folders:
            messagebox.showinfo("开始扫描", "请先添加一个照片文件夹。")
            return
        if full:
            preview = "\n".join(str(path) for path in folders[:8])
            if len(folders) > 8:
                preview += f"\n另有 {len(folders) - 8} 个文件夹"
            if not messagebox.askyesno("全量重扫", f"重新读取以下 {len(folders)} 个文件夹中的照片？\n\n{preview}"):
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
            self._update_scan_controls("扫描中…")
        else:
            self.progress_frame.pack_forget()
            self.result_frame.pack(fill="both", expand=True)
            self._update_scan_controls()

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
        folders = self._scan_paths([folder.path for folder in self.db.list_folders()])
        if not folders:
            messagebox.showinfo("质量分析", "请先添加一个照片文件夹。")
            return
        infos = self.db.find_photos_under(folders)
        if not infos:
            messagebox.showinfo("质量分析", "扫描范围内还没有照片记录，请先扫描。")
            return

        if not messagebox.askyesno(
            "质量分析",
            f"将分析扫描范围内 {len(folders)} 个文件夹中的 {len(infos):,} 张照片（清晰度 / 曝光 / 重复）。\n\n"
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
            self._update_scan_controls("分析中…")
        else:
            self.progress_frame.pack_forget()
            self.result_frame.pack(fill="both", expand=True)
            self._update_scan_controls()
            self.btn_cancel.config(command=self.cancel_scan)

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
