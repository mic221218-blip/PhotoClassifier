"""`src.tags` 的纯逻辑测试：规范化、颜色、预置列表。"""

from __future__ import annotations

import pytest

from src.tags import (
    MAX_TAG_NAME_LENGTH,
    PALETTE,
    PRESET_TAGS,
    TagConflict,
    TagError,
    chip_colors,
    color_for,
    normalize_tag_name,
    tag_key,
)


def test_normalize_strips_hash_and_whitespace() -> None:
    assert normalize_tag_name("  #建筑  ") == "建筑"
    assert normalize_tag_name("建筑") == "建筑"
    assert normalize_tag_name("待  修") == "待 修"
    assert normalize_tag_name("Landscape") == "Landscape"


def test_normalize_empty_and_invalid() -> None:
    for value in ("", "   ", "#", None):
        with pytest.raises(TagError):
            normalize_tag_name(value)  # type: ignore[arg-type]
    with pytest.raises(TagError, match="过长"):
        normalize_tag_name("x" * (MAX_TAG_NAME_LENGTH + 1))
    with pytest.raises(TagError, match="控制字符"):
        normalize_tag_name("a\nb")


def test_normalize_unicode_nfc() -> None:
    composed = "建筑"
    decomposed = "建" + "筑"
    assert normalize_tag_name(decomposed) == composed


def test_tag_key_is_case_insensitive() -> None:
    assert tag_key("Landscape") == tag_key("landscape")
    assert tag_key("#Landscape") == "landscape"


def test_colors_are_stable_and_from_palette() -> None:
    assert color_for("建筑") == color_for("建筑")
    assert color_for("建筑") in PALETTE
    assert color_for("#建筑") == color_for("建筑")


def test_chip_colors_handle_invalid_values() -> None:
    background, foreground = chip_colors("#ffffff")
    assert background == "#ffffff" and foreground == "#1b1c21"
    background, _ = chip_colors(None)
    assert background


def test_presets_are_unique() -> None:
    assert len(PRESET_TAGS) == 10
    assert len(set(PRESET_TAGS)) == len(PRESET_TAGS)
    assert "建筑" in PRESET_TAGS


def test_tag_conflict_is_a_tag_error() -> None:
    error = TagConflict("建筑")
    assert isinstance(error, TagError)
    assert "建筑" in str(error)
