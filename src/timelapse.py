"""从照片库的 EXIF 拍摄时间识别适合在浏览视图折叠的延时序列。"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import groupby, pairwise
from pathlib import Path
from statistics import median

from src.photo_info import EXIF_ERROR, PhotoInfo
from src.scanner import CATEGORY_JPEG, CATEGORY_NEF

_MIN_FRAMES = 11  # 10 帧及以下始终逐张显示。
_MIN_SPAN_SECONDS = 30
_FAST_MIN_SPAN_SECONDS = 60
_CADENCE_SAMPLE = 8
_MIN_GAPS_BETWEEN_DROPS = 5
_FRAME_NAME = re.compile(r"(.*?)([0-9]{1,12})$")

Camera = tuple[str, str]


@dataclass(frozen=True)
class TimelapseFrame:
    taken_at: datetime
    members: tuple[PhotoInfo, ...]


@dataclass(frozen=True)
class TimelapseStack:
    id: str
    frames: tuple[TimelapseFrame, ...]
    cover: PhotoInfo
    cadence_seconds: float

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def members(self) -> tuple[PhotoInfo, ...]:
        return tuple(photo for frame in self.frames for photo in frame.members)

    def only_raw(self) -> TimelapseStack | None:
        """保留原序列的标识和节奏，用可见的 RAW 帧生成叠卡。"""
        frames = []
        for frame in self.frames:
            members = tuple(photo for photo in frame.members if photo.category == CATEGORY_NEF)
            if members:
                frames.append(TimelapseFrame(frame.taken_at, members))
        if not frames:
            return None
        cover = frames[len(frames) // 2].members[0]
        return replace(self, frames=tuple(frames), cover=cover)


@dataclass(frozen=True)
class _Capture:
    photo: PhotoInfo
    taken_at: datetime | None
    camera: Camera


def detect_timelapse(photos: Iterable[PhotoInfo]) -> tuple[TimelapseStack, ...]:
    """按目录和相机时间线检测稳定的长序列，只读取照片库记录。"""
    by_name: dict[Path, list[_Capture]] = defaultdict(list)
    for photo in photos:
        if photo.category not in (CATEGORY_NEF, CATEGORY_JPEG):
            continue
        camera = (
            (photo.camera_make or "").strip().casefold(),
            (photo.camera_model or "").strip().casefold(),
        )
        by_name[photo.path.with_suffix("")].append(_Capture(photo, _capture_time(photo), camera))

    timelines: dict[tuple[Path, Camera], list[TimelapseFrame]] = defaultdict(list)
    for name, captures in by_name.items():
        for camera, frame in _frames_for_name(captures):
            timelines[name.parent, camera].append(frame)

    stacks = [stack for timeline in timelines.values() for stack in _detect_timeline(timeline)]
    return tuple(sorted(stacks, key=lambda stack: (stack.frames[0].taken_at, stack.id)))


def _capture_time(photo: PhotoInfo) -> datetime | None:
    if not photo.taken_at or len(photo.taken_at) != 19:
        return None
    try:
        taken_at = datetime.fromisoformat(photo.taken_at)
    except ValueError:
        return None
    return taken_at if taken_at.tzinfo is None and taken_at.microsecond == 0 else None


def _frames_for_name(captures: list[_Capture]) -> Iterator[tuple[Camera, TimelapseFrame]]:
    if len(captures) == 2:
        raw, jpeg = sorted(captures, key=lambda item: item.photo.category != CATEGORY_NEF)
        if raw.photo.category == CATEGORY_NEF and jpeg.photo.category == CATEGORY_JPEG:
            matching_camera = all(not a or not b or a == b for a, b in zip(raw.camera, jpeg.camera))
            matching_time = not raw.taken_at or not jpeg.taken_at or raw.taken_at == jpeg.taken_at
            taken_at = raw.taken_at or jpeg.taken_at
            if taken_at and matching_camera and matching_time:
                make = raw.camera[0] or jpeg.camera[0]
                model = raw.camera[1] or jpeg.camera[1]
                yield (make, model), TimelapseFrame(taken_at, (raw.photo, jpeg.photo))
                return

    for capture in captures:
        if capture.taken_at is not None:
            yield capture.camera, TimelapseFrame(capture.taken_at, (capture.photo,))


def _detect_timeline(frames: list[TimelapseFrame]) -> Iterator[TimelapseStack]:
    frames.sort(key=lambda frame: frame.taken_at)
    clear_frames: list[TimelapseFrame] = []
    for _, same_time in groupby(frames, key=lambda frame: frame.taken_at):
        simultaneous = tuple(same_time)
        if len(simultaneous) == 1:
            clear_frames.append(simultaneous[0])
        else:
            # 相机相同且记录到同一秒的多个独立帧，可能来自交错相机或高速连拍。
            yield from _regular_runs(clear_frames)
            clear_frames = []
    yield from _regular_runs(clear_frames)


def _regular_runs(frames: list[TimelapseFrame]) -> Iterator[TimelapseStack]:
    if len(frames) < _MIN_FRAMES:
        return
    gaps = [(right.taken_at - left.taken_at).total_seconds() for left, right in pairwise(frames)]
    start = 0
    while start + _MIN_FRAMES <= len(frames):
        sample = gaps[start : start + _CADENCE_SAMPLE]
        cadence = float(median(sample))
        steady_gaps = sum(_matches_gap(gap, cadence) for gap in sample)
        if not _matches_gap(sample[0], cadence) or steady_gaps < _CADENCE_SAMPLE - 1:
            start += 1
            continue

        reference_exposure = median(
            frame.members[0].exposure_time_s or 0
            for frame in frames[start : start + _CADENCE_SAMPLE]
        )
        end = start
        last_drop = start - _MIN_GAPS_BETWEEN_DROPS
        while end < len(gaps):
            gap = gaps[end]
            steady = _matches_gap(gap, cadence) and not _starts_new_cadence(gaps, end, cadence)
            if not steady and not _matches_exposure_gap(frames[end], frames[end + 1], gap, cadence, reference_exposure):
                if not _isolated_drop(gaps, end, cadence, last_drop):
                    break
                last_drop = end
            end += 1

        span = (frames[end].taken_at - frames[start].taken_at).total_seconds()
        min_span = _FAST_MIN_SPAN_SECONDS if cadence < 2 else _MIN_SPAN_SECONDS
        if end - start + 1 < _MIN_FRAMES or span < min_span:
            start += 1
            continue

        run = tuple(frames[start : end + 1])
        if not _has_conflicting_numbers(run):
            middle = run[len(run) // 2]
            cover = next(
                (
                    photo for photo in middle.members
                    if photo.category == CATEGORY_JPEG and photo.exif_status != EXIF_ERROR
                ),
                middle.members[0],
            )
            yield TimelapseStack(f"timelapse:{run[0].members[0].path}", run, cover, cadence)
        start = end + 1


def _matches_gap(gap: float, cadence: float) -> bool:
    rounding_tolerance = 0.75 if cadence < 3 else 1.0
    return abs(gap - cadence) <= max(rounding_tolerance, cadence * 0.10)


def _matches_exposure_gap(
    left: TimelapseFrame, right: TimelapseFrame, gap: float, cadence: float, reference_exposure: float,
) -> bool:
    previous, following = left.members[0], right.members[0]
    if not previous.exposure_time_s or not following.exposure_time_s:
        return False
    if previous.focal_length_mm != following.focal_length_mm:
        return False

    shortest, longest = sorted((previous.exposure_time_s, following.exposure_time_s))
    # Exposure near the interval can delay the next capture. During an exposure
    # ramp, the gap can also track the changed shutter time plus the same wait.
    delayed = longest >= max(1, cadence - 0.5) and cadence < gap <= cadence + min(longest, cadence) + 1
    wait = cadence - reference_exposure
    ramped = (
        4 <= reference_exposure <= cadence + 1
        and shortest >= 1
        and max(abs(shortest - reference_exposure), abs(longest - reference_exposure)) >= 1
        and shortest + wait - 2 <= gap <= longest + wait + 2
    )
    if not (delayed or ramped):
        return False

    before = _FRAME_NAME.fullmatch(previous.path.stem.casefold())
    after = _FRAME_NAME.fullmatch(following.path.stem.casefold())
    return bool(before and after and before[1] == after[1] and int(after[2]) == int(before[2]) + 1)


def _starts_new_cadence(gaps: list[float], position: int, cadence: float) -> bool:
    difference = gaps[position] - cadence
    if abs(difference) < 1.0:
        return False
    sample = gaps[position : position + _CADENCE_SAMPLE]
    if len(sample) < _CADENCE_SAMPLE:
        return False
    direction = 1 if difference > 0 else -1
    return all((gap - cadence) * direction >= 1.0 for gap in sample)


def _isolated_drop(gaps: list[float], position: int, cadence: float, last_drop: int) -> bool:
    return (
        position - last_drop >= _MIN_GAPS_BETWEEN_DROPS
        and position + 2 < len(gaps)
        and _matches_gap(gaps[position], cadence * 2)
        and _matches_gap(gaps[position + 1], cadence)
        and _matches_gap(gaps[position + 2], cadence)
    )


def _has_conflicting_numbers(run: tuple[TimelapseFrame, ...]) -> bool:
    previous: tuple[str, int] | None = None
    transitions = 0
    conflicts = 0
    for frame in run:
        match = _FRAME_NAME.fullmatch(frame.members[0].path.stem.casefold())
        if not match:
            previous = None
            continue
        prefix, number = match[1], int(match[2])
        if previous is not None:
            transitions += 1
            if previous[0] != prefix or previous[1] >= number:
                conflicts += 1
        previous = prefix, number
    # 多次编号倒退或前缀交错，比一次文件名计数器回绕更像混合了多个拍摄流。
    return conflicts >= 3 and conflicts * 5 >= transitions
