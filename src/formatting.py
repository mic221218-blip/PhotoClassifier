"""PhotoClassifier 通用显示格式化。

CLI、GUI 与规则引擎共用的数值格式化函数，
避免规则模块反向依赖命令行模块造成循环导入。
"""

from __future__ import annotations


def format_size(size_bytes: int) -> str:
    """字节数 → 人类可读（如 "45.2 MB"）。"""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_shutter(seconds: float) -> str:
    """快门秒数 → "1/250s" / "2s"。"""
    if seconds >= 1:
        text = f"{seconds:g}"
        return f"{text}s"
    denominator = round(1 / seconds) if seconds > 0 else 0
    if denominator > 0 and abs(1 / denominator - seconds) <= seconds * 0.05:
        return f"1/{denominator}s"
    return f"{seconds:g}s"
