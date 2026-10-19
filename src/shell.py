"""Windows 资源管理器 / 默认程序集成。

- “打开图片”：用系统默认程序打开文件（NEF/JPEG 通常是“照片”应用）；
- “打开文件位置”：不启动新的 ``explorer.exe`` 进程，而是调用 Shell API
  ``SHOpenFolderAndSelectItems``，让**已经在运行的**资源管理器定位文件。

失败时退回到“打开所在文件夹”，再失败就把路径交给调用方
（GUI 会复制到剪贴板并提示）。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from urllib.parse import quote

#: Windows“照片”应用支持的 URI 激活（没有默认关联时兜底）
_PHOTOS_URI = "ms-photos:viewer?fileName={encoded}"

#: 图片扩展名（统一用 Windows“照片”打开，避免被第三方看图工具接管）
_IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".bmp",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
}

#: 常见 RAW 扩展名（Windows“照片”可借助 RAW 扩展打开预览）
_RAW_SUFFIXES = {
    ".nef",
    ".nrw",
    ".cr2",
    ".cr3",
    ".arw",
    ".srf",
    ".sr2",
    ".raf",
    ".dng",
    ".orf",
    ".rw2",
    ".pef",
    ".srw",
}


def open_with_default_app(path: str | Path) -> tuple[bool, str | None]:
    """用系统默认程序打开文件。

    - 有默认关联（JPG/PNG 等）：直接交给默认应用；
    - NEF 等 RAW 没有关联时：用 ``ms-photos:`` URI 让“照片”应用打开；
    - 其他情况：尝试系统“打开方式”。

    返回 ``(成功, 错误信息)``；不抛异常。
    """
    target = Path(path)
    try:
        if not target.exists():
            return False, f"文件不存在：{target}"
    except OSError as exc:
        return False, f"无法访问文件：{exc}"

    if not hasattr(os, "startfile"):
        return False, "当前系统不支持用默认程序打开文件"

    suffix = target.suffix.lower()
    if suffix in _IMAGE_SUFFIXES | _RAW_SUFFIXES:
        # 图片统一用 Windows“照片”打开（这台机器上 .NEF/.PNG 的默认关联是
        # 第三方看图工具，用户要的是“照片”这样的查看体验）
        uri = _PHOTOS_URI.format(encoded=quote(str(target), safe=""))
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
            return True, None
        except OSError:
            pass  # 没有“照片”应用时退回默认关联

    try:
        os.startfile(str(target))  # type: ignore[attr-defined]
        return True, None
    except OSError as exc:
        return False, f"无法打开文件：{exc}"


def open_in_explorer(path: str | Path) -> tuple[bool, str | None]:
    """在资源管理器中定位并选中文件。

    返回 ``(成功, 错误信息)``。任何失败都不会抛异常，也不会弹系统错误框。
    """
    target = Path(path)
    try:
        if not target.exists():
            return False, f"文件不存在：{target}"
    except OSError as exc:
        return False, f"无法访问文件：{exc}"

    error = _select_with_shell_api(target)
    if error is None:
        return True, None

    # 退路：至少打开所在文件夹（文件很多时也还能手动找）
    try:
        os.startfile(str(target.parent))
        return True, None
    except OSError:
        pass

    return False, error


def _select_with_shell_api(target: Path) -> str | None:
    """调用 SHOpenFolderAndSelectItems；成功返回 None，失败返回原因。"""
    try:
        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32

        shell32.SHParseDisplayName.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        shell32.SHParseDisplayName.restype = ctypes.c_long
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long

        # Tk 主线程通常尚未初始化 COM；已初始化时返回 S_FALSE / RPC_E_CHANGED_MODE 都无妨
        ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED

        pidl = ctypes.c_void_p()
        attributes = wintypes.DWORD()
        parse_result = shell32.SHParseDisplayName(
            str(target), None, ctypes.byref(pidl), 0, ctypes.byref(attributes)
        )
        if parse_result != 0:
            return f"无法解析文件路径（0x{parse_result & 0xFFFFFFFF:08X}）"
        try:
            result = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
        finally:
            ole32.CoTaskMemFree(pidl)
        if result != 0:
            return f"系统拒绝打开文件位置（0x{result & 0xFFFFFFFF:08X}）"
        return None
    except Exception as exc:  # 非 Windows / 受限环境：交给调用方降级
        return f"系统接口不可用：{exc}"
