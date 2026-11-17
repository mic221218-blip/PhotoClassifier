from __future__ import annotations

import os
import sqlite3
import threading

import pytest

from src.database import PhotoDatabase, _SCHEMA
from src.exif_reader import read_photo_info
from src.main import run_scan
from src.quality import QualityMeasurement
from src.quality_store import QualityStore
from src import quality_scan, scan_catalog


def test_incremental_scan_handles_same_size_same_second_change_and_full_scan(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    photo = library / "sample.JPG"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    db_path = tmp_path / "catalog.db"
    reads = []
    def read(path, category):
        reads.append(path)
        return read_photo_info(path, category)
    monkeypatch.setattr(scan_catalog, "read_photo_info", read)
    assert run_scan(library, db_path).db_written == 1
    assert run_scan(library, db_path).db_written == 0
    with PhotoDatabase.open(db_path) as db:
        info = db.get_photo(photo)
        db.save_person_result(info, "test", info.file_mtime_ns, True, 0.1)
    stat = photo.stat()
    second = stat.st_mtime_ns // 1_000_000_000
    changed = second * 1_000_000_000 + (25 if stat.st_mtime_ns != second * 1_000_000_000 + 25 else 250)
    os.utime(photo, ns=(stat.st_atime_ns, changed))
    assert photo.stat().st_mtime_ns != stat.st_mtime_ns
    assert run_scan(library, db_path).db_written == 1
    with PhotoDatabase.open(db_path) as db:
        assert db.person_results("test") == {}
    assert run_scan(library, db_path, full=True).db_written == 1
    assert len(reads) == 3


def test_v3_catalog_adds_precision_without_losing_rows(tmp_path):
    path = tmp_path / "v3.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            _SCHEMA.replace("    file_mtime_ns     INTEGER,\n", "")
            + "INSERT INTO photos(path,file_name,category,size_bytes,exif_status,first_seen_at,last_scanned_at) "
            "VALUES ('C:\\test.jpg', 'test.jpg', 'jpeg', 1, 'ok', 'now', 'now');"
            "PRAGMA user_version=3;"
        )
    with PhotoDatabase.open(path) as db:
        assert db.count_photos() == 1
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(photos)")}
        assert "file_mtime_ns" in columns


def test_quality_cancellation_preserves_completed_without_hash_pass(tmp_path, monkeypatch):
    cancel = threading.Event()
    paths = [tmp_path / f"{i}.JPG" for i in range(8)]
    for path in paths:
        path.write_bytes(b"\xff\xd8\xff\xd9")
    infos = [read_photo_info(path, "jpeg") for path in paths]
    def fake_measure(info, _config):
        return QualityMeasurement(path=str(info.path), blur_score=7)
    def unexpected(*_args, **_kwargs):
        pytest.fail("Post-processing ran after cancellation")
    monkeypatch.setattr(quality_scan, "measure_photo", fake_measure)
    monkeypatch.setattr(quality_scan, "_fill_content_hashes", unexpected)
    monkeypatch.setattr(quality_scan, "_augment_report", unexpected)
    with QualityStore.open(tmp_path / "quality.db") as store:
        report = quality_scan.analyze_photos(
            infos, store, options=quality_scan.AnalyzeOptions(workers=1),
            progress=lambda *_: cancel.set(), is_cancelled=cancel.is_set,
        )
        assert report.cancelled and report.analyzed_now == 1
        assert len(list(store.iter_measurements())) == 1


def test_thumbnail_reads_jpeg_png_and_nef_preview(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from image_maker import jpeg_bytes, nef_with_preview
    from src.ui_thumbnail import _load_thumbnail

    image = Image.new("RGB", (640, 480), "red")
    jpeg = jpeg_bytes(image)
    (tmp_path / "source.JPG").write_bytes(jpeg)
    (tmp_path / "source.NEF").write_bytes(nef_with_preview(jpeg))
    image.save(tmp_path / "source.PNG")
    for extension, category in (("JPG", "jpeg"), ("NEF", "nef"), ("PNG", "png")):
        source = tmp_path / f"source.{extension}"
        before = source.read_bytes()
        thumbnail = _load_thumbnail(read_photo_info(source, category))
        assert thumbnail.size == (144, 108)
        assert thumbnail.getpixel((0, 0))[0] > 240
        assert source.read_bytes() == before
