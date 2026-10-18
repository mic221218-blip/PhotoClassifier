"""构造带真实像素的测试素材（V2.5 质量分析用）。

与 ``exif_maker`` 互补：那个模块只造"结构合法但没有像素"的 NEF/JPEG/PNG，
质量分析必须真的解码出画面，所以这里用 Pillow 画确定性的小图，
再按需把 JPEG 预览塞进合成 NEF 的 IFD0 / SubIFD。

所有图片都很小（640×480 级别），不依赖任何真实照片。
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageFilter

from exif_maker import (
    TAG_DATETIME_ORIGINAL,
    TAG_IMAGE_LENGTH,
    TAG_IMAGE_WIDTH,
    TAG_MAKE,
    TAG_MODEL,
    TYPE_LONG,
    ascii_value,
    build_tiff,
)

TAG_EXIF_IFD_POINTER = 0x8769
TAG_SUB_IFDS = 0x014A
TAG_JPEG_OFFSET = 0x0201
TAG_JPEG_LENGTH = 0x0202
TAG_COMPRESSION = 0x0103
TAG_STRIP_OFFSETS = 0x0111
TAG_STRIP_BYTE_COUNTS = 0x0117

SIZE = (640, 480)


# --------------------------------------------------------------------------- #
# 基础图案
# --------------------------------------------------------------------------- #


def checkerboard(
    size=SIZE, cell: int = 16, *, low: int = 20, high: int = 235
) -> Image.Image:
    """高对比棋盘格：清晰的代表。

    刻意避开 0/255 两个极端（否则 JPEG 会把边缘压成"截断像素"，
    让曝光判定误以为画面过曝）。
    """
    image = Image.new("L", size, low)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = high if ((x // cell) + (y // cell)) % 2 else low
    return image


def underexposed_pattern(size=SIZE, *, cell: int = 8) -> Image.Image:
    """暗调但有细节的画面：均值很低、标准差不足以判黑屏。"""
    image = Image.new("L", size, 5)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = 40 if (x // cell) % 2 else 5
    return image


def gradient(size=SIZE) -> Image.Image:
    """平滑渐变：低对比、拉普拉斯方差很小。"""
    image = Image.new("L", size)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = (x * 255) // max(size[0] - 1, 1)
    return image


def gaussian_blurred(image: Image.Image, radius: float = 4.0) -> Image.Image:
    return image.filter(ImageFilter.GaussianBlur(radius))


def directional_blurred(image: Image.Image, *, horizontal: bool = True) -> Image.Image:
    """单向模糊：制造明显的梯度方向性（运动模糊的近似）。"""
    kernel = (
        ImageFilter.Kernel((9, 1), [1] * 9, scale=9)
        if horizontal
        else ImageFilter.Kernel((1, 9), [1] * 9, scale=9)
    )
    return image.filter(kernel)


def flat_image(value: int, size=SIZE) -> Image.Image:
    return Image.new("L", size, value)


# --------------------------------------------------------------------------- #
# 保存成文件
# --------------------------------------------------------------------------- #


def save_jpeg(path: str | Path, image: Image.Image, *, quality: int = 92) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="JPEG", quality=quality)
    return target


def save_png(path: str | Path, image: Image.Image) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG")
    return target


def jpeg_bytes(image: Image.Image, *, quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def exif_tiff(
    *,
    taken_at: str = "2026:07:13 05:30:00",
    width: int = 640,
    height: int = 480,
    make: str = "NIKON CORPORATION",
    model: str = "NIKON Z6III",
) -> bytes:
    """一个只含基础字段的 EXIF(TIFF) 块，供 :func:`save_jpeg_with_exif` 使用。"""
    return build_tiff(
        [
            (TAG_IMAGE_WIDTH, TYPE_LONG, (width,)),
            (TAG_IMAGE_LENGTH, TYPE_LONG, (height,)),
            (TAG_MAKE, *ascii_value(make)),
            (TAG_MODEL, *ascii_value(model)),
            (TAG_DATETIME_ORIGINAL, *ascii_value(taken_at)),
        ]
    )


def save_jpeg_with_exif(
    path: str | Path,
    image: Image.Image,
    *,
    taken_at: str = "2026:07:13 05:30:00",
    quality: int = 92,
) -> Path:
    """保存带 EXIF（含拍摄时间）的真实 JPEG —— 质量分析 + 时间窗测试用。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    exif = b"Exif\x00\x00" + exif_tiff(taken_at=taken_at, width=image.width, height=image.height)
    image.save(target, format="JPEG", quality=quality, exif=exif)
    return target


