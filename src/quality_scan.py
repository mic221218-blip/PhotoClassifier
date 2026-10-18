"""PhotoClassifier V2.5：质量分析流程（把预览、度量、哈希串起来）。

职责：

1. 挑出"需要分析"的照片（增量：只看没有记录 / 版本变了 / 文件变了的）；
2. 多线程读预览、算指标、算文件指纹；
3. 结果批量写入 ``photo_quality``；
4. 为"同尺寸 + 首尾指纹相同"的候补补算整文件哈希（精确重复判定用）。

整个流程只读原始照片；失败只记一行错误，不中断任务。
"""

from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from src.duplicates import DuplicateResult, DuplicateFinder, candidates_from
from src.photo_info import PhotoInfo
from src.preview import PillowMissing, PreviewError, is_supported, load_preview
from src.quality import (
    ANALYZER_VERSION,
    FLAG_LABELS,
    PREVIEW_NONE,
    QualityConfig,
    QualityEngine,
    QualityMeasurement,
    measure_gray,
    dhash_from_grid,
)
from src.quality_store import QualityStore

#: 首尾各读多少字节做"快速指纹"（精确重复的第一道筛）。
_QUICK_HASH_BYTES = 128 * 1024

#: 每写多少条记录提交一次。
_WRITE_BATCH = 50


@dataclass
class AnalysisReport:
    """一次质量分析的汇总（CLI 报告 / GUI 状态栏）。"""

    considered: int = 0
    analyzed_now: int = 0
    analyzed_total: int = 0
    failed: int = 0
    skipped: int = 0
    already: int = 0
    elapsed: float = 0.0
    embedded: int = 0
    full_image: int = 0
    no_preview: int = 0
    flag_counts: dict[str, int] = field(default_factory=dict)
    duplicate_groups: int = 0
    exact_groups: int = 0
    near_groups: int = 0
    sequence_groups: int = 0
    duplicate_photos: int = 0
    cancelled: bool = False

    @property
    def rate(self) -> float:
        return self.analyzed_now / self.elapsed if self.elapsed > 0 else 0.0


@dataclass
class AnalyzeOptions:
    """分析参数。"""

    workers: int = 4
    force: bool = False
    limit: int = 0
    batch_size: int = _WRITE_BATCH


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 文件指纹
# --------------------------------------------------------------------------- #


def quick_hash(path: str | Path) -> str:
    """首尾各 128KiB + 文件大小的 SHA-256（便宜的第一道筛）。"""
    file_path = Path(path)
    size = file_path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with open(file_path, "rb") as handle:
        if size <= _QUICK_HASH_BYTES * 2:
            digest.update(handle.read())
        else:
            digest.update(handle.read(_QUICK_HASH_BYTES))
            handle.seek(-_QUICK_HASH_BYTES, os.SEEK_END)
            digest.update(handle.read(_QUICK_HASH_BYTES))
    return digest.hexdigest()


