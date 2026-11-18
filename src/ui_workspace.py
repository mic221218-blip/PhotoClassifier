"""Desktop project navigation, saved browsing state and filter presets."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from src.filtering import FilterSelection
from src.photo_scope import source_photos
from src.ui_theme import PANEL_BG, TEXT, TEXT_SUB


def _position(value, default):
    return tuple(value) if isinstance(value, list) and len(value) == len(default) else default


class WorkspaceMixin:
    def _init_workspace(self) -> None:
        saved = self.db.desktop_setting("workspace")
        self._workspace_views = saved.get("views", {})
        self._presets = self.db.desktop_setting("filter_presets")
        self._projects = {item.id: item for item in self.db.list_projects()}
        self._project_id = saved.get("project")
        if self._project_id not in self._projects:
            self._project_id = None
        self._position_after_id: str | None = None

    @property
    def active_project(self):
        return self._projects.get(self._project_id)

    def _catalog_folders(self):
        folders = self.db.list_folders()
        if self._project_id is None:
            return folders
        paths = self.db.project_folder_paths(self._project_id)
        return [item for item in folders if str(item.path) in paths]

    def _view_photos(self):
        folders = [Path(self._browse_folder)] if self._browse_folder else [f.path for f in self._catalog_folders()]
        return source_photos(self.db, folders, pick_list_id=self._opened_pick_id)

    def _build_projects(self, parent) -> None:
        header = tk.Frame(parent, bg=PANEL_BG)
        header.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(header, text="项目", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9, bold=True)).pack(side="left")
        manage, self.project_menu = self._menu_button(header, "管理 ▾")
        manage.pack(side="right")
        for label, action in (
            ("新建项目…", self.create_project),
            ("编辑项目…", self.edit_project),
            ("移入选片清单…", self.move_pick_list_to_project),
            ("移除项目…", self.remove_project),
        ):
            self.project_menu.add_command(label=label, command=action)
        self.project_choice = ttk.Combobox(parent, state="readonly", style="PickList.TCombobox")
        self.project_choice.pack(fill="x", padx=12, pady=(0, 5))
        self.project_choice.bind("<<ComboboxSelected>>", self._on_project_selected)
        self._refresh_projects()

    def _refresh_projects(self) -> None:
        self._projects = {item.id: item for item in self.db.list_projects()}
        ids = (None, *self._projects)
        self.project_choice.config(values=("全部图库", *(item.name for item in self._projects.values())))
        self.project_choice.current(ids.index(self._project_id) if self._project_id in ids else 0)
        for index in range(1, 4):
            self.project_menu.entryconfigure(index, state="normal" if self.active_project else "disabled")

    def _on_project_selected(self, _event=None) -> None:
        index = self.project_choice.current()
        if index >= 0:
            self.switch_project((None, *self._projects)[index])

    def switch_project(self, project_id: int | None) -> None:
        if project_id == self._project_id or (project_id is not None and project_id not in self._projects):
            return
        self._remember_workspace()
        self._project_id = project_id
        self._refresh_projects()
        self._restore_workspace_view()
        self._save_workspace()

    def _edit_project_dialog(self, project=None) -> bool:
        folders = self.db.list_folders()
        selected = self.db.project_folder_paths(project.id) if project else {self._browse_folder}
        dialog = _ProjectDialog(self, folders, selected, project)
        if dialog.result is None:
            return False
        self._refresh_projects()
        if project:
            self.refresh_library()
            self.refresh_results()
        else:
            self.switch_project(dialog.result)
        return True

    def create_project(self) -> None:
        self._edit_project_dialog()

    def edit_project(self) -> None:
        if self.active_project:
            self._edit_project_dialog(self.active_project)

    def remove_project(self) -> None:
        project = self.active_project
        if project is None or not messagebox.askyesno(
            "移除项目", f"移除「{project.name}」？照片和选片清单仍可在全部图库访问。", parent=self.root,
        ):
            return
        self.db.remove_project(project.id)
        self.switch_project(None)
        self._workspace_views.pop(str(project.id), None)
        self._save_workspace()

    def move_pick_list_to_project(self) -> None:
        project = self.active_project
        if project is None:
            return
        existing = {item.id for item in self.db.list_pick_lists(project_id=project.id)}
        choices = {item.name: item.id for item in self.db.list_pick_lists() if item.id not in existing}
        if not choices:
            messagebox.showinfo("移入选片清单", "没有其他清单可移入。", parent=self.root)
            return
        choice = self._choose_workspace_item(
            "移入选片清单", f"将选片清单移到「{project.name}」", choices, action="移入",
        )
        if choice is not None:
            self.db.assign_pick_list(choices[choice], project.id)
            self.refresh_results()

    def _choose_workspace_item(self, title, prompt, choices, *, action="继续") -> str | None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.config(bg=PANEL_BG)
        tk.Label(dialog, text=prompt, bg=PANEL_BG, fg=TEXT).pack(padx=16, pady=(12, 6))
        selection = ttk.Combobox(dialog, state="readonly", values=tuple(choices), width=30)
        selection.current(0)
        selection.pack(padx=16, pady=6)
        choice = None

        def confirm():
            nonlocal choice
            choice = selection.get()
            dialog.destroy()

        footer = tk.Frame(dialog, bg=PANEL_BG)
        footer.pack(fill="x", padx=16, pady=(4, 12))
        self._button(footer, action, confirm, primary=True).pack(side="right")
        self._button(footer, "取消", dialog.destroy).pack(side="right", padx=6)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        selection.focus_set()
        dialog.wait_window()
        return choice

    def _remember_workspace(self) -> None:
        self._workspace_views[str(self._project_id or 0)] = {
            "folder": self._browse_folder, "list": self._opened_pick_id, "target": self._active_pick_id,
            "filters": self._filter_selection().to_settings(), "quick_filter": self._quick_filter,
            "view": self._result_view, "page": self._result_page, "focus": self._focus_iid,
            "stack": self._opened_timelapse_id, "browse_position": self._browse_position,
            "scroll": (self.gallery.yview()[0], self.result_tree.yview()[0]),
            "scan_folders": sorted(self.checked_folders),
        }

    def _save_workspace(self) -> None:
        self.db.write_desktop_setting("workspace", {"project": self._project_id, "views": self._workspace_views})

    def _restore_workspace_view(self) -> None:
        saved = self._workspace_views.get(str(self._project_id or 0), {})
        self._opened_pick_id = None
        if self._preview_window is not None:
            self._preview_window.destroy()
        if self._selecting_results:
            self.cancel_result_selection()
        old_selection = self.result_tree.selection()
        if old_selection:
            self.result_tree.selection_remove(*old_selection)
        self.result_tree.focus("")
        self.set_detail(None)
        self._browse_folder = saved.get("folder")
        self.checked_folders = set(saved.get("scan_folders", []))
        self._active_pick_id = saved.get("target")
        self.refresh_library()
        self.refresh_pick_lists()
        if saved.get("list") in self._pick_lists:
            self._opened_pick_id = saved["list"]
        elif saved.get("list") is not None:
            self._browse_folder = None
        self._browse_position = _position(saved.get("browse_position"), (0, "", None, 0, 0, False))
        if self._opened_pick_id is None:
            self._browse_position = None
        elif self.filter_panel.winfo_manager():
            self.toggle_filters()
        self._quick_filter = saved.get("quick_filter", "all")
        if self._quick_filter not in self.quick_filter_buttons:
            self._quick_filter = "all"
        self._apply_filter_selection(FilterSelection.from_settings(saved.get("filters", {})))
        self._result_page = max(0, saved.get("page", 0))
        self._opened_timelapse_id = saved.get("stack") if self._opened_pick_id is None else None
        self.set_result_view("list" if saved.get("view") == "list" else "grid", focus=False)
        self._sync_library_selection()
        self.refresh_results()
        self._restore_result_position(saved.get("focus", ""), *_position(saved.get("scroll"), (0, 0)))

    def _restore_result_position(self, focus: str, grid_scroll: float, list_scroll: float) -> None:
        self._cancel_position_restore()
        if focus in self.result_tree.get_children():
            self._focus_gallery_photo(focus)
        self.result_tree.yview_moveto(list_scroll)
        source = self._project_id, self._browse_folder, self._opened_pick_id, self._result_page

        def restore_grid():
            self._position_after_id = None
            if source == (self._project_id, self._browse_folder, self._opened_pick_id, self._result_page):
                self.gallery.scroll("moveto", grid_scroll)

        self._position_after_id = self.gallery.after_idle(restore_grid)

    def _cancel_position_restore(self) -> None:
        if self._position_after_id is not None:
            self.gallery.after_cancel(self._position_after_id)
            self._position_after_id = None

    def _apply_filter_selection(self, selection: FilterSelection) -> None:
        for variables, ids in (
            (self.rule_vars, selection.rule_ids), (self.quality_vars, selection.quality_ids),
            (self.ai_filters, selection.selected_ai),
        ):
            for key, var in variables.items():
                var.set(key in ids)
        self.active_rules = self.rule_vars.keys() & selection.rule_ids
        self.active_quality = self.quality_vars.keys() & selection.quality_ids
        self._ensure_tag_cache()
        self.active_tags = self.tag_controller.tags_by_id.keys() & selection.tag_ids
        self.and_mode.set(selection.and_mode)
        self.raw_only.set(selection.raw_only)
        self.gps_only.set(selection.gps_only)
        self.quality_ok_var.set(selection.quality_ok)
        self._refresh_tag_list()

    def _build_filter_presets(self, parent) -> None:
        button, self.preset_menu = self._menu_button(parent, "筛选预设 ▾")
        button.pack(anchor="w", padx=12, pady=(0, 6))
        self._refresh_preset_menu()

    def _refresh_preset_menu(self) -> None:
        menu = self.preset_menu
        menu.delete(0, "end")
        menu.add_command(label="保存当前筛选…", command=self.save_filter_preset)
        state = "normal" if self._presets else "disabled"
        menu.add_command(label="重命名预设…", command=self.rename_filter_preset, state=state)
        menu.add_command(label="删除预设…", command=self.delete_filter_preset, state=state)
        if self._presets:
            menu.add_separator()
        for name in self._presets:
            menu.add_command(label=name, command=lambda item=name: self.apply_filter_preset(item))

    def _preset_name(self, title, current=None):
        name = simpledialog.askstring(title, "预设名称", initialvalue=current or "", parent=self.root)
        if not name or not name.strip():
            return None
        name = name.strip()
        conflict = next((key for key in self._presets if key != current and key.casefold() == name.casefold()), None)
        if conflict and not messagebox.askyesno(title, f"替换预设「{conflict}」？", parent=self.root):
            return None
        if conflict:
            del self._presets[conflict]
        return name

    def _save_presets(self) -> None:
        self.db.write_desktop_setting("filter_presets", self._presets)
        self._refresh_preset_menu()

    def save_filter_preset(self) -> None:
        name = self._preset_name("保存当前筛选")
        if name:
            self._presets[name] = {"filters": self._filter_selection().to_settings(), "scope": self._quick_filter}
            self._save_presets()

    def rename_filter_preset(self) -> None:
        current = self._choose_workspace_item("重命名预设", "选择预设", self._presets)
        if current in self._presets and (name := self._preset_name("重命名预设", current)):
            self._presets[name] = self._presets.pop(current)
            self._save_presets()

    def delete_filter_preset(self) -> None:
        name = self._choose_workspace_item("删除预设", "选择预设", self._presets)
        if name in self._presets and messagebox.askyesno("删除预设", f"删除「{name}」？", parent=self.root):
            del self._presets[name]
            self._save_presets()

    def apply_filter_preset(self, name: str) -> None:
        preset = self._presets.get(name)
        if preset is not None and self._opened_pick_id is None:
            self._apply_filter_selection(FilterSelection.from_settings(preset["filters"]))
            if preset["scope"] != self._quick_filter:
                self.set_quick_filter(preset["scope"])
            else:
                self.refresh_results()


class _ProjectDialog(simpledialog.Dialog):
    def __init__(self, app, folders, selected, project):
        self._db = app.db
        self._paths = [str(folder.path) for folder in folders]
        self._selected = selected
        self._project = project
        super().__init__(app.root, "编辑项目" if project else "新建项目")

    def body(self, parent):
        ttk.Label(parent, text="项目名称").pack(anchor="w")
        self.name = ttk.Entry(parent, width=50)
        self.name.pack(fill="x", pady=(2, 10))
        if self._project:
            self.name.insert(0, self._project.name)
        ttk.Label(parent, text="素材文件夹").pack(anchor="w")
        rows = ttk.Frame(parent)
        rows.pack(fill="both", expand=True)
        self.folders = tk.Listbox(rows, selectmode="multiple", exportselection=False, height=max(2, min(10, len(self._paths))))
        scroll = ttk.Scrollbar(rows, orient="vertical", command=self.folders.yview)
        self.folders.config(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.folders.pack(side="left", fill="both", expand=True)
        for index, path in enumerate(self._paths):
            self.folders.insert("end", path)
            if path in self._selected:
                self.folders.selection_set(index)
        return self.name

    def validate(self):
        try:
            self.result = self._db.save_project(
                self.name.get(), (self._paths[index] for index in self.folders.curselection()),
                project_id=self._project.id if self._project else None,
            )
        except ValueError as exc:
            messagebox.showerror(self.title(), str(exc), parent=self)
            return False
        return True
