"""GUI 主题、常量与纯显示辅助（供 gui / 面板 / 对话框共用）。"""

from __future__ import annotations

from pathlib import Path

from src.formatting import format_shutter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "photos.db"
ICON_PATH = PROJECT_ROOT / "assets" / "photoclassifier.ico"

WIN_BG = "#1b1c21"
PANEL_BG = "#202127"
CARD_BG = "#23242b"
INPUT_BG = "#26272e"
BORDER = "#2f313b"
BORDER_SOFT = "#2a2b33"
ACCENT = "#4c8dff"
ACCENT_DIM = "#31415e"
GREEN = "#3fb27f"
ORANGE = "#e8a33d"
TEXT = "#e9eaf0"
TEXT_SUB = "#9a9dab"
TEXT_FAINT = "#6e717e"

#: 结果列表的列定义：(键, 标题, 宽度, 锚)
RESULT_COLUMNS = [
    ("name", "文件名", 180, "w"),
    ("category", "类型", 55, "w"),
    ("taken_at", "拍摄时间", 150, "w"),
    ("camera", "相机", 110, "w"),
    ("iso", "ISO", 50, "e"),
    ("shutter", "快门", 80, "e"),
    ("rules", "自动标签", 180, "w"),
    ("quality", "质量", 90, "w"),
    ("size", "大小", 80, "e"),
]

#: 质量列的紧凑写法（空间有限，详情面板里显示完整原因）
QUALITY_SHORT = {
    "blurry": "模糊",
    "motion_blur": "运动",
    "overexposed": "过曝",
    "underexposed": "欠曝",
    "black_frame": "黑屏",
    "duplicate": "重复",
}


def mm(value):
    return f"{value:g} mm" if value is not None else None


def f_number(value):
    return f"f/{value:g}" if value is not None else None


def ev(value):
    if value is None:
        return None
    return "0 EV" if value == 0 else f"{value:+.1f} EV"


#: 详情面板展示的字段（与 V1.5 数据库字段一一对应）
DETAIL_FIELDS = [
    ("拍摄时间", lambda i: i.taken_at),
    ("相机", lambda i: i.camera),
    ("镜头", lambda i: i.lens_model),
    ("焦距", lambda i: mm(i.focal_length_mm)),
    ("光圈", lambda i: f_number(i.f_number)),
    ("快门", lambda i: format_shutter(i.exposure_time_s) if i.exposure_time_s else None),
    ("ISO", lambda i: str(i.iso) if i.iso is not None else None),
    ("曝光补偿", lambda i: ev(i.exposure_bias_ev)),
    ("GPS", lambda i: f"{i.gps_latitude:.6f}, {i.gps_longitude:.6f}" if i.has_gps else None),
    ("尺寸", lambda i: f"{i.width_px} x {i.height_px}" if i.width_px else None),
    ("修改时间", lambda i: i.modified_at),
]

#: 分类对应的预览渐变色（顶部色, 底部色）
PREVIEW_COLORS = {
    "nef": ((58, 48, 92), (206, 120, 74)),
    "jpeg": ((30, 52, 84), (120, 160, 150)),
    "png": ((28, 44, 60), (104, 170, 130)),
    "video": ((40, 41, 48), (64, 66, 76)),
    "unsupported": ((40, 41, 48), (64, 66, 76)),
}


def category_label(category: str) -> str:
    return {
        "nef": "NEF",
        "jpeg": "JPEG",
        "png": "PNG",
        "video": "视频",
        "unsupported": "其他",
    }.get(category, category)
