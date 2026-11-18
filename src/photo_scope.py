"""Resolve photo scopes independently of pages and widgets."""

from collections.abc import Iterable, Mapping
from pathlib import Path

from src.database import PhotoDatabase
from src.photo_info import PhotoInfo
from src.preview import is_supported


def source_photos(db: PhotoDatabase, folders: Iterable[Path], *, pick_list_id: int | None = None) -> list[PhotoInfo]:
    if pick_list_id is not None:
        return db.photos_in_pick_list(pick_list_id)
    return db.find_photos_under(folders)


def photos_at_paths(paths: Iterable[str], infos: Mapping[str, PhotoInfo]) -> list[PhotoInfo]:
    return [infos[path] for path in paths if path in infos]


def supported_photos(photos: Iterable[PhotoInfo]) -> list[PhotoInfo]:
    return [info for info in photos if is_supported(info.category)]


def result_paths(rows: Iterable[str], stacks: Mapping[str, tuple[str, ...]]) -> Iterable[str]:
    for row in rows:
        yield from stacks.get(row, (row,))
