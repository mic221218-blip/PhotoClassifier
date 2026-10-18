"""PhotoClassifier V2.5：重复照片分组（纯函数，不读文件）。

两类重复：

- **精确重复**：``content_hash`` 相同（整文件 SHA-256），通常是拷贝/备份；
- **近似重复**：dHash 汉明距离 ≤ 阈值，且拍摄时间相差 ≤ 时间窗，
  通常是同一瞬间的连拍、包围曝光。

两个重要的现实约束（用真实延时摄影素材校准过）：

1. **同名 RAW+JPEG 不算重复**：``DSC_0001.NEF`` 与 ``DSC_0001.JPG``
   是同一张照片的两种格式，dHash 完全相同，但它们是成对保存的正常工作流；
2. **延时/连拍序列不算重复**：连续几百帧延时素材彼此都很像，
   逐对比较会连成一大团。因此近似组超过
   :attr:`QualityConfig.sequence_min_size` 张时归为"序列"，
   只报告、不标记为重复。

本模块只接受 :class:`DuplicateCandidate`（来自测量表 + photos 表），
不做 I/O，结果只用于展示与筛选。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from src.quality import QualityConfig, QualityMeasurement, hamming_distance


@dataclass(frozen=True)
class DuplicateCandidate:
    """参与重复判定的一张照片（测量值 + 必要的时间信息）。"""

    path: str
    size_bytes: int
    taken_at: str | None = None
    modified_at: str | None = None
    dhash: str | None = None
    quick_hash: str | None = None
    content_hash: str | None = None
    blur_score: float | None = None
    pixel_count: int | None = None

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def stem(self) -> str:
        return Path(self.path).stem.lower()

    @property
    def suffix(self) -> str:
        return Path(self.path).suffix.lower()

    @property
    def timestamp(self) -> float | None:
        """拍摄时间（缺失时退回文件修改时间），转成 epoch 秒。"""
        for text in (self.taken_at, self.modified_at):
            if not text:
                continue
            try:
                return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                continue
        return None

    @property
    def sharpness(self) -> float:
        return self.blur_score if self.blur_score is not None else -1.0


@dataclass(frozen=True)
class DuplicateGroup:
    """一组重复照片，``members[0]`` 是建议保留的那张（只是排序建议）。"""

    group_id: int
    kind: str  # "exact" | "near"
    members: tuple[DuplicateCandidate, ...]
    distance: int | None = None
    span_seconds: float | None = None

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def best(self) -> DuplicateCandidate:
        return self.members[0]

    @property
    def kind_label(self) -> str:
        return {"exact": "精确重复", "near": "近似重复", "sequence": "延时/连拍序列"}.get(
            self.kind, self.kind
        )


@dataclass(frozen=True)
class DuplicateInfo:
    """单张照片在重复分组里的位置（GUI 筛选 / 详情用）。"""

    group_id: int
    kind: str
    size: int
    is_best: bool
    distance: int | None
    best_name: str


@dataclass(frozen=True)
class DuplicateResult:
    """一次重复检测的完整结果。"""

    groups: tuple[DuplicateGroup, ...] = ()
    sequences: tuple[DuplicateGroup, ...] = ()
    by_path: Mapping[str, DuplicateInfo] = field(default_factory=dict)

    def info_for(self, path: str | Path) -> DuplicateInfo | None:
        return self.by_path.get(str(Path(path).resolve()))

    def flag_reason(self, path: str | Path) -> str | None:
        info = self.info_for(path)
        if info is None:
            return None
        if info.kind == "exact":
            return f"与 {info.best_name} 完全相同（共 {info.size} 张）"
        detail = f"dHash 距离 {info.distance}" if info.distance is not None else "dHash 相近"
        return f"与 {info.best_name} 近似（{detail}，共 {info.size} 张）"

    @property
    def flagged_count(self) -> int:
        return len(self.by_path)


class DuplicateFinder:
    """按配置把候选照片分成精确重复 / 近似重复 / 序列。"""

    def __init__(self, config: QualityConfig | None = None):
        self.config = config or QualityConfig()

    def find(self, candidates: Iterable[DuplicateCandidate]) -> DuplicateResult:
        items = list(candidates)
        sequences: list[DuplicateGroup] = []

        # 1) 先按拍摄节奏识别"延时/连拍序列"：这些帧互不算重复
        sequence_paths: set[str] = set()
        sequence_dirs: set[str] = set()
        for members in self._sequence_runs(items):
            sequence_paths.update(member.path for member in members)
            sequence_dirs.update(str(Path(member.path).parent) for member in members)
            times = [member.timestamp for member in members if member.timestamp is not None]
            sequences.append(
                DuplicateGroup(
                    group_id=len(sequences) + 1,
                    kind="sequence",
                    members=tuple(members),
                    span_seconds=(max(times) - min(times)) if times else None,
                )
            )
        remaining = [item for item in items if item.path not in sequence_paths]

        raw: list[tuple[str, tuple[DuplicateCandidate, ...], int | None, float | None]] = []
        for members, distance, span in self._exact_groups(items):
            raw.append(("exact", members, distance, span))
        for members, distance, span in self._near_groups(remaining):
            raw.append(("near", members, distance, span))

        duplicates: list[DuplicateGroup] = []
        for kind, members, distance, span in raw:
            group = DuplicateGroup(
                group_id=0,
                kind=kind,
                members=members,
                distance=distance,
                span_seconds=span,
            )
            if kind == "near" and group.size >= self.config.sequence_min_size:
                # 剩下的短时间窗里仍然超大的一团：按序列处理（不误标成重复）
                sequences.append(
                    DuplicateGroup(
                        group_id=len(sequences) + 1,
                        kind="sequence",
                        members=members,
                        distance=distance,
                        span_seconds=span,
                    )
                )
            elif kind == "near" and self._in_sequence_folder(members, sequence_dirs):
                # 延时摄影文件夹里的"相似但不是同一张"属于正常拍摄节奏
                sequences.append(
                    DuplicateGroup(
                        group_id=len(sequences) + 1,
                        kind="sequence",
                        members=members,
                        distance=distance,
                        span_seconds=span,
                    )
                )
            else:
                duplicates.append(
                    DuplicateGroup(
                        group_id=len(duplicates) + 1,
                        kind=kind,
                        members=members,
                        distance=distance,
                        span_seconds=span,
                    )
                )

        by_path: dict[str, DuplicateInfo] = {}
        for group in duplicates:
            for index, member in enumerate(group.members):
                by_path[str(Path(member.path).resolve())] = DuplicateInfo(
                    group_id=group.group_id,
                    kind=group.kind,
                    size=group.size,
                    is_best=index == 0,
                    distance=group.distance,
                    best_name=group.best.name,
                )

        return DuplicateResult(
            groups=tuple(duplicates),
            sequences=tuple(sequences),
            by_path=by_path,
        )

    # ---- 序列识别 -------------------------------------------------------- #

    def _sequence_runs(self, items: list[DuplicateCandidate]) -> list[list[DuplicateCandidate]]:
        """按拍摄节奏找出延时/连拍序列（只依赖时间，不需要像素）。

        - 相邻拍摄时间差 ≤ ``sequence_gap_s`` 的连续照片归为一组；
        - 组内不同拍摄时刻（同一时刻的 RAW+JPEG 只算一帧）≥
          ``sequence_min_frames``，且时间跨度 ≥ ``sequence_min_span_s``
          才判定为序列 —— 这样 5 张连拍不会被误当延时。
        """
        cfg = self.config
        by_time: dict[float, list[DuplicateCandidate]] = {}
        for item in items:
            timestamp = item.timestamp
            if timestamp is None:
                continue
            by_time.setdefault(timestamp, []).append(item)
        times = sorted(by_time)
        if not times:
            return []

        runs: list[list[float]] = []
        current: list[float] = []
        for timestamp in times:
            if current and timestamp - current[-1] > cfg.sequence_gap_s:
                runs.append(current)
                current = []
            current.append(timestamp)
        if current:
            runs.append(current)

        groups: list[list[DuplicateCandidate]] = []
        for run in runs:
            span = run[-1] - run[0]
            if len(run) < cfg.sequence_min_frames or span < cfg.sequence_min_span_s:
                continue
            members = [item for timestamp in run for item in by_time[timestamp]]
            members.sort(key=lambda item: item.timestamp or 0.0)
            groups.append(members)
        return groups

    # ---- 精确重复 -------------------------------------------------------- #

    def _exact_groups(self, items: list[DuplicateCandidate]):
        buckets: dict[tuple[int, str], list[DuplicateCandidate]] = {}
        for item in items:
            if not item.content_hash:
                continue
            buckets.setdefault((item.size_bytes, item.content_hash), []).append(item)
        groups = []
        for members in buckets.values():
            if len(members) > 1:
                groups.append((self._sorted_members(members), None, None))
        return groups

    # ---- 近似重复 -------------------------------------------------------- #

    def _near_groups(self, items: list[DuplicateCandidate]):
        timed = [item for item in items if item.dhash and item.timestamp is not None]
        timed.sort(key=lambda item: item.timestamp)
        parent = list(range(len(timed)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        window = self.config.dupe_time_window_s
        distance_max = self.config.dhash_distance_max
        for i, left in enumerate(timed):
            left_time = left.timestamp
            assert left_time is not None
            for j in range(i + 1, len(timed)):
                right = timed[j]
                right_time = right.timestamp
                assert right_time is not None
                if right_time - left_time > window:
                    break
                if self._is_sibling_format(left, right):
                    continue
                if hamming_distance(left.dhash, right.dhash) <= distance_max:
                    union(i, j)

        components: dict[int, list[int]] = {}
        for index in range(len(timed)):
            components.setdefault(find(index), []).append(index)

        groups = []
        for indexes in components.values():
            if len(indexes) < 2:
                continue
            members = [timed[index] for index in indexes]
            distance = self._max_distance(members)
            times = [member.timestamp for member in members if member.timestamp is not None]
            span = (max(times) - min(times)) if times else None
            groups.append((self._sorted_members(members), distance, span))
        return groups

    @staticmethod
    def _is_sibling_format(left: DuplicateCandidate, right: DuplicateCandidate) -> bool:
        """同名不同后缀（如 DSC_0001.NEF / DSC_0001.JPG）不是重复。"""
        return left.stem == right.stem and left.suffix != right.suffix

    def _in_sequence_folder(
        self, members: tuple[DuplicateCandidate, ...], sequence_dirs: set[str]
    ) -> bool:
        """整组都来自同一个"含延时序列"的文件夹。"""
        if not self.config.sequence_folder_mode or not sequence_dirs:
            return False
        parents = {str(Path(member.path).parent) for member in members}
        return len(parents) == 1 and parents.pop() in sequence_dirs

    @staticmethod
    def _max_distance(members: list[DuplicateCandidate]) -> int | None:
        """组内两两 dHash 距离的最大值（比"参照一张"更保守、更可解释）。"""
        hashes = [member.dhash for member in members if member.dhash]
        if len(hashes) < 2:
            return None
        maximum = 0
        for i in range(len(hashes)):
            for j in range(i + 1, len(hashes)):
                maximum = max(maximum, hamming_distance(hashes[i], hashes[j]))
        return maximum

    @staticmethod
    def _sorted_members(members: list[DuplicateCandidate]) -> tuple[DuplicateCandidate, ...]:
        ordered = sorted(
            members,
            key=lambda item: (
                -item.sharpness,
                -(item.pixel_count or 0),
                -item.size_bytes,
                item.taken_at or "",
                item.path,
            ),
        )
        return tuple(ordered)


def candidates_from(
    measurements: Iterable[QualityMeasurement], infos: Mapping[str, object]
) -> list[DuplicateCandidate]:
    """把测量值和照片信息合并成重复检测候选。

    ``infos`` 是 ``{绝对路径: PhotoInfo}``；缺少照片信息时，
    没有拍摄时间，只参与精确重复。
    """
    result: list[DuplicateCandidate] = []
    for measurement in measurements:
        key = str(Path(measurement.path).resolve())
        info = infos.get(key)
        pixel_count = None
        taken_at = None
        modified_at = measurement.file_mtime
        size_bytes = measurement.file_size or 0
        if info is not None:
            width = getattr(info, "width_px", None)
            height = getattr(info, "height_px", None)
            if width and height:
                pixel_count = int(width) * int(height)
            taken_at = getattr(info, "taken_at", None)
            modified_at = getattr(info, "modified_at", None) or modified_at
            size_bytes = getattr(info, "size_bytes", size_bytes)
        result.append(
            DuplicateCandidate(
                path=key,
                size_bytes=int(size_bytes or 0),
                taken_at=taken_at,
                modified_at=modified_at,
                dhash=measurement.dhash,
                quick_hash=measurement.quick_hash,
                content_hash=measurement.content_hash,
                blur_score=measurement.blur_score,
                pixel_count=pixel_count,
            )
        )
    return result
