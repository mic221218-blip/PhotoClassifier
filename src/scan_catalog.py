"""Incremental, read-only scan shared by the desktop and command line."""
from __future__ import annotations

from os.path import normcase

from src.exif_reader import read_photo_info
from src.paths import db_key
from src.photo_info import EXIF_ERROR, PhotoInfo
from src.scanner import scan_folder


def scan_catalog(db, root, *, full=False, cancelled=lambda: False, progress=None):
    result = scan_folder(root, cancelled=cancelled)
    infos: list[PhotoInfo] = []
    batch: list[PhotoInfo] = []
    written = 0
    if not result.files or cancelled():
        return result, infos, written
    canonical = result.root.resolve()
    previous = {} if full else {normcase(db_key(info.path)): info for info in db.find_photos_under([canonical])}
    total = len(result.files)
    for index, item in enumerate(result.files, 1):
        if cancelled():
            break
        old = previous.get(normcase(db_key(canonical / item.path.relative_to(result.root))))
        if (
            old is not None and old.exif_status != EXIF_ERROR and item.mtime_ns is not None
            and (old.size_bytes, old.file_mtime_ns) == (item.size_bytes, item.mtime_ns)
        ):
            infos.append(old)
        else:
            info = read_photo_info(item.path, item.category)
            infos.append(info)
            batch.append(info)
        if len(batch) >= 128:
            written += db.upsert_many(batch)
            batch.clear()
        if progress is not None and (index % 5 == 0 or index == total):
            progress(index, total, item.path.name)
    if batch:
        written += db.upsert_many(batch)
    return result, infos, written
