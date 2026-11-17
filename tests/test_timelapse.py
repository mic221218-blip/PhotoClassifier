from dataclasses import replace
from datetime import datetime, timedelta
from itertools import accumulate
from pathlib import Path

import pytest

from src.photo_info import PhotoInfo
from src.timelapse import detect_timelapse


START = datetime(2026, 11, 18, 6, 10)


def _shoot(
    count: int, *,
    interval: int = 5,
    start: datetime = START,
    folder: str = "shoot",
    camera: str = "Z6III",
    prefix: str = "DSC_",
    first_number: int = 0,
) -> list[PhotoInfo]:
    return [
        PhotoInfo(
            path=Path(folder) / f"{prefix}{first_number + index:04}.NEF",
            category="nef",
            size_bytes=1024,
            taken_at=(start + timedelta(seconds=interval * index)).isoformat(sep=" "),
            camera_make="Nikon",
            camera_model=camera,
        )
        for index in range(count)
    ]


@pytest.mark.parametrize("interval", [5, 30, 60])
def test_long_sequence_becomes_one_ordered_stack(interval):
    photos = _shoot(1000, interval=interval)

    (stack,) = detect_timelapse(reversed(photos))

    assert stack.frame_count == 1000
    assert stack.members == tuple(photos)
    assert stack.cover == photos[500]
    assert stack.cadence_seconds == interval
    assert stack.id == detect_timelapse(photos)[0].id


def test_short_burst_and_missing_exif_are_not_stacked():
    original = _shoot(100, interval=1)
    burst = original[:10]
    missing_exif = [
        replace(photo, taken_at=None, modified_at=photo.taken_at)
        for photo in original[10:]
    ]

    assert detect_timelapse(burst + missing_exif) == ()
    assert detect_timelapse(original[:50]) == ()  # 只有 49 秒


@pytest.mark.parametrize("frame_count", [11, 15])
def test_short_regular_timelapse_is_independent_of_smaller_batches(frame_count):
    sequence = _shoot(frame_count, interval=3)
    small_batch = _shoot(10, start=START + timedelta(minutes=5), first_number=frame_count)

    (stack,) = detect_timelapse(sequence + small_batch)

    assert stack.members == tuple(sequence)


def test_same_name_raw_and_jpeg_are_one_frame_and_both_kept():
    photos = _shoot(60)
    jpegs = [
        replace(raw, path=raw.path.with_suffix(".JPG"), category="jpeg", taken_at=None)
        for raw in photos
    ]

    (stack,) = detect_timelapse([*photos, *jpegs])

    assert stack.frame_count == 60
    assert len(stack.members) == 120
    assert stack.frames[0].members == (photos[0], jpegs[0])
    assert stack.cover == jpegs[30]
    assert detect_timelapse([*photos[:10], *jpegs[:10]]) == ()


def test_dropped_frame_and_small_jitter_do_not_split_the_sequence():
    photos = _shoot(100)
    for index in (3, 7, 30, 64):
        original = datetime.fromisoformat(photos[index].taken_at)
        photos[index] = replace(photos[index], taken_at=(original + timedelta(seconds=1)).isoformat(sep=" "))
    del photos[50]

    (stack,) = detect_timelapse(photos)

    assert stack.members == tuple(photos)
    assert stack.cadence_seconds == 5


@pytest.mark.parametrize(("gaps", "shutters"), [
    ([3, 2, 4] * 14 + [5, 4, 6, 5, 6, 7, 5, 6, 6, 6] + [3] * 14, [3] * 67),
    ([33] * 7 + [31, 30, 26, 27, 24, 25], [30] * 8 + [25] * 4 + [20] * 2),
])
def test_exposure_changes_do_not_drop_or_split_timelapse_frames(gaps, shutters):
    photos = [
        replace(photo, taken_at=(START + timedelta(seconds=elapsed)).isoformat(sep=" "),
                exposure_time_s=shutter, focal_length_mm=85)
        for photo, elapsed, shutter in zip(_shoot(len(shutters)), accumulate(gaps, initial=0), shutters, strict=True)
    ]
    (stack,) = detect_timelapse(photos)

    assert stack.members == tuple(photos)


@pytest.mark.parametrize(("new_interval", "transition_gap"), [(30, 30), (6, 6), (5, 45)])
def test_sustained_cadence_change_or_pause_starts_a_new_stack(new_interval, transition_gap):
    first = _shoot(50)
    restart = datetime.fromisoformat(first[-1].taken_at) + timedelta(seconds=transition_gap)
    second = _shoot(50, start=restart, interval=new_interval, first_number=50)

    stacks = detect_timelapse(first + second)

    assert [stack.cadence_seconds for stack in stacks] == [5, new_interval]
    assert [stack.members for stack in stacks] == [tuple(first), tuple(second)]


def test_folder_and_camera_separate_simultaneous_sequences():
    first = _shoot(60, folder="first")
    other_folder = _shoot(60, folder="second")
    other_camera = _shoot(60, folder="first", camera="Z9", prefix="Z9_")

    stacks = detect_timelapse(first + other_folder + other_camera)

    assert {stack.members for stack in stacks} == {tuple(first), tuple(other_folder), tuple(other_camera)}


@pytest.mark.parametrize("offset", [0, 5])
def test_ambiguous_same_model_sequences_stay_individual(offset):
    first = _shoot(60, interval=10)
    other_body = _shoot(60, interval=10, first_number=1200, start=START + timedelta(seconds=offset))

    assert detect_timelapse(first + other_body) == ()


def test_unrelated_burst_is_left_out_of_an_adjacent_sequence():
    burst = _shoot(8, interval=0)
    sequence = _shoot(100, start=START + timedelta(seconds=6), first_number=8)

    (stack,) = detect_timelapse(burst + sequence)

    assert stack.members == tuple(sequence)
