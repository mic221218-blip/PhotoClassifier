"""PhotoClassifier V2.5：质量分析的像素来源。

职责只有一个：**给一张照片拿到可度量的灰度图**。

- NEF：解析 TIFF 结构，取内嵌的 JPEG 预览（IFD0 或 SubIFD 的
  ``JPEGInterchangeFormat`` / ``StripOffsets``），不做 RAW 解码；
- JPEG / PNG：直接解码原图；
- 视频 / 未知格式：不分析。

Pillow 是**可选依赖**，只在本模块导入。没安装时抛
:class:`PillowMissing`，由调用方给出安装提示 —— 扫描、EXIF、规则分类
等核心功能完全不受影响。
"""

from __future__ import annotations

import io
import mmap
import struct
from dataclasses import dataclass
from pathlib import Path

from src.exif_reader import ExifFormatError, iter_tiff_ifds
from src.photo_info import PhotoInfo
from src.quality import PREVIEW_EMBEDDED, PREVIEW_FULL, GrayImage, QualityConfig
from src.scanner import CATEGORY_JPEG, CATEGORY_NEF, CATEGORY_PNG

try:  # Pillow 是可选依赖：缺失时核心功能仍然可用
    from PIL import Image, ImageOps

    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境相关
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False

#: TIFF 标签
_TAG_COMPRESSION = 0x0103
_TAG_STRIP_OFFSETS = 0x0111
_TAG_STRIP_BYTE_COUNTS = 0x0117
_TAG_JPEG_OFFSET = 0x0201
_TAG_JPEG_LENGTH = 0x0202

_TYPE_SHORT = 3
_TYPE_LONG = 4

#: 单个预览上限，防御异常文件。
_MAX_PREVIEW_BYTES = 32 * 1024 * 1024

#: 解码前的像素数上限（24MP / 45MP 相机都在内；异常文件直接拒绝）。
_MAX_PIXELS = 80_000_000


class PreviewError(Exception):
    """这张照片拿不到可用的像素（无预览、损坏、格式不支持…）。"""


class PillowMissing(RuntimeError):
    """没有安装 Pillow —— 质量分析不可用，但核心功能照常。"""

    def __init__(self) -> None:
        super().__init__("需要 Pillow 才能分析图像质量：pip install -r requirements-quality.txt")


@dataclass(frozen=True)
class Preview:
    """归一化后的预览图。"""

    source: str  # embedded_jpeg / full_image
    width: int  # 解码出的预览原始尺寸
    height: int
    gray: GrayImage
    hash_grid: bytes  # 9x8 灰度采样，用于 dHash


def ensure_pillow() -> None:
    """没有 Pillow 时抛出带安装指引的异常。"""
    if not PILLOW_AVAILABLE:
        raise PillowMissing()


def is_supported(category: str) -> bool:
    """该文件类型是否参与质量分析。"""
    return category in (CATEGORY_NEF, CATEGORY_JPEG, CATEGORY_PNG)


def load_preview(info: PhotoInfo, config: QualityConfig | None = None) -> Preview:
    """读取一张照片的预览并归一化成灰度图。

    失败时抛 :class:`PreviewError`（调用方记录为该文件的 error，不影响其他文件）。
    """
    ensure_pillow()
    cfg = config or QualityConfig()

    if not is_supported(info.category):
        raise PreviewError("非照片文件，不参与质量分析")

    path = Path(info.path)
    if info.category == CATEGORY_NEF:
        blob, reason = _extract_embedded_preview(path)
        if blob is None:
            raise PreviewError(reason or "未找到内嵌预览")
        source = PREVIEW_EMBEDDED
        image, original_size = _decode(blob, cfg, label="内嵌预览")
    else:
        source = PREVIEW_FULL
        image, original_size = _decode_file(path, cfg)

    return _normalize(image, source, cfg, original_size)


# --------------------------------------------------------------------------- #
# NEF 内嵌预览
# --------------------------------------------------------------------------- #


def _extract_embedded_preview(data_path: Path) -> tuple[bytes | None, str | None]:
    """从 NEF 里取出最大的内嵌 JPEG 预览，返回 (bytes, 失败原因)。"""
    try:
        with open(data_path, "rb") as handle:
            try:
                with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                    endian = _detect_endian(mapped)
                    candidates = _collect_candidates(mapped, endian)
                    return _pick_candidate(candidates, mapped)
            except ValueError:
                return None, "文件为空（可能已损坏）"
    except OSError as exc:
        return None, f"无法读取文件: {exc}"
    except ExifFormatError as exc:
        return None, f"NEF 结构损坏: {exc}"


@dataclass(frozen=True)
class _Candidate:
    kind: str  # "jpeg" | "strips"
    total_bytes: int
    offset: int = 0
    length: int = 0
    offsets: tuple[int, ...] = ()
    counts: tuple[int, ...] = ()


def _detect_endian(data) -> str:
    magic = bytes(data[:2])
    if magic == b"II":
        return "<"
    if magic == b"MM":
        return ">"
    raise ExifFormatError("不是有效的 TIFF/NEF 文件头")


