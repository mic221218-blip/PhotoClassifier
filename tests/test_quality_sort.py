"""P1：清晰度排序纯函数测试。"""

from __future__ import annotations

from pathlib import Path

from src.quality import QualityMeasurement
from src.quality_sort import (
    SORT_DEFAULT,
    sharpness_key,
    sharpness_score,
    sort_by_sharpness,
)

from test_database import make_info


def measurement(path: Path, score: float | None, *, error: str | None = None) -> QualityMeasurement:
    return QualityMeasurement(
        path=str(path.resolve()),
        blur_score=score,
        error=error,
    )


def test_scores_sorted_high_to_low(tmp_path: Path) -> None:
    a = make_info(tmp_path / "a.JPG")
    b = make_info(tmp_path / "b.JPG")
    c = make_info(tmp_path / "c.JPG")
    data = {
        str(a.path): measurement(a.path, 100),
        str(b.path): measurement(b.path, 500),
        str(c.path): measurement(c.path, 300),
    }
    ordered = sort_by_sharpness([a, b, c], data)
    assert [item.file_name for item in ordered] == ["b.JPG", "c.JPG", "a.JPG"]


def test_unanalyzed_and_failed_go_last(tmp_path: Path) -> None:
    sharp = make_info(tmp_path / "sharp.JPG")
    unknown = make_info(tmp_path / "unknown.JPG")
    failed = make_info(tmp_path / "failed.JPG")
    data = {
        str(sharp.path): measurement(sharp.path, 500),
        str(failed.path): measurement(failed.path, None, error="decode failed"),
    }
    ordered = sort_by_sharpness([unknown, failed, sharp], data)
    assert ordered[0].file_name == "sharp.JPG"
    assert {item.file_name for item in ordered[1:]} == {"unknown.JPG", "failed.JPG"}


def test_missing_measurement_and_nan_are_invalid(tmp_path: Path) -> None:
    info = make_info(tmp_path / "a.JPG")
    assert sharpness_score(None) is None
    assert sharpness_score(measurement(info.path, None)) is None
    assert sharpness_score(measurement(info.path, float("nan"))) is None


def test_ties_use_taken_at_then_name(tmp_path: Path) -> None:
    later = make_info(tmp_path / "later.JPG", taken_at="2026-07-13 10:00:02")
    earlier_b = make_info(tmp_path / "b.JPG", taken_at="2026-07-13 10:00:01")
    earlier_a = make_info(tmp_path / "a.JPG", taken_at="2026-07-13 10:00:01")
    data = {
        str(later.path): measurement(later.path, 300),
        str(earlier_b.path): measurement(earlier_b.path, 300),
        str(earlier_a.path): measurement(earlier_a.path, 300),
    }
    ordered = sort_by_sharpness([later, earlier_b, earlier_a], data)
    assert [item.file_name for item in ordered] == ["a.JPG", "b.JPG", "later.JPG"]


def test_default_mode_constant() -> None:
    assert SORT_DEFAULT == "default"
