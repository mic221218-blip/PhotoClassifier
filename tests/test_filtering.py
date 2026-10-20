"""`src.filtering` 的统一筛选测试。"""

from __future__ import annotations

from src.filtering import FilterSelection, matches
from src.photo_info import PhotoInfo


def make_info(*, gps: bool = False) -> PhotoInfo:
    return PhotoInfo(
        path="C:/photos/a.JPG",
        category="jpeg",
        size_bytes=1,
        gps_latitude=31.2 if gps else None,
        gps_longitude=121.4 if gps else None,
    )


def test_empty_selection_passes() -> None:
    selection = FilterSelection()
    assert selection.is_empty
    assert matches(make_info(), rule_hits=set(), quality_hits=set(), tag_hits=set(), selection=selection)


def test_gps_only() -> None:
    selection = FilterSelection(gps_only=True)
    assert matches(
        make_info(gps=True), rule_hits=set(), quality_hits=set(), tag_hits=set(), selection=selection
    )
    assert not matches(
        make_info(gps=False), rule_hits=set(), quality_hits=set(), tag_hits=set(), selection=selection
    )


def test_rules_and_tags_and_mode() -> None:
    selection = FilterSelection(rule_ids=frozenset({"night"}), tag_ids=frozenset({1}))
    assert matches(
        make_info(),
        rule_hits={"night"},
        quality_hits=set(),
        tag_hits={1},
        selection=selection,
    )
    assert not matches(
        make_info(),
        rule_hits={"night"},
        quality_hits=set(),
        tag_hits=set(),
        selection=selection,
    )


def test_or_mode() -> None:
    selection = FilterSelection(
        rule_ids=frozenset({"night"}),
        quality_ids=frozenset({"blurry"}),
        tag_ids=frozenset({1}),
        and_mode=False,
    )
    assert matches(
        make_info(),
        rule_hits=set(),
        quality_hits={"blurry"},
        tag_hits=set(),
        selection=selection,
    )
    assert not matches(
        make_info(),
        rule_hits=set(),
        quality_hits=set(),
        tag_hits=set(),
        selection=selection,
    )


def test_multiple_tags_require_all_in_and_mode() -> None:
    selection = FilterSelection(tag_ids=frozenset({1, 2}))
    assert matches(
        make_info(), rule_hits=set(), quality_hits=set(), tag_hits={1, 2}, selection=selection
    )
    assert not matches(
        make_info(), rule_hits=set(), quality_hits=set(), tag_hits={1}, selection=selection
    )


def test_gps_is_hard_condition_even_in_or_mode() -> None:
    selection = FilterSelection(tag_ids=frozenset({1}), and_mode=False, gps_only=True)
    assert not matches(
        make_info(gps=False), rule_hits=set(), quality_hits=set(), tag_hits={1}, selection=selection
    )


def test_str_and_int_keys_do_not_collide() -> None:
    # 规则 id 是字符串，标签 id 是整数；集合里不能互相误判
    selection = FilterSelection(rule_ids=frozenset({"1"}))
    assert not matches(
        make_info(), rule_hits=set(), quality_hits=set(), tag_hits={1}, selection=selection
    )


def test_with_values_and_count() -> None:
    selection = FilterSelection(rule_ids=frozenset({"night"}))
    updated = selection.with_values(tag_ids=[1, 2], and_mode=False)
    assert updated.rule_ids == frozenset({"night"})
    assert updated.tag_ids == frozenset({1, 2})
    assert updated.and_mode is False
    assert updated.selected_count == 3
    assert not updated.is_empty


def test_quality_ok_rejects_technical_flags() -> None:
    selection = FilterSelection(quality_ok=True)
    assert not selection.is_empty
    assert matches(
        make_info(), rule_hits=set(), quality_hits=set(), tag_hits=set(), selection=selection
    )
    assert not matches(
        make_info(),
        rule_hits=set(),
        quality_hits={"blurry"},
        tag_hits=set(),
        selection=selection,
    )
    assert not matches(
        make_info(),
        rule_hits=set(),
        quality_hits={"__unanalyzed__"},
        tag_hits=set(),
        selection=selection,
    )


def test_quality_ok_ignores_duplicate_flag() -> None:
    selection = FilterSelection(quality_ok=True)
    assert matches(
        make_info(),
        rule_hits=set(),
        quality_hits={"duplicate"},
        tag_hits=set(),
        selection=selection,
    )


def test_quality_ok_combines_with_rules_and_tags() -> None:
    selection = FilterSelection(rule_ids=frozenset({"night"}), tag_ids=frozenset({1}), quality_ok=True)
    assert matches(
        make_info(), rule_hits={"night"}, quality_hits=set(), tag_hits={1}, selection=selection
    )
    assert not matches(
        make_info(), rule_hits={"night"}, quality_hits={"overexposed"}, tag_hits={1}, selection=selection
    )


def test_quality_ok_with_values_and_count() -> None:
    selection = FilterSelection().with_values(quality_ok=True)
    assert selection.quality_ok
    assert selection.selected_count == 1
    assert not selection.is_empty
