"""`src.main`（CLI 与整体流程）的测试（V1.5）。

核心场景复刻规划第二十八节的"最终测试场景"：
一个混合了 NEF / JPEG / PNG / MOV / 损坏 NEF / 未知文件的文件夹，
程序必须完整跑完全部流程并给出报告，且**不改动任何原始文件**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import PhotoDatabase
from src.main import (
    format_classify_report,
    format_report,
    format_shutter,
    format_size,
    format_stats,
    main,
    run_scan,
)
from src.photo_info import EXIF_ERROR, EXIF_NO_EXIF, EXIF_OK, EXIF_SKIPPED
from src.rules import RuleEngine

from exif_maker import (
    build_jpeg_without_exif,
    build_png,
    nikon_z6iii_jpeg,
    nikon_z6iii_nef,
)
from test_database import make_info


def make_file(root: Path, relative: str, content: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def snapshot_tree(root: Path) -> list[tuple[str, int, int, bytes]]:
    """给目录树拍快照（相对路径、大小、mtime、内容），用于只读验证。"""
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            stat = path.stat()
            entries.append((relative, stat.st_size, stat.st_mtime_ns, path.read_bytes()))
    return entries


@pytest.fixture()
def mixed_folder(tmp_path: Path) -> Path:
    """规划中的"最终测试场景"文件夹。"""
    folder = tmp_path / "TestPhotos"
    make_file(folder, "DSC_0001.NEF", nikon_z6iii_nef())
    make_file(folder, "DSC_0002.NEF", nikon_z6iii_nef())
    make_file(folder, "DSC_0003.JPG", nikon_z6iii_jpeg())
    make_file(folder, "DSC_0004.NEF", b"corrupted nef content")
    make_file(folder, "DSC_0005.MOV", b"\x00\x00\x00\x14ftypisom" + b"v" * 64)
    make_file(folder, "DSC_0006.JPG", build_jpeg_without_exif())
    make_file(folder, "sub/DSC_0007.NEF", nikon_z6iii_nef())  # 子文件夹递归
    make_file(folder, "shot.PNG", build_png(800, 600))
    make_file(folder, "other.txt", b"just a note")
    return folder


# --------------------------------------------------------------------------- #
# 1. 整体流程 run_scan
# --------------------------------------------------------------------------- #


def test_run_scan_full_pipeline(tmp_path: Path, mixed_folder: Path) -> None:
    db_path = tmp_path / "db" / "photos.db"

    outcome = run_scan(mixed_folder, db_path=db_path)

    # 扫描结果
    assert outcome.result.total_files == 9
    assert outcome.result.issues == []
    assert len(outcome.infos) == 9

    # 数据库
    assert outcome.db_written == 9
    assert outcome.db_total == 9
    assert db_path.exists()

    by_name = {info.file_name: info for info in outcome.infos}

    # 完整 EXIF 的 NEF
    nef = by_name["DSC_0001.NEF"]
    assert nef.exif_status == EXIF_OK
    assert nef.camera == "NIKON Z6III"
    assert nef.lens_model == "NIKKOR Z 24-120mm f/4 S"
    assert nef.taken_at == "2026-09-18 20:15:32"

    # 完整 EXIF 的 JPEG
    jpg = by_name["DSC_0003.JPG"]
    assert jpg.exif_status == EXIF_OK
    assert jpg.iso == 1600

    # 损坏的 NEF：异常但不中断
    broken = by_name["DSC_0004.NEF"]
    assert broken.exif_status == EXIF_ERROR
    assert broken.exif_error is not None

    # 无 EXIF 的 JPEG：正常
    plain_jpg = by_name["DSC_0006.JPG"]
    assert plain_jpg.exif_status == EXIF_NO_EXIF

    # 视频 / 未知格式：只记录文件信息
    assert by_name["DSC_0005.MOV"].exif_status == EXIF_SKIPPED
    assert by_name["other.txt"].exif_status == EXIF_SKIPPED

    # PNG：读了尺寸
    png = by_name["shot.PNG"]
    assert png.exif_status == EXIF_SKIPPED
    assert (png.width_px, png.height_px) == (800, 600)

    # 子文件夹递归扫描到了
    assert "sub/DSC_0007.NEF" in {i.path.relative_to(mixed_folder).as_posix() for i in outcome.infos}


def test_run_scan_never_touches_original_files(tmp_path: Path, mixed_folder: Path) -> None:
    """原始照片安全原则：整个流程结束后，目录树一个字节都不能变。"""
    before = snapshot_tree(mixed_folder)
    db_path = tmp_path / "db" / "photos.db"

    run_scan(mixed_folder, db_path=db_path)
    run_scan(mixed_folder, db_path=db_path)  # 再跑一遍也一样

    assert snapshot_tree(mixed_folder) == before
    # 数据库在照片文件夹之外
    assert not (mixed_folder / "photos.db").exists()


def test_rescan_updates_records_instead_of_duplicating(tmp_path: Path, mixed_folder: Path) -> None:
    db_path = tmp_path / "db" / "photos.db"

    first = run_scan(mixed_folder, db_path=db_path)
    second = run_scan(mixed_folder, db_path=db_path)

    assert first.db_total == 9
    assert second.db_total == 9  # 仍然是 9 条，不是 18 条


def test_run_scan_with_invalid_root_still_writes_report(tmp_path: Path) -> None:
    db_path = tmp_path / "db" / "photos.db"
    missing = tmp_path / "does_not_exist"

    outcome = run_scan(missing, db_path=db_path)

    assert outcome.root_is_invalid
    assert outcome.infos == []
    assert outcome.db_written == 0
    # 根目录无效时报告里要有对应信息
    report = format_report(outcome)
    assert "总文件：0" in report
    assert "无法访问" in report


# --------------------------------------------------------------------------- #
# 2. 扫描报告
# --------------------------------------------------------------------------- #


def test_format_report_matches_plan_style(tmp_path: Path, mixed_folder: Path) -> None:
    outcome = run_scan(mixed_folder, db_path=tmp_path / "db" / "photos.db")
    report = format_report(outcome)

    assert "扫描完成" in report
    assert "总文件：9" in report
    assert "NEF：3（含 EXIF：3）" in report  # 4 个 NEF：3 个成功 + 1 个损坏
    assert "JPEG：2（含 EXIF：1）" in report
    assert "PNG：1" in report
    assert "视频：1" in report
    assert "其他：1" in report
    assert "异常：" in report
    assert "NEF：1" in report
    assert "任务完成。" in report


# --------------------------------------------------------------------------- #
# 3. CLI 子命令
# --------------------------------------------------------------------------- #


def test_cli_scan_returns_zero(tmp_path: Path, mixed_folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["scan", str(mixed_folder), "--db", str(tmp_path / "db" / "photos.db")])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "扫描完成" in output
    assert "总文件：9" in output


def test_cli_scan_invalid_root_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["scan", str(tmp_path / "nope"), "--db", str(tmp_path / "db" / "photos.db")])

    assert exit_code == 1
    assert "无法访问" in capsys.readouterr().out


def test_cli_show_prints_records(tmp_path: Path, mixed_folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "db" / "photos.db"
    main(["scan", str(mixed_folder), "--db", str(db_path)])
    capsys.readouterr()

    exit_code = main(["show", "--db", str(db_path), "--match", "DSC_0001", "--limit", "10"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DSC_0001.NEF" in output
    assert "相机：NIKON Z6III" in output
    assert "镜头：NIKKOR Z 24-120mm f/4 S" in output
    assert "快门：1/250s" in output
    assert "ISO：1600" in output
    assert "DSC_0002" not in output  # match 生效


def test_cli_show_prints_corrupted_file_error(tmp_path: Path, mixed_folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "db" / "photos.db"
    main(["scan", str(mixed_folder), "--db", str(db_path)])
    capsys.readouterr()

    exit_code = main(["show", "--db", str(db_path), "--match", "DSC_0004"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DSC_0004.NEF" in output
    assert "读取失败：" in output


def test_cli_show_no_match_returns_one(tmp_path: Path, mixed_folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "db" / "photos.db"
    main(["scan", str(mixed_folder), "--db", str(db_path)])
    capsys.readouterr()

    exit_code = main(["show", "--db", str(db_path), "--match", "NOTHING_MATCHES"])

    assert exit_code == 1
    assert "没有匹配" in capsys.readouterr().out


def test_cli_stats_prints_summaries(tmp_path: Path, mixed_folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "db" / "photos.db"
    main(["scan", str(mixed_folder), "--db", str(db_path)])
    capsys.readouterr()

    exit_code = main(["stats", "--db", str(db_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "记录总数：9" in output
    assert "按文件类型：" in output
    assert "按相机：" in output
    assert "NIKON Z6III" in output
    assert "按镜头：" in output
    assert "EXIF 状态：" in output


# --------------------------------------------------------------------------- #
# 4. 显示格式化
# --------------------------------------------------------------------------- #


def test_format_size() -> None:
    assert format_size(0) == "0 B"
    assert format_size(512) == "512 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(45_200_000) == "43.1 MB"
    assert format_size(3_500_000_000) == "3.3 GB"


def test_format_shutter() -> None:
    assert format_shutter(1 / 250) == "1/250s"
    assert format_shutter(1 / 30) == "1/30s"
    assert format_shutter(2.0) == "2s"
    assert format_shutter(1.5) == "1.5s"


def test_format_stats_on_empty_database(tmp_path: Path) -> None:
    output = format_stats(tmp_path / "empty" / "photos.db")

    assert "记录总数：0" in output
    assert "数据库：" in output


# --------------------------------------------------------------------------- #
# 5. 规则分类 CLI（V2）：classify / filter
# --------------------------------------------------------------------------- #


def seed_rule_database(db_path: Path) -> None:
    """预置 3 张照片：A=夜景+高感，B=只有夜景，C=只有高感。"""
    with PhotoDatabase.open(db_path) as db:
        db.upsert_many(
            [
                make_info(
                    Path("D:/Photos/2026/a.NEF"),
                    taken_at="2026-09-18 20:15:00",
                    iso=6400,
                ),
                make_info(
                    Path("D:/Photos/2026/b.NEF"),
                    taken_at="2026-09-18 21:00:00",
                    iso=200,
                ),
                make_info(
                    Path("D:/Photos/2026/c.NEF"),
                    taken_at="2026-09-18 12:00:00",
                    iso=6400,
                ),
            ]
        )


def test_cli_classify_prints_rule_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "photos.db"
    seed_rule_database(db_path)

    exit_code = main(["classify", "--db", str(db_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "规则分类完成（共 3 张）" in output
    assert "夜景  Night" in output
    assert "高感  High ISO" in output
    assert "ISO ≥ 3200" in output
    assert "时间窗 18:00–06:00" in output


def test_cli_filter_and_matches_intersection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "photos.db"
    seed_rule_database(db_path)

    exit_code = main(["filter", "--db", str(db_path), "--rules", "night,high_iso"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "a.NEF" in output
    assert "b.NEF" not in output
    assert "（共 1 张）" in output


def test_cli_filter_or_matches_union(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "photos.db"
    seed_rule_database(db_path)

    exit_code = main(["filter", "--db", str(db_path), "--rules", "night,high_iso", "--or"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "a.NEF" in output and "b.NEF" in output and "c.NEF" in output
    assert "（共 3 张）" in output


def test_cli_filter_explain_prints_reasons(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "photos.db"
    seed_rule_database(db_path)

    exit_code = main(["filter", "--db", str(db_path), "--rules", "night,high_iso", "--explain"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "夜景：" in output
    assert "ISO 6400 ≥ 3200" in output


def test_cli_filter_gps_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "photos.db"
    with PhotoDatabase.open(db_path) as db:
        db.upsert_many(
            [
                make_info(Path("D:/Photos/g1.NEF")),  # make_info 默认带 GPS
                make_info(
                    Path("D:/Photos/g2.NEF"),
                    gps_latitude=None,
                    gps_longitude=None,
                ),
            ]
        )

    exit_code = main(["filter", "--db", str(db_path), "--gps"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "g1.NEF" in output
    assert "g2.NEF" not in output


def test_cli_filter_without_condition_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "photos.db"
    seed_rule_database(db_path)

    exit_code = main(["filter", "--db", str(db_path)])

    assert exit_code == 2
    assert "--rules 或 --gps" in capsys.readouterr().out


def test_cli_filter_unknown_rule_exits(tmp_path: Path) -> None:
    db_path = tmp_path / "photos.db"
    seed_rule_database(db_path)

    with pytest.raises(SystemExit):
        main(["filter", "--db", str(db_path), "--rules", "bogus_rule"])


def test_format_classify_report_on_empty_database(tmp_path: Path) -> None:
    output = format_classify_report(tmp_path / "empty" / "photos.db")

    assert "规则分类完成（共 0 张）" in output
    assert "夜景" in output