def full_hash(path: str | Path) -> str:
    """整文件 SHA-256（只在快速指纹撞车时计算）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 单张分析
# --------------------------------------------------------------------------- #


def measure_photo(
    info: PhotoInfo,
    config: QualityConfig | None = None,
    *,
    compute_quick_hash: bool = True,
) -> QualityMeasurement:
    """分析一张照片，返回测量记录（任何失败都只体现在 ``error`` 里）。"""
    cfg = config or QualityConfig()
    path = Path(info.path)
    key = str(path.resolve())
    measurement = QualityMeasurement(
        path=key,
        analyzer_version=ANALYZER_VERSION,
        analyzed_at=_now(),
        preview_source=PREVIEW_NONE,
        file_size=info.size_bytes,
        file_mtime=info.modified_at,
    )

    quick = None
    if compute_quick_hash:
        try:
            quick = quick_hash(path)
        except OSError as exc:
            return _replace(measurement, error=f"无法读取文件: {exc}")

    try:
        preview = load_preview(info, cfg)
    except PillowMissing:
        raise
    except PreviewError as exc:
        return _replace(measurement, quick_hash=quick, error=str(exc))
    except Exception as exc:  # 防御性：任何意外都只影响这张照片
        return _replace(measurement, quick_hash=quick, error=f"分析失败: {exc}")

    try:
        metrics = measure_gray(preview.gray)
        digest = dhash_from_grid(preview.hash_grid)
    except Exception as exc:
        return _replace(measurement, quick_hash=quick, error=f"度量失败: {exc}")

    return QualityMeasurement(
        path=key,
        analyzer_version=ANALYZER_VERSION,
        analyzed_at=_now(),
        preview_source=preview.source,
        preview_width=preview.width,
        preview_height=preview.height,
        blur_score=metrics.blur_score,
        contrast=metrics.contrast,
        anisotropy=metrics.anisotropy,
        mean_lum=metrics.mean_lum,
        std_lum=metrics.std_lum,
        clip_low_frac=metrics.clip_low_frac,
        clip_high_frac=metrics.clip_high_frac,
        dhash=digest,
        quick_hash=quick,
        file_size=info.size_bytes,
        file_mtime=info.modified_at,
    )


def _replace(measurement: QualityMeasurement, **changes) -> QualityMeasurement:
    from dataclasses import replace

    return replace(measurement, **changes)


# --------------------------------------------------------------------------- #
# 批量分析
# --------------------------------------------------------------------------- #


def analyze_photos(
    infos: Sequence[PhotoInfo],
    store: QualityStore,
    *,
    config: QualityConfig | None = None,
    options: AnalyzeOptions | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> AnalysisReport:
    """分析一批照片并写入数据库。

    - ``infos``：候选照片（视频/未知格式会计入 skipped，不分析）；
    - 增量：只处理还没有记录、版本变了、或文件变化的照片；
    - ``progress(done, total, file_name)`` 在调用线程里回调（GUI 可安全更新界面）；
    - ``is_cancelled()`` 返回 True 时尽快停下，已完成的结果保留。
    """
    cfg = config or QualityConfig()
    opts = options or AnalyzeOptions()
    report = AnalysisReport(considered=len(infos))
    started = time.perf_counter()

    analyzable = [info for info in infos if is_supported(info.category)]
    report.skipped = len(infos) - len(analyzable)
    pending_all = store.pending(analyzable, force=opts.force)
    report.already = len(analyzable) - len(pending_all)
    pending = pending_all[: opts.limit] if opts.limit > 0 else pending_all

    total = len(pending)
    if total == 0:
        report.elapsed = time.perf_counter() - started
        _augment_report(report, store, infos, cfg)
        return report

    workers = max(1, min(opts.workers, os.cpu_count() or 4, total))
    buffer: list[QualityMeasurement] = []
    done = 0

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(measure_photo, info, cfg): info for info in pending
            }
            try:
                for future in as_completed(futures):
                    if is_cancelled is not None and is_cancelled():
                        report.cancelled = True
                        for pending_future in futures:
                            pending_future.cancel()
                        break
                    info = futures[future]
                    try:
                        measurement = future.result()
                    except PillowMissing:
                        raise
                    except Exception as exc:  # 理论上 measure_photo 已兜底
                        measurement = QualityMeasurement(
                            path=str(Path(info.path).resolve()),
                            analyzer_version=ANALYZER_VERSION,
                            analyzed_at=_now(),
                            preview_source=PREVIEW_NONE,
                            file_size=info.size_bytes,
                            file_mtime=info.modified_at,
                            error=f"分析失败: {exc}",
                        )
                    buffer.append(measurement)
                    done += 1
                    if len(buffer) >= opts.batch_size:
                        store.upsert_many(buffer)
                        buffer.clear()
                    if progress is not None:
                        progress(done, total, Path(info.path).name)
            finally:
                if buffer:
                    store.upsert_many(buffer)
                    buffer.clear()
    except PillowMissing:
        raise

    report.analyzed_now = done
    report.cancelled = report.cancelled or bool(is_cancelled and is_cancelled())

    # 清理照片库里已不存在的质量记录（不碰任何文件）
    try:
        store.prune()
    except Exception:
        pass

    # 精确重复：为撞车的候补补算整文件哈希（数量很少）
    _fill_content_hashes(store)
    report.elapsed = time.perf_counter() - started
    _augment_report(report, store, infos, cfg)
    return report


def _fill_content_hashes(store: QualityStore) -> None:
    try:
        candidates = store.exact_duplicate_candidates()
    except Exception:
        return
    for measurement in candidates:
        try:
            digest = full_hash(measurement.path)
        except OSError:
            continue
        store.set_content_hash(measurement.path, digest)


def _augment_report(
    report: AnalysisReport,
    store: QualityStore,
    infos: Sequence[PhotoInfo],
    config: QualityConfig,
) -> None:
    """用数据库里的全量测量值补齐统计（预览来源 / 标记数 / 重复组）。"""
    measurements = list(store.iter_measurements())
    infos_by_path = {str(Path(info.path).resolve()): info for info in infos}
    engine = QualityEngine(config)
    counts = {flag_id: 0 for flag_id in FLAG_LABELS}
    for measurement in measurements:
        info = infos_by_path.get(measurement.path)
        for flag in engine.flags(measurement, info):
            counts[flag.flag_id] = counts.get(flag.flag_id, 0) + 1
        if measurement.preview_source == "embedded_jpeg":
            report.embedded += 1
        elif measurement.preview_source == "full_image":
            report.full_image += 1
        else:
            report.no_preview += 1

    report.analyzed_total = sum(1 for measurement in measurements if measurement.analyzed)
    report.failed = sum(1 for measurement in measurements if measurement.error)

    if infos_by_path:
        candidates = candidates_from(measurements, infos_by_path)
    else:
        candidates = candidates_from(measurements, {})
    finding = DuplicateFinder(config).find(candidates)
    counts["duplicate"] = finding.flagged_count
    report.duplicate_groups = len(finding.groups)
    report.exact_groups = sum(1 for group in finding.groups if group.kind == "exact")
    report.near_groups = sum(1 for group in finding.groups if group.kind == "near")
    report.sequence_groups = len(finding.sequences)
    report.duplicate_photos = finding.flagged_count
    report.flag_counts = counts


def rebuild_duplicates(
    store: QualityStore, infos: Iterable[PhotoInfo], config: QualityConfig | None = None
) -> DuplicateResult:
    """从数据库重新计算重复分组（GUI 刷新 / CLI 报告用）。"""
    cfg = config or QualityConfig()
    info_list = list(infos)
    infos_by_path = {str(Path(info.path).resolve()): info for info in info_list}
    measurements = list(store.iter_measurements())
    candidates = candidates_from(measurements, infos_by_path)
    return DuplicateFinder(cfg).find(candidates)
