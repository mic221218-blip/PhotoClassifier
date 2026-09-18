"""PhotoClassifier V1：照片文件夹扫描器。

本模块只负责三件事：

1. 扫描文件（递归扫描一个文件夹）；
2. 判断文件类型（基于扩展名，大小写不敏感）；
3. 收集扫描结果（分类清单 + 问题清单）。

明确不做的内容（留给后续版本）：EXIF、AI、GUI、数据库、Lightroom 集成、
照片内容识别。

安全约定：整个扫描过程是只读的，绝不会移动、复制、删除或修改任何原始文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# 文件类型定义
# --------------------------------------------------------------------------- #

CATEGORY_NEF = "nef"
CATEGORY_JPEG = "jpeg"
CATEGORY_PNG = "png"
CATEGORY_VIDEO = "video"
CATEGORY_UNSUPPORTED = "unsupported"

#: 所有可能的分类，顺序固定，方便输出与测试。
CATEGORIES: tuple[str, ...] = (
    CATEGORY_NEF,
    CATEGORY_JPEG,
    CATEGORY_PNG,
    CATEGORY_VIDEO,
    CATEGORY_UNSUPPORTED,
)

#: 扩展名 -> 分类。键一律使用小写，比较前先 lower()，因此大小写不敏感
#: （`.NEF`、`.nef`、`.JPG`、`.jpg` 都能正确识别）。
CATEGORY_BY_EXTENSION: dict[str, str] = {
    ".nef": CATEGORY_NEF,
    ".jpg": CATEGORY_JPEG,
    ".jpeg": CATEGORY_JPEG,
    ".png": CATEGORY_PNG,
    ".mov": CATEGORY_VIDEO,
    ".mp4": CATEGORY_VIDEO,
    ".m4v": CATEGORY_VIDEO,
    ".avi": CATEGORY_VIDEO,
    ".mkv": CATEGORY_VIDEO,
    ".mts": CATEGORY_VIDEO,
    ".m2ts": CATEGORY_VIDEO,
}


# --------------------------------------------------------------------------- #
# 结果数据结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScannedFile:
    """一个被成功扫描到的文件。"""

    path: Path
    category: str
    size_bytes: int


@dataclass(frozen=True)
class ScanIssue:
    """扫描过程中遇到的一个问题（文件或文件夹无法访问、被跳过等）。"""

    path: Path
    message: str


@dataclass
class ScanResult:
    """一次扫描的完整结果。"""

    root: Path
    files: list[ScannedFile] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        """扫描到的文件总数（不含被记录的问题）。"""
        return len(self.files)

    def count_by_category(self) -> dict[str, int]:
        """返回每个分类的文件数量。"""
        counts = {category: 0 for category in CATEGORIES}
        for item in self.files:
            counts[item.category] = counts.get(item.category, 0) + 1
        return counts

    def files_by_category(self, category: str) -> list[ScannedFile]:
        """返回指定分类下的全部文件。"""
        return [item for item in self.files if item.category == category]


# --------------------------------------------------------------------------- #
# 类型判断
# --------------------------------------------------------------------------- #


def classify_file(path: str | os.PathLike[str]) -> str:
    """根据扩展名判断文件类型，大小写不敏感。

    无法识别的扩展名返回 ``CATEGORY_UNSUPPORTED``，不会抛异常。
    """
    suffix = Path(path).suffix.lower()
    return CATEGORY_BY_EXTENSION.get(suffix, CATEGORY_UNSUPPORTED)


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #


def scan_folder(
    root: str | os.PathLike[str],
    *,
    follow_symlinks: bool = False,
) -> ScanResult:
    """递归扫描 ``root`` 文件夹，返回 :class:`ScanResult`。

    - 允许文件夹中混合 NEF、JPEG、PNG、视频和其他未知文件。
    - 任何单个文件/文件夹出错都只记录到 ``result.issues``，扫描继续。
    - 只读取文件信息，不会修改任何原始文件。
    """
    root_path = Path(root).expanduser()
    result = ScanResult(root=root_path)

    try:
        is_directory = root_path.is_dir()
    except OSError as exc:
        result.issues.append(ScanIssue(root_path, f"无法访问路径: {exc}"))
        return result

    if not is_directory:
        result.issues.append(ScanIssue(root_path, "路径不存在或不是文件夹"))
        return result

    _scan_directory(root_path, result, follow_symlinks=follow_symlinks)
    return result


def _scan_directory(directory: Path, result: ScanResult, *, follow_symlinks: bool) -> None:
    """递归扫描单个文件夹，把结果写入 ``result``；出错只记录，不抛出。"""
    try:
        with os.scandir(directory) as entries:
            dir_entries = list(entries)
    except OSError as exc:
        result.issues.append(ScanIssue(directory, f"无法读取文件夹: {exc}"))
        return

    # 排序只是为了让结果稳定，方便阅读和测试。
    for entry in sorted(dir_entries, key=lambda item: item.name.lower()):
        path = Path(entry.path)
        try:
            if entry.is_dir(follow_symlinks=follow_symlinks):
                _scan_directory(path, result, follow_symlinks=follow_symlinks)
            elif entry.is_file(follow_symlinks=follow_symlinks):
                _scan_file(path, result)
            elif entry.is_symlink():
                result.issues.append(ScanIssue(path, "已跳过：符号链接"))
            else:
                result.issues.append(ScanIssue(path, "已跳过：不是普通文件"))
        except OSError as exc:
            result.issues.append(ScanIssue(path, f"无法访问，已跳过: {exc}"))


def _scan_file(path: Path, result: ScanResult) -> None:
    """读取单个文件的信息并归类；失败时只记录问题，不中断扫描。"""
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        result.issues.append(ScanIssue(path, f"无法读取文件信息，已跳过: {exc}"))
        return

    result.files.append(
        ScannedFile(path=path, category=classify_file(path), size_bytes=size_bytes)
    )
