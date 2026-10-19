"""`src.tag_store` 与 V3 迁移的测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.database import SCHEMA_VERSION, PhotoDatabase
from src.tag_store import Tag, TagStore
from src.tags import PRESET_TAGS, TagConflict

from test_database import make_info


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "photos.db"
    with PhotoDatabase.open(path) as database:
        database.upsert_many(
            [
                make_info(tmp_path / "a.JPG", size_bytes=1),
                make_info(tmp_path / "b.JPG", size_bytes=2),
                make_info(tmp_path / "c.JPG", size_bytes=3),
            ]
        )
    return path


def test_schema_seeds_presets(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        names = {tag.name for tag in store.list_tags()}
    assert names == set(PRESET_TAGS)


def test_create_and_get_tag(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        tag = store.create_tag("#客户 A")
        assert tag.name == "客户 A"
        assert store.get(tag.id) == tag
        assert store.get_by_name("客户 A") == tag
        assert store.get_by_name("#客户 A") == tag


def test_duplicate_name_is_rejected_case_insensitively(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        store.create_tag("Architecture")
        with pytest.raises(TagConflict):
            store.create_tag("architecture")
        with pytest.raises(TagConflict):
            store.create_tag("#Architecture")


def test_ensure_tag_returns_existing(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        first = store.ensure_tag("建筑")
        second = store.ensure_tag("#建筑")
        assert first.id == second.id
        assert len([tag for tag in store.list_tags() if tag.name == "建筑"]) == 1


def test_rename_and_conflict(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        tag = store.create_tag("旧名")
        renamed = store.rename_tag(tag.id, "新名")
        assert renamed.name == "新名"
        with pytest.raises(TagConflict):
            store.rename_tag(tag.id, "建筑")
        with pytest.raises(TagConflict):
            store.create_tag("新名")


def test_add_tags_idempotent_and_unknown_paths(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        tag = store.ensure_tag("建筑")
        a = db_path.parent / "a.JPG"
        b = db_path.parent / "b.JPG"
        added, unknown = store.add_tags([a, b, db_path.parent / "missing.JPG"], [tag.id])
        assert len(added) == 2
        assert len(unknown) == 1

        added_again, _ = store.add_tags([a, b], [tag.id])
        assert added_again == []  # 幂等

        assert store.counts()[tag.id] == 2
        assert [item.name for item in store.tags_for_path(a)] == ["建筑"]


def test_tags_for_paths_and_assignments(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        arch = store.ensure_tag("建筑")
        sky = store.ensure_tag("天空")
        paths = [db_path.parent / "a.JPG", db_path.parent / "b.JPG"]
        store.add_tags(paths, [arch.id, sky.id])

        mapping = store.tags_for_paths(paths)
        assert [item.name for item in mapping[str(paths[0].resolve())]] == ["建筑", "天空"] or [
            item.name for item in mapping[str(paths[0].resolve())]
        ] == ["天空", "建筑"]

        assignments = store.assignments()
        assert assignments[str(paths[0].resolve())] == {arch.id, sky.id}


def test_remove_tags_and_undo_pairs(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        tag = store.ensure_tag("待修")
        path = db_path.parent / "a.JPG"
        store.add_tags([path], [tag.id])

        removed = store.remove_tags([path], [tag.id])
        assert removed == [(str(path.resolve()), tag.id)]
        assert store.counts().get(tag.id, 0) == 0

        store.add_assignments(removed)
        assert store.counts()[tag.id] == 1
        store.remove_assignments(removed)
        assert store.counts().get(tag.id, 0) == 0


def test_delete_tag_cascades_assignments(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        tag = store.ensure_tag("客户")
        store.add_tags([db_path.parent / "a.JPG", db_path.parent / "b.JPG"], [tag.id])
        removed = store.delete_tag(tag.id)
        assert removed == 2
        assert store.get(tag.id) is None
        assert store.count_assignments() == 0


def test_photos_with_tag(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        tag = store.ensure_tag("建筑")
        store.add_tags([db_path.parent / "a.JPG"], [tag.id])
        paths = store.photos_with_tag(tag.id)
        assert len(paths) == 1
        assert paths[0].endswith("a.JPG")


def test_prune_removes_orphans(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        tag = store.ensure_tag("建筑")
        # 直接插入一个不在 photos 表里的关联，模拟文件被移走
        with store._conn:
            store._conn.execute(
                "INSERT INTO photo_tags (path, tag_id, source, created_at) VALUES (?, ?, 'user', 'x')",
                (str(db_path.parent / "gone.JPG"), tag.id),
            )
        assert store.count_assignments() == 1
        assert store.prune() == 1
        assert store.count_assignments() == 0


def test_migration_from_v1_seeds_tags_and_keeps_photos(db_path: Path) -> None:
    # 模拟 V2.5 的库：user_version=1，没有标签
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            connection.execute("DELETE FROM photo_tags")
            connection.execute("DELETE FROM tags")
            connection.execute("PRAGMA user_version = 1")
        photos_before = connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    finally:
        connection.close()

    with PhotoDatabase.open(db_path) as database:
        assert database.count_photos() == photos_before
    connection = sqlite3.connect(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tag_count = connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    finally:
        connection.close()
    assert version == SCHEMA_VERSION == 2
    assert tag_count == len(PRESET_TAGS)


def test_delete_tag_of_unknown_id_is_safe(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        assert store.delete_tag(999999) == 0


def test_tag_dataclass_fields(db_path: Path) -> None:
    with TagStore.open(db_path) as store:
        tag = store.ensure_tag("建筑")
        assert isinstance(tag, Tag)
        assert tag.color
        assert tag.kind == "user"
