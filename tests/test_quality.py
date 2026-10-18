"""`src.quality` 的测试：度量（纯 Python）与判定规则。

度量测试不依赖 Pillow：直接构造灰度字节数组，
这样即使没有安装可选依赖，核心判定逻辑也始终有测试覆盖。
"""

from __future__ import annotations

import pytest

from src.photo_info import PhotoInfo
from src.quality import (
    ANALYZER_VERSION,
    FLAG_IDS,
    GrayImage,
    QualityConfig,
    QualityEngine,
    QualityMeasurement,
    dhash_from_grid,
    hamming_distance,
    measure_gray,
)


# --------------------------------------------------------------------------- #
# 灰度图构造工具
# --------------------------------------------------------------------------- #


def vertical_stripes(width: int = 64, height: int = 48, *, period: int = 2) -> GrayImage:
    """竖条纹：横向高频，用来模拟"清晰"的画面。"""
    data = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            data[y * width + x] = 255 if (x // period) % 2 else 0
    return GrayImage(width, height, bytes(data))


def horizontal_stripes(width: int = 64, height: int = 48, *, period: int = 2) -> GrayImage:
    data = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            data[y * width + x] = 255 if (y // period) % 2 else 0
    return GrayImage(width, height, bytes(data))


def smooth_gradient(width: int = 64, height: int = 48) -> GrayImage:
    data = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            data[y * width + x] = (x * 255) // max(width - 1, 1)
    return GrayImage(width, height, bytes(data))


def flat(value: int, width: int = 32, height: int = 32) -> GrayImage:
    return GrayImage(width, height, bytes([value]) * (width * height))


def measurement(**overrides) -> QualityMeasurement:
    """一张"曝光正常、清晰"的默认测量记录。"""
    fields = dict(
        path="C:/photos/a.JPG",
        preview_source="full_image",
        preview_width=6048,
        preview_height=4032,
        blur_score=500.0,
        contrast=50.0,
        anisotropy=0.10,
        mean_lum=110.0,
        std_lum=45.0,
        clip_low_frac=0.001,
        clip_high_frac=0.001,
        error=None,
    )
    fields.update(overrides)
    return QualityMeasurement(**fields)


def make_info(**overrides) -> PhotoInfo:
    fields = dict(
        path="C:/photos/a.JPG",
        category="jpeg",
        size_bytes=1000,
        modified_at="2026-07-13 05:30:00",
        exif_status="ok",
        taken_at="2026-07-13 05:30:00",
        exposure_time_s=1 / 250,
    )
    fields.update(overrides)
    return PhotoInfo(**fields)


def flag_ids(engine: QualityEngine, item: QualityMeasurement, info=None) -> set[str]:
    return {flag.flag_id for flag in engine.flags(item, info)}


# --------------------------------------------------------------------------- #
# 度量
# --------------------------------------------------------------------------- #


def test_vertical_stripes_are_high_frequency() -> None:
    metrics = measure_gray(vertical_stripes())
    assert metrics.blur_score > 1000
    # 竖条纹只在水平方向有梯度 → 方向性接近 1
    assert metrics.anisotropy > 0.9


def test_horizontal_stripes_are_directional() -> None:
    metrics = measure_gray(horizontal_stripes())
    assert metrics.blur_score > 1000
    assert metrics.anisotropy > 0.9


def test_smooth_gradient_has_no_details() -> None:
    blurred = measure_gray(smooth_gradient())
    sharp = measure_gray(vertical_stripes())
    assert blurred.blur_score < 20
    assert sharp.blur_score > blurred.blur_score * 20
    # 渐变图的对比度主要来自整体亮度跨度
    assert 60 < blurred.contrast < 90


def test_flat_dark_and_bright_images() -> None:
    black = measure_gray(flat(0))
    assert black.mean_lum == 0
    assert black.std_lum == 0
    assert black.clip_low_frac == 1.0
    assert black.blur_score == 0

    white = measure_gray(flat(255))
    assert white.mean_lum == 255
    assert white.clip_high_frac == 1.0


def test_clipping_fractions() -> None:
    # 一半 0、一半 255：两端截断各 50%
    width, height = 40, 20
    data = bytes([0] * (width // 2) + [255] * (width // 2)) * height
    metrics = measure_gray(GrayImage(width, height, data))
    assert metrics.clip_low_frac == 0.5
    assert metrics.clip_high_frac == 0.5


def test_measure_gray_rejects_tiny_images() -> None:
    with pytest.raises(ValueError):
        measure_gray(GrayImage(2, 2, bytes(4)))


def test_measure_gray_rejects_mismatched_length() -> None:
    with pytest.raises(ValueError):
        measure_gray(GrayImage(10, 10, bytes(99)))


# --------------------------------------------------------------------------- #
# dHash
# --------------------------------------------------------------------------- #


def test_dhash_increasing_grid_all_zero() -> None:
    grid = bytes([index for row in range(8) for index in range(row * 9, row * 9 + 9)])
    assert dhash_from_grid(grid) == "0" * 16


def test_dhash_decreasing_grid_all_one() -> None:
    grid = bytes([255 - index for row in range(8) for index in range(row * 9, row * 9 + 9)])
    assert dhash_from_grid(grid) == "f" * 16


def test_dhash_requires_72_bytes() -> None:
    with pytest.raises(ValueError):
        dhash_from_grid(b"\x00" * 71)


def test_hamming_distance() -> None:
    assert hamming_distance("0" * 16, "0" * 16) == 0
    assert hamming_distance("0" * 16, "f" * 16) == 64
    assert hamming_distance("0" * 15 + "1", "0" * 16) == 1
    assert hamming_distance("bad", "0" * 16) == 64


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #


def test_normal_photo_has_no_flags() -> None:
    engine = QualityEngine()
    assert engine.flags(measurement()) == []


def test_blurry_photo() -> None:
    engine = QualityEngine()
    flags = engine.flags(measurement(blur_score=68.0))
    assert [flag.flag_id for flag in flags] == ["blurry"]
    assert "68" in flags[0].reason and "120" in flags[0].reason


def test_motion_blur_requires_directionality() -> None:
    engine = QualityEngine()
    flags = engine.flags(measurement(blur_score=70.0, anisotropy=0.71, ) )
    assert {flag.flag_id for flag in flags} == {"motion_blur"}
    assert "方向性" in flags[0].reason


def test_low_contrast_is_not_blurry() -> None:
    engine = QualityEngine()
    assert flag_ids(engine, measurement(blur_score=5.0, contrast=10.0)) == set()


def test_black_frame_wins_over_blur() -> None:
    engine = QualityEngine()
    flags = engine.flags(measurement(blur_score=3.0, contrast=0.0, mean_lum=4.0, std_lum=3.0))
    assert {flag.flag_id for flag in flags} == {"black_frame"}
    assert "镜头盖" in flags[0].reason


def test_overexposed_by_mean_and_clipping() -> None:
    engine = QualityEngine()
    by_mean = engine.flags(measurement(mean_lum=228.0))
    assert {flag.flag_id for flag in by_mean} == {"overexposed"}
    by_clip = engine.flags(measurement(mean_lum=150.0, clip_high_frac=0.21))
    assert {flag.flag_id for flag in by_clip} == {"overexposed"}
    assert "高光截断" in by_clip[0].reason


def test_underexposed_with_night_hint() -> None:
    engine = QualityEngine()
    info = make_info(taken_at="2026-07-13 05:30:00")
    flags = engine.flags(measurement(mean_lum=32.0), info)
    assert {flag.flag_id for flag in flags} == {"underexposed"}
    assert "夜景" in flags[0].reason


def test_exposure_flags_are_mutually_exclusive() -> None:
    engine = QualityEngine()
    flags = engine.flags(measurement(mean_lum=220.0, clip_low_frac=0.9))
    assert {flag.flag_id for flag in flags} == {"overexposed"}


def test_blur_suppressed_on_overexposed_photo() -> None:
    engine = QualityEngine()
    flags = engine.flags(measurement(blur_score=10.0, mean_lum=230.0))
    assert {flag.flag_id for flag in flags} == {"overexposed"}


def test_motion_hint_uses_shutter() -> None:
    engine = QualityEngine()
    info = make_info(taken_at="2026-07-13 12:00:00", exposure_time_s=1 / 5)
    flags = engine.flags(measurement(blur_score=66.0, anisotropy=0.67), info)
    assert flags[0].flag_id == "motion_blur"
    assert "1/5s" in flags[0].reason


def test_focus_hint_uses_fast_shutter() -> None:
    engine = QualityEngine()
    info = make_info(taken_at="2026-07-13 12:00:00", exposure_time_s=1 / 1000)
    flags = engine.flags(measurement(blur_score=50.0, anisotropy=0.1), info)
    assert flags[0].flag_id == "blurry"
    assert "虚焦" in flags[0].reason


def test_custom_thresholds_change_verdict() -> None:
    engine = QualityEngine(QualityConfig(blur_var_min=50.0))
    assert flag_ids(engine, measurement(blur_score=68.0)) == set()
    low = QualityEngine().with_config(blur_var_min=600.0)
    assert flag_ids(low, measurement(blur_score=500.0)) == {"blurry"}


def test_failed_measurement_has_no_flags() -> None:
    engine = QualityEngine()
    failed = QualityMeasurement(path="C:/a.JPG", preview_source="none", error="解码失败")
    assert engine.flags(failed) == []


def test_engine_describe_and_flag_order() -> None:
    engine = QualityEngine()
    for flag_id in FLAG_IDS:
        assert engine.describe(flag_id)
    assert FLAG_IDS == (
        "blurry",
        "motion_blur",
        "overexposed",
        "underexposed",
        "black_frame",
        "duplicate",
    )


def test_analyzer_version_is_int() -> None:
    assert isinstance(ANALYZER_VERSION, int) and ANALYZER_VERSION >= 1
