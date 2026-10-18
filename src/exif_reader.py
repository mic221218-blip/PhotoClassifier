"""PhotoClassifier V1.5：零依赖的 EXIF 读取器。

技术决策（对应总体规划"尚未确定的事项"中的 EXIF 库选型）：
**不引入第三方 EXIF 库，用标准库实现一个最小的 EXIF 解析器。**

理由：

- NEF 本质上是 TIFF 结构，JPEG 的 EXIF 存放在 APP1 段中，两者都遵循
  公开、稳定的规范（TIFF 6.0 / EXIF 2.3），本项目需要的字段全部是
  标准 IFD 标签，不涉及 MakerNotes 解析；
- 零依赖意味着安装即用、完全离线、不受上游库变动影响；
- 自研解析器可以精确区分"文件损坏"（结构解析失败 → 异常）和
  "没有 EXIF"（结构正常但无数据 → 正常处理），这正是规划中
  容错原则需要的语义。

解析范围（总体规划第九节"EXIF 信息"）：

- 拍摄时间 DateTimeOriginal
- 相机 Make / Model
- 镜头 LensModel
- 焦距 FocalLength
- 光圈 FNumber
- 快门 ExposureTime
- ISO ISOSpeedRatings
- 曝光补偿 ExposureBiasValue
- GPS 纬度 / 经度
- 图片尺寸 ImageWidth / ImageLength（JPEG 取 PixelXDimension / PixelYDimension）

安全与容错：

- 整个过程只读，使用 ``mmap`` 按需读取，不把大文件整个载入内存；
- 所有偏移都做边界检查，任何结构错误都抛 :class:`ExifFormatError`，
  绝不让单个文件拖垮整个任务；
- 单个字段解码失败只影响该字段（保持 ``None``），不影响其他字段。
"""

from __future__ import annotations

import mmap
import struct
from datetime import datetime
from pathlib import Path

from src.photo_info import (
    EXIF_ERROR,
    EXIF_NO_EXIF,
    EXIF_OK,
    EXIF_SKIPPED,
    PhotoInfo,
)
from src.scanner import CATEGORY_JPEG, CATEGORY_NEF, CATEGORY_PNG


class ExifFormatError(Exception):
    """文件结构损坏（不是有效的 JPEG / TIFF-RAW / PNG），无法解析。"""


# --------------------------------------------------------------------------- #
# TIFF 数据类型与标签号
# --------------------------------------------------------------------------- #

_TYPE_BYTE = 1
_TYPE_ASCII = 2
_TYPE_SHORT = 3
_TYPE_LONG = 4
_TYPE_RATIONAL = 5
_TYPE_UNDEFINED = 7
_TYPE_SLONG = 9
_TYPE_SRATIONAL = 10
_TYPE_IFD = 13  # SubIFD 指针的类型（规范推荐 LONG，部分相机用 IFD）

_TYPE_SIZE = {
    _TYPE_BYTE: 1,
    _TYPE_ASCII: 1,
    _TYPE_SHORT: 2,
    _TYPE_LONG: 4,
    _TYPE_RATIONAL: 8,
    _TYPE_UNDEFINED: 1,
    _TYPE_SLONG: 4,
    _TYPE_SRATIONAL: 8,
    _TYPE_IFD: 4,
}

# --- IFD0（基本图像目录） ---
_TAG_IMAGE_WIDTH = 0x0100
_TAG_IMAGE_LENGTH = 0x0101
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_EXIF_IFD_POINTER = 0x8769
_TAG_GPS_IFD_POINTER = 0x8825
_TAG_SUB_IFDS = 0x014A

# --- Exif 子 IFD ---
_TAG_EXPOSURE_TIME = 0x829A
_TAG_FNUMBER = 0x829D
_TAG_ISO = 0x8827
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_EXPOSURE_BIAS = 0x9204
_TAG_FOCAL_LENGTH = 0x920A
_TAG_PIXEL_X_DIMENSION = 0xA002
_TAG_PIXEL_Y_DIMENSION = 0xA003
_TAG_LENS_MODEL = 0xA434

# --- GPS 子 IFD ---
_TAG_GPS_LATITUDE_REF = 0x0001
_TAG_GPS_LATITUDE = 0x0002
_TAG_GPS_LONGITUDE_REF = 0x0003
_TAG_GPS_LONGITUDE = 0x0004

# 防"伪造超大 IFD"之类的病态数据：单个 IFD 最多这么多条目。
_MAX_IFD_ENTRIES = 512
# IFD 链（IFD0 → IFD1 → …）最多走这么多层，防止循环引用。
_MAX_IFD_CHAIN = 16

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# 底层读取工具
# --------------------------------------------------------------------------- #


