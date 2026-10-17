"""`src.exif_reader` 的测试（V1.5）。

测试文件全部由 `tests/exif_maker.py` 按规范合成，不接触任何真实照片：

- 带 EXIF 的 NEF / JPEG → 字段全部正确读取；
- 无 EXIF 的 JPEG → 状态"无 EXIF"，不报错；
- 损坏 / 空文件 / 截断文件 → 状态"读取失败"，不抛异常；
- PNG → 只读尺寸；视频 / 未知格式 → 只记录文件信息。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.exif_reader import ExifFormatError, read_exif, read_photo_info
from src.photo_info import (
    EXIF_ERROR,
    EXIF_NO_EXIF,
    EXIF_OK,
    EXIF_SKIPPED,
    PhotoInfo,
)
from src.scanner import (
    CATEGORY_JPEG,
    CATEGORY_NEF,
    CATEGORY_PNG,
    CATEGORY_UNSUPPORTED,
    CATEGORY_VIDEO,
)

from exif_maker import (
    TYPE_LONG,
    TYPE_RATIONAL,
    TYPE_SHORT,
    TAG_DATETIME_ORIGINAL,
    TAG_IMAGE_LENGTH,
    TAG_IMAGE_WIDTH,
    TAG_MAKE,
    TAG_MODEL,
    ascii_value,
    build_jpeg,
    build_jpeg_without_exif,
    build_png,
    build_tiff,
    nikon_z6iii_jpeg,
    nikon_z6iii_nef,
)

GPS_LATITUDE = 31 + 13 / 60 + 49.53 / 3600  # 31°13'49.53"N
GPS_LONGITUDE = 121 + 28 / 60 + 25.322 / 3600  # 121°28'25.322"E


def make_file(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    path.write_bytes(content)
    return path


def assert_full_exif(info: PhotoInfo, path: Path) -> None:
    """对"仿真 Nikon Z6III"文件的完整字段断言。"""
    assert info.path == path
    assert info.size_bytes == path.stat().st_size
    assert info.exif_status == EXIF_OK
    assert info.exif_error is None
    assert info.camera_make == "NIKON CORPORATION"
    assert info.camera_model == "NIKON Z6III"
    assert info.camera == "NIKON Z6III"
    assert info.lens_model == "NIKKOR Z 24-120mm f/4 S"
    assert info.taken_at == "2026-09-18 20:15:32"
    assert info.focal_length_mm == pytest.approx(120.0)
    assert info.f_number == pytest.approx(4.0)
    assert info.exposure_time_s == pytest.approx(1 / 250)
    assert info.iso == 1600
    assert info.exposure_bias_ev == pytest.approx(-1 / 3)
    assert info.gps_latitude == pytest.approx(GPS_LATITUDE, abs=1e-6)
    assert info.gps_longitude == pytest.approx(GPS_LONGITUDE, abs=1e-6)
    assert info.width_px == 6048
    assert info.height_px == 4024


# --------------------------------------------------------------------------- #
# 1. NEF：完整 EXIF 读取
# --------------------------------------------------------------------------- #


def test_nef_with_full_exif(tmp_path: Path) -> None:
    path = make_file(tmp_path, "DSC_0012.NEF", nikon_z6iii_nef())

    info = read_photo_info(path, CATEGORY_NEF)

    assert_full_exif(info, path)
    assert info.category == CATEGORY_NEF
    assert info.modified_at  # 文件修改时间总是有


def test_big_endian_nef_is_parsed(tmp_path: Path) -> None:
    """TIFF 允许大端字节序（MM），也要能读。"""
    from exif_maker import nikon_z6iii_entries

    ifd0, exif, gps = nikon_z6iii_entries()
    content = build_tiff(ifd0, exif, gps, endian=">")
    path = make_file(tmp_path, "DSC_0001.NEF", content)

    info = read_photo_info(path, CATEGORY_NEF)

    assert info.exif_status == EXIF_OK
    assert info.camera_model == "NIKON Z6III"
    assert info.iso == 1600
    assert info.exposure_time_s == pytest.approx(1 / 250)


def test_nef_without_gps(tmp_path: Path) -> None:
    from exif_maker import nikon_z6iii_entries

    ifd0, exif, _gps = nikon_z6iii_entries()
    path = make_file(tmp_path, "DSC_0002.NEF", build_tiff(ifd0, exif))

    info = read_photo_info(path, CATEGORY_NEF)

    assert info.exif_status == EXIF_OK
    assert info.gps_latitude is None
    assert info.gps_longitude is None
    assert not info.has_gps


def test_nef_with_partial_exif_only_datetime(tmp_path: Path) -> None:
    """只有拍摄时间、没有其他参数的文件：能读多少是多少。"""
    ifd0 = [
        (TAG_IMAGE_WIDTH, TYPE_LONG, (6048,)),
        (TAG_IMAGE_LENGTH, TYPE_LONG, (4024,)),
    ]
    exif = [(TAG_DATETIME_ORIGINAL, *ascii_value("2026:01:02 03:04:05"))]
    path = make_file(tmp_path, "DSC_0003.NEF", build_tiff(ifd0, exif))

    info = read_photo_info(path, CATEGORY_NEF)

    assert info.exif_status == EXIF_OK
    assert info.taken_at == "2026-01-02 03:04:05"
    assert info.camera_make is None
    assert info.lens_model is None
    assert info.iso is None


def test_iso_can_be_long_type(tmp_path: Path) -> None:
    """ISO 偶尔会以 LONG 类型存储，也要能读。"""
    ifd0 = [(TAG_MAKE, *ascii_value("Test"))]
    exif = [
        (0x8827, TYPE_LONG, (6400,)),  # ISO，用 LONG
        (TAG_DATETIME_ORIGINAL, *ascii_value("2026:01:02 03:04:05")),
    ]
    path = make_file(tmp_path, "A.NEF", build_tiff(ifd0, exif))

    info = read_photo_info(path, CATEGORY_NEF)

    assert info.iso == 6400


# --------------------------------------------------------------------------- #
# 2. JPEG：完整 EXIF 读取
# --------------------------------------------------------------------------- #


def test_jpeg_with_full_exif(tmp_path: Path) -> None:
    path = make_file(tmp_path, "DSC_0012.JPG", nikon_z6iii_jpeg())

    info = read_photo_info(path, CATEGORY_JPEG)

    assert_full_exif(info, path)


def test_jpeg_without_exif_is_not_an_error(tmp_path: Path) -> None:
    """结构正常但没有 EXIF 段的 JPEG：正常处理，只是缺少信息。"""
    path = make_file(tmp_path, "web_pic.jpg", build_jpeg_without_exif())

    info = read_photo_info(path, CATEGORY_JPEG)

    assert info.exif_status == EXIF_NO_EXIF
    assert info.exif_error is None
    assert info.taken_at is None
    assert info.size_bytes > 0


def test_jpeg_exif_case_insensitive_extension(tmp_path: Path) -> None:
    path = make_file(tmp_path, "photo.Jpeg", nikon_z6iii_jpeg())

    info = read_photo_info(path, CATEGORY_JPEG)

    assert info.exif_status == EXIF_OK


# --------------------------------------------------------------------------- #
# 3. 损坏文件：必须标记异常，绝不能抛异常
# --------------------------------------------------------------------------- #


def test_corrupted_nef_reports_error(tmp_path: Path) -> None:
    path = make_file(tmp_path, "broken.NEF", b"this is not a tiff file at all")

    info = read_photo_info(path, CATEGORY_NEF)

    assert info.exif_status == EXIF_ERROR
    assert info.exif_error is not None
    assert "TIFF" in info.exif_error or "损坏" in info.exif_error


def test_empty_nef_reports_error(tmp_path: Path) -> None:
    path = make_file(tmp_path, "empty.NEF", b"")

    info = read_photo_info(path, CATEGORY_NEF)

    assert info.exif_status == EXIF_ERROR
    assert "空" in (info.exif_error or "")


def test_truncated_nef_reports_error(tmp_path: Path) -> None:
    """把一个合法 NEF 从中间截断 → 读取失败而不是崩溃。"""
    content = nikon_z6iii_nef()
    path = make_file(tmp_path, "truncated.NEF", content[: len(content) // 2])

    info = read_photo_info(path, CATEGORY_NEF)

    assert info.exif_status == EXIF_ERROR


def test_garbage_jpeg_reports_error(tmp_path: Path) -> None:
    path = make_file(tmp_path, "garbage.JPG", b"\x00\x01\x02not jpeg")

    info = read_photo_info(path, CATEGORY_JPEG)

    assert info.exif_status == EXIF_ERROR
    assert "JPEG" in (info.exif_error or "")


def test_truncated_jpeg_segment_reports_error(tmp_path: Path) -> None:
    """JPEG 段头声称的长度超过文件实际大小 → 损坏。"""
    broken = b"\xff\xd8" + b"\xff\xe1" + b"\x40\x00" + b"Exif\x00\x00" + b"\x00" * 8
    path = make_file(tmp_path, "cut.JPG", broken)

    info = read_photo_info(path, CATEGORY_JPEG)

    assert info.exif_status == EXIF_ERROR


def test_read_exif_raises_for_corrupted_nef(tmp_path: Path) -> None:
    """底层接口 read_exif 对损坏文件抛 ExifFormatError（由上层兜底）。"""
    path = make_file(tmp_path, "bad.NEF", b"nope")

    with pytest.raises(ExifFormatError):
        read_exif(path)


# --------------------------------------------------------------------------- #
# 4. PNG / 视频 / 未知格式
# --------------------------------------------------------------------------- #


def test_png_reads_dimensions_only(tmp_path: Path) -> None:
    path = make_file(tmp_path, "screen.PNG", build_png(1920, 1080))

    info = read_photo_info(path, CATEGORY_PNG)

    assert info.exif_status == EXIF_SKIPPED
    assert info.exif_error is None
    assert info.width_px == 1920
    assert info.height_px == 1080
    assert info.taken_at is None


def test_corrupted_png_reports_error(tmp_path: Path) -> None:
    path = make_file(tmp_path, "fake.png", b"\x89PNG\r\n\x1a\nGarbageGarbageGarba")

    info = read_photo_info(path, CATEGORY_PNG)

    assert info.exif_status == EXIF_ERROR


def test_video_only_records_file_info(tmp_path: Path) -> None:
    path = make_file(tmp_path, "clip.MOV", b"\x00\x00\x00\x14ftypisom" + b"x" * 100)

    info = read_photo_info(path, CATEGORY_VIDEO)

    assert info.exif_status == EXIF_SKIPPED
    assert info.exif_error is None
    assert info.size_bytes == path.stat().st_size
    assert info.taken_at is None
    assert info.width_px is None


def test_unsupported_file_only_records_file_info(tmp_path: Path) -> None:
    path = make_file(tmp_path, "notes.txt", b"hello")

    info = read_photo_info(path, CATEGORY_UNSUPPORTED)

    assert info.exif_status == EXIF_SKIPPED
    assert info.exif_error is None
    assert info.size_bytes == 5


def test_video_with_garbage_content_never_raises(tmp_path: Path) -> None:
    """视频内容再乱也不解析、不报错（规划：视频不能导致程序崩溃）。"""
    path = make_file(tmp_path, "weird.mp4", bytes(range(256)) * 4)

    info = read_photo_info(path, CATEGORY_VIDEO)

    assert info.exif_status == EXIF_SKIPPED
    assert info.exif_error is None


# --------------------------------------------------------------------------- #
# 5. 时间格式规范化
# --------------------------------------------------------------------------- #


def test_datetime_colons_are_normalized(tmp_path: Path) -> None:
    ifd0 = [(TAG_MODEL, *ascii_value("Cam"))]
    exif = [(TAG_DATETIME_ORIGINAL, *ascii_value("2025:12:31 23:59:59"))]
    path = make_file(tmp_path, "T.NEF", build_tiff(ifd0, exif))

    info = read_photo_info(path, CATEGORY_NEF)

    assert info.taken_at == "2025-12-31 23:59:59"


def test_unparseable_datetime_is_kept_as_is(tmp_path: Path) -> None:
    ifd0 = [(TAG_MODEL, *ascii_value("Cam"))]
    exif = [(TAG_DATETIME_ORIGINAL, *ascii_value("some-random-text"))]
    path = make_file(tmp_path, "T2.NEF", build_tiff(ifd0, exif))

    info = read_photo_info(path, CATEGORY_NEF)

    assert info.taken_at == "some-random-text"


# --------------------------------------------------------------------------- #
# 6. 其他细节
# --------------------------------------------------------------------------- #


def test_rational_with_zero_denominator_is_ignored(tmp_path: Path) -> None:
    """分母为 0 的 RATIONAL 是坏数据，应忽略而不是抛 ZeroDivisionError。"""
    ifd0 = [(TAG_MAKE, *ascii_value("Cam"))]
    exif = [
        (0x829D, TYPE_RATIONAL, ((0, 0),)),  # FNumber = 0/0
        (0x920A, TYPE_RATIONAL, ((50, 1),)),
    ]
    path = make_file(tmp_path, "Z.NEF", build_tiff(ifd0, exif))

    info = read_photo_info(path, CATEGORY_NEF)

    assert info.f_number is None
    assert info.focal_length_mm == pytest.approx(50.0)


def test_photo_info_camera_merge_variants() -> None:
    """camera 属性的合并显示逻辑。"""
    base = dict(path=Path("a.NEF"), category=CATEGORY_NEF, size_bytes=1)
    assert PhotoInfo(camera_make="NIKON CORPORATION", camera_model="NIKON Z6III", **base).camera == "NIKON Z6III"
    assert PhotoInfo(camera_make="SONY", camera_model="ILCE-7M4", **base).camera == "SONY ILCE-7M4"
    assert PhotoInfo(camera_make=None, camera_model="X100VI", **base).camera == "X100VI"
    assert PhotoInfo(camera_make="Canon", camera_model=None, **base).camera == "Canon"
    assert PhotoInfo(camera_make=None, camera_model=None, **base).camera is None
