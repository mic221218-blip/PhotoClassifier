"""`src.duplicates` 的测试：精确重复、近似重复、延时序列豁免。"""

from __future__ import annotations

from pathlib import Path

from src.duplicates import (
    DuplicateCandidate,
    DuplicateFinder,
    candidates_from,
)
from src.quality import QualityConfig, QualityMeasurement

from test_database import make_info


def at(seconds: int) -> str:
    """从 05:00:00 起第 seconds 秒的拍摄时间。"""
    hour, remainder = divmod(seconds, 3600)
    minute, second = divmod(remainder, 60)
    return f"2026-07-13 {5 + hour:02d}:{minute:02d}:{second:02d}"


def candidate(
    name: str,
    *,
    taken_at: str | None = "2026-07-13 05:00:00",
    dhash: str = "0000000000000000",
    content_hash: str | None = None,
    size: int = 1000,
    blur: float = 500.0,
    folder: str = "C:/photos",
) -> DuplicateCandidate:
    return DuplicateCandidate(
        path=f"{folder}/{name}",
        size_bytes=size,
        taken_at=taken_at,
        dhash=dhash,
        content_hash=content_hash,
        blur_score=blur,
    )


def distance_hash(bits: int) -> str:
    return f"{bits:016x}"


# --------------------------------------------------------------------------- #
# 精确重复
# --------------------------------------------------------------------------- #


def test_exact_duplicates_group() -> None:
    result = DuplicateFinder().find(
        [
            candidate("a.JPG", taken_at=None, content_hash="aaa"),
            candidate("a - 副本.JPG", taken_at=None, content_hash="aaa"),
            candidate("b.JPG", taken_at=None, content_hash="bbb"),
        ]
    )
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.kind == "exact"
    assert group.size == 2
    assert result.info_for("C:/photos/a.JPG") is not None
    assert result.info_for("C:/photos/a.JPG").group_id == group.group_id


def test_exact_duplicate_requires_same_size() -> None:
    result = DuplicateFinder().find(
        [
            candidate("a.JPG", taken_at=None, content_hash="same", size=100),
            candidate("b.JPG", taken_at=None, content_hash="same", size=200),
        ]
    )
    assert result.groups == ()


# --------------------------------------------------------------------------- #
# 近似重复
# --------------------------------------------------------------------------- #


def test_near_duplicates_within_window() -> None:
    result = DuplicateFinder().find(
        [
            candidate("a.JPG", taken_at=at(0), dhash=distance_hash(0)),
            candidate("b.JPG", taken_at=at(2), dhash=distance_hash(0x3)),
        ]
    )
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.kind == "near"
    assert group.distance == 2
    assert group.size == 2


def test_near_duplicate_distance_boundary() -> None:
    finder = DuplicateFinder()
    inside = finder.find(
        [
            candidate("a.JPG", taken_at=at(0), dhash=distance_hash(0)),
            candidate("b.JPG", taken_at=at(1), dhash=distance_hash(0xFF)),  # 距离 8
        ]
    )
    assert len(inside.groups) == 1

    outside = finder.find(
        [
            candidate("a.JPG", taken_at=at(0), dhash=distance_hash(0)),
            candidate("b.JPG", taken_at=at(1), dhash=distance_hash(0x1FF)),  # 距离 9
        ]
    )
    assert outside.groups == ()


def test_near_duplicate_time_window_boundary() -> None:
    finder = DuplicateFinder()
    inside = finder.find(
        [
            candidate("a.JPG", taken_at=at(0), dhash=distance_hash(0)),
            candidate("b.JPG", taken_at=at(10), dhash=distance_hash(0)),
        ]
    )
    assert len(inside.groups) == 1

    outside = finder.find(
        [
            candidate("a.JPG", taken_at=at(0), dhash=distance_hash(0)),
            candidate("b.JPG", taken_at=at(11), dhash=distance_hash(0)),
        ]
    )
    assert outside.groups == ()


