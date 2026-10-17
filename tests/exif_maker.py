"""构造带 EXIF 的合成测试文件（NEF / JPEG / PNG）。

测试不能依赖真实照片（真实 RAW 文件很大且不适合放进仓库），所以这里
按照 TIFF 6.0 / EXIF 2.3 / PNG 规范手工构造最小的合法文件：

- NEF 本质上是 TIFF 结构 → :func:`build_tiff` 直接生成；
- JPEG 的 EXIF 存放在 APP1 段 → :func:`build_jpeg` 把 TIFF 包进去；
- PNG 只需要签名 + IHDR 头 → :func:`build_png`。

生成的文件足够小（几百字节），且能被 ``src.exif_reader`` 完整解析。
"""

from __future__ import annotations

import struct
import zlib

# TIFF 数据类型
TYPE_ASCII = 2
TYPE_SHORT = 3
TYPE_LONG = 4
TYPE_RATIONAL = 5
TYPE_SRATIONAL = 10

# 标签号（与 src/exif_reader 保持一致）
TAG_IMAGE_WIDTH = 0x0100
TAG_IMAGE_LENGTH = 0x0101
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_EXIF_IFD_POINTER = 0x8769
TAG_GPS_IFD_POINTER = 0x8825
TAG_EXPOSURE_TIME = 0x829A
TAG_FNUMBER = 0x829D
TAG_ISO = 0x8827
TAG_DATETIME_ORIGINAL = 0x9003
TAG_EXPOSURE_BIAS = 0x9204
TAG_FOCAL_LENGTH = 0x920A
TAG_LENS_MODEL = 0xA434
TAG_GPS_LATITUDE_REF = 0x0001
TAG_GPS_LATITUDE = 0x0002
TAG_GPS_LONGITUDE_REF = 0x0003
TAG_GPS_LONGITUDE = 0x0004

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# 值构造器：返回 (类型, 值描述)
# --------------------------------------------------------------------------- #


def ascii_value(text: str) -> tuple[int, bytes]:
    return (TYPE_ASCII, text.encode("ascii") + b"\x00")


def short_value(*values: int) -> tuple[int, tuple[int, ...]]:
    return (TYPE_SHORT, values)


def long_value(*values: int) -> tuple[int, tuple[int, ...]]:
    return (TYPE_LONG, values)


def rational_value(*pairs: tuple[int, int]) -> tuple[int, tuple[tuple[int, int], ...]]:
    return (TYPE_RATIONAL, pairs)


def srational_value(*pairs: tuple[int, int]) -> tuple[int, tuple[tuple[int, int], ...]]:
    return (TYPE_SRATIONAL, pairs)


# --------------------------------------------------------------------------- #
# TIFF 构建
# --------------------------------------------------------------------------- #


def _pack_values(endian: str, typ: int, values) -> bytes:
    """把值描述打包成字节。ASCII 的 values 直接就是字节串。"""
    if typ == TYPE_ASCII:
        return values
    if typ == TYPE_SHORT:
        return b"".join(struct.pack(endian + "H", v) for v in values)
    if typ == TYPE_LONG:
        return b"".join(struct.pack(endian + "I", v) for v in values)
    if typ == TYPE_RATIONAL:
        return b"".join(struct.pack(endian + "II", n, d) for n, d in values)
    if typ == TYPE_SRATIONAL:
        return b"".join(struct.pack(endian + "ii", n, d) for n, d in values)
    raise ValueError(f"不支持的类型：{typ}")


def _entry_count(typ: int, values) -> int:
    """IFD 条目的 count 字段。"""
    if typ == TYPE_ASCII:
        return len(values)
    return len(values)


def _ifd_block_size(entry_count: int) -> int:
    """IFD 块大小：2 字节数量 + n * 12 字节条目 + 4 字节链指针。"""
    return 2 + entry_count * 12 + 4


def _data_size(endian: str, entries) -> int:
    """一个 IFD 的所有"超长值"需要的额外数据区大小（偶数对齐）。"""
    total = 0
    for _tag, typ, values in entries:
        payload = _pack_values(endian, typ, values)
        if len(payload) > 4:
            total += len(payload) + (len(payload) % 2)
    return total


