"""`src.shell` 的测试：打开文件位置的降级链（不真的启动资源管理器）。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest

from src import shell


def test_missing_file_returns_error(tmp_path: Path) -> None:
    ok, error = shell.open_in_explorer(tmp_path / "nope.JPG")
    assert ok is False
    assert error and "不存在" in error


def test_shell_api_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "a.JPG"
    target.write_bytes(b"x")
    monkeypatch.setattr(shell, "_select_with_shell_api", lambda _path: None)
    assert shell.open_in_explorer(target) == (True, None)


def test_falls_back_to_opening_parent_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "a.JPG"
    target.write_bytes(b"x")
    monkeypatch.setattr(shell, "_select_with_shell_api", lambda _path: "拒绝访问")
    opened = []
    monkeypatch.setattr(shell.os, "startfile", lambda path: opened.append(path))
    assert shell.open_in_explorer(target) == (True, None)
    assert opened == [str(target.parent)]


def test_reports_error_when_everything_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "a.JPG"
    target.write_bytes(b"x")
    monkeypatch.setattr(shell, "_select_with_shell_api", lambda _path: "系统拒绝打开文件位置")
    monkeypatch.setattr(
        shell.os, "startfile", lambda _path: (_ for _ in ()).throw(PermissionError(5, "拒绝访问"))
    )
    ok, error = shell.open_in_explorer(target)
    assert ok is False
    assert error == "系统拒绝打开文件位置"


# --------------------------------------------------------------------------- #
# 用默认程序打开图片
# --------------------------------------------------------------------------- #


def test_open_with_default_app_missing_file(tmp_path: Path) -> None:
    ok, error = shell.open_with_default_app(tmp_path / "nope.NEF")
    assert ok is False
    assert error and "不存在" in error


def test_open_image_uses_windows_photos_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "a.JPG"
    target.write_bytes(b"x")
    opened = []
    monkeypatch.setattr(shell.os, "startfile", lambda path: opened.append(path))
    assert shell.open_with_default_app(target) == (True, None)
    assert opened and opened[0].startswith("ms-photos:viewer?fileName=")
    assert quote(str(target), safe="") in opened[0]


def test_open_video_uses_default_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "a.MOV"
    target.write_bytes(b"x")
    opened = []
    monkeypatch.setattr(shell.os, "startfile", lambda path: opened.append(path))
    assert shell.open_with_default_app(target) == (True, None)
    assert opened == [str(target)]


def test_open_falls_back_to_default_when_photos_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "a.NEF"
    target.write_bytes(b"x")
    calls = []

    def fake_startfile(path: str) -> None:
        calls.append(path)
        if path.startswith("ms-photos:"):
            raise OSError(5, "没有照片应用")

    monkeypatch.setattr(shell.os, "startfile", fake_startfile)
    assert shell.open_with_default_app(target) == (True, None)
    assert calls[0].startswith("ms-photos:")
    assert calls[1] == str(target)


def test_open_with_default_app_reports_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "a.NEF"
    target.write_bytes(b"x")
    monkeypatch.setattr(
        shell.os, "startfile", lambda _path: (_ for _ in ()).throw(PermissionError(5, "拒绝访问"))
    )
    ok, error = shell.open_with_default_app(target)
    assert ok is False
    assert error and "无法打开文件" in error
