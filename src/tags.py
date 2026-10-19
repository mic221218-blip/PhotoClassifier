"""PhotoClassifier V3：标签的纯逻辑（规范化、颜色、预置列表）。

不读写数据库、不依赖 Tkinter；供 ``tag_store`` / CLI / GUI 共用。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

#: 标签名最大长度（字符数，按 Unicode 码点计）。
MAX_TAG_NAME_LENGTH = 30

#: 预置标签：只创建标签，不自动关联任何照片。
PRESET_TAGS: tuple[str, ...] = (
    "风光",
    "建筑",
    "夜景",
    "人物",
    "天空",
    "水面",
    "城市",
    "运动",
    "花卉",
    "日落",
)

#: 与深色主题协调的色板。
PALETTE: tuple[str, ...] = (
    "#4c8dff",
    "#3fb27f",
    "#e8a33d",
    "#d96a8b",
    "#8f7bd8",
    "#4bb3c4",
    "#c9a227",
    "#6fa8dc",
    "#b06ac9",
    "#5fbf8f",
)


class TagError(ValueError):
    """标签操作的业务错误（给用户看的消息）。"""


class TagConflict(TagError):
    """标签名冲突。"""

    def __init__(self, name: str):
        super().__init__(f"标签「{name}」已存在")
        self.name = name


def normalize_tag_name(raw: str | None) -> str:
    """把用户输入规范成可入库的标签名。

    - 去首尾空白、去开头的 ``#``；
    - Unicode NFC，连续空白折叠成一个空格；
    - 拒绝空名、控制字符、超长名称。
    """
    if raw is None:
        raise TagError("标签名不能为空")
    name = unicodedata.normalize("NFC", str(raw)).strip()
    if name.startswith("#"):
        name = name[1:].strip()
    if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in name):
        raise TagError("标签名不能包含换行或控制字符")
    name = re.sub(r"\s+", " ", name)
    if not name:
        raise TagError("标签名不能为空")
    if len(name) > MAX_TAG_NAME_LENGTH:
        raise TagError(f"标签名过长（最多 {MAX_TAG_NAME_LENGTH} 个字符）")
    return name


def tag_key(name: str) -> str:
    """大小写不敏感的查找键。"""
    return normalize_tag_name(name).casefold()


def color_for(name: str) -> str:
    """按名称稳定地分配一个色板颜色。"""
    key = tag_key(name)
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def chip_colors(color: str | None) -> tuple[str, str]:
    """根据背景色返回 ``(背景, 前景)``，保证深色/浅色底都能看清。"""
    if not color:
        return "#2f313b", "#e9eaf0"
    value = color.lstrip("#")
    if len(value) != 6:
        return "#2f313b", "#e9eaf0"
    try:
        red = int(value[0:2], 16)
        green = int(value[2:4], 16)
        blue = int(value[4:6], 16)
    except ValueError:
        return "#2f313b", "#e9eaf0"
    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
    return color, "#1b1c21" if luminance > 150 else "#ffffff"
