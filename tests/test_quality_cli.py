"""V2.5 CLI 测试：analyze / quality / dupes / filter --quality。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("PIL.Image")

from src.database import PhotoDatabase  # noqa: E402
from src.main import main  # noqa: E402
from src.quality import ANALYZER_VERSION, QualityMeasurement  # noqa: E402
from src.quality_store import QualityStore  # noqa: E402

from image_maker import (  # noqa: E402
    checkerboard,
    copy_file,
    gaussian_blurred,
    resized_copy,
    save_black_jpeg,
    save_blurred_jpeg,
    save_jpeg_with_exif,
    save_overexposed_jpeg,
    save_sharp_jpeg,
    save_underexposed_jpeg,
)


@pytest.fixture()
def photo_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "photos"
    folder.mkdir()
    save_sharp_jpeg(folder / "sharp.JPG")
    save_blurred_jpeg(folder / "blurred.JPG")
    save_overexposed_jpeg(folder / "over.JPG")
    save_underexposed_jpeg(folder / "under.JPG")
    save_black_jpeg(folder / "black.JPG")
    return folder


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "photos.db"


def scan_and_analyze(folder: Path, db_path: Path, capsys) -> str:
    assert main(["scan", str(folder), "--db", str(db_path)]) == 0
    code = main(["analyze", "--db", str(db_path), "--workers", "1"])
    assert code == 0
    return capsys.readouterr().out


# --------------------------------------------------------------------------- #
# analyze
# --------------------------------------------------------------------------- #


def test_analyze_creates_measurements(photo_folder: Path, db_path: Path, capsys) -> None:
    output = scan_and_analyze(photo_folder, db_path, capsys)
    assert "质量分析完成" in output
    assert "本次分析：5 张" in output
    assert "内嵌 JPEG 0" in output
    assert "原图 5" in output

    with QualityStore.open(db_path) as store:
        assert store.count() == 5
        stats = store.stats()
    assert stats.analyzed == 5
    assert stats.failed == 0


def test_analyze_is_incremental(photo_folder: Path, db_path: Path, capsys) -> None:
    scan_and_analyze(photo_folder, db_path, capsys)
    capsys.readouterr()
    assert main(["analyze", "--db", str(db_path), "--workers", "1"]) == 0
    output = capsys.readouterr().out
    assert "本次分析：0 张" in output
    assert "已是最新 5" in output


def test_analyze_without_pillow_returns_3(
    photo_folder: Path, db_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["scan", str(photo_folder), "--db", str(db_path)]) == 0
    capsys.readouterr()
    monkeypatch.setattr("src.preview.PILLOW_AVAILABLE", False)
    assert main(["analyze", "--db", str(db_path)]) == 3
    output = capsys.readouterr().out
    assert "requirements-quality" in output


def test_analyze_exports_csv(photo_folder: Path, db_path: Path, tmp_path: Path, capsys) -> None:
    assert main(["scan", str(photo_folder), "--db", str(db_path)]) == 0
    csv_path = tmp_path / "quality.csv"
    assert (
        main(["analyze", "--db", str(db_path), "--workers", "1", "--csv", str(csv_path)])
        == 0
    )
    text = csv_path.read_text(encoding="utf-8-sig")
    assert "blur_score" in text
    assert "sharp.JPG" in text


# --------------------------------------------------------------------------- #
# quality
# --------------------------------------------------------------------------- #


def test_quality_report_flags_each_category(
    photo_folder: Path, db_path: Path, capsys
) -> None:
    scan_and_analyze(photo_folder, db_path, capsys)
    capsys.readouterr()
    assert main(["quality", "--db", str(db_path), "--limit", "20"]) == 0
    output = capsys.readouterr().out
    assert "模糊（blurry）：" in output
    assert "过曝（overexposed）：" in output
    assert "欠曝（underexposed）：" in output
    assert "黑屏（black_frame）：" in output

    assert main(["quality", "--db", str(db_path), "--flags", "blurry", "--explain"]) == 0
    output = capsys.readouterr().out
    assert "blurred.JPG" in output
    assert "sharp.JPG" not in output
    assert "拉普拉斯方差" in output


def test_quality_without_analysis_hints(
    photo_folder: Path, db_path: Path, capsys
) -> None:
    assert main(["scan", str(photo_folder), "--db", str(db_path)]) == 0
    capsys.readouterr()
    assert main(["quality", "--db", str(db_path)]) == 1
    assert "先运行" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# filter --quality
# --------------------------------------------------------------------------- #


def test_filter_by_quality(photo_folder: Path, db_path: Path, capsys) -> None:
    scan_and_analyze(photo_folder, db_path, capsys)
    capsys.readouterr()

    assert main(["filter", "--db", str(db_path), "--quality", "blurry", "--explain"]) == 0
    output = capsys.readouterr().out
    assert "blurred.JPG" in output
    assert "sharp.JPG" not in output
    assert "共 1 张" in output


def test_filter_quality_unknown_flag_exits(
    photo_folder: Path, db_path: Path, capsys
) -> None:
    scan_and_analyze(photo_folder, db_path, capsys)
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["filter", "--db", str(db_path), "--quality", "bogus"])


def test_filter_without_conditions_returns_2(photo_folder: Path, db_path: Path) -> None:
    assert main(["scan", str(photo_folder), "--db", str(db_path)]) == 0
    assert main(["filter", "--db", str(db_path)]) == 2


# --------------------------------------------------------------------------- #
# dupes
# --------------------------------------------------------------------------- #


def test_dupes_reports_exact_and_near(tmp_path: Path, db_path: Path, capsys) -> None:
    folder = tmp_path / "dupes"
    folder.mkdir()
    base = save_jpeg_with_exif(folder / "base.JPG", checkerboard())
    copy_file(base, folder / "base - copy.JPG")
    resized_path = resized_copy(base, tmp_path / "resized.JPG")
    with Image.open(resized_path) as resized:
        save_jpeg_with_exif(folder / "near.JPG", resized)

    assert main(["scan", str(folder), "--db", str(db_path)]) == 0
    assert main(["analyze", "--db", str(db_path), "--workers", "1"]) == 0

    capsys.readouterr()
    assert main(["dupes", "--db", str(db_path)]) == 0
    output = capsys.readouterr().out
    assert "重复照片：" in output
    assert "精确重复" in output
    assert "近似重复" in output or "精确" in output


def test_dupes_empty_folder(tmp_path: Path, db_path: Path, capsys) -> None:
    folder = tmp_path / "single"
    folder.mkdir()
    save_sharp_jpeg(folder / "only.JPG")
    assert main(["scan", str(folder), "--db", str(db_path)]) == 0
    assert main(["analyze", "--db", str(db_path), "--workers", "1"]) == 0
    capsys.readouterr()
    assert main(["dupes", "--db", str(db_path)]) == 0
    assert "没有发现重复照片" in capsys.readouterr().out
