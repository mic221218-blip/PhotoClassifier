"""P0 性能/结构修复的回归测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

from src.database import PhotoDatabase
from src.duplicates import DuplicateCandidate, DuplicateFinder
from src.paths import db_key


def test_duplicate_lookup_does_not_resolve_paths(tmp_path: Path) -> None:
    candidate = DuplicateCandidate(
        path=str(tmp_path / "a.JPG"),
        size_bytes=1,
        taken_at="2026-10-20 10:00:00",
        dhash="0" * 16,
    )
    other = DuplicateCandidate(
        path=str(tmp_path / "b.JPG"),
        size_bytes=1,
        taken_at="2026-10-20 10:00:01",
        dhash="0" * 16,
    )
    result = DuplicateFinder().find([candidate, other])
    assert result.groups

    with mock.patch("pathlib.Path.resolve", side_effect=AssertionError("不应调用 resolve")):
        info = result.info_for(candidate.path)
        assert info is not None
        assert result.flag_reason(candidate.path)


def test_wal_and_busy_timeout_enabled(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    with PhotoDatabase.open(db_path):
        pass
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        connection.close()


def test_db_key_matches_database_path(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    target = tmp_path / "a.JPG"
    with PhotoDatabase.open(db_path) as database:
        from test_database import make_info

        database.upsert_photo(make_info(target))
        info = database.get_photo(target)
    assert info is not None
    assert db_key(info.path) == str(info.path)
