"""Copy selected originals into a folder without replacing existing files."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory


@dataclass
class ExportResult:
    copied: int = 0
    skipped: int = 0
    failures: list[tuple[Path, str]] = field(default_factory=list)


def _copy_original(source: Path, destination: Path) -> None:
    # Publish the complete copy on the same volume. Claim the final name
    # atomically, since another process may write to the folder during export.
    with TemporaryDirectory(prefix=".photoclassifier-", dir=destination) as staging:
        staged = Path(staging) / "photo"
        shutil.copyfile(source, staged)
        for number in count(1):
            name = source.name if number == 1 else f"{source.stem} ({number}){source.suffix}"
            target = destination / name
            try:
                if os.name == "nt":
                    staged.rename(target)  # Windows rename fails if the target exists.
                else:
                    os.link(staged, target)
            except FileExistsError:
                continue
            return


def export_photos(
    sources: Iterable[Path], destination: Path, *, progress: Callable[[int, str], None] | None = None,
) -> ExportResult:
    """Copy each source as-is and report failures with their original paths."""
    result = ExportResult()
    for done, source in enumerate(dict.fromkeys(sources), 1):
        try:
            if not source.is_file():
                raise FileNotFoundError("找不到源文件，或该路径不是文件")
            if source.parent.samefile(destination):
                result.skipped += 1
            else:
                _copy_original(source, destination)
                result.copied += 1
        except OSError as exc:
            result.failures.append((source, str(exc)))
        if progress is not None:
            progress(done, source.name)
    return result
