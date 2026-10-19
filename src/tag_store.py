"""PhotoClassifier V3：标签持久化（``tags`` / ``photo_tags``）。

只负责数据库读写；名称规范化与颜色在 :mod:`src.tags`，
筛选判定在 :mod:`src.filtering`。所有操作只写数据库，不碰照片文件。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from src.database import TAGS_SCHEMA
from src.tags import TagConflict, TagError, color_for, normalize_tag_name, tag_key


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _resolve_path(path: str | Path) -> str:
    """写入/精确查找用：与 ``photos.path`` 的存储方式保持一致。"""
    return str(Path(path).resolve())


def _db_path(path: str | Path) -> str:
    """热路径用：数据库里的路径已经是绝对路径，不再触发文件系统 resolve。

    仅用于「路径来自 photos 表」的读取场景；写入/单点查找用 ``_resolve_path``。
    相对路径仍会 resolve。
    """
    text = os.path.normpath(str(path))
    candidate = Path(text)
    if candidate.is_absolute():
        return text
    return str(candidate.resolve())


@dataclass(frozen=True)
class Tag:
    id: int
    name: str
    color: str | None
    kind: str
    created_at: str


class TagStore:
    """标签与关联的读写封装。数据库路径与照片库相同。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(TAGS_SCHEMA)

    # ---- 生命周期 -------------------------------------------------------- #

    @classmethod
    def open(cls, db_path: str | Path) -> "TagStore":
        return cls(db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TagStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- 标签 CRUD ------------------------------------------------------- #

    def list_tags(self) -> list[Tag]:
        rows = self._conn.execute(
            "SELECT id, name, color, kind, created_at FROM tags ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [_row_to_tag(row) for row in rows]

    def get(self, tag_id: int) -> Tag | None:
        row = self._conn.execute(
            "SELECT id, name, color, kind, created_at FROM tags WHERE id = ?", (tag_id,)
        ).fetchone()
        return _row_to_tag(row) if row is not None else None

    def get_by_name(self, name: str) -> Tag | None:
        normalized = normalize_tag_name(name)
        row = self._conn.execute(
            "SELECT id, name, color, kind, created_at FROM tags "
            "WHERE name = ? COLLATE NOCASE",
            (normalized,),
        ).fetchone()
        if row is not None:
            return _row_to_tag(row)
        # SQLite NOCASE 只处理 ASCII，非 ASCII 大小写（如俄文）再兜底比较一次
        key = tag_key(normalized)
        for tag in self.list_tags():
            if tag_key(tag.name) == key:
                return tag
        return None

    def create_tag(self, name: str) -> Tag:
        normalized = normalize_tag_name(name)
        existing = self.get_by_name(normalized)
        if existing is not None:
            raise TagConflict(existing.name)
        color = color_for(normalized)
        created_at = _now()
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO tags (name, color, kind, created_at) VALUES (?, ?, 'user', ?)",
                (normalized, color, created_at),
            )
        return Tag(int(cursor.lastrowid), normalized, color, "user", created_at)

    def ensure_tag(self, name: str) -> Tag:
        existing = self.get_by_name(name)
        if existing is not None:
            return existing
        return self.create_tag(name)

    def rename_tag(self, tag_id: int, new_name: str) -> Tag:
        tag = self.get(tag_id)
        if tag is None:
            raise TagError(f"标签不存在（id={tag_id}）")
        normalized = normalize_tag_name(new_name)
        conflict = self.get_by_name(normalized)
        if conflict is not None and conflict.id != tag_id:
            raise TagConflict(conflict.name)
        with self._conn:
            self._conn.execute("UPDATE tags SET name = ? WHERE id = ?", (normalized, tag_id))
        updated = self.get(tag_id)
        assert updated is not None
        return updated

    def delete_tag(self, tag_id: int) -> int:
        """删除标签及其全部关联，返回移除的关联数（不删照片）。"""
        tag = self.get(tag_id)
        if tag is None:
            return 0
        with self._conn:
            cursor = self._conn.execute("DELETE FROM photo_tags WHERE tag_id = ?", (tag_id,))
            self._conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        return int(cursor.rowcount or 0)

    # ---- 关联 ------------------------------------------------------------ #

    def counts(self) -> dict[int, int]:
        rows = self._conn.execute(
            "SELECT tag_id, COUNT(*) AS n FROM photo_tags GROUP BY tag_id"
        ).fetchall()
        return {int(row["tag_id"]): int(row["n"]) for row in rows}

    def add_tags(
        self, paths: Iterable[str | Path], tag_ids: Iterable[int], *, source: str = "user"
    ) -> tuple[list[tuple[str, int]], list[str]]:
        """给一批照片加标签。

        返回 ``(新增的 (path, tag_id) 列表, 不在 photos 表里的路径列表)``。
        已存在的关联不会重复添加。
        """
        path_list = list(dict.fromkeys(_resolve_path(path) for path in paths))
        tag_list = list(dict.fromkeys(int(tag_id) for tag_id in tag_ids))
        if not path_list or not tag_list:
            return [], []

        known_paths = self._known_paths(path_list)
        unknown = [path for path in path_list if path not in known_paths]
        known_tags = self._known_tag_ids(tag_list)
        added: list[tuple[str, int]] = []

        with self._conn:
            for path in path_list:
                if path not in known_paths:
                    continue
                for tag_id in tag_list:
                    if tag_id not in known_tags:
                        continue
                    cursor = self._conn.execute(
                        "INSERT OR IGNORE INTO photo_tags "
                        "(path, tag_id, source, confidence, created_at) "
                        "VALUES (?, ?, ?, NULL, ?)",
                        (path, tag_id, source, _now()),
                    )
                    if cursor.rowcount:
                        added.append((path, tag_id))
        return added, unknown

    def add_assignments(
        self, pairs: Iterable[tuple[str, int]], *, source: str = "user"
    ) -> int:
        """按精确的 (path, tag_id) 恢复关联（撤销用）。"""
        pair_list = [(str(path), int(tag_id)) for path, tag_id in pairs]
        known_tags = self._known_tag_ids(sorted({tag_id for _, tag_id in pair_list}))
        added = 0
        with self._conn:
            for path, tag_id in pair_list:
                if tag_id not in known_tags:
                    continue
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO photo_tags "
                    "(path, tag_id, source, confidence, created_at) "
                    "VALUES (?, ?, ?, NULL, ?)",
                    (_resolve_path(path), int(tag_id), source, _now()),
                )
                added += 1 if cursor.rowcount else 0
        return added

    def remove_tags(
        self, paths: Iterable[str | Path], tag_ids: Iterable[int]
    ) -> list[tuple[str, int]]:
        """从一批照片移除标签，返回实际删除的 (path, tag_id)。"""
        path_list = list(dict.fromkeys(_resolve_path(path) for path in paths))
        tag_list = list(dict.fromkeys(int(tag_id) for tag_id in tag_ids))
        removed: list[tuple[str, int]] = []
        with self._conn:
            for path in path_list:
                for tag_id in tag_list:
                    cursor = self._conn.execute(
                        "DELETE FROM photo_tags WHERE path = ? AND tag_id = ?",
                        (path, tag_id),
                    )
                    if cursor.rowcount:
                        removed.append((path, tag_id))
        return removed

    def remove_assignments(self, pairs: Iterable[tuple[str, int]]) -> int:
        """按精确的 (path, tag_id) 删除关联（撤销用）。"""
        removed = 0
        with self._conn:
            for path, tag_id in pairs:
                cursor = self._conn.execute(
                    "DELETE FROM photo_tags WHERE path = ? AND tag_id = ?",
                    (_db_path(path), int(tag_id)),
                )
                removed += 1 if cursor.rowcount else 0
        return removed

    # ---- 查询 ------------------------------------------------------------ #

    def tags_for_path(self, path: str | Path) -> list[Tag]:
        rows = self._conn.execute(
            "SELECT t.id, t.name, t.color, t.kind, t.created_at "
            "FROM tags t JOIN photo_tags pt ON pt.tag_id = t.id "
            "WHERE pt.path = ? ORDER BY t.name COLLATE NOCASE",
            (_resolve_path(path),),
        ).fetchall()
        return [_row_to_tag(row) for row in rows]

    def tags_for_paths(self, paths: Iterable[str | Path]) -> dict[str, list[Tag]]:
        # 热路径：调用方应传 photos 表里的绝对路径
        path_list = list(dict.fromkeys(_db_path(path) for path in paths))
        result: dict[str, list[Tag]] = {path: [] for path in path_list}
        if not path_list:
            return result
        tags_by_id = {tag.id: tag for tag in self.list_tags()}
        for chunk_start in range(0, len(path_list), 500):
            chunk = path_list[chunk_start : chunk_start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT path, tag_id FROM photo_tags WHERE path IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                tag = tags_by_id.get(int(row["tag_id"]))
                if tag is not None:
                    result[row["path"]].append(tag)
        for items in result.values():
            items.sort(key=lambda tag: tag.name.casefold())
        return result

    def assignments(self) -> dict[str, set[int]]:
        """全部关联：``{path: {tag_id, …}}``，供筛选缓存使用。"""
        result: dict[str, set[int]] = {}
        for row in self._conn.execute("SELECT path, tag_id FROM photo_tags"):
            result.setdefault(row["path"], set()).add(int(row["tag_id"]))
        return result

    def count_assignments(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM photo_tags").fetchone()
        return int(row["n"])

    def photos_with_tag(self, tag_id: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT path FROM photo_tags WHERE tag_id = ? ORDER BY path", (tag_id,)
        ).fetchall()
        return [row["path"] for row in rows]

    def prune(self) -> int:
        """清理照片库中已不存在的路径的关联。"""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM photo_tags WHERE path NOT IN (SELECT path FROM photos)"
            )
        return int(cursor.rowcount or 0)

    # ---- 内部 ------------------------------------------------------------ #

    def _known_paths(self, paths: Sequence[str]) -> set[str]:
        known: set[str] = set()
        for start in range(0, len(paths), 500):
            chunk = paths[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT path FROM photos WHERE path IN ({placeholders})", chunk
            ).fetchall()
            known.update(row["path"] for row in rows)
        return known

    def _known_tag_ids(self, tag_ids: Sequence[int]) -> set[int]:
        if not tag_ids:
            return set()
        placeholders = ", ".join("?" for _ in tag_ids)
        rows = self._conn.execute(
            f"SELECT id FROM tags WHERE id IN ({placeholders})", list(tag_ids)
        ).fetchall()
        return {int(row["id"]) for row in rows}


def _row_to_tag(row: sqlite3.Row) -> Tag:
    return Tag(
        id=int(row["id"]),
        name=row["name"],
        color=row["color"],
        kind=row["kind"],
        created_at=row["created_at"],
    )
