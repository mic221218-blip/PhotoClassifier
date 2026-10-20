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
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

from src.database import PhotoDatabase
from src.exif_reader import read_photo_info
from src.filtering import FilterSelection, matches as selection_matches
from src.formatting import format_shutter, format_size
from src.photo_info import (
    EXIF_ERROR,
    EXIF_NO_EXIF,
    EXIF_OK,
    EXIF_SKIPPED,
    PhotoInfo,
)
from src.preview import PillowMissing, is_supported
from src.quality import (
    FLAG_IDS,
    FLAG_LABELS,
    QualityConfig,
    QualityEngine,
    QualityFlag,
    QualityMeasurement,
)
from src.quality_scan import (
    AnalysisReport,
    AnalyzeOptions,
    analyze_photos,
    rebuild_duplicates,
)
from src.quality_store import QualityStore
from src.rules import RuleEngine, RuleMatch
from src.tag_cli import (
    cmd_tag_add,
    cmd_tag_delete,
    cmd_tag_list,
    cmd_tag_remove,
    cmd_tag_rename,
    format_tags_report,
    resolve_tags,
)
from src.tag_store import Tag, TagStore
from src.tags import TagConflict, TagError
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
# 规则分类（V2）/ 筛选入口
# --------------------------------------------------------------------------- #


def format_classify_report(db_path: str | Path) -> str:
    """按规则统计数据库中的照片，并给出少量匹配示例（可解释）。"""
    engine = RuleEngine()
    labels = engine.labels()
    counts = {rule_id: 0 for rule_id in engine.rule_ids}
    examples = {rule_id: [] for rule_id in engine.rule_ids}
    total = 0

    with PhotoDatabase.open(db_path) as database:
        photos = list(database.iter_photos())

    total = len(photos)
    for info in photos:
        for match in engine.evaluate(info):
            counts[match.rule_id] += 1
            if len(examples[match.rule_id]) < 3:
                examples[match.rule_id].append((info, match))

    lines = [f"规则分类完成（共 {total:,} 张）"]
    for rule_id in engine.rule_ids:
        zh, en = labels[rule_id]
        label = f"{zh}  {en}"
        lines.append(f"  {label}：{counts[rule_id]:,} 张（{engine.describe(rule_id)}）")
    if any(examples.values()):
        lines.append("示例 / examples:")
        for rule_id in engine.rule_ids:
            for info, match in examples[rule_id]:
                lines.append(f"  {info.file_name}  {match.label}：{match.reason}")
    return "\n".join(lines)


def _parse_rule_ids(raw: str, engine: RuleEngine) -> list[str]:
    """解析 --rules 参数（逗号分隔），未知规则给出可读错误。"""
    requested = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not requested:
        raise SystemExit("filter: --rules 不能为空，可用规则：" + ", ".join(engine.rule_ids))
    unknown = [rule_id for rule_id in requested if rule_id not in engine.rule_ids]
    if unknown:
        raise SystemExit(
            f"filter: 未知规则 {', '.join(unknown)}；可用规则：" + ", ".join(engine.rule_ids)
        )
    return requested


def _parse_quality_ids(raw: str) -> list[str]:
    """解析 --quality / --flags 参数（逗号分隔的标记 id）。"""
    requested = [part.strip().lower() for part in raw.split(",") if part.strip()]
    unknown = [flag_id for flag_id in requested if flag_id not in FLAG_IDS]
    if unknown:
        raise SystemExit(
            f"未知质量标记 {', '.join(unknown)}；可用标记：" + ", ".join(FLAG_IDS)
        )
    return requested


# --------------------------------------------------------------------------- #
# 质量分析（V2.5）
# --------------------------------------------------------------------------- #


