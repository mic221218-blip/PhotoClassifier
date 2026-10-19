"""PhotoClassifier V2.5：``photo_quality`` 表的读写。

只负责持久化测量值，不做判定（判定在 :mod:`src.quality`），
也不做分组（分组在 :mod:`src.duplicates`）。

与其他模块一样只读原始照片：本模块只写自己的数据库表。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from src.database import QUALITY_SCHEMA
from src.paths import db_key, resolve_key
from src.photo_info import PhotoInfo
from src.quality import ANALYZER_VERSION, QualityMeasurement


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_COLUMNS: tuple[str, ...] = (
    "path",
    "analyzer_version",
    "analyzed_at",
    "preview_source",
    "preview_width",
    "preview_height",
    "blur_score",
    "contrast",
    "anisotropy",
    "mean_lum",
    "std_lum",
    "clip_low_frac",
    "clip_high_frac",
    "dhash",
    "quick_hash",
    "content_hash",
    "file_size",
    "file_mtime",
    "error",
)

_UPSERT_SQL = f"""
INSERT INTO photo_quality ({", ".join(_COLUMNS)})
VALUES ({", ".join("?" for _ in _COLUMNS)})
ON CONFLICT(path) DO UPDATE SET
{", ".join(f"{column}=excluded.{column}" for column in _COLUMNS if column != "path")}
"""


@dataclass(frozen=True)
class QualityStats:
    """数据库里的质量数据概况（GUI 状态栏 / CLI 报告用）。"""

    total: int
    analyzed: int
    failed: int
    embedded: int
    full_image: int
    no_preview: int


class QualityStore:
    """质量测量表的读写封装。数据库路径与照片库相同。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        # 表结构唯一来源在 database.QUALITY_SCHEMA；独立使用时也在这里兜底建表
        with self._conn:
            self._conn.executescript(QUALITY_SCHEMA)

    # ---- 生命周期 -------------------------------------------------------- #

    @classmethod
    def open(cls, db_path: str | Path) -> "QualityStore":
        return cls(db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "QualityStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- 写入 ------------------------------------------------------------ #

    def _values(self, measurement: QualityMeasurement) -> tuple:
        path = resolve_key(measurement.path)
        analyzed_at = measurement.analyzed_at or _now()
        return (
            path,
            measurement.analyzer_version,
            analyzed_at,
            measurement.preview_source,
            measurement.preview_width,
            measurement.preview_height,
            measurement.blur_score,
            measurement.contrast,
            measurement.anisotropy,
            measurement.mean_lum,
            measurement.std_lum,
            measurement.clip_low_frac,
            measurement.clip_high_frac,
            measurement.dhash,
            measurement.quick_hash,
            measurement.content_hash,
            measurement.file_size,
            measurement.file_mtime,
            measurement.error,
        )

    def upsert(self, measurement: QualityMeasurement) -> None:
        with self._conn:
            self._conn.execute(_UPSERT_SQL, self._values(measurement))

    def upsert_many(self, measurements: Iterable[QualityMeasurement]) -> int:
        written = 0
        with self._conn:
            for measurement in measurements:
                self._conn.execute(_UPSERT_SQL, self._values(measurement))
                written += 1
        return written

    def set_content_hash(self, path: str | Path, content_hash: str) -> None:
        key = db_key(path)
        with self._conn:
            self._conn.execute(
                "UPDATE photo_quality SET content_hash = ? WHERE path = ?",
                (content_hash, key),
            )

    def delete(self, path: str | Path) -> None:
        key = db_key(path)
        with self._conn:
            self._conn.execute("DELETE FROM photo_quality WHERE path = ?", (key,))

    def prune(self) -> int:
        """清理照片库里已不存在的质量记录（不碰任何文件）。"""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM photo_quality WHERE path NOT IN (SELECT path FROM photos)"
            )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    # ---- 读取 ------------------------------------------------------------ #

    def get(self, path: str | Path) -> QualityMeasurement | None:
        key = db_key(path)
        row = self._conn.execute(
            "SELECT * FROM photo_quality WHERE path = ?", (key,)
        ).fetchone()
        return _row_to_measurement(row) if row is not None else None

    def iter_measurements(self) -> Iterator[QualityMeasurement]:
        cursor = self._conn.execute("SELECT * FROM photo_quality ORDER BY path")
        for row in cursor:
            yield _row_to_measurement(row)

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM photo_quality").fetchone()
        return int(row["n"])

    def stats(self) -> QualityStats:
        total = self.count()
        analyzed = int(
            self._conn.execute(
                "SELECT COUNT(*) AS n FROM photo_quality WHERE error IS NULL AND blur_score IS NOT NULL"
            ).fetchone()["n"]
        )
        sources = {
            row["preview_source"]: int(row["n"])
            for row in self._conn.execute(
                "SELECT preview_source, COUNT(*) AS n FROM photo_quality GROUP BY preview_source"
            )
        }
        return QualityStats(
            total=total,
            analyzed=analyzed,
            failed=total - analyzed,
            embedded=sources.get("embedded_jpeg", 0),
            full_image=sources.get("full_image", 0),
            no_preview=sources.get("none", 0),
        )

    def pending(self, infos: Iterable[PhotoInfo], *, force: bool = False) -> list[PhotoInfo]:
        """挑出还需要分析的照片：没有记录、版本变了、或文件变了。"""
        rows = {
            row["path"]: (row["analyzer_version"], row["file_size"], row["file_mtime"])
            for row in self._conn.execute(
                "SELECT path, analyzer_version, file_size, file_mtime FROM photo_quality"
            )
        }
        result: list[PhotoInfo] = []
        for info in infos:
            if force:
                result.append(info)
                continue
            key = resolve_key(info.path)
            row = rows.get(key)
            if row is None:
                result.append(info)
            elif row[0] != ANALYZER_VERSION:
                result.append(info)
            elif row[1] != info.size_bytes or row[2] != info.modified_at:
                result.append(info)
        return result

    def exact_duplicate_candidates(self) -> list[QualityMeasurement]:
        """需要整文件哈希的候选：同尺寸 + quick_hash 相同，且还没算 content_hash。"""
        rows = self._conn.execute(
            """
            SELECT q.* FROM photo_quality q
            JOIN (
                SELECT file_size, quick_hash FROM photo_quality
                WHERE quick_hash IS NOT NULL
                GROUP BY file_size, quick_hash
                HAVING COUNT(*) > 1
            ) d ON q.file_size = d.file_size AND q.quick_hash = d.quick_hash
            WHERE q.content_hash IS NULL
            ORDER BY q.path
            """
        ).fetchall()
        return [_row_to_measurement(row) for row in rows]


def _row_to_measurement(row: sqlite3.Row) -> QualityMeasurement:
    return QualityMeasurement(
        path=row["path"],
        analyzer_version=int(row["analyzer_version"]),
        analyzed_at=row["analyzed_at"],
        preview_source=row["preview_source"],
        preview_width=row["preview_width"],
        preview_height=row["preview_height"],
        blur_score=row["blur_score"],
        contrast=row["contrast"],
        anisotropy=row["anisotropy"],
        mean_lum=row["mean_lum"],
        std_lum=row["std_lum"],
        clip_low_frac=row["clip_low_frac"],
        clip_high_frac=row["clip_high_frac"],
        dhash=row["dhash"],
        quick_hash=row["quick_hash"],
        content_hash=row["content_hash"],
        file_size=row["file_size"],
        file_mtime=row["file_mtime"],
        error=row["error"],
    )
