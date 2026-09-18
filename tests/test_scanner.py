"""`src.scanner` 的基础测试（V1）。

约定：

- 全部使用 pytest 的临时目录（``tmp_path``），不接触任何真实照片；
- 所有创建/写入都发生在临时目录内，测试结束后由 pytest 自动清理；
- 不测试扫描器以外的功能（EXIF、AI、GUI 等均未实现）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.scanner import (
    CATEGORIES,
    CATEGORY_JPEG,
    CATEGORY_NEF,
    CATEGORY_PNG,
    CATEGORY_UNSUPPORTED,
    CATEGORY_VIDEO,
    classify_file,
    scan_folder,
)

# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def make_file(root: Path, relative: str, content: bytes = b"fake") -> Path:
    """在临时目录里创建一个文件（必要时先建父目录），返回该文件路径。"""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def snapshot_tree(root: Path) -> list[tuple[str, str, int, int, bytes]]:
    """给整个目录树拍快照：相对路径、类型、大小、mtime、内容。

    用于证明扫描过程没有修改、移动、删除或新建任何文件。
    """
    entries: list[tuple[str, str, int, int, bytes]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative, "dir", 0, 0, b""))
            continue
        stat = path.stat()
        entries.append(
            (relative, "file", stat.st_size, stat.st_mtime_ns, path.read_bytes())
        )
    return entries


def names_of(files) -> set[str]:
    """取出扫描结果的文件名集合。"""
    return {item.path.name for item in files}


# --------------------------------------------------------------------------- #
# 1. 扩展名识别（大小写不敏感）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["IMG.NEF", "img.nef", "DSC_0001.Nef", "a.NeF"])
def test_nef_is_case_insensitive(name: str) -> None:
    assert classify_file(name) == CATEGORY_NEF


@pytest.mark.parametrize("name", ["IMG.JPG", "img.jpg", "IMG.JPEG", "img.jpeg", "a.JpEg"])
def test_jpg_and_jpeg_are_case_insensitive(name: str) -> None:
    assert classify_file(name) == CATEGORY_JPEG


@pytest.mark.parametrize("name", ["IMG.PNG", "img.png", "a.PnG"])
def test_png_is_case_insensitive(name: str) -> None:
    assert classify_file(name) == CATEGORY_PNG


@pytest.mark.parametrize(
    "name", ["clip.MOV", "clip.mov", "clip.MP4", "clip.mp4", "clip.MTS", "clip.m2ts"]
)
def test_video_is_case_insensitive(name: str) -> None:
    assert classify_file(name) == CATEGORY_VIDEO


@pytest.mark.parametrize(
    "name",
    ["notes.txt", "doc.PDF", "archive.zip", "unknown.xyz", "raw.ARW", "no_extension"],
)
def test_unknown_extensions_are_unsupported(name: str) -> None:
    assert classify_file(name) == CATEGORY_UNSUPPORTED


def test_classify_accepts_path_objects_and_nested_paths() -> None:
    assert classify_file(Path("a/b/IMG.NEF")) == CATEGORY_NEF
    assert classify_file("a/b/IMG.jPeG") == CATEGORY_JPEG
    assert classify_file(Path("a/b/notes.txt")) == CATEGORY_UNSUPPORTED


def test_classify_does_not_raise_for_odd_names() -> None:
    for name in ["", ".", "..", ".hidden", "trailing.", "a/b/c"]:
        assert classify_file(name) == CATEGORY_UNSUPPORTED


# --------------------------------------------------------------------------- #
# 2. 扫描：混合文件夹
# --------------------------------------------------------------------------- #


def test_mixed_folder_is_classified_correctly(tmp_path: Path) -> None:
    make_file(tmp_path, "A.NEF")
    make_file(tmp_path, "b.nef")
    make_file(tmp_path, "C.JPG", b"jpeg-uppercase")
    make_file(tmp_path, "d.JPEG", b"jpeg-lowercase")
    make_file(tmp_path, "E.PnG")
    make_file(tmp_path, "f.MOV")
    make_file(tmp_path, "g.mp4")
    make_file(tmp_path, "notes.txt")
    make_file(tmp_path, "IMG.xyz")

    result = scan_folder(tmp_path)

    assert result.root == tmp_path
    assert result.issues == []
    assert result.total_files == 9
    assert result.count_by_category() == {
        CATEGORY_NEF: 2,
        CATEGORY_JPEG: 2,
        CATEGORY_PNG: 1,
        CATEGORY_VIDEO: 2,
        CATEGORY_UNSUPPORTED: 2,
    }
    assert names_of(result.files_by_category(CATEGORY_NEF)) == {"A.NEF", "b.nef"}
    assert names_of(result.files_by_category(CATEGORY_JPEG)) == {"C.JPG", "d.JPEG"}
    assert names_of(result.files_by_category(CATEGORY_PNG)) == {"E.PnG"}
    assert names_of(result.files_by_category(CATEGORY_VIDEO)) == {"f.MOV", "g.mp4"}
    assert names_of(result.files_by_category(CATEGORY_UNSUPPORTED)) == {
        "notes.txt",
        "IMG.xyz",
    }

    # 每个文件的分类都必须是已知分类，且 size 来自真实文件
    for item in result.files:
        assert item.category in CATEGORIES
        assert item.size_bytes == item.path.stat().st_size


def test_folder_with_only_unsupported_files_does_not_fail(tmp_path: Path) -> None:
    make_file(tmp_path, "a.txt")
    make_file(tmp_path, "b.psd")
    make_file(tmp_path, "c")  # 没有扩展名

    result = scan_folder(tmp_path)

    assert result.issues == []
    assert result.total_files == 3
    assert result.count_by_category()[CATEGORY_UNSUPPORTED] == 3


def test_empty_folder(tmp_path: Path) -> None:
    result = scan_folder(tmp_path)

    assert result.total_files == 0
    assert result.files == []
    assert result.issues == []
    assert result.count_by_category() == {category: 0 for category in CATEGORIES}


def test_accepts_string_path(tmp_path: Path) -> None:
    make_file(tmp_path, "a.NEF")

    result = scan_folder(str(tmp_path))

    assert result.total_files == 1
    assert names_of(result.files) == {"a.NEF"}


# --------------------------------------------------------------------------- #
# 3. 扫描：递归子文件夹
# --------------------------------------------------------------------------- #


def test_scan_is_recursive(tmp_path: Path) -> None:
    make_file(tmp_path, "top.NEF")
    make_file(tmp_path, "2024/01/IMG_0001.JPG")
    make_file(tmp_path, "2024/01/IMG_0001.NEF")
    make_file(tmp_path, "2024/02/notes.txt")
    make_file(tmp_path, "2024/02/video/clip.MP4")
    make_file(tmp_path, "2024/02/video/deep/deeper/x.PNG")

    result = scan_folder(tmp_path)

    assert result.issues == []
    assert result.total_files == 6
    assert {item.path.relative_to(tmp_path).as_posix() for item in result.files} == {
        "top.NEF",
        "2024/01/IMG_0001.JPG",
        "2024/01/IMG_0001.NEF",
        "2024/02/notes.txt",
        "2024/02/video/clip.MP4",
        "2024/02/video/deep/deeper/x.PNG",
    }
    assert result.count_by_category() == {
        CATEGORY_NEF: 2,
        CATEGORY_JPEG: 1,
        CATEGORY_PNG: 1,
        CATEGORY_VIDEO: 1,
        CATEGORY_UNSUPPORTED: 1,
    }


# --------------------------------------------------------------------------- #
# 4. 扫描：路径无效
# --------------------------------------------------------------------------- #


def test_nonexistent_folder_reports_issue_instead_of_raising(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"

    result = scan_folder(missing)

    assert result.files == []
    assert result.total_files == 0
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.path == missing
    assert "路径不存在或不是文件夹" in issue.message


def test_path_pointing_to_a_file_reports_issue(tmp_path: Path) -> None:
    file_path = make_file(tmp_path, "single.JPG")

    result = scan_folder(file_path)

    assert result.total_files == 0
    assert len(result.issues) == 1
    assert result.issues[0].path == file_path


# --------------------------------------------------------------------------- #
# 5. 扫描：单个文件/文件夹出错时应记录并继续
# --------------------------------------------------------------------------- #


def test_unreadable_file_is_recorded_and_scan_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_file(tmp_path, "ok1.NEF")
    make_file(tmp_path, "locked.JPG")
    make_file(tmp_path, "ok2.MOV")

    real_stat = Path.stat

    def fake_stat(self: Path, *, follow_symlinks: bool = True):
        if self.name == "locked.JPG":
            raise PermissionError(13, "simulated access denied")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", fake_stat)

    result = scan_folder(tmp_path)

    # 坏文件被记录成 issue，好文件照常返回
    assert names_of(result.files) == {"ok1.NEF", "ok2.MOV"}
    assert result.total_files == 2
    assert len(result.issues) == 1
    assert result.issues[0].path.name == "locked.JPG"
    assert "无法读取文件信息" in result.issues[0].message


def test_unreadable_subfolder_is_recorded_and_scan_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_file(tmp_path, "keep.NEF")
    make_file(tmp_path, "locked_dir/inner.JPG")
    make_file(tmp_path, "other/ok.PNG")

    real_scandir = os.scandir

    def fake_scandir(path):
        if Path(path).name == "locked_dir":
            raise PermissionError(13, "simulated access denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    result = scan_folder(tmp_path)

    assert names_of(result.files) == {"keep.NEF", "ok.PNG"}
    assert result.total_files == 2
    assert [issue.path.name for issue in result.issues] == ["locked_dir"]
    assert "无法读取文件夹" in result.issues[0].message


def test_many_broken_files_still_return_every_good_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = {f"broken{i}.JPG" for i in range(5)}
    for name in sorted(broken):
        make_file(tmp_path, name)
    make_file(tmp_path, "good.NEF")
    make_file(tmp_path, "sub/good.PNG")

    real_stat = Path.stat

    def fake_stat(self: Path, *, follow_symlinks: bool = True):
        if self.name in broken:
            raise OSError(5, "simulated I/O error")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", fake_stat)

    result = scan_folder(tmp_path)

    assert names_of(result.files) == {"good.NEF", "good.PNG"}
    assert {issue.path.name for issue in result.issues} == broken


# --------------------------------------------------------------------------- #
# 6. 扫描过程必须只读
# --------------------------------------------------------------------------- #


def test_scan_does_not_modify_move_or_delete_files(tmp_path: Path) -> None:
    make_file(tmp_path, "A.NEF", b"nef-content")
    make_file(tmp_path, "B.JPG", b"jpeg-content")
    make_file(tmp_path, "sub/C.PNG", b"png-content")
    make_file(tmp_path, "sub/deeper/D.MOV", b"video-content")
    make_file(tmp_path, "notes.txt", b"text-content")

    before = snapshot_tree(tmp_path)

    result = scan_folder(tmp_path)
    assert result.total_files == 5

    # 目录树（路径 + 大小 + mtime + 内容）完全没变
    assert snapshot_tree(tmp_path) == before

    # 再扫一次同样不改变任何东西
    scan_folder(tmp_path)
    assert snapshot_tree(tmp_path) == before


def test_scan_does_not_create_files(tmp_path: Path) -> None:
    make_file(tmp_path, "one.NEF")
    before = snapshot_tree(tmp_path)

    scan_folder(tmp_path)

    assert len(snapshot_tree(tmp_path)) == len(before)