def _read(data, offset: int, size: int, *, what: str = "数据") -> bytes:
    """带边界检查的读取：越界一律视为文件损坏。"""
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ExifFormatError(f"{what}超出文件范围（文件可能被截断或损坏）")
    return bytes(data[offset : offset + size])


# --------------------------------------------------------------------------- #
# TIFF / IFD 解析
# --------------------------------------------------------------------------- #


def _parse_ifd(
    data, base: int, endian: str, offset: int, visited: set[int]
) -> tuple[dict[int, tuple[int, int, bytes]], int]:
    """解析一个 IFD，返回 ({标签: (类型, 数量, 值字节)}, 下一个 IFD 的偏移)。

    未知的数据类型只跳过该条目，不报错。
    """
    if offset in visited:
        raise ExifFormatError("IFD 出现循环引用")
    visited.add(offset)

    (count,) = struct.unpack(endian + "H", _read(data, base + offset, 2, what="IFD 条目数"))
    if count > _MAX_IFD_ENTRIES:
        raise ExifFormatError(f"IFD 条目数异常（{count}）")

    entries: dict[int, tuple[int, int, bytes]] = {}
    for i in range(count):
        raw = _read(data, base + offset + 2 + i * 12, 12, what="IFD 条目")
        tag, typ, cnt = struct.unpack(endian + "HHI", raw[:8])
        if typ not in _TYPE_SIZE:
            continue
        total = _TYPE_SIZE[typ] * cnt
        if total <= 4:
            value_bytes = raw[8 : 8 + total]
        else:
            (value_offset,) = struct.unpack(endian + "I", raw[8:12])
            value_bytes = _read(data, base + value_offset, total, what="IFD 条目值")
        entries[tag] = (typ, cnt, value_bytes)

    (next_offset,) = struct.unpack(
        endian + "I", _read(data, base + offset + 2 + count * 12, 4, what="IFD 链指针")
    )
    return entries, next_offset


def _parse_tiff(data, base: int) -> dict[int, tuple[int, int, bytes]]:
    """从 ``base`` 处开始解析 TIFF 结构，返回合并后的标签表。

    会跟随 IFD0 → Exif 子 IFD → GPS 子 IFD，以及 IFD 链。
    相同标签先出现者优先（IFD0 优先于 IFD1 等）。
    """
    header = _read(data, base, 8, what="TIFF 文件头")
    if header[:2] == b"II":
        endian = "<"
    elif header[:2] == b"MM":
        endian = ">"
    else:
        raise ExifFormatError("不是有效的 TIFF/NEF 文件头")
    (magic,) = struct.unpack(endian + "H", header[2:4])
    if magic != 42:
        raise ExifFormatError("不是有效的 TIFF/NEF 版本号")
    (ifd_offset,) = struct.unpack(endian + "I", header[4:8])

    merged: dict[int, tuple[int, int, bytes]] = {}
    visited: set[int] = set()

    def absorb(entries: dict[int, tuple[int, int, bytes]]) -> None:
        for tag, value in entries.items():
            merged.setdefault(tag, value)

    for _ in range(_MAX_IFD_CHAIN):
        if ifd_offset == 0:
            break
        entries, ifd_offset = _parse_ifd(data, base, endian, ifd_offset, visited)
        absorb(entries)

        # 跟随 Exif / GPS 子 IFD 指针
        for pointer_tag in (_TAG_EXIF_IFD_POINTER, _TAG_GPS_IFD_POINTER):
            entry = entries.get(pointer_tag)
            if entry is None:
                continue
            typ, cnt, value_bytes = entry
            if typ == _TYPE_LONG and cnt >= 1:
                (sub_offset,) = struct.unpack(endian + "I", value_bytes[:4])
                if sub_offset:
                    sub_entries, _ = _parse_ifd(data, base, endian, sub_offset, visited)
                    absorb(sub_entries)

    return merged


