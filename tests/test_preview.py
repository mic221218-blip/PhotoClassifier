"""`src.preview` 的测试：NEF 内嵌预览、JPEG/PNG 解码、失败容错。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PIL.Image")

from src.exif_reader import iter_tiff_ifds, read_photo_info  # noqa: E402
from src.preview import (  # noqa: E402
    PILLOW_AVAILABLE,
    PillowMissing,
    PreviewError,
    ensure_pillow,
    is_supported,
    load_preview,
)

from image_maker import (  # noqa: E402
    checkerboard,
    jpeg_bytes,
    nef_with_preview,
    nef_without_preview,
    save_png,
    save_sharp_jpeg,
    write_nef,
)


def photo_info(path: Path, category: str):
    return read_photo_info(path, category)


def test_embedded_preview_in_ifd0(tmp_path: Path) -> None:
    preview = jpeg_bytes(checkerboard())
    nef = write_nef(tmp_path / "shot.NEF", nef_with_preview(preview))
    result = load_preview(photo_info(nef, "nef"))

    assert result.source == "embedded_jpeg"
    assert (result.width, result.height) == (640, 480)
    assert max(result.gray.width, result.gray.height) <= 512
    assert len(result.hash_grid) == 72


def test_embedded_preview_in_subifd(tmp_path: Path) -> None:
    preview = jpeg_bytes(checkerboard())
    data = nef_with_preview(preview, in_subifd=True)
    nef = write_nef(tmp_path / "shot.NEF", data)

    # 预览确实在 SubIFD 里：不跟随 SubIFD 时找不到
    without_sub = list(iter_tiff_ifds(data, include_subifds=False))
    assert not any(0x0201 in entries for entries in without_sub)
    with_sub = list(iter_tiff_ifds(data, include_subifds=True))
    assert any(0x0201 in entries for entries in with_sub)

    result = load_preview(photo_info(nef, "nef"))
    assert result.source == "embedded_jpeg"
    assert (result.width, result.height) == (640, 480)


def test_nef_without_preview_fails_cleanly(tmp_path: Path) -> None:
    nef = write_nef(tmp_path / "no_preview.NEF", nef_without_preview())
    with pytest.raises(PreviewError, match="未找到"):
        load_preview(photo_info(nef, "nef"))


def test_corrupt_nef_fails_cleanly(tmp_path: Path) -> None:
    nef = tmp_path / "broken.NEF"
    nef.write_bytes(b"not a tiff at all")
    with pytest.raises(PreviewError):
        load_preview(photo_info(nef, "nef"))


def test_truncated_embedded_jpeg_fails_cleanly(tmp_path: Path) -> None:
    preview = jpeg_bytes(checkerboard())[: 40]
    nef = write_nef(tmp_path / "truncated.NEF", nef_with_preview(preview))
    with pytest.raises(PreviewError):
        load_preview(photo_info(nef, "nef"))


def test_jpeg_uses_full_image(tmp_path: Path) -> None:
    jpeg = save_sharp_jpeg(tmp_path / "shot.JPG")
    result = load_preview(photo_info(jpeg, "jpeg"))
    assert result.source == "full_image"
    assert (result.width, result.height) == (640, 480)
    assert max(result.gray.width, result.gray.height) <= 512


def test_png_is_supported(tmp_path: Path) -> None:
    png = save_png(tmp_path / "shot.png", checkerboard())
    result = load_preview(photo_info(png, "png"))
    assert result.source == "full_image"
    assert result.gray.width > 0


def test_video_is_rejected(tmp_path: Path) -> None:
    video = tmp_path / "clip.MOV"
    video.write_bytes(b"\x00" * 128)
    info = photo_info(video, "video")
    assert not is_supported(info.category)
    with pytest.raises(PreviewError, match="非照片"):
        load_preview(info)


def test_missing_pillow_raises_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.preview.PILLOW_AVAILABLE", False)
    with pytest.raises(PillowMissing, match="requirements-quality"):
        ensure_pillow()


def test_pillow_available_in_test_env() -> None:
    assert PILLOW_AVAILABLE is True
