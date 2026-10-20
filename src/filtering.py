"""PhotoClassifier V3：统一筛选判定。

规则（EXIF）、质量标记、用户标签、GPS 都进入同一个 :class:`FilterSelection`，
GUI 与 CLI 都只调用 :func:`matches`，不再各自实现一份 AND/OR。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.photo_info import PhotoInfo


@dataclass(frozen=True)
class FilterSelection:
    """一次筛选的全部条件。``tag_ids`` 是用户标签的主键。"""

    rule_ids: frozenset[str] = frozenset()
    quality_ids: frozenset[str] = frozenset()
    tag_ids: frozenset[int] = frozenset()
    and_mode: bool = True
    gps_only: bool = False
    #: 只看“质量正常”：没有技术问题（模糊/运动/曝光/黑屏），重复单独判断。
    quality_ok: bool = False

    @property
    def selected_count(self) -> int:
        return (
            len(self.rule_ids)
            + len(self.quality_ids)
            + len(self.tag_ids)
            + int(self.quality_ok)
        )

    @property
    def is_empty(self) -> bool:
        return self.selected_count == 0 and not self.gps_only

    def with_values(
        self,
        *,
        rule_ids: Iterable[str] | None = None,
        quality_ids: Iterable[str] | None = None,
        tag_ids: Iterable[int] | None = None,
        and_mode: bool | None = None,
        gps_only: bool | None = None,
        quality_ok: bool | None = None,
    ) -> "FilterSelection":
        return FilterSelection(
            rule_ids=self.rule_ids if rule_ids is None else frozenset(rule_ids),
            quality_ids=(
                self.quality_ids if quality_ids is None else frozenset(quality_ids)
            ),
            tag_ids=self.tag_ids if tag_ids is None else frozenset(tag_ids),
            and_mode=self.and_mode if and_mode is None else bool(and_mode),
            gps_only=self.gps_only if gps_only is None else bool(gps_only),
            quality_ok=self.quality_ok if quality_ok is None else bool(quality_ok),
        )


def matches(
    info: PhotoInfo,
    *,
    rule_hits: Iterable[str],
    quality_hits: Iterable[str],
    tag_hits: Iterable[int],
    selection: FilterSelection,
) -> bool:
    """照片是否满足当前筛选条件。

    - GPS 是额外硬条件；
    - 规则/质量/标签合并成一个集合：``and_mode`` 为真要求全部命中，
      否则任意命中即可；
    - 没有选择任何条件时全部通过（GPS 除外）。
    """
    if selection.gps_only and not info.has_gps:
        return False

    quality_hits = set(quality_hits)
    if selection.quality_ok:
        # 重复是“关系”不是“质量缺陷”，不在这里排除；未分析/分析失败也不算正常
        technical = {flag for flag in quality_hits if flag != "duplicate"}
        if technical:
            return False

    selected: set[str | int] = set(selection.rule_ids)
    selected.update(selection.quality_ids)
    selected.update(selection.tag_ids)
    if not selected:
        return True

    hits: set[str | int] = set(rule_hits)
    hits.update(quality_hits)
    hits.update(int(tag_id) for tag_id in tag_hits)

    if selection.and_mode:
        return selected.issubset(hits)
    return bool(selected & hits)
