"""从 logo PNG 生成 Windows 友好的 .ico（BMP/DIB 帧，而不是全 PNG 帧）。

为什么不用 ``PIL.Image.save(..., format="ICO")``：Pillow 会把每一帧都写成
PNG 压缩帧，Windows 资源管理器/桌面在 16–128px 的取图路径上对纯 PNG 帧
兼容性不佳，可能显示成一张空白页。这里手工写标准的 32 位 BMP 帧
（256px 也用 BMP），Tkinter 的 ``iconbitmap`` 与 Windows 外壳都能识别。

用法（项目根目录）::

    python assets/logo/make_icon.py
"""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "assets" / "logo" / "logo-icon-512.png"
TARGETS = (
    ROOT / "assets" / "photoclassifier.ico",
    ROOT / "assets" / "shortcut-icon.ico",
)
SIZES = (16, 24, 32, 48, 64, 128, 256)


def bmp_frame(image: Image.Image) -> bytes:
    """把一个 RGBA 方块编码成 ICO 使用的 BMP 帧（BITMAPINFOHEADER + XOR + AND）。"""
    size = image.width
    pixels = image.load()

    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        size,        # biWidth
        size * 2,    # biHeight：XOR 图 + AND 掩码
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        size * size * 4,
        0,
        0,
        0,
        0,
    )

    # XOR 位图：自下而上，BGRA
    rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray()
        for x in range(size):
            r, g, b, a = pixels[x, y]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    xor = b"".join(rows)

    # AND 掩码：1 表示透明，这里依靠 alpha 通道，全部填 0
    mask_row = ((size + 31) // 32) * 4
    mask = b"\x00" * (mask_row * size)
    return header + xor + mask


def build_ico(source: Path, target: Path) -> None:
    image = Image.open(source).convert("RGBA")
    frames = []
    for size in SIZES:
        frame = image.resize((size, size), Image.LANCZOS)
        frames.append((size, bmp_frame(frame)))

    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    for size, data in frames:
        out += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        offset += len(data)
    for _size, data in frames:
        out += data
    target.write_bytes(bytes(out))
    print(f"wrote {target} ({len(out)} bytes, {len(frames)} frames)")


def main() -> None:
    for target in TARGETS:
        build_ico(SOURCE, target)


if __name__ == "__main__":
    main()
