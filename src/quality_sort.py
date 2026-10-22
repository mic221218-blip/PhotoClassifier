"""P1：按清晰度排序（纯函数，方便测试）。

清晰度来自 ``QualityMeasurement.blur_score``（拉普拉斯方差，越大越锐）。
未分析、分析失败、视频/无测量值的照片统一排在最后，不能当成 0 分。
"""

from __future__ import annotations

import math
from typing import Iterable, Mapping

from src.photo_info import PhotoInfo
from src.quality import QualityMeasurement

SORT_DEFAULT = "default"
SORT_SHARPNESS = "sharpness"


def sharpness_score(measurement: QualityMeasurement | None) -> float | None:
    """返回有效清晰度分数；未分析 / 失败 / 非有限值返回 None。"""
    if measurement is None or not measurement.analyzed:
        return None
    score = measurement.blur_score
    if score is None or not math.isfinite(float(score)):
        return None
    return float(score)


def sharpness_key(
    info: PhotoInfo, measurement: QualityMeasurement | None
) -> tuple[int, float, str, str, str]:
    """排序 key：有效分数在前；分数降序；再按拍摄时间、文件名稳定排序。"""
    score = sharpness_score(measurement)
    if score is None:
        # 未分析 / 失败 / 视频：统一放最后，内部保持路径顺序
        return (1, 0.0, "", "", str(info.path).casefold())
    return (
        0,
        -score,
        info.taken_at or "",
        info.file_name.casefold(),
        str(info.path).casefold(),
    )


def sort_by_sharpness(
    photos: Iterable[PhotoInfo], measurements: Mapping[str, QualityMeasurement]
) -> list[PhotoInfo]:
    """按清晰度降序（未分析置底），返回新列表。"""
    items = list(photos)
    items.sort(key=lambda info: sharpness_key(info, measurements.get(str(info.path))))
    return items
