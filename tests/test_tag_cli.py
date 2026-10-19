"""V3 CLI 测试：tags / tag add|remove|list|rename|delete / filter --tags。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PIL.Image")

from src.main import main  # noqa: E402
from src.tag_store import TagStore  # noqa: E402
from src.tags import PRESET_TAGS  # noqa: E402

from image_maker import checkerboard, save_jpeg_with_exif  # noqa: E402


@pytest.fixture()
def photo_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "photos"
    folder.mkdir()
    save_jpeg_with_exif(folder / "a.JPG", checkerboard(), taken_at="2026:07:13 20:15:00")
    save_jpeg_with_exif(folder / "b.JPG", checkerboard(), taken_at="2026:07:13 05:30:00")
    return folder


@pytest.fixture()
def db_path(tmp_path: Path, photo_folder: Path) -> Path:
    path = tmp_path / "photos.db"
    assert main(["scan", str(photo_folder), "--db", str(path)]) == 0
    return path


def test_tags_lists_presets(db_path: Path, capsys) -> None:
    assert main(["tags", "--db", str(db_path)]) == 0
    output = capsys.readouterr().out
    assert "标签：10 个" in output
    for name in PRESET_TAGS:
        assert name in output


def test_tag_add_and_list_path(db_path: Path, photo_folder: Path, capsys) -> None:
    target = photo_folder / "a.JPG"
    assert main(["tag", "add", "--db", str(db_path), "--tag", "建筑", "--tag", "待修", str(target)]) == 0
    output = capsys.readouterr().out
    assert "新建标签：待修" in output
    assert "新增 2 条关联" in output

    assert main(["tag", "list", "--db", str(db_path), str(target)]) == 0
    output = capsys.readouterr().out
    assert "建筑" in output and "待修" in output


def test_tag_add_by_match(db_path: Path, photo_folder: Path, capsys) -> None:
    assert main(["tag", "add", "--db", str(db_path), "--tag", "风光", "--match", "a.JPG"]) == 0
    output = capsys.readouterr().out
    assert "匹配照片：1 张" in output
    assert "新增 1 条关联" in output


def test_tag_add_unknown_path_returns_1(db_path: Path, tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope.JPG"
    assert main(["tag", "add", "--db", str(db_path), "--tag", "建筑", str(missing)]) == 1
    assert "不在照片库" in capsys.readouterr().out


def test_filter_by_tag(db_path: Path, photo_folder: Path, capsys) -> None:
    assert main(["tag", "add", "--db", str(db_path), "--tag", "建筑", str(photo_folder / "a.JPG")]) == 0
    capsys.readouterr()
    assert main(["filter", "--db", str(db_path), "--tags", "建筑"]) == 0
    output = capsys.readouterr().out
    assert "a.JPG" in output and "b.JPG" not in output
    assert "共 1 张" in output


def test_filter_rule_and_tag(db_path: Path, photo_folder: Path, capsys) -> None:
    assert main(["tag", "add", "--db", str(db_path), "--tag", "建筑", str(photo_folder / "a.JPG")]) == 0
    capsys.readouterr()
    # a.JPG 20:15 命中夜景；b.JPG 没有建筑标签
    assert main(["filter", "--db", str(db_path), "--rules", "night", "--tags", "建筑"]) == 0
    output = capsys.readouterr().out
    assert "共 1 张" in output

    # OR：夜景（两张都命中）或 建筑
    assert main(["filter", "--db", str(db_path), "--rules", "night", "--tags", "建筑", "--or"]) == 0
    assert "共 2 张" in capsys.readouterr().out


def test_filter_unknown_tag_exits(db_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["filter", "--db", str(db_path), "--tags", "不存在的标签"])


def test_tag_remove(db_path: Path, photo_folder: Path, capsys) -> None:
    target = photo_folder / "a.JPG"
    main(["tag", "add", "--db", str(db_path), "--tag", "建筑", str(target)])
    capsys.readouterr()
    assert main(["tag", "remove", "--db", str(db_path), "--tag", "建筑", str(target)]) == 0
    assert "删除 1 条关联" in capsys.readouterr().out


def test_tag_rename_and_delete(db_path: Path, photo_folder: Path, capsys) -> None:
    target = photo_folder / "a.JPG"
    main(["tag", "add", "--db", str(db_path), "--tag", "待修", str(target)])
    capsys.readouterr()

    assert main(["tag", "rename", "--db", str(db_path), "待修", "待精修"]) == 0
    assert "待修 → 待精修" in capsys.readouterr().out

    # 删除需要 --yes
    assert main(["tag", "delete", "--db", str(db_path), "待精修"]) == 1
    assert "确认删除请加 --yes" in capsys.readouterr().out
    assert main(["tag", "delete", "--db", str(db_path), "待精修", "--yes"]) == 0
    assert "已删除标签" in capsys.readouterr().out


def test_show_prints_tags(db_path: Path, photo_folder: Path, capsys) -> None:
    main(["tag", "add", "--db", str(db_path), "--tag", "建筑", str(photo_folder / "a.JPG")])
    capsys.readouterr()
    assert main(["show", "--db", str(db_path), "--match", "a.JPG"]) == 0
    assert "标签：建筑" in capsys.readouterr().out


def test_tags_counts_after_add(db_path: Path, photo_folder: Path, capsys) -> None:
    main(["tag", "add", "--db", str(db_path), "--tag", "建筑", str(photo_folder / "a.JPG")])
    capsys.readouterr()
    assert main(["tags", "--db", str(db_path)]) == 0
    output = capsys.readouterr().out
    assert "建筑：1 张" in output


def test_tag_list_by_tag(db_path: Path, photo_folder: Path, capsys) -> None:
    main(["tag", "add", "--db", str(db_path), "--tag", "建筑", str(photo_folder / "a.JPG")])
    capsys.readouterr()
    assert main(["tag", "list", "--db", str(db_path), "--tag", "建筑"]) == 0
    output = capsys.readouterr().out
    assert "标签「建筑」：1 张" in output
    assert "a.JPG" in output
