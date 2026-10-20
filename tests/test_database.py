"""`src.database` 的测试（V1.5）。

全部使用 pytest 临时目录里的 SQLite 文件，不接触任何真实数据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import PhotoDatabase
from src.photo_info import EXIF_ERROR, EXIF_NO_EXIF, EXIF_OK, EXIF_SKIPPED, PhotoInfo
from src.quality import ANALYZER_VERSION, QualityMeasurement
from src.quality_store import QualityStore
from src.tag_store import TagStore
from src.scanner import CATEGORY_JPEG, CATEGORY_NEF, CATEGORY_VIDEO


def make_info(path: Path, **overrides) -> PhotoInfo:
    """构造一条测试用的照片记录。"""
    fields = dict(
        path=path,
        category=CATEGORY_NEF,
        size_bytes=12345,
        modified_at="2026-09-18 21:00:00",
        exif_status=EXIF_OK,
        taken_at="2026-09-18 20:15:32",
        camera_make="NIKON CORPORATION",
        camera_model="NIKON Z6III",
        lens_model="NIKKOR Z 24-120mm f/4 S",
        focal_length_mm=120.0,
        f_number=4.0,
        exposure_time_s=1 / 250,
        iso=1600,
        exposure_bias_ev=-1 / 3,
        gps_latitude=31.230425,
        gps_longitude=121.473700,
        width_px=6048,
        height_px=4024,
    )
    fields.update(overrides)
    return PhotoInfo(**fields)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "photos.db"


# --------------------------------------------------------------------------- #
# 1. 写入与读取
# --------------------------------------------------------------------------- #


def test_upsert_and_count(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        assert db.count_photos() == 0

        db.upsert_many(
            [
                make_info(Path("D:/Photos/a.NEF")),
                make_info(Path("D:/Photos/b.JPG"), category=CATEGORY_JPEG, iso=800),
                make_info(Path("D:/Photos/c.MOV"), category=CATEGORY_VIDEO, exif_status=EXIF_SKIPPED, taken_at=None),
            ]
        )

        assert db.count_photos() == 3


def test_roundtrip_all_fields(db_path: Path) -> None:
    path = Path("D:/Photos/DSC_0012.NEF")
    original = make_info(path)

    with PhotoDatabase.open(db_path) as db:
        db.upsert_photo(original)
        restored = db.get_photo(path)

    assert restored is not None
    assert restored.path == Path(path).resolve()
    assert restored.category == CATEGORY_NEF
    assert restored.size_bytes == 12345
    assert restored.modified_at == "2026-09-18 21:00:00"
    assert restored.exif_status == EXIF_OK
    assert restored.exif_error is None
    assert restored.taken_at == "2026-09-18 20:15:32"
    assert restored.camera_make == "NIKON CORPORATION"
    assert restored.camera_model == "NIKON Z6III"
    assert restored.lens_model == "NIKKOR Z 24-120mm f/4 S"
    assert restored.focal_length_mm == pytest.approx(120.0)
    assert restored.f_number == pytest.approx(4.0)
    assert restored.exposure_time_s == pytest.approx(1 / 250)
    assert restored.iso == 1600
    assert restored.exposure_bias_ev == pytest.approx(-1 / 3)
    assert restored.gps_latitude == pytest.approx(31.230425)
    assert restored.gps_longitude == pytest.approx(121.473700)
    assert restored.width_px == 6048
    assert restored.height_px == 4024


def test_error_status_and_message_roundtrip(db_path: Path) -> None:
    info = make_info(
        Path("D:/Photos/broken.NEF"),
        exif_status=EXIF_ERROR,
        exif_error="不是有效的 TIFF/NEF 文件头",
        taken_at=None,
        iso=None,
    )

    with PhotoDatabase.open(db_path) as db:
        db.upsert_photo(info)
        restored = db.get_photo(Path("D:/Photos/broken.NEF"))

    assert restored is not None
    assert restored.exif_status == EXIF_ERROR
    assert restored.exif_error == "不是有效的 TIFF/NEF 文件头"


def test_get_photo_missing_returns_none(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        assert db.get_photo(Path("D:/nope/missing.NEF")) is None


# --------------------------------------------------------------------------- #
# 2. 重复扫描：按路径去重
# --------------------------------------------------------------------------- #


def test_upsert_same_path_twice_updates_instead_of_duplicating(db_path: Path) -> None:
    path = Path("D:/Photos/a.NEF")

    with PhotoDatabase.open(db_path) as db:
        db.upsert_photo(make_info(path, size_bytes=100, iso=100))
        db.upsert_photo(make_info(path, size_bytes=200, iso=200))

        assert db.count_photos() == 1
        restored = db.get_photo(path)

    assert restored is not None
    assert restored.size_bytes == 200
    assert restored.iso == 200


def test_different_paths_are_separate_records(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.upsert_many(
            [
                make_info(Path("D:/Photos/a.NEF")),
                make_info(Path("D:/Photos/sub/a.NEF")),  # 同名不同目录
            ]
        )
        assert db.count_photos() == 2


def test_unresolved_path_is_stored_resolved(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """相对路径在入库时按当时 cwd 解析成绝对路径。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "x.NEF").write_bytes(b"x")

    with PhotoDatabase.open(db_path) as db:
        db.upsert_photo(make_info(Path("photos/x.NEF")))
        assert db.count_photos() == 1
        assert db.get_photo(Path("photos/x.NEF")) is not None
        assert db.get_photo(tmp_path / "photos" / "x.NEF") is not None


