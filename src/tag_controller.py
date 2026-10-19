"""V3 标签状态控制器（纯逻辑，不依赖 Tkinter）。

集中管理：标签列表/计数/关联缓存、批量操作的撤销栈。
GUI 只负责把结果画出来，CLI/测试也可以直接用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from src.tag_store import Tag, TagStore


@dataclass
class _Undo:
    action: str  # "add" | "remove"
    pairs: list[tuple[str, int]] = field(default_factory=list)


class TagController:
    def __init__(self, store: TagStore):
        self.store = store
        self.tags_by_id: dict[int, Tag] = {}
        self.tag_counts: dict[int, int] = {}
        self.tags_by_path: dict[str, list[Tag]] = {}
        self.tag_ids_by_path: dict[str, set[int]] = {}
        self._loaded = False
        self._dirty = True
        self._revision = 0
        self._undo: _Undo | None = None

    # ---- 缓存 ------------------------------------------------------------ #

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def undo_available(self) -> bool:
        return self._undo is not None

    def mark_dirty(self) -> None:
        self._dirty = True
        self._revision += 1

    def ensure_loaded(self) -> None:
        if self._loaded and not self._dirty:
            return
        tags = self.store.list_tags()
        self.tags_by_id = {tag.id: tag for tag in tags}
        self.tag_counts = self.store.counts()
        self.tag_ids_by_path = self.store.assignments()
        self.tags_by_path = {}
        for path, tag_ids in self.tag_ids_by_path.items():
            items = [self.tags_by_id[tag_id] for tag_id in tag_ids if tag_id in self.tags_by_id]
            items.sort(key=lambda tag: tag.name.casefold())
            self.tags_by_path[path] = items
        self._loaded = True
        self._dirty = False

    def sorted_tags(self) -> list[Tag]:
        self.ensure_loaded()
        return sorted(self.tags_by_id.values(), key=lambda tag: tag.name.casefold())

    def tags_for_path(self, path: str) -> list[Tag]:
        self.ensure_loaded()
        return self.tags_by_path.get(path, [])

    def tag_ids_for_path(self, path: str) -> set[int]:
        self.ensure_loaded()
        return self.tag_ids_by_path.get(path, set())

    # ---- 标签 CRUD ------------------------------------------------------- #

    def get_by_name(self, name: str) -> Tag | None:
        return self.store.get_by_name(name)

    def ensure_tag(self, name: str) -> Tag:
        tag = self.store.ensure_tag(name)
        self.mark_dirty()
        return tag

    def create_tag(self, name: str) -> Tag:
        tag = self.store.create_tag(name)
        self.mark_dirty()
        return tag

    def rename_tag(self, tag_id: int, name: str) -> Tag:
        tag = self.store.rename_tag(tag_id, name)
        self.clear_undo()
        self.mark_dirty()
        return tag

    def delete_tag(self, tag_id: int) -> int:
        removed = self.store.delete_tag(tag_id)
        self.clear_undo()
        self.mark_dirty()
        return removed

    # ---- 批量关联 / 撤销 ------------------------------------------------- #

    def apply_add(
        self, paths: Iterable[str], tag_ids: Iterable[int]
    ) -> tuple[list[tuple[str, int]], list[str]]:
        added, unknown = self.store.add_tags(paths, tag_ids)
        if added:
            self._undo = _Undo("add", added)
            self.mark_dirty()
        return added, unknown

    def apply_remove(
        self, paths: Iterable[str], tag_ids: Iterable[int]
    ) -> list[tuple[str, int]]:
        removed = self.store.remove_tags(paths, tag_ids)
        if removed:
            self._undo = _Undo("remove", removed)
            self.mark_dirty()
        return removed

    def add_single(self, path: str, tag_id: int) -> list[tuple[str, int]]:
        added, _unknown = self.store.add_tags([path], [tag_id])
        self.clear_undo()
        self.mark_dirty()
        return added

    def remove_single(self, path: str, tag_id: int) -> list[tuple[str, int]]:
        removed = self.store.remove_tags([path], [tag_id])
        self.clear_undo()
        self.mark_dirty()
        return removed

    def undo_last(self) -> str | None:
        """撤销最近一次批量操作，返回提示文本；没有可撤销时返回 None。"""
        if self._undo is None:
            return None
        action, pairs = self._undo.action, list(self._undo.pairs)
        self._undo = None
        if action == "add":
            self.store.remove_assignments(pairs)
            message = "已撤销上一次批量添加标签"
        else:
            self.store.add_assignments(pairs)
            message = "已撤销上一次批量移除标签"
        self.mark_dirty()
        return message

    def clear_undo(self) -> None:
        self._undo = None