def iter_tiff_ifds(data, base: int = 0, *, include_subifds: bool = False):
    """逐个产出 TIFF 结构里的 IFD 条目字典（供预览图提取使用）。

    - 跟随 IFD 链（IFD0 → IFD1 → …）；
    - ``include_subifds=True`` 时额外跟随 SubIFD（标签 0x014A），
      Nikon 的 NEF 常把全尺寸 JPEG 预览放在 SubIFD 里；
    - 产出顺序按发现顺序（先 IFD0，再 SubIFD，再下一个 IFD），
      由调用方决定如何挑选。

    结构损坏时抛 :class:`ExifFormatError`（调用方按单文件容错处理）。
    本函数只读，不修改 ``data``。
    """
    header = _read(data, base, 8, what="TIFF 文件头")
    if header[:2] == b"II":
        endian = "<"
    elif header[:2] == b"MM":
        endian = ">"
    else:
        raise ExifFormatError("不是有效的 TIFF/NEF 文件头")
    (magic,) = struct.unpack(endian + "H", header[2:4])
    if magic != 42:
        raise ExifFormatError("不是有效的 TIFF/NEF 版本号")
    (ifd_offset,) = struct.unpack(endian + "I", header[4:8])

    visited: set[int] = set()
    pending: list[int] = [ifd_offset]

    while pending:
        offset = pending.pop(0)
        if not offset or offset in visited:
            continue
        entries, next_offset = _parse_ifd(data, base, endian, offset, visited)
        yield entries

        if include_subifds:
            sub = entries.get(_TAG_SUB_IFDS)
            if sub is not None and sub[0] in (_TYPE_LONG, _TYPE_IFD) and sub[1]:
                for index in range(min(sub[1], 64)):
                    (child,) = struct.unpack_from(endian + "I", sub[2], index * 4)
                    if child:
                        pending.append(child)

        if next_offset:
            pending.append(next_offset)


# --------------------------------------------------------------------------- #
# 字段解码
# --------------------------------------------------------------------------- #


def _decode_ascii(value_bytes: bytes) -> str:
    """ASCII 字段：截断到第一个 NUL，去掉首尾空白。"""
    return value_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def _decode_int(endian: str, typ: int, value_bytes: bytes) -> int | None:
    """取第一个整数值（SHORT / LONG / SLONG / BYTE）。"""
    try:
        if typ == _TYPE_SHORT:
            return struct.unpack(endian + "H", value_bytes[:2])[0]
        if typ == _TYPE_LONG:
            return struct.unpack(endian + "I", value_bytes[:4])[0]
        if typ == _TYPE_SLONG:
            return struct.unpack(endian + "i", value_bytes[:4])[0]
        if typ == _TYPE_BYTE:
            return value_bytes[0]
    except struct.error:
        return None
    return None


def _decode_rational(endian: str, typ: int, value_bytes: bytes, index: int = 0) -> float | None:
    """取第 index 个 RATIONAL / SRATIONAL 值（分母为 0 视为无数据）。"""
    try:
        if typ == _TYPE_RATIONAL:
            num, den = struct.unpack_from(endian + "II", value_bytes, index * 8)
        elif typ == _TYPE_SRATIONAL:
            num, den = struct.unpack_from(endian + "ii", value_bytes, index * 8)
        else:
            return None
    except struct.error:
        return None
    if den == 0:
        return None
    return num / den


def _normalize_datetime(raw: str | None) -> str | None:
    """EXIF 时间 "YYYY:MM:DD HH:MM:SS" → "YYYY-MM-DD HH:MM:SS"。

    无法识别的格式原样保留（宁可有原始数据也不要丢掉）。
    """
    if not raw:
        return None
    normalized = raw.replace(":", "-", 2) if len(raw) == 19 and raw[4] == ":" and raw[7] == ":" else raw
    try:
        datetime.strptime(normalized[:19], "%Y-%m-%d %H:%M:%S")
        return normalized[:19]
    except ValueError:
        return raw


def _gps_decimal(entries, endian: str, value_tag: int, ref_tag: int) -> float | None:
    """把 GPS 的"度分秒 + 方向参考"换算成十进制度（带南北/东西符号）。"""
    value = entries.get(value_tag)
    ref = entries.get(ref_tag)
    if value is None or ref is None:
        return None
    typ, cnt, value_bytes = value
    if typ != _TYPE_RATIONAL or cnt < 3:
        return None
    degrees = _decode_rational(endian, typ, value_bytes, 0)
    minutes = _decode_rational(endian, typ, value_bytes, 1)
    seconds = _decode_rational(endian, typ, value_bytes, 2)
    if degrees is None or minutes is None or seconds is None:
        return None
    result = degrees + minutes / 60.0 + seconds / 3600.0
    if _decode_ascii(ref[2]).upper().startswith(("S", "W")):
        result = -result
    return result