def format_analyze_report(
    report: AnalysisReport, db_path: str | Path, *, workers: int = 4
) -> str:
    """`analyze` 命令的报告。"""
    skipped_parts: list[str] = []
    if report.skipped:
        skipped_parts.append(f"非照片 {report.skipped:,}")
    if report.already:
        skipped_parts.append(f"已是最新 {report.already:,}")
    skipped_text = "，".join(skipped_parts) if skipped_parts else "无"

    lines = [f"质量分析完成（候选 {report.considered:,} 张）"]
    lines.append(f"  本次分析：{report.analyzed_now:,} 张（跳过：{skipped_text}）")
    lines.append(
        f"  预览来源：内嵌 JPEG {report.embedded:,} · "
        f"原图 {report.full_image:,} · 无预览 {report.no_preview:,}"
    )
    if report.analyzed_now:
        lines.append(
            f"  耗时：{report.elapsed:.1f} 秒（{report.rate:.1f} 张/秒，{workers} 线程）"
        )
    else:
        lines.append(f"  耗时：{report.elapsed:.1f} 秒（没有需要分析的照片）")
    if report.cancelled:
        lines.append("  已取消：只保留已完成的部分（再次运行会继续增量分析）")

    counts = report.flag_counts
    flag_text = " · ".join(
        f"{FLAG_LABELS[flag_id]} {counts.get(flag_id, 0):,}"
        for flag_id in FLAG_IDS
        if flag_id != "duplicate"
    )
    if flag_text:
        lines.append(f"  {flag_text}")

    duplicate = (
        f"  重复：{report.duplicate_groups:,} 组"
        f"（精确 {report.exact_groups:,} / 近似 {report.near_groups:,}，"
        f"涉及 {report.duplicate_photos:,} 张）"
    )
    if report.sequence_groups:
        duplicate += f"；延时/连拍序列 {report.sequence_groups:,} 组（不标记为重复）"
    lines.append(duplicate)

    if report.failed:
        lines.append(f"  读取失败：{report.failed:,} 张（详见 quality 输出）")
    lines.append(f"  结果已写入：{Path(db_path)}（photo_quality 表）")
    return "\n".join(lines)


