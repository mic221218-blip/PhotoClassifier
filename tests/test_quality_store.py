"""`src.quality_store` 与 schema 迁移的测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.database import SCHEMA_VERSION, PhotoDatabase
from src.quality import ANALYZER_VERSION, QualityMeasurement
from src.quality_store import QualityStore

from test_database import make_info


def measurement(path: Path | str, **overrides) -> QualityMeasurement:
    fields = dict(
        path=str(Path(path).resolve()),
        analyzer_version=ANALYZER_VERSION,
        analyzed_at="2026-07-13 06:00:00",
        preview_source="full_image",
        preview_width=640,
        preview_height=480,
        blur_score=500.0,
        contrast=50.0,
        anisotropy=0.10,
        mean_lum=110.0,
        std_lum=45.0,
        clip_low_frac=0.001,
        clip_high_frac=0.001,
        dhash="0000000000000000",
        quick_hash="quick",
        file_size=12345,
        file_mtime="2026-09-18 21:00:00",
    )
    fields.update(overrides)
    return QualityMeasurement(**fields)


def test_schema_creates_quality_table_and_version(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    with PhotoDatabase.open(db_path):
        pass
    connection = sqlite3.connect(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
    assert version == SCHEMA_VERSION
    assert "photo_quality" in tables


def test_store_works_on_raw_database_file(tmp_path: Path) -> None:
    db_path = tmp_path / "raw.db"
    sqlite3.connect(db_path).close()  # 空文件
    with QualityStore.open(db_path) as store:
        assert store.count() == 0
        store.upsert(measurement(tmp_path / "a.JPG"))
        assert store.count() == 1


def test_upsert_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    PhotoDatabase.open(db_path).close()
    with QualityStore.open(db_path) as store:
        original = measurement(tmp_path / "a.JPG")
        store.upsert(original)
        loaded = store.get(tmp_path / "a.JPG")
    assert loaded == original


def test_upsert_many_and_iter(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    PhotoDatabase.open(db_path).close()
    with QualityStore.open(db_path) as store:
        written = store.upsert_many(
            [measurement(tmp_path / "b.JPG"), measurement(tmp_path / "a.JPG")]
        )
        assert written == 2
        assert store.count() == 2
        paths = [item.path for item in store.iter_measurements()]
    assert paths == sorted(paths)


def test_stats_counts_analyzed_and_sources(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    PhotoDatabase.open(db_path).close()
    with QualityStore.open(db_path) as store:
        store.upsert_many(
            [
                measurement(tmp_path / "a.JPG"),
                measurement(
                    tmp_path / "b.NEF", preview_source="embedded_jpeg", blur_score=800.0
                ),
                measurement(
                    tmp_path / "c.JPG",
                    preview_source="none",
                    blur_score=None,
                    error="未找到内嵌预览",
                ),
            ]
        )
        stats = store.stats()
    assert stats.total == 3
    assert stats.analyzed == 2
    assert stats.failed == 1
    assert stats.embedded == 1
    assert stats.full_image == 1
    assert stats.no_preview == 1


def test_pending_detects_new_changed_and_forced(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    PhotoDatabase.open(db_path).close()
    info = make_info(tmp_path / "a.JPG", size_bytes=12345, modified_at="2026-09-18 21:00:00")
    with QualityStore.open(db_path) as store:
        assert store.pending([info]) == [info]

        store.upsert(measurement(info.path))
        assert store.pending([info]) == []

        changed = make_info(tmp_path / "a.JPG", size_bytes=999, modified_at="2026-09-18 21:00:00")
        assert store.pending([changed]) == [changed]
        assert store.pending([info], force=True) == [info]

        old_version = make_info(tmp_path / "a.JPG", size_bytes=12345)
        store.upsert(
            measurement(info.path, analyzer_version=ANALYZER_VERSION - 1, file_size=12345)
        )
        assert store.pending([old_version]) == [old_version]


def test_exact_duplicate_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    PhotoDatabase.open(db_path).close()
    with QualityStore.open(db_path) as store:
        store.upsert_many(
            [
                measurement(tmp_path / "a.JPG", quick_hash="same", file_size=10),
                measurement(tmp_path / "b.JPG", quick_hash="same", file_size=10),
                measurement(tmp_path / "c.JPG", quick_hash="other", file_size=10),
            ]
        )
        candidates = store.exact_duplicate_candidates()
        assert {Path(item.path).name for item in candidates} == {"a.JPG", "b.JPG"}

        store.set_content_hash(tmp_path / "a.JPG", "full-a")
        assert {Path(item.path).name for item in store.exact_duplicate_candidates()} == {"b.JPG"}


def test_prune_removes_orphans(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    database = PhotoDatabase.open(db_path)
    database.upsert_photo(make_info(tmp_path / "a.JPG"))
    with QualityStore.open(db_path) as store:
        store.upsert_many(
            [
                measurement(tmp_path / "a.JPG"),
                measurement(tmp_path / "gone.JPG"),
            ]
        )
        removed = store.prune()
        assert removed == 1
        assert store.count() == 1
    database.close()


def test_measurement_with_error_still_stored(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    PhotoDatabase.open(db_path).close()
    with QualityStore.open(db_path) as store:
        failed = measurement(
            tmp_path / "broken.NEF",
            preview_source="none",
            blur_score=None,
            error="NEF 结构损坏",
        )
        store.upsert(failed)
        loaded = store.get(tmp_path / "broken.NEF")
    assert loaded is not None
    assert loaded.error == "NEF 结构损坏"
    assert loaded.analyzed is False