def build_tiff(
    ifd0_entries,
    exif_entries=None,
    gps_entries=None,
    *,
    endian: str = "<",
) -> bytes:
    """构造一个完整的 TIFF 结构（也就是一个最小的"NEF"文件）。

    ``ifd0_entries`` / ``exif_entries`` / ``gps_entries`` 是
    ``(标签号, 类型, 值)`` 列表，值用 :func:`ascii_value` 等构造。
    提供了 exif/gps 条目时会自动生成对应的子 IFD 指针。
    """
    ifd0 = list(ifd0_entries)
    exif = list(exif_entries or [])
    gps = list(gps_entries or [])

    n0 = len(ifd0) + (1 if exif else 0) + (1 if gps else 0)

    # 布局：头(8) | IFD0 | IFD0数据 | ExifIFD | Exif数据 | GPS IFD | GPS数据
    ifd0_offset = 8
    ifd0_data_offset = ifd0_offset + _ifd_block_size(n0)
    exif_offset = ifd0_data_offset + _data_size(endian, ifd0)
    exif_data_offset = exif_offset + (_ifd_block_size(len(exif)) if exif else 0)
    gps_offset = exif_data_offset + _data_size(endian, exif)
    gps_data_offset = gps_offset + (_ifd_block_size(len(gps)) if gps else 0)
    total_size = gps_data_offset + _data_size(endian, gps)

    out = bytearray()
    out += b"II" if endian == "<" else b"MM"
    out += struct.pack(endian + "H", 42)
    out += struct.pack(endian + "I", ifd0_offset)

    def render_ifd(entries, data_offset: int, pointer_entries) -> bytes:
        all_entries = list(entries) + [
            (tag, TYPE_LONG, (target,)) for tag, target in pointer_entries
        ]
        all_entries.sort(key=lambda entry: entry[0])
        block = bytearray()
        block += struct.pack(endian + "H", len(all_entries))
        data = bytearray()
        for tag, typ, values in all_entries:
            payload = _pack_values(endian, typ, values)
            count = _entry_count(typ, values)
            if len(payload) <= 4:
                field = payload.ljust(4, b"\x00")
            else:
                field = struct.pack(endian + "I", data_offset + len(data))
                data += payload
                if len(data) % 2:
                    data += b"\x00"
            block += struct.pack(endian + "HHI", tag, typ, count)
            block += field
        block += struct.pack(endian + "I", 0)  # 下一个 IFD = 0
        return bytes(block) + bytes(data)

    pointer_entries = [
        pointer
        for pointer in (
            (TAG_EXIF_IFD_POINTER, exif_offset) if exif else None,
            (TAG_GPS_IFD_POINTER, gps_offset) if gps else None,
        )
        if pointer is not None
    ]
    out += render_ifd(ifd0, ifd0_data_offset, pointer_entries)
    if exif:
        out += render_ifd(exif, exif_data_offset, [])
    if gps:
        out += render_ifd(gps, gps_data_offset, [])

    assert len(out) == total_size, f"布局计算错误：{len(out)} != {total_size}"
    return bytes(out)


# --------------------------------------------------------------------------- #
# JPEG / PNG 构建
# --------------------------------------------------------------------------- #


def build_jpeg(tiff_bytes: bytes) -> bytes:
    """把 TIFF(Exif) 数据包进一个最小的合法 JPEG。"""
    app1_payload = b"Exif\x00\x00" + tiff_bytes
    app1 = b"\xff\xe1" + struct.pack(">H", len(app1_payload) + 2) + app1_payload
    # SOI + APP1(Exif) + SOS + EOI（SOS 之后就是图像数据，解析器到 SOS 为止）
    return b"\xff\xd8" + app1 + b"\xff\xda\x00\x02" + b"\xff\xd9"


def build_jpeg_without_exif() -> bytes:
    """一个结构正常但没有 EXIF 段的 JPEG（SOI + APP0(JFIF) + SOS + EOI）。"""
    app0_payload = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    app0 = b"\xff\xe0" + struct.pack(">H", len(app0_payload) + 2) + app0_payload
    return b"\xff\xd8" + app0 + b"\xff\xda\x00\x02" + b"\xff\xd9"


def build_png(width: int, height: int) -> bytes:
    """一个最小的合法 PNG（签名 + IHDR）。"""
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    chunk = b"IHDR" + ihdr
    crc = zlib.crc32(chunk) & 0xFFFFFFFF
    return PNG_SIGNATURE + struct.pack(">I", len(ihdr)) + chunk + struct.pack(">I", crc)


# --------------------------------------------------------------------------- #
# 仿真拍摄数据
# --------------------------------------------------------------------------- #


def nikon_z6iii_entries():
    """一组仿真的 Nikon Z6III 拍摄参数（数值来自规划文档第九节示例）。

    返回 (ifd0, exif, gps) 三个条目列表。
    """
    ifd0 = [
        (TAG_IMAGE_WIDTH, TYPE_LONG, (6048,)),
        (TAG_IMAGE_LENGTH, TYPE_LONG, (4024,)),
        (TAG_MAKE, *ascii_value("NIKON CORPORATION")),
        (TAG_MODEL, *ascii_value("NIKON Z6III")),
    ]
    exif = [
        (TAG_EXPOSURE_TIME, TYPE_RATIONAL, ((1, 250),)),
        (TAG_FNUMBER, TYPE_RATIONAL, ((4, 1),)),
        (TAG_ISO, TYPE_SHORT, (1600,)),
        (TAG_DATETIME_ORIGINAL, *ascii_value("2026:09:18 20:15:32")),
        (TAG_EXPOSURE_BIAS, TYPE_SRATIONAL, ((-1, 3),)),
        (TAG_FOCAL_LENGTH, TYPE_RATIONAL, ((120, 1),)),
        (TAG_LENS_MODEL, *ascii_value("NIKKOR Z 24-120mm f/4 S")),
    ]
    gps = [
        (TAG_GPS_LATITUDE_REF, *ascii_value("N")),
        (TAG_GPS_LATITUDE, TYPE_RATIONAL, ((31, 1), (13, 1), (4953, 100))),
        (TAG_GPS_LONGITUDE_REF, *ascii_value("E")),
        (TAG_GPS_LONGITUDE, TYPE_RATIONAL, ((121, 1), (28, 1), (25322, 1000))),
    ]
    return ifd0, exif, gps


def nikon_z6iii_nef() -> bytes:
    """一个带完整 EXIF 的仿真 NEF 文件。"""
    ifd0, exif, gps = nikon_z6iii_entries()
    return build_tiff(ifd0, exif, gps)


def nikon_z6iii_jpeg() -> bytes:
    """一个带完整 EXIF 的仿真 JPEG 文件。"""
    return build_jpeg(nikon_z6iii_nef())
