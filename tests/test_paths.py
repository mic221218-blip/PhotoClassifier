"""`src.paths` 的测试：数据库 key 与用户路径解析。"""

from __future__ import annotations

import os
from pathlib import Path

from src.paths import db_key, resolve_key


def test_db_key_absolute_is_normalised_without_resolve(tmp_path: Path) -> None:
    target = tmp_path / "a.JPG"
    assert db_key(target) == os.path.normpath(str(target))
    assert db_key(str(target).replace("\\", "/")) == os.path.normpath(str(target))


def test_db_key_relative_is_resolved() -> None:
    assert Path(db_key("a.JPG")).is_absolute()


def test_resolve_key_is_absolute() -> None:
    assert Path(resolve_key("a.JPG")).is_absolute()