def save_sharp_jpeg(path: str | Path) -> Path:
    return save_jpeg(path, checkerboard())


def save_blurred_jpeg(path: str | Path, *, radius: float = 3.0) -> Path:
    return save_jpeg(path, gaussian_blurred(checkerboard(), radius))


def save_overexposed_jpeg(path: str | Path) -> Path:
    return save_jpeg(path, flat_image(252))


def save_underexposed_jpeg(path: str | Path) -> Path:
    return save_jpeg(path, underexposed_pattern())


def save_black_jpeg(path: str | Path) -> Path:
    return save_jpeg(path, flat_image(0))


def save_gradient_jpeg(path: str | Path) -> Path:
    return save_jpeg(path, gradient())


def copy_file(source: str | Path, target: str | Path) -> Path:
    """字节级复制（精确重复测试用）。"""
    source_path, target_path = Path(source), Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(source_path.read_bytes())
    return target_path


def resized_copy(source: str | Path, target: str | Path, *, scale: float = 0.6) -> Path:
    """缩小的副本（近似重复测试用）。"""
    with Image.open(source) as image:
        resized = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )
    return save_jpeg(target, resized)


def recompressed_copy(source: str | Path, target: str | Path, *, quality: int = 55) -> Path:
    """重新压缩的副本（近似重复测试用）。"""
    with Image.open(source) as image:
        copy = image.convert("RGB")
    return save_jpeg(target, copy, quality=quality)


# --------------------------------------------------------------------------- #
# 带内嵌预览的合成 NEF
# --------------------------------------------------------------------------- #


def _nef_entries(width: int, height: int):
    return [
        (TAG_IMAGE_WIDTH, TYPE_LONG, (width,)),
        (TAG_IMAGE_LENGTH, TYPE_LONG, (height,)),
        (TAG_MAKE, *ascii_value("NIKON CORPORATION")),
        (TAG_MODEL, *ascii_value("NIKON Z6III")),
        (TAG_DATETIME_ORIGINAL, *ascii_value("2026:07:13 05:30:00")),
    ]


def _subifd_block(jpeg_offset: int, jpeg_length: int) -> bytes:
    entries = [
        (TAG_JPEG_OFFSET, TYPE_LONG, jpeg_offset, 1),
        (TAG_JPEG_LENGTH, TYPE_LONG, jpeg_length, 1),
    ]
    block = struct.pack("<H", len(entries))
    for tag, typ, value, count in entries:
        block += struct.pack("<HHI", tag, typ, count) + struct.pack("<I", value)
    block += struct.pack("<I", 0)
    return block


def nef_with_preview(
    preview: bytes,
    *,
    width: int = 640,
    height: int = 480,
    in_subifd: bool = False,
) -> bytes:
    """合成一个 NEF：IFD0（或 SubIFD）里有 JPEG 预览，预览字节接在 TIFF 之后。"""
    entries = _nef_entries(width, height)

    if in_subifd:
        probe = build_tiff(entries + [(TAG_SUB_IFDS, TYPE_LONG, (0,))])
        sub_offset = len(probe)
        block = _subifd_block(0, len(preview))
        jpeg_offset = sub_offset + len(block)
        tiff = build_tiff(entries + [(TAG_SUB_IFDS, TYPE_LONG, (sub_offset,))])
        assert len(tiff) == len(probe)
        return tiff + _subifd_block(jpeg_offset, len(preview)) + preview

    probe = build_tiff(
        entries
        + [
            (TAG_JPEG_OFFSET, TYPE_LONG, (0,)),
            (TAG_JPEG_LENGTH, TYPE_LONG, (len(preview),)),
        ]
    )
    jpeg_offset = len(probe)
    tiff = build_tiff(
        entries
        + [
            (TAG_JPEG_OFFSET, TYPE_LONG, (jpeg_offset,)),
            (TAG_JPEG_LENGTH, TYPE_LONG, (len(preview),)),
        ]
    )
    assert len(tiff) == len(probe)
    return tiff + preview


def nef_without_preview() -> bytes:
    """只有 EXIF、没有内嵌预览的 NEF。"""
    return build_tiff(_nef_entries(640, 480))


def write_nef(path: str | Path, data: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target