def _extract_fields(entries: dict[int, tuple[int, int, bytes]], endian: str) -> dict:
    """从合并标签表中提取本项目关心的字段；单个字段失败不影响其他字段。"""
    fields: dict = {}

    def ascii_field(tag: int) -> str | None:
        entry = entries.get(tag)
        if entry is None or entry[0] != _TYPE_ASCII:
            return None
        try:
            text = _decode_ascii(entry[2])
        except Exception:
            return None
        return text or None

    def int_field(*tags: int) -> int | None:
        for tag in tags:
            entry = entries.get(tag)
            if entry is None:
                continue
            value = _decode_int(endian, entry[0], entry[2])
            if value is not None:
                return value
        return None

    def rational_field(tag: int) -> float | None:
        entry = entries.get(tag)
        if entry is None:
            return None
        return _decode_rational(endian, entry[0], entry[2])

    fields["camera_make"] = ascii_field(_TAG_MAKE)
    fields["camera_model"] = ascii_field(_TAG_MODEL)
    fields["lens_model"] = ascii_field(_TAG_LENS_MODEL)
    fields["taken_at"] = _normalize_datetime(ascii_field(_TAG_DATETIME_ORIGINAL))
    fields["focal_length_mm"] = rational_field(_TAG_FOCAL_LENGTH)
    fields["f_number"] = rational_field(_TAG_FNUMBER)
    fields["exposure_time_s"] = rational_field(_TAG_EXPOSURE_TIME)
    fields["iso"] = int_field(_TAG_ISO)
    fields["exposure_bias_ev"] = rational_field(_TAG_EXPOSURE_BIAS)
    fields["gps_latitude"] = _gps_decimal(entries, endian, _TAG_GPS_LATITUDE, _TAG_GPS_LATITUDE_REF)
    fields["gps_longitude"] = _gps_decimal(entries, endian, _TAG_GPS_LONGITUDE, _TAG_GPS_LONGITUDE_REF)
    fields["width_px"] = int_field(_TAG_IMAGE_WIDTH, _TAG_PIXEL_X_DIMENSION)
    fields["height_px"] = int_field(_TAG_IMAGE_LENGTH, _TAG_PIXEL_Y_DIMENSION)

    return {key: value for key, value in fields.items() if value is not None}


# --------------------------------------------------------------------------- #
# JPEG 段定位
# --------------------------------------------------------------------------- #


def _find_jpeg_exif_offset(data) -> int | None:
    """在 JPEG 中定位 APP1(Exif) 段里 TIFF 结构的起始偏移。

    返回 ``None`` 表示这个 JPEG 结构正常、但没有 EXIF 段。
    段结构损坏时抛 :class:`ExifFormatError`。
    """
    if _read(data, 0, 2, what="JPEG 文件头") != b"\xff\xd8":
        raise ExifFormatError("不是有效的 JPEG 文件（缺少 SOI 标记）")

    pos = 2
    size = len(data)
    while pos < size:
        if data[pos] != 0xFF:
            raise ExifFormatError("JPEG 段结构损坏")
        while pos < size and data[pos] == 0xFF:
            pos += 1
        if pos >= size:
            raise ExifFormatError("JPEG 段结构损坏（标记不完整）")
        marker = data[pos]
        pos += 1

        # 独立标记（无长度字段）
        if marker == 0xD8 or marker == 0xD9 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        # SOS：图像数据开始，EXIF 只会在它之前
        if marker == 0xDA:
            return None

        (seg_len,) = struct.unpack(">H", _read(data, pos, 2, what="JPEG 段长度"))
        if seg_len < 2:
            raise ExifFormatError("JPEG 段长度无效")
        payload = _read(data, pos + 2, seg_len - 2, what="JPEG 段内容")
        if marker == 0xE1 and payload[:6] == b"Exif\x00\x00":
            return pos + 2 + 6
        pos += seg_len

    # 没走到 SOS 就结束了：文件不完整，但 EXIF 只可能在 SOS 之前，
    # 走到这里说明确实没有 EXIF 段。
    return None


# --------------------------------------------------------------------------- #
# PNG 基础信息
# --------------------------------------------------------------------------- #


def _read_png_dimensions(data) -> tuple[int, int]:
    """读取 PNG 的宽高（IHDR 固定在文件开头）。签名不对则视为损坏。"""
    if _read(data, 0, 8, what="PNG 文件头") != _PNG_SIGNATURE:
        raise ExifFormatError("不是有效的 PNG 文件（签名不匹配）")
    if _read(data, 12, 4, what="PNG IHDR") != b"IHDR":
        raise ExifFormatError("不是有效的 PNG 文件（缺少 IHDR）")
    width, height = struct.unpack(">II", _read(data, 16, 8, what="PNG 尺寸"))
    if width == 0 or height == 0:
        raise ExifFormatError("PNG 尺寸无效")
    return int(width), int(height)


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #


