"""Copy originals into an export folder without replacing existing files."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from itertools import count
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Literal


@dataclass(frozen=True)
class ExportFile:
    source: Path
    name: str


ExportGroup = tuple[ExportFile, ...]


@dataclass(frozen=True)
class ExportOutcome:
    file: ExportFile
    status: Literal["copied", "skipped", "failed"]
    reason: str = ""


@dataclass
class ExportResult:
    outcomes: list[ExportOutcome] = field(default_factory=list)
    cancelled: bool = False


def valid_export_name(name: str) -> bool:
    """Validate a single Windows filename component, including folder names."""
    return bool(
        name and name[-1] not in " ." and not PureWindowsPath(name).is_reserved()
        and all(ord(char) >= 32 and char not in '<>:"/\\|?*' for char in name)
    )


def group_export_sources(sources: Iterable[Path]) -> tuple[tuple[Path, ...], ...]:
    """Pair same-folder, same-name NEF and JPEG files within the given scope."""
    ordered = dict.fromkeys(sources)
    candidates: dict[tuple[Path, str], list[Path]] = {}
    for source in ordered:
        if source.suffix.lower() in (".nef", ".jpg", ".jpeg"):
            candidates.setdefault((source.parent, source.stem.casefold()), []).append(source)

    partners = {}
    for members in candidates.values():
        if len(members) == 2 and sum(path.suffix.lower() == ".nef" for path in members) == 1:
            first, second = members
            partners[first] = second

    paired_later = set(partners.values())
    return tuple((source, partners[source]) if source in partners else (source,)
                 for source in ordered if source not in paired_later)


def name_export_groups(groups: Iterable[tuple[Path, ...]], prefix: str = "") -> Iterator[ExportGroup]:
    for sequence, group in enumerate(groups, 1):
        stem = f"{prefix}_{sequence:05d}"
        yield tuple(ExportFile(source, f"{stem}{source.suffix}" if prefix else source.name)
                    for source in group)


def _choose_names(
    group: ExportGroup, destination: Path, reserved: set[Path], *, skip_existing: bool,
) -> ExportGroup | None:
    # Allocate a name for the whole pair so a collision on one extension does
    # not separate a JPEG from its RAW file.
    for number in count(1):
        if number == 1:
            named = group
        else:
            named = tuple(replace(file, name=f"{Path(file.name).stem} ({number}){Path(file.name).suffix}")
                          for file in group)
        targets = {destination / file.name for file in named}
        if not targets.intersection(reserved) and not any(os.path.lexists(path) for path in targets):
            reserved.update(targets)
            return named
        if skip_existing:
            return None


def _copy_original(source: Path, target: Path) -> None:
    # Publish only complete files on the destination volume. If another process
    # claims the planned name meanwhile, keep its file and report the conflict.
    with TemporaryDirectory(prefix=".photoclassifier-", dir=target.parent) as staging:
        staged = Path(staging) / "photo"
        shutil.copyfile(source, staged)
        if os.name == "nt":
            staged.rename(target)  # Windows rename fails if the target exists.
        else:
            os.link(staged, target)


def _export_group(
    group: ExportGroup, destination: Path, reserved: set[Path], *, skip_existing: bool, keep_names: bool,
) -> Iterator[ExportOutcome]:
    try:
        same_folder = group[0].source.parent.samefile(destination)
    except FileNotFoundError:
        same_folder = False  # The source folder may need to be reconnected before retrying.
    except OSError as exc:
        yield from (ExportOutcome(file, "failed", str(exc)) for file in group)
        return

    if not same_folder and not keep_names:
        named = _choose_names(group, destination, reserved, skip_existing=skip_existing)
        if named is None:
            yield from (ExportOutcome(file, "skipped") for file in group)
            return
        group = named

    for file in group:
        try:
            if not file.source.is_file():
                raise FileNotFoundError("找不到源文件，或该路径不是文件")
            if same_folder:
                yield ExportOutcome(file, "skipped")
            else:
                _copy_original(file.source, destination / file.name)
                yield ExportOutcome(file, "copied")
        except OSError as exc:
            yield ExportOutcome(file, "failed", str(exc))


def export_photos(
    groups: tuple[ExportGroup, ...], destination: Path, *,
    create_subfolder: bool = False, skip_existing: bool = False, keep_names: bool = False,
    progress: Callable[[int, str], None] | None = None, cancelled: Callable[[], bool] = lambda: False,
) -> ExportResult:
    """Process one photo or pair at a time; retries can retain their assigned names."""
    result = ExportResult()
    if cancelled():
        result.cancelled = True
        return result
    try:
        if create_subfolder:
            destination.mkdir(exist_ok=True)
        if not destination.is_dir():
            raise NotADirectoryError(f"导出文件夹不可用：{destination}")
    except OSError as exc:
        result.outcomes = [ExportOutcome(file, "failed", str(exc)) for group in groups for file in group]
        return result

    reserved: set[Path] = set()
    for group in groups:
        if cancelled():
            result.cancelled = True
            break
        for outcome in _export_group(group, destination, reserved, skip_existing=skip_existing, keep_names=keep_names):
            result.outcomes.append(outcome)
            if progress is not None:
                progress(len(result.outcomes), outcome.file.source.name)
    return result
