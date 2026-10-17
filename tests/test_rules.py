"""`src.rules` 的测试（V2 规则分类）。

覆盖：每条规则的命中 / 不命中 / 边界值、EXIF 缺失、自定义阈值、
原因文本、批量分类，以及 1 万张照片的性能基线。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.photo_info import EXIF_OK, PhotoInfo
from src.rules import RuleConfig, RuleEngine


def make_info(**overrides) -> PhotoInfo:
    """构造测试用 PhotoInfo（默认是一张"普通照片"，不命中任何规则）。"""
    fields = dict(
        path=Path("D:/Photos/2026/DSC_0001.NEF"),
        category="nef",
        size_bytes=1000,
        exif_status=EXIF_OK,
        taken_at="2026-09-18 14:00:00",
        iso=200,
        exposure_time_s=1 / 250,
        focal_length_mm=50.0,
        f_number=4.0,
        camera_make="NIKON CORPORATION",
        camera_model="NIKON Z6III",
    )
    fields.update(overrides)
    return PhotoInfo(**fields)


def matched_ids(info: PhotoInfo, engine: RuleEngine | None = None) -> set[str]:
    return {match.rule_id for match in (engine or RuleEngine()).evaluate(info)}


# --------------------------------------------------------------------------- #
# 1. 夜景
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "taken_at, hit",
    [
        ("2026-09-18 20:15:00", True),   # 夜间时段内（20 点）
        ("2026-09-18 23:59:00", True),
        ("2026-09-18 00:30:00", True),
        ("2026-09-18 05:59:00", True),
        ("2026-09-18 06:00:00", False),  # 6 点整不算夜间
        ("2026-09-18 17:59:00", False),
        ("2026-09-18 12:00:00", False),
    ],
)
def test_night_window(taken_at: str, hit: bool) -> None:
    assert ("night" in matched_ids(make_info(taken_at=taken_at))) is hit


def test_night_missing_taken_at() -> None:
    assert "night" not in matched_ids(make_info(taken_at=None))


def test_night_malformed_taken_at() -> None:
    assert "night" not in matched_ids(make_info(taken_at="2026-09-18"))


def test_night_custom_window() -> None:
    engine = RuleEngine(replace(RuleConfig(), night_start_hour=21, night_end_hour=5))
    assert "night" in matched_ids(make_info(taken_at="2026-09-18 21:30:00"), engine)
    assert "night" not in matched_ids(make_info(taken_at="2026-09-18 20:15:00"), engine)


def test_night_reason_text() -> None:
    matches = RuleEngine().evaluate(make_info(taken_at="2026-09-18 20:15:00"))
    night = next(m for m in matches if m.rule_id == "night")
    assert "20:15" in night.reason
    assert "18:00" in night.reason and "06:00" in night.reason


# --------------------------------------------------------------------------- #
# 2. 高感
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "iso, hit",
    [
        (1600, False),
        (3200, True),   # 边界：等于阈值应命中（≥）
        (6400, True),
        (100, False),
    ],
)
def test_high_iso(iso: int, hit: bool) -> None:
    assert ("high_iso" in matched_ids(make_info(iso=iso))) is hit


def test_high_iso_missing() -> None:
    assert "high_iso" not in matched_ids(make_info(iso=None))


def test_high_iso_reason() -> None:
    matches = RuleEngine().evaluate(make_info(iso=6400))
    high = next(m for m in matches if m.rule_id == "high_iso")
    assert high.reason == "ISO 6400 ≥ 3200"


def test_high_iso_custom_threshold() -> None:
    engine = RuleEngine(replace(RuleConfig(), high_iso=6400))
    assert "high_iso" not in matched_ids(make_info(iso=3200), engine)
    assert "high_iso" in matched_ids(make_info(iso=6400), engine)


# --------------------------------------------------------------------------- #
# 3. 慢门
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exposure, hit",
    [
        (1 / 15, True),
        (2.0, True),
        (1 / 30, False),   # 边界：等于 1/30 不算慢门（<）
        (1 / 250, False),
    ],
)
def test_slow_shutter(exposure: float, hit: bool) -> None:
    assert ("slow_shutter" in matched_ids(make_info(exposure_time_s=exposure))) is hit


def test_slow_shutter_missing() -> None:
    assert "slow_shutter" not in matched_ids(make_info(exposure_time_s=None))


def test_slow_shutter_reason() -> None:
    matches = RuleEngine().evaluate(make_info(exposure_time_s=1 / 15))
    slow = next(m for m in matches if m.rule_id == "slow_shutter")
    assert "1/15s" in slow.reason and "1/30s" in slow.reason

# --------------------------------------------------------------------------- #
# 4. 长焦 / 广角
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "focal, tele, wide",
    [
        (120.0, True, False),
        (70.0, True, False),   # 边界 ≥ 70 命中长焦
        (69.9, False, False),
        (24.0, False, True),   # 边界 ≤ 24 命中广角
        (14.0, False, True),
        (25.0, False, False),
        (50.0, False, False),
    ],
)
def test_focal_rules(focal: float, tele: bool, wide: bool) -> None:
    ids = matched_ids(make_info(focal_length_mm=focal))
    assert ("telephoto" in ids) is tele
    assert ("wide_angle" in ids) is wide


def test_focal_missing() -> None:
    ids = matched_ids(make_info(focal_length_mm=None))
    assert "telephoto" not in ids and "wide_angle" not in ids


def test_focal_reasons() -> None:
    tele = next(m for m in RuleEngine().evaluate(make_info(focal_length_mm=120.0)) if m.rule_id == "telephoto")
    assert "120 mm ≥ 70 mm" in tele.reason
    wide = next(m for m in RuleEngine().evaluate(make_info(focal_length_mm=16.0)) if m.rule_id == "wide_angle")
    assert "16 mm ≤ 24 mm" in wide.reason


# --------------------------------------------------------------------------- #
# 5. 大光圈
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "f_number, hit",
    [
        (1.8, True),
        (2.8, True),    # 边界 ≤ 2.8 命中
        (3.5, False),
        (4.0, False),
    ],
)
def test_fast_lens(f_number: float, hit: bool) -> None:
    assert ("fast_lens" in matched_ids(make_info(f_number=f_number))) is hit


def test_fast_lens_missing() -> None:
    assert "fast_lens" not in matched_ids(make_info(f_number=None))


def test_fast_lens_reason() -> None:
    matches = RuleEngine().evaluate(make_info(f_number=1.8))
    fast = next(m for m in matches if m.rule_id == "fast_lens")
    assert "f/1.8" in fast.reason and "f/2.8" in fast.reason


# --------------------------------------------------------------------------- #
# 6. 组合与元信息
# --------------------------------------------------------------------------- #


def test_multiple_rules_can_match_one_photo() -> None:
    """一张照片可以同时命中多条规则（对应"一张照片多个标签"的设计思想）。"""
    info = make_info(
        taken_at="2026-09-18 20:15:00",
        iso=6400,
        exposure_time_s=1 / 8,
        focal_length_mm=120.0,
        f_number=1.8,
    )
    assert matched_ids(info) == {"night", "high_iso", "slow_shutter", "telephoto", "fast_lens"}


def test_no_match_returns_empty() -> None:
    assert RuleEngine().evaluate(make_info()) == []


def test_engine_labels_and_describe() -> None:
    engine = RuleEngine()
    labels = engine.labels()
    assert labels["night"] == ("夜景", "Night")
    assert "ISO ≥ 3200" in engine.describe("high_iso")
    assert "1/30s" in engine.describe("slow_shutter")
    assert "70 mm" in engine.describe("telephoto")
    assert "24 mm" in engine.describe("wide_angle")
    assert "f/2.8" in engine.describe("fast_lens")


def test_rule_ids_order() -> None:
    assert RuleEngine().rule_ids == (
        "night",
        "high_iso",
        "slow_shutter",
        "telephoto",
        "wide_angle",
        "fast_lens",
    )


def test_with_config_returns_new_engine() -> None:
    base = RuleEngine()
    modified = base.with_config(high_iso=6400)
    assert base.config.high_iso == 3200
    assert modified.config.high_iso == 6400


def test_classify_batch() -> None:
    engine = RuleEngine()
    infos = [
        make_info(path=Path("D:/P/a.NEF"), iso=6400),
        make_info(path=Path("D:/P/b.NEF"), iso=100),
        make_info(path=Path("D:/P/c.NEF"), taken_at="2026-09-18 21:00:00"),
    ]
    result = engine.classify(infos)
    assert result[str(Path("D:/P/a.NEF"))]        # 高感
    assert result[str(Path("D:/P/b.NEF"))] == []
    assert result[str(Path("D:/P/c.NEF"))]        # 夜景


# --------------------------------------------------------------------------- #
# 7. 性能基线：1 万张照片全量分类
# --------------------------------------------------------------------------- #


def test_classify_10k_performance() -> None:
    """1 万张照片 × 6 条规则的分类要足够快（GUI 实时筛选依赖这一点）。"""
    import time

    engine = RuleEngine()
    infos = [
        make_info(
            path=Path(f"D:/P/{i:05d}.NEF"),
            taken_at="2026-09-18 20:15:00" if i % 3 == 0 else "2026-09-18 12:00:00",
            iso=6400 if i % 5 == 0 else 200,
            focal_length_mm=120.0 if i % 7 == 0 else 50.0,
        )
        for i in range(10000)
    ]
    started = time.perf_counter()
    result = engine.classify(infos)
    elapsed = time.perf_counter() - started

    assert len(result) == 10000
    assert elapsed < 5.0, f"classification too slow: {elapsed:.2f}s"