def read_exif(path: str | Path) -> dict:
    """读取一个 NEF / JPEG 文件的 EXIF 字段。

    返回字典的键与 :class:`PhotoInfo` 的 EXIF 字段同名（可能只有部分键）。
    文件结构损坏时抛 :class:`ExifFormatError`；
    文件正常但没有 EXIF 数据时返回空字典。
    """
    file_path = Path(path)
    try:
        with open(file_path, "rb") as handle:
            try:
                with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                    return _read_exif_from_mapped(mapped, file_path)
            except ValueError:
                # mmap 不能映射空文件
                raise ExifFormatError("文件为空（可能已损坏）") from None
    except OSError as exc:
        raise ExifFormatError(f"无法读取文件: {exc}") from exc


def _read_exif_from_mapped(mapped, file_path: Path) -> dict:
    suffix = file_path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        tiff_offset = _find_jpeg_exif_offset(mapped)
        if tiff_offset is None:
            return {}
    else:  # .nef / .tif：整个文件就是 TIFF 结构
        tiff_offset = 0
    entries = _parse_tiff(mapped, tiff_offset)
    if not entries:
        return {}
    endian = "<" if bytes(mapped[tiff_offset : tiff_offset + 2]) == b"II" else ">"
    return _extract_fields(entries, endian)


def read_photo_info(path: str | Path, category: str) -> PhotoInfo:
    """把一个扫描到的文件变成完整的 :class:`PhotoInfo`。

    这是 V1.5 的核心入口：**任何情况下都不抛异常**——
    读取失败只记录状态与错误信息，任务可以继续处理下一个文件。
    """
    file_path = Path(path)

    # ---- 文件信息 ----
    try:
        stat = file_path.stat()
        size_bytes = stat.st_size
        modified_at = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError as exc:
        return PhotoInfo(
            path=file_path,
            category=category,
            size_bytes=0,
            exif_status=EXIF_ERROR,
            exif_error=f"无法读取文件信息: {exc}",
        )

    # ---- EXIF / 基础信息 ----
    fields: dict = {}
    exif_status = EXIF_SKIPPED
    exif_error: str | None = None

    if category in (CATEGORY_NEF, CATEGORY_JPEG):
        try:
            fields = read_exif(file_path)
        except ExifFormatError as exc:
            exif_status = EXIF_ERROR
            exif_error = str(exc)
        else:
            exif_status = EXIF_OK if fields else EXIF_NO_EXIF
    elif category == CATEGORY_PNG:
        # PNG 没有 EXIF，但按规划"可以读取基础信息"（尺寸），
        # 顺便用签名校验文件是否损坏。
        try:
            with open(file_path, "rb") as handle:
                head = handle.read(24)
            if len(head) < 24 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
                raise ExifFormatError("不是有效的 PNG 文件（签名或 IHDR 缺失）")
            width, height = struct.unpack(">II", head[16:24])
            if width == 0 or height == 0:
                raise ExifFormatError("PNG 尺寸无效")
            fields = {"width_px": int(width), "height_px": int(height)}
        except ExifFormatError as exc:
            exif_status = EXIF_ERROR
            exif_error = str(exc)
        except OSError as exc:
            exif_status = EXIF_ERROR
            exif_error = f"无法读取文件: {exc}"
        else:
            exif_status = EXIF_SKIPPED  # PNG 不算"有 EXIF"，但读取了基础信息
    else:
        # 视频和未知格式：只记录文件信息
        exif_status = EXIF_SKIPPED

    return PhotoInfo(
        path=file_path,
        category=category,
        size_bytes=size_bytes,
        modified_at=modified_at,
        exif_status=exif_status,
        exif_error=exif_error,
        taken_at=fields.get("taken_at"),
        camera_make=fields.get("camera_make"),
        camera_model=fields.get("camera_model"),
        lens_model=fields.get("lens_model"),
        focal_length_mm=fields.get("focal_length_mm"),
        f_number=fields.get("f_number"),
        exposure_time_s=fields.get("exposure_time_s"),
        iso=fields.get("iso"),
        exposure_bias_ev=fields.get("exposure_bias_ev"),
        gps_latitude=fields.get("gps_latitude"),
        gps_longitude=fields.get("gps_longitude"),
        width_px=fields.get("width_px"),
        height_px=fields.get("height_px"),
    )
