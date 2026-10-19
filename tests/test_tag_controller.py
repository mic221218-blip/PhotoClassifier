"""`src.tag_controller` 的测试：缓存、批量操作与撤销。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import PhotoDatabase
from src.tag_controller import TagController
from src.tag_store import TagStore

from test_database import make_info


@pytest.fixture()
def setup(tmp_path: Path):
    db_path = tmp_path / "photos.db"
    with PhotoDatabase.open(db_path) as database:
        database.upsert_many(
            [make_info(tmp_path / "a.JPG", size_bytes=1), make_info(tmp_path / "b.JPG", size_bytes=2)]
        )
    store = TagStore.open(db_path)
    controller = TagController(store)
    controller.ensure_loaded()
    yield controller, store, tmp_path
    store.close()


def test_loads_presets(setup) -> None:
    controller, _store, _tmp = setup
    assert len(controller.tags_by_id) == 10
    assert controller.tags_for_path("C:/nothing.JPG") == []


def test_mark_dirty_reloads_assignments(setup) -> None:
    controller, store, tmp = setup
    tag = store.ensure_tag("建筑")
    path = str((tmp / "a.JPG").resolve())
    store.add_tags([path], [tag.id])
    controller.mark_dirty()
    controller.ensure_loaded()
    assert [item.id for item in controller.tags_for_path(path)] == [tag.id]


def test_apply_add_and_undo(setup) -> None:
    controller, _store, tmp = setup
    tag = controller.ensure_tag("建筑")
    paths = [str((tmp / "a.JPG").resolve()), str((tmp / "b.JPG").resolve())]

    added, unknown = controller.apply_add(paths, [tag.id])
    assert len(added) == 2 and unknown == []
    assert controller.undo_available

    message = controller.undo_last()
    assert message and "撤销" in message
    assert not controller.undo_available
    assert controller.tag_counts.get(tag.id, 0) == 0


def test_apply_remove_and_undo(setup) -> None:
    controller, _store, tmp = setup
    tag = controller.ensure_tag("天空")
    paths = [str((tmp / "a.JPG").resolve()), str((tmp / "b.JPG").resolve())]
    controller.apply_add(paths, [tag.id])

    removed = controller.apply_remove(paths, [tag.id])
    assert len(removed) == 2
    controller.ensure_loaded()
    assert controller.tag_counts.get(tag.id, 0) == 0

    controller.undo_last()
    controller.ensure_loaded()
    assert controller.tag_counts.get(tag.id, 0) == 2


def test_single_operation_clears_undo(setup) -> None:
    controller, _store, tmp = setup
    tag = controller.ensure_tag("待修")
    path = str((tmp / "a.JPG").resolve())
    controller.apply_add([path], [tag.id])
    assert controller.undo_available

    controller.add_single(path, tag.id)
    assert not controller.undo_available


def test_rename_and_delete_mark_dirty(setup) -> None:
    controller, _store, _tmp = setup
    tag = controller.ensure_tag("客户")
    revision = controller.revision
    renamed = controller.rename_tag(tag.id, "客户 A")
    assert renamed.name == "客户 A"
    assert controller.revision > revision

    controller.delete_tag(tag.id)
    assert controller.get_by_name("客户 A") is None
    assert not controller.undo_available