def _collect_candidates(data, endian: str) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for entries in iter_tiff_ifds(data, include_subifds=True):
        jpeg_offset = entries.get(_TAG_JPEG_OFFSET)
        jpeg_length = entries.get(_TAG_JPEG_LENGTH)
        if jpeg_offset is not None and jpeg_length is not None:
            if jpeg_offset[0] == _TYPE_LONG and jpeg_length[0] in (_TYPE_LONG, _TYPE_SHORT):
                offset = _unpack_value(_TYPE_LONG, jpeg_offset[2], endian)
                length = _unpack_value(jpeg_length[0], jpeg_length[2], endian)
                if offset and length and 0 < length <= _MAX_PREVIEW_BYTES:
                    candidates.append(_Candidate("jpeg", length, offset, length))

        compression = entries.get(_TAG_COMPRESSION)
        strip_offsets = entries.get(_TAG_STRIP_OFFSETS)
        strip_counts = entries.get(_TAG_STRIP_BYTE_COUNTS)
        if compression and strip_offsets and strip_counts:
            if _unpack_value(compression[0], compression[2], endian) == 7:
                offsets = _unpack_array(strip_offsets, endian)
                counts = _unpack_array(strip_counts, endian)
                if offsets and len(offsets) == len(counts):
                    total = sum(counts)
                    if 0 < total <= _MAX_PREVIEW_BYTES:
                        candidates.append(
                            _Candidate(
                                "strips",
                                total,
                                offsets=tuple(offsets),
                                counts=tuple(counts),
                            )
                        )
    return candidates


def _pick_candidate(candidates: list[_Candidate], data) -> tuple[bytes | None, str | None]:
    """挑选体积最大且通过 JPEG 首尾校验的候选。"""
    if not candidates:
        return None, "未找到内嵌 JPEG 预览"
    for candidate in sorted(candidates, key=lambda item: -item.total_bytes):
        if candidate.kind == "jpeg":
            if candidate.offset + candidate.length > len(data):
                continue
            blob = bytes(data[candidate.offset : candidate.offset + candidate.length])
        else:
            pieces: list[bytes] = []
            for offset, count in zip(candidate.offsets, candidate.counts):
                if offset + count > len(data):
                    pieces = []
                    break
                pieces.append(bytes(data[offset : offset + count]))
            blob = b"".join(pieces)
        trimmed = _trim_jpeg(blob)
        if trimmed is not None:
            return trimmed, None
    return None, "内嵌预览不是有效的 JPEG（文件可能已损坏）"


def _trim_jpeg(blob: bytes) -> bytes | None:
    """校验 JPEG 的 SOI/EOI 并截掉尾部填充；不是 JPEG 返回 None。"""
    if len(blob) < 4 or blob[:2] != b"\xff\xd8":
        return None
    end = blob.rfind(b"\xff\xd9")
    if end < 2:
        return None
    return blob[: end + 2]


def _unpack_value(type_code: int, raw: bytes, endian: str) -> int:
    if not raw:
        return 0
    if type_code == _TYPE_SHORT and len(raw) >= 2:
        return struct.unpack(endian + "H", raw[:2])[0]
    if len(raw) >= 4:
        return struct.unpack(endian + "I", raw[:4])[0]
    return 0


def _unpack_array(entry: tuple[int, int, bytes], endian: str) -> list[int]:
    type_code, count, raw = entry
    size = 2 if type_code == _TYPE_SHORT else 4
    if count <= 0 or len(raw) < size * count:
        return []
    fmt = endian + ("H" if size == 2 else "I")
    return [struct.unpack_from(fmt, raw, index * size)[0] for index in range(count)]


# --------------------------------------------------------------------------- #
# 解码与归一化
# --------------------------------------------------------------------------- #


def _decode(blob: bytes, cfg: QualityConfig, *, label: str):
    try:
        image = Image.open(io.BytesIO(blob))
        return _prepare(image, cfg, label=label)
    except PreviewError:
        raise
    except Exception as exc:  # Pillow 的异常类型很多，统一转成可读错误
        raise PreviewError(f"{label}解码失败: {exc}") from exc


def _decode_file(path: Path, cfg: QualityConfig):
    try:
        image = Image.open(path)
        return _prepare(image, cfg, label="原图")
    except PreviewError:
        raise
    except Exception as exc:
        raise PreviewError(f"解码失败: {exc}") from exc


def _prepare(image, cfg: QualityConfig, *, label: str = "预览"):
    """返回 (归一化灰度图, 预览原始尺寸)。"""
    width, height = image.size
    if width <= 0 or height <= 0:
        raise PreviewError(f"{label}尺寸无效: {width}x{height}")
    if width * height > _MAX_PIXELS:
        raise PreviewError(f"{label}像素数过大（{width}x{height}），已跳过")

    original_size = (width, height)
    # draft 让 libjpeg 在解码阶段就降采样（JPEG 专用；其他格式是无害的空操作）
    try:
        image.draft("L", (cfg.thumb_edge, cfg.thumb_edge))
    except Exception:
        pass

    try:
        image = ImageOps.exif_transpose(image)
    except Exception:
        pass
    image = image.convert("L")
    image.thumbnail((cfg.thumb_edge, cfg.thumb_edge), Image.LANCZOS)
    return image, original_size


def _normalize(
    image, source: str, cfg: QualityConfig, original_size: tuple[int, int]
) -> Preview:
    width, height = image.size
    if width < 3 or height < 3:
        raise PreviewError(f"预览过小，无法分析：{width}x{height}")
    grid = image.resize((9, 8), Image.LANCZOS).tobytes()
    return Preview(
        source=source,
        width=original_size[0],
        height=original_size[1],
        gray=GrayImage(width=width, height=height, data=image.tobytes()),
        hash_grid=grid,
    )