# --------------------------------------------------------------------------- #
# 3. 查询
# --------------------------------------------------------------------------- #


def test_find_photos_by_name_substring_case_insensitive(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.upsert_many(
            [
                make_info(Path("D:/Photos/DSC_0012.NEF")),
                make_info(Path("D:/Photos/DSC_0031.NEF")),
                make_info(Path("D:/Photos/IMG_0007.JPG"), category=CATEGORY_JPEG),
                make_info(Path("D:/Other/dsc_0099.NEF")),
            ]
        )

        found = db.find_photos("DSC_00")
        assert {info.file_name for info in found} == {
            "DSC_0012.NEF",
            "DSC_0031.NEF",
            "dsc_0099.NEF",
        }

        # 也能按路径中的目录名匹配（不用分隔符，跨 Windows / POSIX）
        assert {info.file_name for info in db.find_photos("Other")} == {"dsc_0099.NEF"}


def test_find_photos_limit(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.upsert_many([make_info(Path(f"D:/Photos/DSC_{i:04d}.NEF")) for i in range(10)])
        assert len(db.find_photos("DSC_", limit=3)) == 3


def test_find_photos_empty_match_returns_anything(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.upsert_many([make_info(Path("D:/Photos/a.NEF")), make_info(Path("D:/Photos/b.NEF"))])
        assert len(db.find_photos("", limit=10)) == 2


def test_iter_photos_is_ordered_and_complete(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.upsert_many(
            [
                make_info(Path("D:/Photos/b.NEF")),
                make_info(Path("D:/Photos/a.NEF")),
                make_info(Path("D:/Photos/c.NEF")),
            ]
        )
        names = [info.file_name for info in db.iter_photos()]

    assert names == ["a.NEF", "b.NEF", "c.NEF"]


# --------------------------------------------------------------------------- #
# 4. 统计
# --------------------------------------------------------------------------- #


def test_count_by_category_and_exif_status(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.upsert_many(
            [
                make_info(Path("D:/P/a.NEF"), exif_status=EXIF_OK),
                make_info(Path("D:/P/b.NEF"), exif_status=EXIF_OK),
                make_info(Path("D:/P/c.NEF"), exif_status=EXIF_ERROR, exif_error="broken"),
                make_info(Path("D:/P/d.JPG"), category=CATEGORY_JPEG, exif_status=EXIF_NO_EXIF),
                make_info(Path("D:/P/e.MOV"), category=CATEGORY_VIDEO, exif_status=EXIF_SKIPPED, taken_at=None),
            ]
        )

        assert db.count_by("category") == {"nef": 3, "jpeg": 1, "video": 1}

        statuses = db.count_by_exif_status()
        assert statuses[EXIF_OK] == 2
        assert statuses[EXIF_ERROR] == 1
        assert statuses[EXIF_NO_EXIF] == 1
        assert statuses[EXIF_SKIPPED] == 1


def test_count_by_camera_make(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.upsert_many(
            [
                make_info(Path("D:/P/a.NEF"), camera_make="NIKON CORPORATION"),
                make_info(Path("D:/P/b.NEF"), camera_make="NIKON CORPORATION"),
                make_info(Path("D:/P/c.NEF"), camera_make=None),
            ]
        )

        counts = db.count_by("camera_make")
        assert counts["NIKON CORPORATION"] == 2
        assert counts["（未知）"] == 1


def test_count_by_rejects_unknown_column(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        with pytest.raises(ValueError):
            db.count_by("path; DROP TABLE photos; --")


# --------------------------------------------------------------------------- #
# 5. 数据库文件行为
# --------------------------------------------------------------------------- #


def test_database_file_is_created_with_parent_dirs(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "photos.db"
    with PhotoDatabase.open(db_path) as db:
        db.upsert_photo(make_info(Path("D:/P/a.NEF")))
    assert db_path.exists()
    assert db_path.stat().st_size > 0


def test_reopening_existing_database_keeps_records(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.upsert_photo(make_info(Path("D:/P/a.NEF")))

    with PhotoDatabase.open(db_path) as db:
        assert db.count_photos() == 1
        assert db.get_photo(Path("D:/P/a.NEF")) is not None


def test_empty_upsert_many_writes_nothing(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        assert db.upsert_many([]) == 0
        assert db.count_photos() == 0


# --------------------------------------------------------------------------- #
# 6. 文件夹库
# --------------------------------------------------------------------------- #


def test_folder_library_add_list_remove(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.add_folder("D:/Photos/2026")
        db.add_folder("D:/Photos/2026")  # 重复添加被忽略
        db.add_folder("D:/Photos/2025")

        folders = db.list_folders()
        assert [f.path for f in folders] == [
            Path("D:/Photos/2025").resolve(),
            Path("D:/Photos/2026").resolve(),
        ]
        assert all(f.photo_count == 0 for f in folders)
        assert all(f.last_scanned_at is None for f in folders)

        db.remove_folder("D:/Photos/2025")
        assert len(db.list_folders()) == 1


def test_mark_folder_scanned_creates_and_updates(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.mark_folder_scanned("D:/Photos/2026")
        first = db.list_folders()[0]
        assert first.last_scanned_at is not None

        db.mark_folder_scanned("D:/Photos/2026")
        assert len(db.list_folders()) == 1


def test_photo_count_under_folder(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.add_folder("D:/Photos/2026")
        db.upsert_many(
            [
                make_info(Path("D:/Photos/2026/a.NEF")),
                make_info(Path("D:/Photos/2026/sub/b.NEF")),
                make_info(Path("D:/Photos/20260/c.NEF")),  # 前缀相似但不是子文件夹
                make_info(Path("D:/Photos/2025/d.NEF")),
            ]
        )

        assert db.count_photos_under("D:/Photos/2026") == 2
        folders = {f.path.name: f.photo_count for f in db.list_folders()}
        assert folders["2026"] == 2


def test_find_photos_under_folders(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.upsert_many(
            [
                make_info(Path("D:/Photos/2026/a.NEF")),
                make_info(Path("D:/Photos/2025/b.NEF")),
                make_info(Path("D:/Other/c.NEF")),
            ]
        )

        found = db.find_photos_under(["D:/Photos/2026", "D:/Photos/2025"])
        assert {info.file_name for info in found} == {"a.NEF", "b.NEF"}

        assert db.find_photos_under([]) == []
        assert len(db.find_photos_under(["D:/Photos"], limit=1)) == 1


def test_remove_folder_keeps_photo_records(db_path: Path) -> None:
    with PhotoDatabase.open(db_path) as db:
        db.add_folder("D:/Photos/2026")
        db.upsert_photo(make_info(Path("D:/Photos/2026/a.NEF")))

        db.remove_folder("D:/Photos/2026")

        assert db.list_folders() == []
        assert db.count_photos() == 1  # 只移除库条目，不动照片记录


def test_remove_photos_cascades_and_keeps_file(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    keep = tmp_path / "keep.JPG"
    drop = tmp_path / "drop.JPG"
    keep.write_bytes(b"keep")
    drop.write_bytes(b"drop")

    with PhotoDatabase.open(db_path) as database:
        database.upsert_many([make_info(keep), make_info(drop)])
    with TagStore.open(db_path) as tags:
        tag = tags.ensure_tag("建筑")
        tags.add_tags([keep, drop], [tag.id])
    with QualityStore.open(db_path) as quality:
        quality.upsert_many(
            [
                QualityMeasurement(
                    path=str(keep.resolve()),
                    analyzer_version=ANALYZER_VERSION,
                    analyzed_at="2026-10-21 00:00:00",
                    preview_source="full_image",
                    preview_width=640,
                    preview_height=480,
                    blur_score=500.0,
                    contrast=50.0,
                    anisotropy=0.1,
                    mean_lum=110.0,
                    std_lum=45.0,
                    clip_low_frac=0.0,
                    clip_high_frac=0.0,
                    dhash="0" * 16,
                    quick_hash="quick",
                    file_size=4,
                    file_mtime="2026-10-21 00:00:00",
                ),
                QualityMeasurement(
                    path=str(drop.resolve()),
                    analyzer_version=ANALYZER_VERSION,
                    analyzed_at="2026-10-21 00:00:00",
                    preview_source="full_image",
                    preview_width=640,
                    preview_height=480,
                    blur_score=500.0,
                    contrast=50.0,
                    anisotropy=0.1,
                    mean_lum=110.0,
                    std_lum=45.0,
                    clip_low_frac=0.0,
                    clip_high_frac=0.0,
                    dhash="1" * 16,
                    quick_hash="quick2",
                    file_size=4,
                    file_mtime="2026-10-21 00:00:00",
                ),
            ]
        )

    with PhotoDatabase.open(db_path) as database:
        assert database.remove_photos([drop]) == 1
        assert database.get_photo(drop) is None
        assert database.count_photos() == 1
    with TagStore.open(db_path) as tags:
        assert tags.counts()[tag.id] == 1
    with QualityStore.open(db_path) as quality:
        assert quality.count() == 1

    assert drop.read_bytes() == b"drop"  # 原文件仍在
    assert keep.read_bytes() == b"keep"
    assert db_path.exists()
