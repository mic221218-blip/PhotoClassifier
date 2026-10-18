"""PhotoClassifier V2.5：图像质量度量与判定（纯函数，不依赖 Pillow）。

设计（见 docs/v2.5-plan.zh-CN.md）：

- **只算测量值**：清晰度、对比度、方向性、亮度统计、dHash 等都只是数；
- **判定是实时的**：是否"模糊/过曝/欠曝/黑屏"由 :class:`QualityConfig`
  的阈值决定，改阈值不需要重新解码图片；
- **EXIF 只用于解释文案**（例如"快门 1/15s 慢于 1/60s，疑似手抖"），
  不参与是否命中的判断；
- EXIF 缺失、预览无法解码都不算错误：没有测量值就不产生标记。

本模块只接受 :class:`GrayImage`（512px 长边灰度字节），
不打开文件、不导入 Pillow —— 像素获取在 ``src/preview.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from src.formatting import format_shutter
from src.photo_info import PhotoInfo


# --------------------------------------------------------------------------- #
# 版本
# --------------------------------------------------------------------------- #

#: 度量算法版本。任何会改变测量数值的改动都必须 +1，旧记录据此失效重算。
ANALYZER_VERSION = 1

#: 预览来源
PREVIEW_EMBEDDED = "embedded_jpeg"
PREVIEW_FULL = "full_image"
PREVIEW_NONE = "none"

PREVIEW_SOURCES: tuple[str, ...] = (PREVIEW_EMBEDDED, PREVIEW_FULL, PREVIEW_NONE)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QualityConfig:
    """质量判定阈值。默认值是初值，已在真实样本上校准（见 README）。

    - ``thumb_edge``：度量统一使用的最长边（像素）。
    - ``blur_var_min``：拉普拉斯方差的模糊阈值（低于它视为疑似模糊）。
    - ``blur_contrast_min``：低于该对比度不判模糊（低对比画面方差天然偏低）。
    - ``motion_anisotropy_min``：梯度方向性阈值（越大越像单向运动模糊）。
    - ``dark_mean_max`` / ``bright_mean_min``：平均亮度的欠曝 / 过曝阈值。
    - ``clip_low_max`` / ``clip_high_max``：暗部 / 高光截断比例阈值。
    - ``black_mean_max`` / ``black_std_max``：黑屏判定（很暗且几乎没有变化）。
    - ``dhash_distance_max``：近似重复的 dHash 汉明距离上限。
    - ``dupe_time_window_s``：只比较拍摄时间相差不超过该值的照片。
    - ``sequence_min_size``：近似组超过该张数视为"延时/连拍序列"，不算重复。
    - ``sequence_gap_s``：拍摄间隔不超过该值的连续照片视为同一组序列。
    - ``sequence_min_frames`` / ``sequence_min_span_s``：一组序列至少要有
      这么多张、跨越这么久，才判定为延时序列（避免把短连拍当序列）。
    - ``sequence_folder_mode``：同一个文件夹里存在延时序列时，该文件夹内
      所有"相似但不同"的照片也按序列处理，不再标记为重复（延时素材的
      正常帧就是这样的）；关闭后只按时间序列判定。
    """

    thumb_edge: int = 512
    blur_var_min: float = 120.0
    blur_contrast_min: float = 18.0
    motion_anisotropy_min: float = 0.55
    dark_mean_max: float = 45.0
    bright_mean_min: float = 210.0
    clip_low_max: float = 0.25
    clip_high_max: float = 0.12
    black_mean_max: float = 15.0
    black_std_max: float = 8.0
    dhash_distance_max: int = 8
    dupe_time_window_s: float = 10.0
    sequence_min_size: int = 8
    sequence_gap_s: float = 12.0
    sequence_min_frames: int = 20
    sequence_min_span_s: float = 60.0
    sequence_folder_mode: bool = True

    def with_config(self, **overrides) -> "QualityConfig":
        """返回覆盖若干阈值后的新配置（便于测试与设置界面）。"""
        return replace(self, **overrides)


# --------------------------------------------------------------------------- #
# 灰度图与测量值
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GrayImage:
    """一张已归一化的灰度图：长边 ≤ thumb_edge，``data`` 为逐行字节。"""

    width: int
    height: int
    data: bytes


@dataclass(frozen=True)
class GrayMetrics:
    """灰度图上算出的全部指标（都是"测量值"，不是结论）。"""

    blur_score: float
    contrast: float
    anisotropy: float
    mean_lum: float
    std_lum: float
    clip_low_frac: float
    clip_high_frac: float


@dataclass(frozen=True)
class QualityMeasurement:
    """一张照片的质量测量记录（对应 ``photo_quality`` 表的一行）。"""

    path: str
    analyzer_version: int = ANALYZER_VERSION
    analyzed_at: str = ""
    preview_source: str = PREVIEW_NONE
    preview_width: int | None = None
    preview_height: int | None = None
    blur_score: float | None = None
    contrast: float | None = None
    anisotropy: float | None = None
    mean_lum: float | None = None
    std_lum: float | None = None
    clip_low_frac: float | None = None
    clip_high_frac: float | None = None
    dhash: str | None = None
    quick_hash: str | None = None
    content_hash: str | None = None
    file_size: int | None = None
    file_mtime: str | None = None
    error: str | None = None

    @property
    def analyzed(self) -> bool:
        """是否拿到了可用的测量值。"""
        return self.error is None and self.blur_score is not None

    @property
    def has_pixels(self) -> bool:
        """是否有可用的预览（用于报告里的来源统计）。"""
        return self.preview_source in (PREVIEW_EMBEDDED, PREVIEW_FULL)


@dataclass(frozen=True)
class QualityFlag:
    """一条质量标记，``reason`` 是给人看的解释。"""

    flag_id: str
    label: str
    reason: str


#: 标记 id → 中文名（顺序即展示顺序）。
FLAG_LABELS: dict[str, str] = {
    "blurry": "模糊",
    "motion_blur": "运动模糊",
    "overexposed": "过曝",
    "underexposed": "欠曝",
    "black_frame": "黑屏",
    "duplicate": "重复",
}

FLAG_IDS: tuple[str, ...] = tuple(FLAG_LABELS)


# --------------------------------------------------------------------------- #
# 度量（纯 Python；输入是 512px 灰度图，约 5 万次循环，毫秒级）
# --------------------------------------------------------------------------- #


def measure_gray(gray: GrayImage) -> GrayMetrics:
    """在灰度图上一次性算出全部指标。

    一次遍历同时得到：拉普拉斯方差、梯度方向能量、亮度直方图统计。
    尺寸不足 3×3 时抛 ``ValueError``（调用方应视为不可分析）。
    """
    width, height, data = gray.width, gray.height, gray.data
    if width < 3 or height < 3:
        raise ValueError(f"图像过小，无法分析：{width}x{height}")
    total = width * height
    if len(data) != total:
        raise ValueError(f"像素数据长度不符：{len(data)} != {total}")

    # ---- 亮度直方图（均值 / 标准差 / 截断比例） ----
    histogram = [0] * 256
    luminance_sum = 0
    for value in data:
        histogram[value] += 1
        luminance_sum += value
    mean_lum = luminance_sum / total
    variance = 0.0
    for value, count in enumerate(histogram):
        if count:
            delta = value - mean_lum
            variance += count * delta * delta
    std_lum = variance / total
    std_lum = std_lum**0.5
    clip_low_frac = sum(histogram[:6]) / total
    clip_high_frac = sum(histogram[250:]) / total

    # ---- 拉普拉斯方差 + 梯度能量（一次遍历） ----
    lap_sum = 0.0
    lap_sq_sum = 0.0
    grad_x_energy = 0.0
    grad_y_energy = 0.0
    count = 0
    row = width
    for y in range(1, height - 1):
        base = y * row
        up = base - row
        down = base + row
        for x in range(1, width - 1):
            index = base + x
            laplacian = (
                4 * data[index]
                - data[index - 1]
                - data[index + 1]
                - data[up + x]
                - data[down + x]
            )
            dx = data[index + 1] - data[index]
            dy = data[down + x] - data[index]
            lap_sum += laplacian
            lap_sq_sum += laplacian * laplacian
            grad_x_energy += dx * dx
            grad_y_energy += dy * dy
            count += 1

    mean_lap = lap_sum / count
    blur_score = lap_sq_sum / count - mean_lap * mean_lap
    energy_sum = grad_x_energy + grad_y_energy
    anisotropy = abs(grad_x_energy - grad_y_energy) / energy_sum if energy_sum else 0.0

    return GrayMetrics(
        blur_score=max(blur_score, 0.0),
        contrast=std_lum,
        anisotropy=anisotropy,
        mean_lum=mean_lum,
        std_lum=std_lum,
        clip_low_frac=clip_low_frac,
        clip_high_frac=clip_high_frac,
    )


def dhash_from_grid(grid: bytes) -> str:
    """把 9×8 的灰度采样（72 字节）转成 64 位 dHash（16 位十六进制）。

    每一位表示"左像素是否比右像素亮"，对整体亮度变化不敏感。
    """
    if len(grid) != 72:
        raise ValueError(f"dHash 采样长度应为 72，实际 {len(grid)}")
    bits = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            bits = (bits << 1) | (1 if grid[offset + column] > grid[offset + column + 1] else 0)
    return f"{bits:016x}"


def hamming_distance(left: str, right: str) -> int:
    """两个 16 位十六进制 dHash 的汉明距离；格式非法时返回 64（视为完全不同）。"""
    if not left or not right or len(left) != 16 or len(right) != 16:
        return 64
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except (TypeError, ValueError):
        return 64


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #


class QualityEngine:
    """把测量值解释成"标记 + 原因"，阈值全部来自 :class:`QualityConfig`。"""

    def __init__(self, config: QualityConfig | None = None):
        self.config = config or QualityConfig()

    def with_config(self, **overrides) -> "QualityEngine":
        return QualityEngine(self.config.with_config(**overrides))

    def describe(self, flag_id: str) -> str:
        """阈值的人类可读摘要（报告用）。"""
        cfg = self.config
        if flag_id == "blurry":
            return f"清晰度 < {cfg.blur_var_min:.0f}（拉普拉斯方差）"
        if flag_id == "motion_blur":
            return f"模糊 且 方向性 ≥ {cfg.motion_anisotropy_min:.2f}"
        if flag_id == "overexposed":
            return f"平均亮度 ≥ {cfg.bright_mean_min:.0f} 或 高光截断 ≥ {cfg.clip_high_max:.0%}"
        if flag_id == "underexposed":
            return f"平均亮度 ≤ {cfg.dark_mean_max:.0f} 或 暗部截断 ≥ {cfg.clip_low_max:.0%}"
        if flag_id == "black_frame":
            return f"平均亮度 ≤ {cfg.black_mean_max:.0f} 且 标准差 ≤ {cfg.black_std_max:.0f}"
        if flag_id == "duplicate":
            return f"dHash 距离 ≤ {cfg.dhash_distance_max} 且拍摄时间差 ≤ {cfg.dupe_time_window_s:g}s"
        return ""

    def flags(
        self, measurement: QualityMeasurement, info: PhotoInfo | None = None
    ) -> list[QualityFlag]:
        """返回一张照片的质量标记（重复标记由 duplicates 模块另行附加）。

        互斥规则：黑屏优先；过曝/欠曝互斥；黑屏或严重过曝时不判模糊。
        """
        if not measurement.analyzed or measurement.contrast is None:
            return []
        cfg = self.config
        flags: list[QualityFlag] = []

        mean_lum = measurement.mean_lum or 0.0
        std_lum = measurement.std_lum or 0.0
        clip_low = measurement.clip_low_frac or 0.0
        clip_high = measurement.clip_high_frac or 0.0
        contrast = measurement.contrast or 0.0
        blur = measurement.blur_score or 0.0
        anisotropy = measurement.anisotropy or 0.0

        black = mean_lum <= cfg.black_mean_max and std_lum <= cfg.black_std_max
        over = mean_lum >= cfg.bright_mean_min or clip_high >= cfg.clip_high_max
        under = mean_lum <= cfg.dark_mean_max or clip_low >= cfg.clip_low_max

        if black:
            # 黑屏时不再报模糊/欠曝：黑图没有"清晰度"可言
            flags.append(
                QualityFlag(
                    "black_frame",
                    FLAG_LABELS["black_frame"],
                    f"平均亮度 {mean_lum:.0f} ≤ {cfg.black_mean_max:.0f}，"
                    f"标准差 {std_lum:.1f} ≤ {cfg.black_std_max:.0f}"
                    f"（暗部 {clip_low:.0%}，疑似镜头盖/全黑）",
                )
            )
            return flags

        if over:
            reason = f"平均亮度 {mean_lum:.0f} ≥ {cfg.bright_mean_min:.0f}"
            if clip_high >= cfg.clip_high_max:
                reason += f"；高光截断 {clip_high:.0%} ≥ {cfg.clip_high_max:.0%}"
            flags.append(QualityFlag("overexposed", FLAG_LABELS["overexposed"], reason))
        elif under:
            reason = f"平均亮度 {mean_lum:.0f} ≤ {cfg.dark_mean_max:.0f}"
            if clip_low >= cfg.clip_low_max:
                reason += f"；暗部截断 {clip_low:.0%} ≥ {cfg.clip_low_max:.0%}"
            night = _matches_night(info)
            if night:
                reason += "（夜景照片欠曝可能是有意为之）"
            flags.append(QualityFlag("underexposed", FLAG_LABELS["underexposed"], reason))

        # 模糊只在曝光正常（非黑屏、非严重过曝）时判定
        if not over and not black and contrast >= cfg.blur_contrast_min and blur < cfg.blur_var_min:
            if anisotropy >= cfg.motion_anisotropy_min:
                reason = (
                    f"清晰度 {blur:.0f} < {cfg.blur_var_min:.0f}，"
                    f"方向性 {anisotropy:.2f} ≥ {cfg.motion_anisotropy_min:.2f}"
                )
                hint = _motion_hint(info)
                if hint:
                    reason += f"；{hint}"
                flags.append(QualityFlag("motion_blur", FLAG_LABELS["motion_blur"], reason))
            else:
                reason = f"清晰度 {blur:.0f} < {cfg.blur_var_min:.0f}（拉普拉斯方差，{cfg.thumb_edge}px）"
                hint = _focus_hint(info)
                if hint:
                    reason += f"；{hint}"
                flags.append(QualityFlag("blurry", FLAG_LABELS["blurry"], reason))

        return flags


# --------------------------------------------------------------------------- #
# 内部：把 EXIF 变成"解释文案"（不改变判定）
# --------------------------------------------------------------------------- #


def _matches_night(info: PhotoInfo | None) -> bool:
    """是否是夜景时段拍的照片（与 V2 规则同一默认时间窗）。"""
    if info is None or not info.taken_at or len(info.taken_at) < 13:
        return False
    try:
        hour = int(info.taken_at[11:13])
    except ValueError:
        return False
    return hour >= 18 or hour < 6


def _motion_hint(info: PhotoInfo | None) -> str | None:
    if info is None or not info.exposure_time_s:
        return None
    seconds = info.exposure_time_s
    if seconds >= 1 / 60:
        return f"快门 {format_shutter(seconds)} 慢于 1/60s，疑似手抖或运动"
    return None


def _focus_hint(info: PhotoInfo | None) -> str | None:
    if info is None or not info.exposure_time_s:
        return None
    if info.exposure_time_s <= 1 / 250:
        return f"快门 {format_shutter(info.exposure_time_s)}，疑似虚焦或镜头问题"
    return None
