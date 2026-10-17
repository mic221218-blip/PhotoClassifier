"""PhotoClassifier V1.5：SQLite 照片信息数据库。

对应《项目总体规划》V1.5 的目标——"建立照片信息数据库"。

设计要点：

- 使用 Python 标准库 ``sqlite3``，不引入额外依赖；
- 以"文件的绝对路径"作为唯一键（一张照片在数据库中只有一条记录，
  重复扫描时更新而不是重复插入）；
- 数据库只记录信息，永远不会改动原始照片文件；
- 所有写操作集中在 :meth:`PhotoDatabase.upsert_photo` /
  :meth:`PhotoDatabase.upsert_many`，读取操作全部只读。

数据库文件本身是一个普通文件，存放在哪里由调用方决定（CLI 默认放在
``data/photos.db``，该目录已在 .gitignore 中，不会上传到 GitHub）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from src.photo_info import EXIF_STATUSES, PhotoInfo


#: photos 表的全部业务列（不含 path 主键），顺序与 _row_to_photo_info 一致。
_PHOTO_COLUMNS: tuple[str, ...] = (
    "path",
    "file_name",
    "category",
    "size_bytes",
    "modified_at",
    "exif_status",
    "exif_error",
    "taken_at",
    "camera_make",
    "camera_model",
    "lens_model",
    "focal_length_mm",
    "f_number",
    "exposure_time_s",
    "iso",
    "exposure_bias_ev",
    "gps_latitude",
    "gps_longitude",
    "width_px",
    "height_px",
    "first_seen_at",
    "last_scanned_at",
)

#: count_by 允许使用的分组列（防止拼接 SQL 时注入任意列名）。
_GROUPABLE_COLUMNS: dict[str, str] = {
    "category": "category",
    "camera": "camera_make || ' ' || camera_model",
    "camera_make": "camera_make",
    "camera_model": "camera_model",
    "lens_model": "lens_model",
    "exif_status": "exif_status",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    path              TEXT PRIMARY KEY,
    file_name         TEXT NOT NULL,
    category          TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL,
    modified_at       TEXT,
    exif_status       TEXT NOT NULL,
    exif_error        TEXT,
    taken_at          TEXT,
    camera_make       TEXT,
    camera_model      TEXT,
    lens_model        TEXT,
    focal_length_mm   REAL,
    f_number          REAL,
    exposure_time_s   REAL,
    iso               INTEGER,
    exposure_bias_ev  REAL,
    gps_latitude      REAL,
    gps_longitude     REAL,
    width_px          INTEGER,
    height_px         INTEGER,
    first_seen_at     TEXT NOT NULL,
    last_scanned_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_photos_category ON photos (category);
CREATE INDEX IF NOT EXISTS idx_photos_taken_at ON photos (taken_at);
CREATE INDEX IF NOT EXISTS idx_photos_file_name ON photos (file_name);

CREATE TABLE IF NOT EXISTS folders (
    path            TEXT PRIMARY KEY,
    added_at        TEXT NOT NULL,
    last_scanned_at TEXT
);
"""

#: 按 path 去重的写入语句（列清单固定，编译一次即可）。
_UPSERT_SQL = """
INSERT INTO photos ({cols}) VALUES ({marks})
ON CONFLICT(path) DO UPDATE SET
    file_name=excluded.file_name,
    category=excluded.category,
    size_bytes=excluded.size_bytes,
    modified_at=excluded.modified_at,
    exif_status=excluded.exif_status,
    exif_error=excluded.exif_error,
    taken_at=excluded.taken_at,
    camera_make=excluded.camera_make,
    camera_model=excluded.camera_model,
    lens_model=excluded.lens_model,
    focal_length_mm=excluded.focal_length_mm,
    f_number=excluded.f_number,
    exposure_time_s=excluded.exposure_time_s,
    iso=excluded.iso,
    exposure_bias_ev=excluded.exposure_bias_ev,
    gps_latitude=excluded.gps_latitude,
    gps_longitude=excluded.gps_longitude,
    width_px=excluded.width_px,
    height_px=excluded.height_px,
    last_scanned_at=excluded.last_scanned_at
""".format(
    cols=", ".join(_PHOTO_COLUMNS),
    marks=", ".join("?" for _ in _PHOTO_COLUMNS),
)