def test_raw_jpeg_sibling_is_not_duplicate() -> None:
    result = DuplicateFinder().find(
        [
            candidate("DSC_0001.NEF", taken_at=at(0), dhash=distance_hash(0)),
            candidate("DSC_0001.JPG", taken_at=at(0), dhash=distance_hash(0)),
        ]
    )
    assert result.groups == ()


def test_different_photo_not_grouped() -> None:
    result = DuplicateFinder().find(
        [
            candidate("a.JPG", taken_at=at(0), dhash=distance_hash(0)),
            candidate("b.JPG", taken_at=at(5), dhash=distance_hash(0xFFFFFFFF)),
        ]
    )
    assert result.groups == ()


def test_group_members_sorted_by_sharpness() -> None:
    result = DuplicateFinder().find(
        [
            candidate("soft.JPG", taken_at=at(0), dhash=distance_hash(0), blur=120.0),
            candidate("sharp.JPG", taken_at=at(1), dhash=distance_hash(0), blur=800.0),
            candidate("middle.JPG", taken_at=at(2), dhash=distance_hash(0), blur=400.0),
        ]
    )
    assert [member.name for member in result.groups[0].members] == [
        "sharp.JPG",
        "middle.JPG",
        "soft.JPG",
    ]


def test_duplicate_reason_text() -> None:
    result = DuplicateFinder().find(
        [
            candidate("a.JPG", taken_at=at(0), dhash=distance_hash(0), blur=800.0),
            candidate("b.JPG", taken_at=at(1), dhash=distance_hash(0x3)),
        ]
    )
    reason = result.flag_reason("C:/photos/b.JPG")
    assert reason and "a.JPG" in reason and "dHash" in reason


# --------------------------------------------------------------------------- #
# 延时 / 连拍序列
# --------------------------------------------------------------------------- #


def sequence_candidates(count: int = 25, *, folder: str = "C:/timelapse") -> list[DuplicateCandidate]:
    return [
        candidate(f"frame{index:03d}.JPG", taken_at=at(index * 3), dhash=distance_hash(0), folder=folder)
        for index in range(count)
    ]


def test_long_regular_sequence_is_not_duplicates() -> None:
    result = DuplicateFinder().find(sequence_candidates())
    assert result.groups == ()
    assert len(result.sequences) == 1
    assert result.sequences[0].size == 25
    assert result.sequences[0].kind == "sequence"


def test_short_burst_is_duplicates() -> None:
    frames = [
        candidate(f"burst{index}.JPG", taken_at=at(index), dhash=distance_hash(0))
        for index in range(5)
    ]
    result = DuplicateFinder().find(frames)
    assert len(result.groups) == 1
    assert result.groups[0].size == 5


def test_sequence_folder_tolerance_and_strict_mode() -> None:
    frames = sequence_candidates(25, folder="C:/mixed")
    burst = [
        candidate(f"burst{index}.JPG", taken_at=at(1000 + index), dhash=distance_hash(0), folder="C:/mixed")
        for index in range(3)
    ]
    data = frames + burst

    tolerant = DuplicateFinder().find(list(data))
    assert tolerant.groups == ()
    assert any(group.size == 3 for group in tolerant.sequences)

    strict = DuplicateFinder(QualityConfig(sequence_folder_mode=False)).find(list(data))
    assert len(strict.groups) == 1
    assert strict.groups[0].size == 3


# --------------------------------------------------------------------------- #
# 与测量表合并
# --------------------------------------------------------------------------- #


def test_candidates_from_measurements(tmp_path: Path) -> None:
    path = tmp_path / "a.JPG"
    info = make_info(path, width_px=6000, height_px=4000, size_bytes=999)
    measurement = QualityMeasurement(
        path=str(path),
        preview_source="full_image",
        dhash="0000000000000000",
        quick_hash="quick",
        content_hash="content",
        blur_score=500.0,
        file_size=999,
    )
    candidates = candidates_from([measurement], {str(path): info})
    assert len(candidates) == 1
    item = candidates[0]
    assert item.path == str(path)
    assert item.size_bytes == 999
    assert item.pixel_count == 6000 * 4000
    assert item.taken_at == info.taken_at
    assert item.content_hash == "content"
