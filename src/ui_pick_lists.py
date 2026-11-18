"""多份选片清单的浏览、加入目标和单步撤销。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from src.database import PickList
from src.photo_info import PhotoInfo
from src.ui_theme import ACCENT, BORDER_SOFT, INPUT_BG, PANEL_BG, TEXT, TEXT_FAINT, TEXT_SUB


class PickListsMixin:
    def _init_pick_lists(self) -> None:
        self._pick_lists: dict[int, PickList] = {}
        self._opened_pick_id: int | None = None
        self._active_pick_id: int | None = None
        self._active_pick_paths: set[str] = set()
        self._pick_undo: tuple[int, bool, list[str]] | None = None
        self._browse_position: tuple[int, str, str | None, float, float, bool] | None = None

    @property
    def opened_pick_list(self) -> PickList | None:
        return self._pick_lists.get(self._opened_pick_id)

    def _build_pick_lists(self, parent) -> None:
        header = tk.Frame(parent, bg=PANEL_BG)
        header.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(header, text="选片清单", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9, bold=True)).pack(side="left")
        self._button(header, "新建", self.create_pick_list).pack(side="right")
        manage, self.pick_list_menu = self._menu_button(header, "管理 ▾")
        manage.pack(side="right", padx=(0, 3))
        self.pick_list_menu.add_command(label="重命名当前清单…", command=self.rename_pick_list, state="disabled")
        self.pick_list_menu.add_command(label="删除当前清单…", command=self.delete_pick_list, state="disabled")

        rows = tk.Frame(parent, bg=PANEL_BG)
        rows.pack(fill="x", padx=12)
        self.pick_list_tree = ttk.Treeview(rows, columns=("count",), show="tree", selectmode="browse", height=3)
        self.pick_list_tree.column("#0", width=130, minwidth=0)
        self.pick_list_tree.column("count", width=42, minwidth=36, stretch=False, anchor="e")
        scroll = ttk.Scrollbar(rows, orient="vertical", command=self.pick_list_tree.yview)
        self.pick_list_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.pick_list_tree.pack(side="left", fill="x", expand=True)
        self.pick_list_tree.bind("<<TreeviewSelect>>", self._on_pick_list_select)
        self.pick_list_tree.bind("<MouseWheel>", self._on_sidebar_wheel)

    def _build_pick_actions(self, parent) -> None:
        row = tk.Frame(parent, bg=PANEL_BG)
        row.pack(side="bottom", fill="x", padx=12, pady=(4, 0))
        self.btn_remove_pick = self._button(row, "移出当前清单", self.remove_from_pick_list)
        self.btn_pick = self._button(row, "加入清单 (P)", self.add_to_pick_list, primary=True, disabled=True)
        self.btn_pick.pack(side="right", padx=(6, 8), pady=5)
        self.pick_target = tk.StringVar(self.root)
        style = ttk.Style(self.root)
        style.configure("PickList.TCombobox", padding=4, arrowcolor=TEXT_SUB, bordercolor=BORDER_SOFT)
        style.map(
            "PickList.TCombobox",
            fieldbackground=[("readonly", INPUT_BG), ("disabled", INPUT_BG)],
            foreground=[("readonly", TEXT), ("disabled", TEXT_FAINT)],
        )
        self.pick_target_box = ttk.Combobox(
            row, textvariable=self.pick_target, state="readonly", width=19,
            style="PickList.TCombobox", font=self._font(9),
        )
        self.pick_target_box.pack(side="right")
        self.pick_target_box.bind("<<ComboboxSelected>>", self._on_pick_target_select)
        tk.Label(row, text="加入到", bg=PANEL_BG, fg=TEXT_SUB, font=self._font(9)).pack(side="right", padx=6)

    def refresh_pick_lists(self) -> None:
        lists = self.db.list_pick_lists()
        self._pick_lists = {item.id: item for item in lists}
        if self._active_pick_id not in self._pick_lists:
            self._active_pick_id = lists[0].id if lists else None
        self._active_pick_paths = self.db.pick_list_paths(self._active_pick_id)

        rows = self.pick_list_tree
        rows.configure(height=max(1, min(len(lists), 3)))
        desired = tuple(str(item.id) for item in lists)
        for iid in set(rows.get_children()).difference(desired):
            rows.delete(iid)
        for item in lists:
            iid = str(item.id)
            count = f"{item.photo_count:,}"
            if not rows.exists(iid):
                rows.insert("", "end", iid=iid, text=item.name, values=(count,))
            elif rows.item(iid, "text") != item.name or str(rows.set(iid, "count")) != count:
                rows.item(iid, text=item.name, values=(count,))
        selection = (str(self._opened_pick_id),) if self.opened_pick_list else ()
        if rows.selection() != selection:
            rows.selection_set(selection)
        self.pick_target_box.configure(
            values=tuple(item.name for item in lists), state="readonly" if lists else "disabled",
        )
        active = self._pick_lists.get(self._active_pick_id)
        self.pick_target.set(active.name if active else "请先新建清单")
        state = "normal" if selection else "disabled"
        for entry in range(2):
            self.pick_list_menu.entryconfigure(entry, state=state)

    def _on_pick_list_select(self, _event=None) -> None:
        selection = self.pick_list_tree.selection()
        if selection:
            self.open_pick_list(int(selection[0]))

    def _on_pick_target_select(self, _event=None) -> None:
        name = self.pick_target.get()
        self._active_pick_id = next((item.id for item in self._pick_lists.values() if item.name == name), None)
        self.refresh_results()
        self._focus_result_widget()

    def open_pick_list(self, list_id: int) -> None:
        if list_id == self._opened_pick_id or list_id not in self._pick_lists:
            return
        if self._opened_pick_id is None:
            self._browse_position = (
                self._result_page, self._focus_iid, self._opened_timelapse_id,
                self.gallery.yview()[0], self.result_tree.yview()[0],
                bool(self.filter_panel.winfo_manager()),
            )
            if self.filter_panel.winfo_manager():
                self.toggle_filters()
        if self._selecting_results:
            self.cancel_result_selection()
        self._opened_pick_id = list_id
        self._opened_timelapse_id = None
        self._result_page = 0
        self.set_detail(None)
        self._sync_library_selection()
        self.refresh_results()
        self.gallery.yview_moveto(0)
        self.result_tree.yview_moveto(0)

    def leave_pick_list(self, *, restore_position: bool = True) -> None:
        if self._opened_pick_id is None:
            return
        if self._selecting_results:
            self.cancel_result_selection()
        self._opened_pick_id = None
        assert self._browse_position is not None
        page, focus, stack, grid_scroll, list_scroll, filters_open = self._browse_position
        self._browse_position = None
        self._result_page = page if restore_position else 0
        self._opened_timelapse_id = stack if restore_position else None
        self.set_detail(None)
        self._sync_library_selection()
        self.refresh_results()
        if filters_open:
            self.toggle_filters()
        if restore_position:
            if focus in self.result_tree.get_children():
                self._focus_gallery_photo(focus)
            self.result_tree.yview_moveto(list_scroll)
            folder = self._browse_folder

            def restore_grid_scroll():
                if self._opened_pick_id is None and self._browse_folder == folder and self._result_page == page:
                    self.gallery.scroll("moveto", grid_scroll)

            self.gallery.after_idle(restore_grid_scroll)

    def create_pick_list(self) -> None:
        name = simpledialog.askstring("新建选片清单", "清单名称", parent=self.root)
        if name is None:
            return
        try:
            self._active_pick_id = self.db.create_pick_list(name)
        except ValueError as exc:
            messagebox.showerror("新建选片清单", str(exc), parent=self.root)
            return
        self.refresh_results()
        self.status_left.config(text=f"当前加入目标为「{self.pick_target.get()}」")

    def rename_pick_list(self) -> None:
        item = self.opened_pick_list
        if item is None:
            return
        name = simpledialog.askstring("重命名选片清单", "清单名称", initialvalue=item.name, parent=self.root)
        if name is None:
            return
        try:
            self.db.rename_pick_list(item.id, name)
        except ValueError as exc:
            messagebox.showerror("重命名选片清单", str(exc), parent=self.root)
            return
        self.refresh_results()

    def delete_pick_list(self) -> None:
        item = self.opened_pick_list
        if item is None or not messagebox.askyesno(
            "删除选片清单", f"删除清单「{item.name}」？原片和其他清单中的记录会保留。", parent=self.root,
        ):
            return
        self.db.delete_pick_list(item.id)
        if self._pick_undo and self._pick_undo[0] == item.id:
            self._pick_undo = None
        self.leave_pick_list()
        self.status_left.config(text=f"已删除清单「{item.name}」")

    def _update_pick_button(self) -> None:
        paths = [str(info.path) for info in self._selected_infos()]
        can_add = bool(set(paths) - self._active_pick_paths)
        label = "已加入目标清单" if paths and not can_add else "加入清单 (P)"
        if self._active_pick_id is None:
            can_add = False
        self.btn_pick.config(
            text=label, state="normal" if can_add else "disabled",
            bg=ACCENT if can_add else INPUT_BG, fg="#ffffff" if can_add else TEXT_FAINT,
        )
        if self.opened_pick_list:
            self.btn_remove_pick.pack(side="left", padx=8, pady=5)
            self.btn_remove_pick.config(state="normal" if paths else "disabled")
        else:
            self.btn_remove_pick.pack_forget()
        self._update_detail_pick_button()

    def _update_detail_pick_button(self) -> None:
        joined = self.current is not None and str(self.current.path) in self._active_pick_paths
        enabled = self.current is not None and self._active_pick_id is not None and not joined
        self.btn_current_pick.config(
            text="已加入目标清单" if joined else "加入目标清单",
            state="normal" if enabled else "disabled",
            fg=TEXT if enabled else TEXT_FAINT,
        )

    def _on_pick_key(self, _event=None) -> str:
        self.add_to_pick_list()
        return "break"

    def add_to_pick_list(self, infos: list[PhotoInfo] | None = None) -> None:
        self._change_pick_membership(self._active_pick_id, infos, add=True)

    def remove_from_pick_list(self) -> None:
        self._change_pick_membership(self._opened_pick_id, None, add=False)

    def _change_pick_membership(self, list_id: int | None, infos: list[PhotoInfo] | None, *, add: bool) -> None:
        item = self._pick_lists.get(list_id)
        infos = self._selected_infos() if infos is None else infos
        if item is None or not infos:
            return
        changed = self.db.change_pick_list(item.id, (info.path for info in infos), add=add)
        if changed:
            self.tag_controller.clear_undo()
            self._pick_undo = (item.id, add, changed)
            self.refresh_results()
            if add:
                message = f"已加入 {len(changed):,} 张到「{item.name}」"
            else:
                message = f"已从「{item.name}」移出 {len(changed):,} 张"
            self.status_left.config(text=message)

    def _undo_pick_change(self) -> bool:
        if self._pick_undo is None:
            return False
        list_id, added, paths = self._pick_undo
        self.db.change_pick_list(list_id, paths, add=not added)
        self._pick_undo = None
        self.refresh_results()
        self.status_left.config(text="已撤销上一次清单变更")
        return True
