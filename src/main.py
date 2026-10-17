"""PhotoClassifier V1.5 命令行入口。

用法::

    python -m src.main scan <文件夹> [--db data/photos.db]
    python -m src.main show [--db data/photos.db] [--match DSC] [--limit 20]
    python -m src.main stats [--db data/photos.db]

- ``scan``：扫描文件夹（复用 V1 扫描器）→ 逐个读取 EXIF（容错）→
  写入 SQLite 照片信息数据库 → 打印扫描报告。
- ``show``：查看数据库中的照片记录。
- ``stats``：查看数据库统计（按类型 / EXIF 状态 / 相机 / 镜头）。

整个流程只读取原始文件，不会移动、复制、删除或修改任何照片。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from src.database import PhotoDatabase
from src.exif_reader import read_photo_info
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
    ScanResult,
    scan_folder,
)

#: 数据库默认位置。data/ 已在 .gitignore 中，真实数据不会上传到 GitHub。
DEFAULT_DB = Path("data") / "photos.db"

#: 报告里各分类的显示名。
CATEGORY_LABELS = {
    CATEGORY_NEF: "NEF",
    CATEGORY_JPEG: "JPEG",
    CATEGORY_PNG: "PNG",
    CATEGORY_VIDEO: "视频",
    CATEGORY_UNSUPPORTED: "其他",
}

#: 会被当作"照片"尝试读取信息的分类（其余分类只记录文件信息）。
_PHOTO_CATEGORIES = (CATEGORY_NEF, CATEGORY_JPEG, CATEGORY_PNG)

#: 扫描报告里"无法访问"最多列出的条目数。
_MAX_ISSUES_SHOWN = 10

#: stats 里每个维度最多显示的条目数。
_MAX_STATS_SHOWN = 10


# --------------------------------------------------------------------------- #
# 扫描流程
# --------------------------------------------------------------------------- #


@dataclass
class ScanOutcome:
    """一次 ``scan`` 的完整结果（给报告和测试用）。"""

    root: Path
    result: ScanResult
    infos: list[PhotoInfo] = field(default_factory=list)
    db_path: Path = DEFAULT_DB
    db_written: int = 0
    db_total: int = 0

    @property
    def root_is_invalid(self) -> bool:
        """根目录本身不可访问（不存在 / 不是文件夹 / 无法读取）。"""
        return any(issue.path == self.result.root for issue in self.result.issues)


def run_scan(root: str | Path, db_path: str | Path = DEFAULT_DB) -> ScanOutcome:
    """完整的 V1.5 流程：扫描 → 读 EXIF → 写数据库。

    任何单个文件的失败都只体现在结果的 ``exif_error`` / ``issues`` 里，
    本函数不抛异常（根目录无效也只是返回带 issue 的结果）。
    """
    result = scan_folder(root)
    infos = [read_photo_info(item.path, item.category) for item in result.files]

    outcome = ScanOutcome(root=Path(root), result=result, infos=infos, db_path=Path(db_path))
    with PhotoDatabase.open(db_path) as database:
        outcome.db_written = database.upsert_many(infos)
        outcome.db_total = database.count_photos()
    return outcome


# --------------------------------------------------------------------------- #
# 显示格式化
# --------------------------------------------------------------------------- #


def format_size(size_bytes: int) -> str:
    """字节数 → 人类可读（如 "45.2 MB"）。"""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_shutter(seconds: float) -> str:
    """快门秒数 → "1/250s" / "2s"。"""
    if seconds >= 1:
        text = f"{seconds:g}"
        return f"{text}s"
    denominator = round(1 / seconds) if seconds > 0 else 0
    if denominator > 0 and abs(1 / denominator - seconds) <= seconds * 0.05:
        return f"1/{denominator}s"
    return f"{seconds:g}s"


def _strip_zero(value: float) -> str:
    """4.0 → "4"，4.5 → "4.5"。"""
    return f"{value:g}"


def format_exif_status(info: PhotoInfo) -> str:
    """EXIF 状态的中文说明。"""
    if info.exif_status == EXIF_OK:
        return "已读取"
    if info.exif_status == EXIF_NO_EXIF:
        return "无 EXIF 数据（文件正常）"
    if info.exif_status == EXIF_ERROR:
        return f"读取失败：{info.exif_error}"
    return "未尝试（非照片文件或仅记录基础信息）"


def print_photo_info(info: PhotoInfo) -> None:
    """打印一条照片记录。"""
    label = CATEGORY_LABELS.get(info.category, info.category)
    print(f"{info.file_name}  [{label}]")
    rows: list[tuple[str, str]] = [("路径", str(info.path)), ("大小", format_size(info.size_bytes))]
    if info.modified_at:
        rows.append(("修改时间", info.modified_at))
    if info.taken_at:
        rows.append(("拍摄时间", info.taken_at))
    if info.camera:
        rows.append(("相机", info.camera))
    if info.lens_model:
        rows.append(("镜头", info.lens_model))
    if info.focal_length_mm is not None:
        rows.append(("焦距", f"{_strip_zero(info.focal_length_mm)}mm"))
    if info.f_number is not None:
        rows.append(("光圈", f"f/{_strip_zero(info.f_number)}"))
    if info.exposure_time_s is not None:
        rows.append(("快门", format_shutter(info.exposure_time_s)))
    if info.iso is not None:
        rows.append(("ISO", str(info.iso)))
    if info.exposure_bias_ev is not None:
        bias = info.exposure_bias_ev
        rows.append(("曝光补偿", "0 EV" if bias == 0 else f"{bias:+.1f} EV"))
    if info.has_gps:
        rows.append(("GPS", f"{info.gps_latitude:.6f}, {info.gps_longitude:.6f}"))
    if info.width_px and info.height_px:
        rows.append(("尺寸", f"{info.width_px} x {info.height_px}"))
    rows.append(("EXIF", format_exif_status(info)))
    for key, value in rows:
        print(f"  {key}：{value}")


# --------------------------------------------------------------------------- #
# 扫描报告
# --------------------------------------------------------------------------- #


def format_report(outcome: ScanOutcome) -> str:
    """按总体规划第七节的格式生成扫描报告。"""
    lines: list[str] = ["扫描完成", f"根目录：{outcome.result.root}"]

    total = len(outcome.infos)
    lines.append(f"总文件：{total}")

    # 成功处理 / 跳过 / 异常，按分类统计
    succeeded: dict[str, int] = {}
    with_exif: dict[str, int] = {}
    skipped: dict[str, int] = {}
    failed: dict[str, int] = {}
    for info in outcome.infos:
        if info.category in _PHOTO_CATEGORIES:
            if info.exif_status == EXIF_ERROR:
                failed[info.category] = failed.get(info.category, 0) + 1
            else:
                succeeded[info.category] = succeeded.get(info.category, 0) + 1
                if info.exif_status == EXIF_OK:
                    with_exif[info.category] = with_exif.get(info.category, 0) + 1
        else:
            skipped[info.category] = skipped.get(info.category, 0) + 1

    def emit_section(title: str, counts: dict[str, int], *, exif_counts: dict[str, int] | None = None) -> None:
        if not counts:
            return
        lines.append(title)
        for category in (CATEGORY_NEF, CATEGORY_JPEG, CATEGORY_PNG, CATEGORY_VIDEO, CATEGORY_UNSUPPORTED):
            count = counts.get(category, 0)
            if not count:
                continue
            label = CATEGORY_LABELS[category]
            if exif_counts is not None and category in (CATEGORY_NEF, CATEGORY_JPEG):
                lines.append(f"  {label}：{count}（含 EXIF：{exif_counts.get(category, 0)}）")
            else:
                lines.append(f"  {label}：{count}")

    emit_section("成功处理：", succeeded, exif_counts=with_exif)
    emit_section("跳过：", skipped)

    issues = outcome.result.issues
    if failed or issues:
        lines.append("异常：")
        for category in (CATEGORY_NEF, CATEGORY_JPEG, CATEGORY_PNG):
            count = failed.get(category, 0)
            if count:
                lines.append(f"  {CATEGORY_LABELS[category]}：{count}")
        if issues:
            lines.append(f"  无法访问：{len(issues)}")
            for issue in issues[:_MAX_ISSUES_SHOWN]:
                lines.append(f"    - {issue.path}（{issue.message}）")
            if len(issues) > _MAX_ISSUES_SHOWN:
                lines.append(f"    - ……以及另外 {len(issues) - _MAX_ISSUES_SHOWN} 项")

    lines.append(
        f"照片信息数据库：{outcome.db_path}"
        f"（本次写入 {outcome.db_written} 条，合计 {outcome.db_total} 条）"
    )
    lines.append("任务完成。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# stats 统计
# --------------------------------------------------------------------------- #


def format_stats(db_path: str | Path) -> str:
    """生成数据库统计报告。"""
    with PhotoDatabase.open(db_path) as database:
        total = database.count_photos()
        lines = [f"数据库：{Path(db_path)}", f"记录总数：{total}"]

        def emit_counts(title: str, counts: dict[str, int]) -> None:
            if not any(counts.values()):
                return
            lines.append(title)
            for label, count in list(counts.items())[:_MAX_STATS_SHOWN]:
                lines.append(f"  {label}：{count}")

        emit_counts(
            "按文件类型：",
            {CATEGORY_LABELS.get(label, label): count for label, count in database.count_by("category").items()},
        )

        status_counts = database.count_by_exif_status()
        status_labels = {
            EXIF_OK: "成功",
            EXIF_NO_EXIF: "缺少 EXIF",
            EXIF_ERROR: "读取失败",
            EXIF_SKIPPED: "未尝试",
        }
        emit_counts(
            "EXIF 状态：",
            {status_labels[status]: count for status, count in status_counts.items()},
        )

        # 相机 / 镜头用 Python 端分组，保证"厂商+型号"的合并显示逻辑一致
        camera_counts: dict[str, int] = {}
        lens_counts: dict[str, int] = {}
        for info in database.iter_photos():
            camera_counts[info.camera or "（未知）"] = camera_counts.get(info.camera or "（未知）", 0) + 1
            lens_counts[info.lens_model or "（未知）"] = lens_counts.get(info.lens_model or "（未知）", 0) + 1
        emit_counts("按相机：", dict(sorted(camera_counts.items(), key=lambda kv: -kv[1])))
        emit_counts("按镜头：", dict(sorted(lens_counts.items(), key=lambda kv: -kv[1])))

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photoclassifier",
        description="PhotoClassifier：面向摄影工作流的 RAW 照片整理与筛选工具（当前版本 V1.5）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="扫描文件夹，读取 EXIF，写入照片信息数据库")
    scan_parser.add_argument("folder", help="要扫描的照片文件夹")
    scan_parser.add_argument(
        "--db", default=str(DEFAULT_DB), help=f"SQLite 数据库路径（默认 {DEFAULT_DB}）"
    )

    show_parser = subparsers.add_parser("show", help="查看数据库中的照片记录")
    show_parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    show_parser.add_argument("--match", default="", help="按文件名或路径模糊筛选")
    show_parser.add_argument("--limit", type=int, default=20, help="最多显示多少条（默认 20）")

    stats_parser = subparsers.add_parser("stats", help="查看数据库统计")
    stats_parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")

    return parser


def main(argv: list[str] | None = None) -> int:
    # GBK 控制台等极端环境下避免 UnicodeEncodeError 中断任务
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "scan":
        outcome = run_scan(args.folder, db_path=args.db)
        print(format_report(outcome))
        return 1 if outcome.root_is_invalid else 0

    if args.command == "show":
        with PhotoDatabase.open(args.db) as database:
            if args.match:
                photos = database.find_photos(args.match, limit=args.limit)
            else:
                photos = []
                for info in database.iter_photos():
                    photos.append(info)
                    if len(photos) >= args.limit:
                        break
        if not photos:
            print("（数据库中没有匹配的记录）")
            return 1
        for index, info in enumerate(photos):
            if index:
                print()
            print_photo_info(info)
        return 0

    if args.command == "stats":
        print(format_stats(args.db))
        return 0

    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
