"""PhotoClassifier V2：基于 EXIF 的规则分类引擎。

对应《项目总体规划》V2「规则分类」：不依赖 AI，用 EXIF 数据做第一轮
自动分类。每条规则都是一个普通判断函数，作用于 :class:`PhotoInfo`；
命中时生成可解释的 :class:`RuleMatch`（原因文本里带实际数值）。

设计要点（见 docs/v2-plan.md）：

- 规则不建数据库表，在已加载的照片列表上即时计算（数据永远最新）；
- EXIF 缺失绝不报错——对应字段为空时该规则直接不命中；
- 阈值集中在 :class:`RuleConfig`，可整体替换（后续配置界面用）；
- 内置 6 条规则：夜景 / 高感 / 慢门 / 长焦 / 广角 / 大光圈。

安全：本模块只读取 PhotoInfo，不接触任何文件。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from src.formatting import format_shutter
from src.photo_info import PhotoInfo


# --------------------------------------------------------------------------- #
# 规则配置
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RuleConfig:
    """规则阈值。全部可配置，默认值来自 v2-plan.md 与规划文档示例。

    - ``night_start_hour`` / ``night_end_hour``：夜间时段（整数小时）。
      区间为 [night_start, 24) ∪ [0, night_end)。
    - ``high_iso``：高感阈值（ISO ≥ 该值）。
    - ``slow_shutter_s``：慢门阈值（快门秒数 < 该值）。
    - ``telephoto_mm``：长焦阈值（焦距 ≥ 该值）。
    - ``wide_angle_mm``：广角阈值（焦距 ≤ 该值）。
    - ``fast_lens_f``：大光圈阈值（光圈 ≤ 该值）。
    """

    night_start_hour: int = 18
    night_end_hour: int = 6
    high_iso: int = 3200
    slow_shutter_s: float = 1 / 30
    telephoto_mm: float = 70
    wide_angle_mm: float = 24
    fast_lens_f: float = 2.8


# --------------------------------------------------------------------------- #
# 规则结果
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RuleMatch:
    """一次规则命中，``reason`` 是给人看的解释。"""

    rule_id: str
    label: str
    reason: str


# --------------------------------------------------------------------------- #
# 内置规则（每个函数返回 RuleMatch 或 None，绝不抛异常）
# --------------------------------------------------------------------------- #


def _match_night(info: PhotoInfo, config: RuleConfig) -> RuleMatch | None:
    taken_at = info.taken_at
    if not taken_at or len(taken_at) < 13:
        return None
    try:
        hour = int(taken_at[11:13])
    except ValueError:
        return None
    start, end = config.night_start_hour, config.night_end_hour
    in_window = hour >= start or hour < end
    if not in_window:
        return None
    return RuleMatch(
        "night",
        "夜景",
        f"拍摄时间 {taken_at[11:19]} 在夜间时段（{start:02d}:00–{end:02d}:00）",
    )


def _match_high_iso(info: PhotoInfo, config: RuleConfig) -> RuleMatch | None:
    if info.iso is None or info.iso < config.high_iso:
        return None
    return RuleMatch("high_iso", "高感", f"ISO {info.iso} ≥ {config.high_iso}")


def _match_slow_shutter(info: PhotoInfo, config: RuleConfig) -> RuleMatch | None:
    # 注意：快门速度 < 1/30s 意味着曝光时间更长（秒数更大），
    # 因此这里比较的是"秒数 > 阈值"。
    if info.exposure_time_s is None or info.exposure_time_s <= config.slow_shutter_s:
        return None
    return RuleMatch(
        "slow_shutter",
        "慢门",
        f"快门 {format_shutter(info.exposure_time_s)} 慢于 {format_shutter(config.slow_shutter_s)}",
    )


def _match_telephoto(info: PhotoInfo, config: RuleConfig) -> RuleMatch | None:
    if info.focal_length_mm is None or info.focal_length_mm < config.telephoto_mm:
        return None
    return RuleMatch(
        "telephoto", "长焦", f"焦距 {info.focal_length_mm:g} mm ≥ {config.telephoto_mm:g} mm"
    )


def _match_wide_angle(info: PhotoInfo, config: RuleConfig) -> RuleMatch | None:
    if info.focal_length_mm is None or info.focal_length_mm > config.wide_angle_mm:
        return None
    return RuleMatch(
        "wide_angle", "广角", f"焦距 {info.focal_length_mm:g} mm ≤ {config.wide_angle_mm:g} mm"
    )


def _match_fast_lens(info: PhotoInfo, config: RuleConfig) -> RuleMatch | None:
    if info.f_number is None or info.f_number > config.fast_lens_f:
        return None
    return RuleMatch(
        "fast_lens", "大光圈", f"光圈 f/{info.f_number:g} ≤ f/{config.fast_lens_f:g}"
    )


#: 内置规则注册表：(rule_id, 中文名, 英文名, 判断函数)，顺序即报告顺序。
RULE_REGISTRY: tuple[tuple[str, str, str, Callable[[PhotoInfo, RuleConfig], RuleMatch | None]], ...] = (
    ("night", "夜景", "Night", _match_night),
    ("high_iso", "高感", "High ISO", _match_high_iso),
    ("slow_shutter", "慢门", "Slow shutter", _match_slow_shutter),
    ("telephoto", "长焦", "Telephoto", _match_telephoto),
    ("wide_angle", "广角", "Wide angle", _match_wide_angle),
    ("fast_lens", "大光圈", "Fast lens", _match_fast_lens),
)


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #


class RuleEngine:
    """规则引擎：对单张照片或照片列表做规则评估。"""

    def __init__(self, config: RuleConfig | None = None):
        self.config = config or RuleConfig()

    # ---- 元信息 ---------------------------------------------------------- #

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(rule_id for rule_id, *_ in RULE_REGISTRY)

    def labels(self) -> dict[str, tuple[str, str]]:
        """``{rule_id: (中文名, 英文名)}``，供 CLI / GUI 展示。"""
        return {rule_id: (zh, en) for rule_id, zh, en, _ in RULE_REGISTRY}

    def describe(self, rule_id: str) -> str:
        """规则阈值的人类可读摘要（报告用）。"""
        cfg = self.config
        if rule_id == "night":
            return f"时间窗 {cfg.night_start_hour:02d}:00–{cfg.night_end_hour:02d}:00"
        if rule_id == "high_iso":
            return f"ISO ≥ {cfg.high_iso}"
        if rule_id == "slow_shutter":
            return f"快门 < {format_shutter(cfg.slow_shutter_s)}"
        if rule_id == "telephoto":
            return f"焦距 ≥ {cfg.telephoto_mm:g} mm"
        if rule_id == "wide_angle":
            return f"焦距 ≤ {cfg.wide_angle_mm:g} mm"
        if rule_id == "fast_lens":
            return f"光圈 ≤ f/{cfg.fast_lens_f:g}"
        return ""

    def with_config(self, **overrides) -> "RuleEngine":
        """返回一个使用覆盖后配置的新引擎（便于测试与配置界面）。"""
        return RuleEngine(replace(self.config, **overrides))

    # ---- 评估 ------------------------------------------------------------ #

    def evaluate(self, info: PhotoInfo) -> list[RuleMatch]:
        """评估一张照片，返回全部命中的规则（可能为空列表）。"""
        matches: list[RuleMatch] = []
        for rule_id, _zh, _en, evaluator in RULE_REGISTRY:
            try:
                match = evaluator(info, self.config)
            except Exception:
                # 单条规则异常不影响其他规则（防御性；正常不会发生）
                match = None
            if match is not None:
                matches.append(match)
        return matches

    def classify(self, infos: Iterable[PhotoInfo]) -> dict[str, list[RuleMatch]]:
        """批量评估，返回 ``{照片绝对路径: 命中列表}``。"""
        result: dict[str, list[RuleMatch]] = {}
        for info in infos:
            result[str(info.path)] = self.evaluate(info)
        return result