def _now() -> str:
    """当前本地时间，统一格式。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class FolderInfo:
    """文件夹库中的一条记录（GUI 的"文件夹库"面板使用）。"""

    path: Path
    added_at: str
    last_scanned_at: str | None
    photo_count: int

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)


def _photo_values(info: PhotoInfo, *, for_insert: bool) -> tuple:
    """把 :class:`PhotoInfo` 转成一行数据。

    ``for_insert=True`` 时补充 ``first_seen_at``（INSERT 用）；
    ``for_insert=False`` 时该位置为 ``None``（ON CONFLICT 更新时不会用到）。
    """
    now = _now()
    return (
        str(info.path.resolve()),
        info.file_name,
        info.category,
        info.size_bytes,
        info.modified_at,
        info.exif_status,
        info.exif_error,
        info.taken_at,
        info.camera_make,
        info.camera_model,
        info.lens_model,
        info.focal_length_mm,
        info.f_number,
        info.exposure_time_s,
        info.iso,
        info.exposure_bias_ev,
        info.gps_latitude,
        info.gps_longitude,
        info.width_px,
        info.height_px,
        now if for_insert else None,
        now,
    )


class PhotoDatabase:
    """照片信息数据库。用法示例::

        with PhotoDatabase.open("data/photos.db") as db:
            db.upsert_many(infos)
            print(db.count_photos())
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    # ---- 生命周期 -------------------------------------------------------- #

    @classmethod
    def open(cls, db_path: str | Path) -> "PhotoDatabase":
        return cls(db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PhotoDatabase":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- 写入 ------------------------------------------------------------ #

    def _execute_upsert(self, info: PhotoInfo) -> None:
        """执行一条 INSERT ... ON CONFLICT 语句（不负责提交）。"""
        self._conn.execute(_UPSERT_SQL, _photo_values(info, for_insert=True))

    def upsert_photo(self, info: PhotoInfo) -> None:
        """写入或更新一条照片记录（按绝对路径去重），并立即提交。"""
        with self._conn:
            self._execute_upsert(info)

    def upsert_many(self, infos) -> int:
        """批量写入照片记录，返回写入条数。整个过程在一个事务里。"""
        written = 0
        with self._conn:
            for info in infos:
                self._execute_upsert(info)
                written += 1
        return written

    # ---- 读取 ------------------------------------------------------------ #

    def count_photos(self) -> int:
        """数据库中的记录总数。"""
        row = self._conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()
        return int(row["n"])

    def iter_photos(self) -> Iterator[PhotoInfo]:
        """按路径顺序遍历全部照片记录。"""
        cursor = self._conn.execute(
            f"SELECT {', '.join(_PHOTO_COLUMNS)} FROM photos ORDER BY path"
        )
        for row in cursor:
            yield _row_to_photo_info(row)

    def get_photo(self, path: str | Path) -> PhotoInfo | None:
        """按绝对路径精确查找一条记录。"""
        key = str(Path(path).resolve())
        row = self._conn.execute(
            f"SELECT {', '.join(_PHOTO_COLUMNS)} FROM photos WHERE path = ?",
            (key,),
        ).fetchone()
        return _row_to_photo_info(row) if row is not None else None

    def find_photos(self, match: str = "", *, limit: int = 50) -> list[PhotoInfo]:
        """按文件名或路径模糊查找（大小写不敏感）。

        ``match`` 为空时返回任意记录，最多返回 ``limit`` 条。
        """
        cursor = self._conn.execute(
            f"""
            SELECT {', '.join(_PHOTO_COLUMNS)} FROM photos
            WHERE file_name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\'
            ORDER BY path
            LIMIT ?
            """,
            (
                f"%{_escape_like(match)}%",
                f"%{_escape_like(match)}%",
                max(1, int(limit)),
            ),
        )
        return [_row_to_photo_info(row) for row in cursor.fetchall()]

    def count_by(self, column: str) -> dict[str, int]:
        """按指定维度分组统计数量，返回 {取值: 数量}，按数量降序。

        ``column`` 只允许 ``category / camera / camera_make / camera_model /
        lens_model / exif_status``。
        """
        expr = _GROUPABLE_COLUMNS.get(column)
        if expr is None:
            raise ValueError(
                f"不允许的分组列 {column!r}，可选：{sorted(_GROUPABLE_COLUMNS)}"
            )
        rows = self._conn.execute(
            f"""
            SELECT COALESCE(NULLIF(TRIM({expr}), ''), '（未知）') AS label, COUNT(*) AS n
            FROM photos
            GROUP BY label
            ORDER BY n DESC, label ASC
            """
        )
        return {row["label"]: int(row["n"]) for row in rows}

    def count_by_exif_status(self) -> dict[str, int]:
        """按 EXIF 读取状态统计，保证所有状态都有条目。"""
        counts = {status: 0 for status in EXIF_STATUSES}
        for label, n in self.count_by("exif_status").items():
            counts[label] = counts.get(label, 0) + n
        return counts

    # ---- 文件夹库 -------------------------------------------------------- #

    def add_folder(self, path: str | Path) -> None:
        """把一个文件夹加入文件夹库（已存在时忽略）。"""
        key = str(Path(path).resolve())
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO folders (path, added_at) VALUES (?, ?)",
                (key, _now()),
            )

    def remove_folder(self, path: str | Path) -> None:
        """从文件夹库移除一个文件夹（不删除照片记录，更不碰原片）。"""
        key = str(Path(path).resolve())
        with self._conn:
            self._conn.execute("DELETE FROM folders WHERE path = ?", (key,))

    def mark_folder_scanned(self, path: str | Path) -> None:
        """确保文件夹在库中，并更新其最近扫描时间。"""
        key = str(Path(path).resolve())
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO folders (path, added_at, last_scanned_at)
                VALUES (?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET last_scanned_at=excluded.last_scanned_at
                """,
                (key, _now(), _now()),
            )

    def list_folders(self) -> list[FolderInfo]:
        """返回文件夹库全部记录（按路径排序），附带各自的照片数量。"""
        rows = self._conn.execute(
            "SELECT path, added_at, last_scanned_at FROM folders ORDER BY path"
        ).fetchall()
        result = []
        for row in rows:
            result.append(
                FolderInfo(
                    path=Path(row["path"]),
                    added_at=row["added_at"],
                    last_scanned_at=row["last_scanned_at"],
                    photo_count=self.count_photos_under(row["path"]),
                )
            )
        return result

    def count_photos_under(self, folder: str | Path) -> int:
        """统计某个文件夹（含子文件夹）下的照片记录数。"""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM photos WHERE path LIKE ? ESCAPE '\\'",
            (_like_prefix(folder),),
        ).fetchone()
        return int(row["n"])

    def find_photos_under(
        self, folders: Iterable[str | Path], *, limit: int = 20000
    ) -> list[PhotoInfo]:
        """返回给定文件夹集合（含子文件夹）下的照片记录，按路径排序。"""
        clauses = []
        params: list[str] = []
        for folder in folders:
            clauses.append("path LIKE ? ESCAPE '\\'")
            params.append(_like_prefix(folder))
        if not clauses:
            return []
        params.append(max(1, int(limit)))
        cursor = self._conn.execute(
            f"""
            SELECT {', '.join(_PHOTO_COLUMNS)} FROM photos
            WHERE {' OR '.join(clauses)}
            ORDER BY path
            LIMIT ?
            """,
            params,
        )
        return [_row_to_photo_info(row) for row in cursor.fetchall()]


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _escape_like(text: str) -> str:
    """转义 LIKE 中的 % _ \\ 。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_prefix(folder: str | Path) -> str:
    """构造"该文件夹及其子文件夹"的 LIKE 前缀模式。

    注意：路径分隔符本身也要转义（\\\\ 表示字面量 \\），
    末尾接未转义的 % 作为通配符。
    """
    return _escape_like(str(Path(folder).resolve())) + "\\\\%"


def _row_to_photo_info(row: sqlite3.Row) -> PhotoInfo:
    """把数据库一行还原成 :class:`PhotoInfo`。"""
    return PhotoInfo(
        path=Path(row["path"]),
        category=row["category"],
        size_bytes=row["size_bytes"],
        modified_at=row["modified_at"],
        exif_status=row["exif_status"],
        exif_error=row["exif_error"],
        taken_at=row["taken_at"],
        camera_make=row["camera_make"],
        camera_model=row["camera_model"],
        lens_model=row["lens_model"],
        focal_length_mm=row["focal_length_mm"],
        f_number=row["f_number"],
        exposure_time_s=row["exposure_time_s"],
        iso=row["iso"],
        exposure_bias_ev=row["exposure_bias_ev"],
        gps_latitude=row["gps_latitude"],
        gps_longitude=row["gps_longitude"],
        width_px=row["width_px"],
        height_px=row["height_px"],
    )
