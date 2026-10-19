"""V3 标签相关对话框与批量操作（Tk 界面层，逻辑委托给 TagController）。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from src.tag_store import Tag
from src.tags import TagError, normalize_tag_name
from src.ui_theme import ACCENT_DIM, CARD_BG, INPUT_BG, PANEL_BG, TEXT, TEXT_SUB


class TagDialogsMixin:
    """给主窗口使用的标签对话框 / 批量菜单。

    依赖宿主提供：``root``、``_font``、``_button``、``tag_store``、
    ``tag_controller``、``infos_by_iid``、``result_tree``、``status_left``、
    ``_selected_infos``、``_after_tag_change``、``open_selected``、
    ``reveal_selected``、``copy_selected_path``。
    """

    def _ask_tag_choice(
        self,
        title: str,
        prompt: str,
        *,
        allowed: set[str] | None = None,
    ) -> str | None:
        """一个带搜索的标签选择小窗口；返回名称或 None。"""
        window = tk.Toplevel(self.root)
        window.title(title)
        window.configure(bg=PANEL_BG)
        window.transient(self.root)
        window.geometry("360x320")
        tk.Label(
            window,
            text=prompt,
            bg=PANEL_BG,
            fg=TEXT_SUB,
            font=self._font(9),
            wraplength=330,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(12, 6))
        variable = tk.StringVar()
        entry = tk.Entry(
            window,
            textvariable=variable,
            bg=INPUT_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=self._font(10),
        )
        entry.pack(fill="x", padx=14, ipady=4)
        listbox = tk.Listbox(
            window,
            bg=INPUT_BG,
            fg=TEXT,
            selectbackground=ACCENT_DIM,
            selectforeground=TEXT,
            relief="flat",
            font=self._font(9),
            highlightthickness=0,
        )
        listbox.pack(fill="both", expand=True, padx=14, pady=(8, 0))
        result: dict[str, str | None] = {"name": None}

        def refresh_list(*_args) -> None:
            query = variable.get().strip().casefold()
            listbox.delete(0, "end")
            for tag in self.tag_store.list_tags():
                if allowed is not None and tag.name not in allowed:
                    continue
                if query and query not in tag.name.casefold():
                    continue
                listbox.insert("end", tag.name)

        variable.trace_add("write", refresh_list)
        refresh_list()

        def choose(_event=None) -> None:
            selected = listbox.curselection()
            if selected:
                variable.set(listbox.get(selected[0]))
            name = variable.get().strip()
            if not name:
                return
            result["name"] = name
            window.destroy()

        listbox.bind("<Double-Button-1>", choose)
        entry.bind("<Return>", choose)
        buttons = tk.Frame(window, bg=PANEL_BG)
        buttons.pack(fill="x", padx=14, pady=10)
        self._button(buttons, "确定", choose, primary=True).pack(side="right", ipady=1)
        self._button(buttons, "取消", window.destroy).pack(side="right", padx=(0, 6), ipady=1)
        entry.focus_set()
        window.grab_set()
        window.wait_window()
        return result["name"]

    def _show_result_menu(self, event) -> None:
        iid = self.result_tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.result_tree.selection():
            self.result_tree.selection_set(iid)
        count = len(self._selected_infos())
        menu = tk.Menu(self.root, tearoff=0, bg=CARD_BG, fg=TEXT, activebackground=ACCENT_DIM)
        menu.add_command(label="打开图片", command=self.open_selected)
        menu.add_command(label="打开文件位置", command=self.reveal_selected)
        menu.add_separator()
        menu.add_command(label=f"添加标签…（{count} 张）", command=self.batch_add_tags)
        menu.add_command(label=f"移除标签…（{count} 张）", command=self.batch_remove_tags)
        menu.add_separator()
        menu.add_command(label="复制路径", command=self.copy_selected_path)
        menu.tk_popup(event.x_root, event.y_root)

    def batch_add_tags(self) -> None:
        infos = self._selected_infos()
        if not infos:
            return
        name = self._ask_tag_choice(
            "批量添加标签", f"给选中的 {len(infos)} 张照片添加哪个标签？"
        )
        if not name:
            return
        try:
            normalized = normalize_tag_name(name)
        except TagError as exc:
            self.status_left.config(text=str(exc))
            return
        tag = self.tag_store.get_by_name(normalized)
        if tag is None:
            if not messagebox.askyesno(
                "新建标签", f"标签「{normalized}」不存在，创建并添加？"
            ):
                return
            try:
                tag = self.tag_store.create_tag(normalized)
            except TagError as exc:
                self.status_left.config(text=str(exc))
                return
        if not messagebox.askyesno(
            "批量添加标签",
            f"将给 {len(infos)} 张照片添加标签「{tag.name}」。\n\n继续？",
        ):
            return
        added, unknown = self.tag_controller.apply_add(
            [str(info.path) for info in infos], [tag.id]
        )
        message = f"已给 {len(infos)} 张照片添加标签「{tag.name}」（新增 {len(added)} 条）"
        if unknown:
            message += f"，跳过 {len(unknown)} 个未知路径"
        self._after_tag_change(message)

    def batch_remove_tags(self) -> None:
        infos = self._selected_infos()
        if not infos:
            return
        common: set[int] = set()
        for index, info in enumerate(infos):
            ids = set(self.tag_controller.tag_ids_by_path.get(str(info.path), set()))
            common = ids if index == 0 else common & ids
        if not common:
            messagebox.showinfo("批量移除标签", "选中的照片没有共同标签。")
            return
        names = {
            self.tag_controller.tags_by_id[tag_id].name
            for tag_id in common
            if tag_id in self.tag_controller.tags_by_id
        }
        if not names:
            return
        name = self._ask_tag_choice(
            "批量移除标签",
            f"从选中的 {len(infos)} 张照片移除哪个标签？（只列出共同标签）",
            allowed=names,
        )
        if not name:
            return
        tag = self.tag_store.get_by_name(name)
        if tag is None or tag.id not in common:
            return
        if not messagebox.askyesno(
            "批量移除标签",
            f"将从 {len(infos)} 张照片移除标签「{tag.name}」（不会删除照片）。\n\n继续？",
        ):
            return
        removed = self.tag_controller.apply_remove(
            [str(info.path) for info in infos], [tag.id]
        )
        self._after_tag_change(
            f"已从 {len(infos)} 张照片移除标签「{tag.name}」（{len(removed)} 条）"
        )

    def open_tag_manager(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("标签管理")
        window.configure(bg=PANEL_BG)
        window.transient(self.root)
        window.geometry("520x420")
        tree = ttk.Treeview(
            window, columns=("name", "count", "created"), show="headings", selectmode="browse"
        )
        for key, title, width in (
            ("name", "标签", 180),
            ("count", "照片数", 80),
            ("created", "创建时间", 160),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="w")
        tree.pack(fill="both", expand=True, padx=14, pady=(14, 8))

        def refresh() -> None:
            tree.delete(*tree.get_children())
            counts = self.tag_store.counts()
            for tag in self.tag_store.list_tags():
                tree.insert(
                    "",
                    "end",
                    iid=str(tag.id),
                    values=(tag.name, counts.get(tag.id, 0), tag.created_at),
                )

        def selected_tag() -> Tag | None:
            selection = tree.selection()
            if not selection:
                return None
            return self.tag_store.get(int(selection[0]))

        def create() -> None:
            name = simpledialog.askstring("新建标签", "标签名：", parent=window)
            if not name:
                return
            try:
                self.tag_store.create_tag(name)
            except TagError as exc:
                messagebox.showwarning("新建标签", str(exc), parent=window)
                return
            refresh()

        def rename() -> None:
            tag = selected_tag()
            if tag is None:
                return
            name = simpledialog.askstring(
                "重命名标签", "新名称：", initialvalue=tag.name, parent=window
            )
            if not name:
                return
            try:
                self.tag_store.rename_tag(tag.id, name)
            except TagError as exc:
                messagebox.showwarning("重命名标签", str(exc), parent=window)
                return
            refresh()

        def delete() -> None:
            tag = selected_tag()
            if tag is None:
                return
            count = self.tag_store.counts().get(tag.id, 0)
            if not messagebox.askyesno(
                "删除标签",
                f"标签「{tag.name}」将从 {count} 张照片移除（不会删除照片）。\n\n继续？",
                parent=window,
            ):
                return
            self.tag_store.delete_tag(tag.id)
            refresh()

        buttons = tk.Frame(window, bg=PANEL_BG)
        buttons.pack(fill="x", padx=14, pady=(0, 12))
        self._button(buttons, "新建", create).pack(side="left", ipady=1)
        self._button(buttons, "重命名", rename).pack(side="left", padx=(6, 0), ipady=1)
        self._button(buttons, "删除", delete).pack(side="left", padx=(6, 0), ipady=1)
        self._button(buttons, "关闭", window.destroy).pack(side="right", ipady=1)
        refresh()
        window.grab_set()
        window.wait_window()
        self.tag_controller.clear_undo()
        self._after_tag_change("标签已更新")
