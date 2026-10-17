"""PhotoClassifier V1.5：照片信息数据结构。

一张照片（或任意被扫描到的文件）在程序中的完整信息记录。
对应《项目总体规划》第八节"照片信息系统"中当前阶段需要收集的内容：

- 文件信息：文件名、文件路径、文件格式（分类）、文件大小、修改时间；
- EXIF：拍摄时间、相机、镜头、焦距、光圈、快门、ISO、曝光补偿、GPS、尺寸。

图像分析、质量分析、用户标签/备注等字段留给 V2 及以后版本。

本模块只定义数据结构，不负责读取，也不接触任何第三方库。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# --------------------------------------------------------------------------- #
# EXIF 读取状态
# --------------------------------------------------------------------------- #

#: 成功读取到 EXIF 数据。
EXIF_OK = "ok"

#: 文件本身可读，但没有找到 EXIF 数据（例如不带 EXIF 的 JPEG、PNG）。
EXIF_NO_EXIF = "no_exif"

#: 尝试读取时出错，文件很可能已损坏。属于扫描报告中的"异常"。
EXIF_ERROR = "error"

#: 没有尝试读取 EXIF（视频、未知格式等非照片文件）。
EXIF_SKIPPED = "skipped"

#: 全部合法状态，顺序固定，方便输出与测试。
EXIF_STATUSES: tuple[str, ...] = (EXIF_OK, EXIF_NO_EXIF, EXIF_ERROR, EXIF_SKIPPED)


# --------------------------------------------------------------------------- #
# 照片信息
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PhotoInfo:
    """一个文件经过 V1.5 分析后的完整信息记录。

    任何字段读取失败都不应导致抛异常：
    读取不到的字段保持 ``None``，整体状态记录在 ``exif_status`` 中。
    """

    # ---- 文件信息（总是可以填写） ----
    path: Path
    category: str
    size_bytes: int
    modified_at: str | None = None  # 文件修改时间，"YYYY-MM-DD HH:MM:SS"

    # ---- EXIF 读取状态 ----
    exif_status: str = EXIF_SKIPPED
    exif_error: str | None = None

    # ---- EXIF 字段（读取不到就是 None） ----
    taken_at: str | None = None  # 拍摄时间，"YYYY-MM-DD HH:MM:SS"
    camera_make: str | None = None  # 相机厂商，如 "NIKON CORPORATION"
    camera_model: str | None = None  # 相机型号，如 "NIKON Z6III"
    lens_model: str | None = None  # 镜头型号，如 "NIKKOR Z 24-120mm f/4 S"
    focal_length_mm: float | None = None  # 焦距（mm）
    f_number: float | None = None  # 光圈值，如 4.0 表示 f/4
    exposure_time_s: float | None = None  # 快门速度（秒），如 0.004 表示 1/250s
    iso: int | None = None  # 感光度
    exposure_bias_ev: float | None = None  # 曝光补偿（EV）
    gps_latitude: float | None = None  # GPS 纬度（十进制度）
    gps_longitude: float | None = None  # GPS 经度（十进制度）
    width_px: int | None = None  # 图片宽度（像素）
    height_px: int | None = None  # 图片高度（像素）

    @property
    def file_name(self) -> str:
        """文件名（不含目录）。"""
        return self.path.name

    @property
    def camera(self) -> str | None:
        """"厂商 + 型号"的合并显示，如 "SONY ILCE-7M4"。

        如果型号本身已经以厂商名开头（例如厂商 "NIKON CORPORATION"、
        型号 "NIKON Z6III"），就只显示型号，避免重复。
        """
        make = (self.camera_make or "").strip()
        model = (self.camera_model or "").strip()
        if make and model:
            make_first_word = make.split()[0]
            if model.upper().startswith(make_first_word.upper()):
                return model
            return f"{make} {model}"
        return make or model or None

    @property
    def has_gps(self) -> bool:
        """是否带有 GPS 坐标。"""
        return self.gps_latitude is not None and self.gps_longitude is not None

    @property
    def is_photo(self) -> bool:
        """是否是会尝试做照片分析的类型（NEF / JPEG / PNG）。"""
        return self.exif_status in (EXIF_OK, EXIF_NO_EXIF, EXIF_ERROR)
