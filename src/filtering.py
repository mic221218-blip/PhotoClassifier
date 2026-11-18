"""PhotoClassifier V3：统一筛选判定。

文件类型、规则（EXIF）、质量标记、用户标签、GPS 都进入同一个 :class:`FilterSelection`，
GUI 与 CLI 都只调用 :func:`matches`，不再各自实现一份 AND/OR。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.photo_info import PhotoInfo
from src.scanner import CATEGORY_NEF


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
    ai_person_only: bool = False
    ai_sky_only: bool = False
    raw_only: bool = False
    ai_ids: frozenset[str] = frozenset()

    def to_settings(self) -> dict:
        return {
            "rule_ids": sorted(self.rule_ids),
            "quality_ids": sorted(self.quality_ids),
            "tag_ids": sorted(self.tag_ids),
            "ai_ids": sorted(self.selected_ai),
            "and_mode": self.and_mode,
            "gps_only": self.gps_only,
            "quality_ok": self.quality_ok,
            "raw_only": self.raw_only,
        }

    @classmethod
    def from_settings(cls, values: dict) -> "FilterSelection":
        def ids(name, kind):
            items = values.get(name)
            return frozenset(item for item in items if type(item) is kind) if isinstance(items, list) else frozenset()

        return cls(
            rule_ids=ids("rule_ids", str), quality_ids=ids("quality_ids", str),
            tag_ids=ids("tag_ids", int), ai_ids=ids("ai_ids", str),
            and_mode=values.get("and_mode") is not False,
            gps_only=values.get("gps_only") is True,
            quality_ok=values.get("quality_ok") is True,
            raw_only=values.get("raw_only") is True,
        )

    @property
    def selected_ai(self) -> frozenset[str]:
        ids = self.ai_ids
        if self.ai_person_only:
            ids = ids.union(("person",))
        if self.ai_sky_only:
            ids = ids.union(("sky",))
        return ids

    @property
    def selected_count(self) -> int:
        return (
            len(self.rule_ids)
            + len(self.quality_ids)
            + len(self.tag_ids)
            + int(self.quality_ok)
            + len(self.selected_ai)
            + int(self.raw_only)
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
        ai_person_only: bool | None = None,
        ai_sky_only: bool | None = None,
        raw_only: bool | None = None,
        ai_ids: Iterable[str] | None = None,
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
            ai_person_only=self.ai_person_only if ai_person_only is None else bool(ai_person_only),
            ai_sky_only=self.ai_sky_only if ai_sky_only is None else bool(ai_sky_only),
            raw_only=self.raw_only if raw_only is None else bool(raw_only),
            ai_ids=self.ai_ids if ai_ids is None else frozenset(ai_ids),
        )


def matches(
    info: PhotoInfo,
    *,
    rule_hits: Iterable[str],
    quality_hits: Iterable[str],
    tag_hits: Iterable[int],
    selection: FilterSelection,
    ai_person: bool = False,
    ai_sky: bool = False,
    ai_hits: Iterable[str] = (),
) -> bool:
    """照片是否满足当前筛选条件。

    - RAW 和 GPS 是额外硬条件；
    - 规则/质量/标签合并成一个集合：``and_mode`` 为真要求全部命中，
      否则任意命中即可；
    - 没有选择集合条件时，只检查额外条件。
    """
    if selection.raw_only and info.category != CATEGORY_NEF:
        return False
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
    selected.update(f"__ai_{key}__" for key in selection.selected_ai)
    if not selected:
        return True

    hits: set[str | int] = set(rule_hits)
    hits.update(quality_hits)
    hits.update(int(tag_id) for tag_id in tag_hits)
    hits.update(f"__ai_{key}__" for key in ai_hits)
    if ai_person:
        hits.add("__ai_person__")
    if ai_sky:
        hits.add("__ai_sky__")

    if selection.and_mode:
        return selected.issubset(hits)
    return bool(selected & hits)