def write_quality_csv(db_path: str | Path, out_path: str | Path) -> int:
    """把测量值导出成 CSV（用于人工校准阈值）。"""
    fields = [
        "path",
        "preview_source",
        "preview_width",
        "preview_height",
        "blur_score",
        "contrast",
        "anisotropy",
        "mean_lum",
        "std_lum",
        "clip_low_frac",
        "clip_high_frac",
        "dhash",
        "error",
    ]
    written = 0
    with QualityStore.open(db_path) as store, open(
        out_path, "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for measurement in store.iter_measurements():
            writer.writerow([getattr(measurement, field) for field in fields])
            written += 1
    return written


def _quality_context(db_path: str | Path):
    """加载（照片, 测量值, 重复分组, 质量引擎）——报告类命令共用。"""
    database = PhotoDatabase.open(db_path)
    photos = list(database.iter_photos())
    store = QualityStore.open(db_path)
    measurements = list(store.iter_measurements())
    duplicates = rebuild_duplicates(store, photos)
    return database, store, photos, measurements, duplicates


def format_quality_report(
    db_path: str | Path,
    *,
    flag_ids: list[str] | None = None,
    explain: bool = False,
    limit: int = 30,
) -> str:
    """`quality` 命令的报告：统计 + 标记清单。"""
    selected = set(flag_ids or FLAG_IDS)
    engine = QualityEngine()
    database, store, photos, measurements, duplicates = _quality_context(db_path)
    try:
        infos_by_path = {str(info.path.resolve()): info for info in photos}
        counts = {flag_id: 0 for flag_id in FLAG_IDS}
        rows: list[tuple[QualityMeasurement, list[QualityFlag]]] = []
        for measurement in measurements:
            info = infos_by_path.get(measurement.path)
            flags = engine.flags(measurement, info)
            reason = duplicates.flag_reason(measurement.path)
            if reason:
                flags.append(QualityFlag("duplicate", FLAG_LABELS["duplicate"], reason))
            for flag in flags:
                counts[flag.flag_id] = counts.get(flag.flag_id, 0) + 1
            chosen = [flag for flag in flags if flag.flag_id in selected]
            if chosen:
                rows.append((measurement, chosen))
        stats = store.stats()
    finally:
        store.close()
        database.close()

    analyzable = sum(1 for info in photos if is_supported(info.category))
    pending = max(0, analyzable - len(measurements))
    lines = [
        f"质量统计（照片 {len(photos):,} 张，已分析 {len(measurements):,} 张，"
        f"未分析 {pending:,} 张）"
    ]
    lines.append(
        f"  预览来源：内嵌 JPEG {stats.embedded:,} · 原图 {stats.full_image:,} · "
        f"无预览 {stats.no_preview:,}"
    )
    for flag_id in FLAG_IDS:
        lines.append(
            f"  {FLAG_LABELS[flag_id]}（{flag_id}）：{counts.get(flag_id, 0):,} 张"
            f"（{engine.describe(flag_id)}）"
        )
    if duplicates.groups:
        lines.append(
            f"  重复分组：{len(duplicates.groups):,} 组，涉及 {duplicates.flagged_count:,} 张"
        )

    if pending:
        lines.append(f"提示：还有 {pending:,} 张没有分析，先运行 `analyze`。")

    if not rows:
        lines.append("（没有匹配的标记）")
        return "\n".join(lines)

    lines.append(f"标记清单（{len(rows):,} 张，最多显示 {limit} 张）：")
    for measurement, chosen in rows[:limit]:
        name = Path(measurement.path).name
        if explain:
            detail = "；".join(f"{flag.label}：{flag.reason}" for flag in chosen)
        else:
            detail = " · ".join(flag.label for flag in chosen)
        lines.append(f"  {name}  {detail}")
    if len(rows) > limit:
        lines.append(f"  ……以及另外 {len(rows) - limit:,} 张（用 --limit 调整）")
    return "\n".join(lines)


def _span_text(best_seconds: float | None, seconds: float | None) -> str:
    if best_seconds is None or seconds is None:
        return ""
    delta = abs(seconds - best_seconds)
    if delta < 1:
        return "同一秒"
    if delta < 60:
        return f"相差 {delta:.0f}s"
    return f"相差 {delta / 60:.1f} 分钟"


def format_dupes_report(
    db_path: str | Path,
    *,
    config: QualityConfig | None = None,
    limit: int = 50,
    show_sequences: bool = False,
) -> str:
    """`dupes` 命令的报告：重复分组 + 建议保留的排序。"""
    cfg = config or QualityConfig()
    with PhotoDatabase.open(db_path) as database:
        photos = list(database.iter_photos())
        with QualityStore.open(db_path) as store:
            duplicates = rebuild_duplicates(store, photos, cfg)

    groups = duplicates.groups
    lines: list[str] = []
    exact = sum(1 for group in groups if group.kind == "exact")
    near = len(groups) - exact
    lines.append(
        f"重复照片：{len(groups):,} 组（精确 {exact:,} / 近似 {near:,}，"
        f"涉及 {duplicates.flagged_count:,} 张）"
    )
    if duplicates.sequences:
        lines.append(
            f"延时/连拍序列：{len(duplicates.sequences):,} 组（"
            f"{sum(group.size for group in duplicates.sequences):,} 张），不标记为重复"
        )

    if not groups:
        lines.append("（没有发现重复照片）")
    for index, group in enumerate(groups[:limit], start=1):
        best = group.best
        header = f"第 {index} 组（{group.kind_label}，{group.size} 张"
        if group.kind == "near" and group.distance is not None:
            header += f"，dHash 距离 {group.distance}"
        header += "）"
        lines.append(header)
        for member in group.members:
            sharpness = f"{member.blur_score:.0f}" if member.blur_score is not None else "—"
            prefix = "[建议保留]" if member is best else "          "
            extra = ""
            if member is not best:
                span = _span_text(best.timestamp, member.timestamp)
                if span:
                    extra = f"   {span}"
            lines.append(f"  {prefix} 清晰度 {sharpness}  {member.name}{extra}")
    if len(groups) > limit:
        lines.append(f"……以及另外 {len(groups) - limit:,} 组（用 --limit 调整）")

    if show_sequences and duplicates.sequences:
        lines.append("")
        lines.append("延时/连拍序列（仅供参考，不计入重复）：")
        for index, group in enumerate(duplicates.sequences[:limit], start=1):
            span = group.span_seconds or 0
            lines.append(
                f"  序列 {index}：{group.size:,} 张，跨度 {span / 60:.1f} 分钟，"
                f"起始 {Path(group.best.path).name}"
            )
    return "\n".join(lines)


def run_filter(
    db_path: str | Path,
    rule_ids: list[str] | None,
    *,
    use_and: bool = True,
    gps_only: bool = False,
    limit: int = 0,
    quality_ids: list[str] | None = None,
    tag_ids: list[int] | None = None,
    strict: bool = False,
    quality_ok: bool = False,
) -> list[tuple[PhotoInfo, list[RuleMatch], list[QualityFlag], list[Tag]]]:
    """按规则 / 质量 / 用户标签筛选照片。

    返回 ``(照片, 规则命中, 质量标记, 用户标签)`` 的列表。
    """
    engine = RuleEngine()
    quality_engine = QualityEngine()
    duplicate_config = QualityConfig()
    if strict:
        duplicate_config = duplicate_config.with_config(sequence_folder_mode=False)
    selection = FilterSelection(
        rule_ids=frozenset(rule_ids or ()),
        quality_ids=frozenset(quality_ids or ()),
        tag_ids=frozenset(tag_ids or ()),
        and_mode=use_and,
        gps_only=gps_only,
        quality_ok=quality_ok,
    )
    if selection.is_empty:
        # 调用方应提前报错；这里防御性返回空
        return []

    matched: list[tuple[PhotoInfo, list[RuleMatch], list[QualityFlag], list[Tag]]] = []

    with PhotoDatabase.open(db_path) as database:
        photos = list(database.iter_photos())
        with QualityStore.open(db_path) as store:
            measurements = {item.path: item for item in store.iter_measurements()}
            duplicates = rebuild_duplicates(store, photos, duplicate_config)
        with TagStore.open(db_path) as tag_store:
            tags_by_path = tag_store.tags_for_paths(info.path for info in photos)

    for info in photos:
        rule_matches = engine.evaluate(info)
        rule_hits = {match.rule_id for match in rule_matches}

        quality_flags: list[QualityFlag] = []
        measurement = measurements.get(str(info.path))
        if measurement is not None and measurement.analyzed:
            quality_flags = quality_engine.flags(measurement, info)
        reason = duplicates.flag_reason(info.path)
        if reason:
            quality_flags.append(QualityFlag("duplicate", FLAG_LABELS["duplicate"], reason))
        quality_hits = {flag.flag_id for flag in quality_flags}
        if measurement is None or measurement.error:
            quality_hits.add("__unanalyzed__")

        user_tags = tags_by_path.get(str(info.path), [])
        tag_hits = {tag.id for tag in user_tags}

        if not selection_matches(
            info,
            rule_hits=rule_hits,
            quality_hits=quality_hits,
            tag_hits=tag_hits,
            selection=selection,
        ):
            continue

        matched.append((info, rule_matches, quality_flags, user_tags))

    if limit > 0:
        matched = matched[:limit]
    return matched


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #


def _cmd_scan(args) -> int:
    outcome = run_scan(args.folder, db_path=args.db)
    print(format_report(outcome))
    return 1 if outcome.root_is_invalid else 0


def _cmd_show(args) -> int:
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
    with TagStore.open(args.db) as tag_store:
        tags_by_path = tag_store.tags_for_paths(info.path for info in photos)
    for index, info in enumerate(photos):
        if index:
            print()
        print_photo_info(info)
        tags = tags_by_path.get(str(info.path), [])
        if tags:
            print("  标签：" + "、".join(tag.name for tag in tags))
    return 0


def _cmd_tags(args) -> int:
    print(format_tags_report(args.db, limit=max(1, args.limit)))
    return 0


def _cmd_tag(args) -> int:
    if args.tag_command == "add":
        return cmd_tag_add(args)
    if args.tag_command == "remove":
        return cmd_tag_remove(args)
    if args.tag_command == "list":
        return cmd_tag_list(args)
    if args.tag_command == "rename":
        return cmd_tag_rename(args)
    if args.tag_command == "delete":
        return cmd_tag_delete(args)
    raise SystemExit(f"未知 tag 子命令：{args.tag_command}")


def _cmd_stats(args) -> int:
    print(format_stats(args.db))
    return 0


def _cmd_classify(args) -> int:
    print(format_classify_report(args.db))
    return 0


def _cmd_analyze(args) -> int:
    folder = args.folder or ""
    try:
        with PhotoDatabase.open(args.db) as database:
            infos = (
                database.find_photos_under([folder])
                if folder
                else list(database.iter_photos())
            )
            with QualityStore.open(args.db) as store:
                report = analyze_photos(
                    infos,
                    store,
                    options=AnalyzeOptions(
                        workers=max(1, args.workers),
                        force=args.force,
                        limit=max(0, args.limit),
                    ),
                    is_cancelled=None,
                )
    except PillowMissing as exc:
        print(str(exc))
        return 3

    print(format_analyze_report(report, args.db, workers=max(1, args.workers)))
    if args.csv:
        written = write_quality_csv(args.db, args.csv)
        print(f"测量值已导出：{args.csv}（{written:,} 条）")
    if report.considered == 0:
        print("（数据库里没有照片记录，先运行 scan）")
        return 1
    return 0


def _cmd_quality(args) -> int:
    flag_ids = _parse_quality_ids(args.flags) if args.flags else None
    with PhotoDatabase.open(args.db) as database:
        if database.count_photos() == 0:
            print("（数据库里没有照片记录，先运行 scan）")
            return 1
    with QualityStore.open(args.db) as store:
        if store.count() == 0:
            print("（还没有质量数据，先运行 `analyze`）")
            return 1
    print(
        format_quality_report(
            args.db,
            flag_ids=flag_ids,
            explain=args.explain,
            limit=max(1, args.limit),
        )
    )
    return 0


def _cmd_dupes(args) -> int:
    config = QualityConfig()
    if args.strict:
        config = config.with_config(sequence_folder_mode=False)
    if args.distance is not None or args.window is not None:
        overrides = {}
        if args.distance is not None:
            overrides["dhash_distance_max"] = max(0, args.distance)
        if args.window is not None:
            overrides["dupe_time_window_s"] = max(0.0, args.window)
        config = config.with_config(**overrides)
    print(
        format_dupes_report(
            args.db,
            config=config,
            limit=max(1, args.limit),
            show_sequences=args.sequences,
        )
    )
    return 0


def _cmd_filter(args) -> int:
    if not args.rules and not args.quality and not args.tags and not args.gps and not args.quality_ok:
        print(
            "filter: 请至少指定 --rules / --quality / --quality-ok / --tags / --gps"
            "（可用规则见 classify，标记见 quality，标签见 tags）"
        )
        return 2
    if args.quality_ok and args.quality:
        print("filter: --quality-ok 与 --quality 不能同时使用（一个看正常，一个看问题）")
        return 2
    engine = RuleEngine()
    rule_ids = _parse_rule_ids(args.rules, engine) if args.rules else None
    quality_ids = _parse_quality_ids(args.quality) if args.quality else None
    selected_tags = resolve_tags(args.db, args.tags) if args.tags else []
    tag_ids = [tag.id for tag in selected_tags] or None
    matched = run_filter(
        args.db,
        rule_ids,
        use_and=not args.or_,
        gps_only=args.gps,
        limit=args.limit,
        quality_ids=quality_ids,
        tag_ids=tag_ids,
        strict=args.strict,
        quality_ok=args.quality_ok,
    )
    if not matched:
        print("（没有匹配的照片）")
        return 1
    selected_tag_ids = {tag.id for tag in selected_tags}
    for info, matches, quality_flags, user_tags in matched:
        if args.explain:
            parts = [
                f"{match.label}：{match.reason}"
                for match in matches
                if rule_ids is None or match.rule_id in rule_ids
            ]
            parts.extend(
                f"{flag.label}：{flag.reason}"
                for flag in quality_flags
                if quality_ids is None or flag.flag_id in quality_ids
            )
            if args.quality_ok:
                parts.append("质量正常")
            parts.extend(
                f"★{tag.name}"
                for tag in user_tags
                if not selected_tag_ids or tag.id in selected_tag_ids
            )
            reasons = "；".join(parts) or "（仅按 GPS 筛选）"
            print(f"{info.file_name}  [{info.category.upper()}]  {reasons}")
        else:
            print(str(info.path))
    print(f"（共 {len(matched):,} 张）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photoclassifier",
        description="PhotoClassifier：面向摄影工作流的 RAW 照片整理与筛选工具（当前版本 V3）",
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

    classify_parser = subparsers.add_parser(
        "classify", help="按 EXIF 规则分类并统计（夜景 / 高感 / 慢门 / 长焦 / 广角 / 大光圈）"
    )
    classify_parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")

    filter_parser = subparsers.add_parser("filter", help="按规则 / 质量标记筛选照片（输出匹配文件）")
    filter_parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    filter_parser.add_argument(
        "--rules", default="", help="逗号分隔的规则，如 night,high_iso（见 classify）"
    )
    filter_parser.add_argument(
        "--quality",
        default="",
        help="逗号分隔的质量标记，如 blurry,duplicate（见 quality）",
    )
    filter_parser.add_argument(
        "--quality-ok",
        action="store_true",
        help="只看质量正常的照片（无模糊/运动/曝光问题；重复不在此列）",
    )
    filter_parser.add_argument(
        "--tags",
        default="",
        help="逗号分隔的用户标签，如 建筑,天空（见 tags）",
    )
    filter_parser.add_argument(
        "--or", dest="or_", action="store_true", help="条件之间用 OR（默认 AND）"
    )
    filter_parser.add_argument("--gps", action="store_true", help="只看有 GPS 坐标的照片")
    filter_parser.add_argument("--limit", type=int, default=0, help="最多输出多少条（0 = 全部）")
    filter_parser.add_argument("--explain", action="store_true", help="输出命中原因")
    filter_parser.add_argument(
        "--strict", action="store_true", help="严格重复判定（忽略延时/连拍序列豁免）"
    )

    analyze_parser = subparsers.add_parser(
        "analyze", help="分析图像质量（模糊 / 曝光 / 重复）并写入数据库"
    )
    analyze_parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    analyze_parser.add_argument("--folder", default="", help="只分析该文件夹（含子文件夹）")
    analyze_parser.add_argument("--force", action="store_true", help="忽略缓存，全部重新分析")
    analyze_parser.add_argument("--workers", type=int, default=4, help="并行线程数（默认 4）")
    analyze_parser.add_argument("--limit", type=int, default=0, help="最多分析多少张（0 = 全部）")
    analyze_parser.add_argument("--csv", default="", help="把测量值导出为 CSV（校准用）")

    quality_parser = subparsers.add_parser("quality", help="查看质量统计与标记清单")
    quality_parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    quality_parser.add_argument("--flags", default="", help="只看这些标记（逗号分隔）")
    quality_parser.add_argument("--explain", action="store_true", help="输出命中原因")
    quality_parser.add_argument("--limit", type=int, default=30, help="最多列出多少张（默认 30）")

    dupes_parser = subparsers.add_parser("dupes", help="查看重复照片分组")
    dupes_parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    dupes_parser.add_argument("--distance", type=int, default=None, help="dHash 距离上限（默认 8）")
    dupes_parser.add_argument("--window", type=float, default=None, help="时间窗秒数（默认 10）")
    dupes_parser.add_argument("--limit", type=int, default=50, help="最多显示多少组（默认 50）")
    dupes_parser.add_argument(
        "--sequences", action="store_true", help="同时列出延时/连拍序列"
    )
    dupes_parser.add_argument(
        "--strict", action="store_true", help="严格模式：延时文件夹里的相似照片也按重复处理"
    )

    tags_parser = subparsers.add_parser("tags", help="查看用户标签统计")
    tags_parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    tags_parser.add_argument("--limit", type=int, default=50, help="最多显示多少个标签")

    tag_parser = subparsers.add_parser(
        "tag", help="管理用户标签（add / remove / list / rename / delete）"
    )
    tag_sub = tag_parser.add_subparsers(dest="tag_command", required=True)

    tag_add = tag_sub.add_parser("add", help="给照片添加标签（不存在则创建）")
    tag_add.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    tag_add.add_argument("--tag", action="append", required=True, help="标签名，可重复")
    tag_add.add_argument("paths", nargs="*", help="照片路径（必须在数据库里）")
    tag_add.add_argument("--match", default="", help="按文件名/路径模糊选择照片")

    tag_remove = tag_sub.add_parser("remove", help="从照片移除标签")
    tag_remove.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    tag_remove.add_argument("--tag", action="append", required=True, help="标签名，可重复")
    tag_remove.add_argument("paths", nargs="*", help="照片路径（必须在数据库里）")
    tag_remove.add_argument("--match", default="", help="按文件名/路径模糊选择照片")

    tag_list = tag_sub.add_parser("list", help="查看照片的标签")
    tag_list.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    tag_list.add_argument("paths", nargs="*", help="照片路径")
    tag_list.add_argument("--match", default="", help="按文件名/路径模糊选择照片")
    tag_list.add_argument("--tag", default="", help="列出拥有该标签的照片")

    tag_rename = tag_sub.add_parser("rename", help="重命名标签")
    tag_rename.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    tag_rename.add_argument("old", help="旧名称")
    tag_rename.add_argument("new", help="新名称")

    tag_delete = tag_sub.add_parser("delete", help="删除标签及其全部关联")
    tag_delete.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    tag_delete.add_argument("name", help="标签名")
    tag_delete.add_argument("--yes", action="store_true", help="确认删除")

    return parser


_COMMANDS = {
    "scan": _cmd_scan,
    "show": _cmd_show,
    "stats": _cmd_stats,
    "classify": _cmd_classify,
    "analyze": _cmd_analyze,
    "quality": _cmd_quality,
    "dupes": _cmd_dupes,
    "tags": _cmd_tags,
    "tag": _cmd_tag,
    "filter": _cmd_filter,
}


def main(argv: list[str] | None = None) -> int:
    # GBK 控制台等极端环境下避免 UnicodeEncodeError 中断任务
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.error(f"未知命令：{args.command}")
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
