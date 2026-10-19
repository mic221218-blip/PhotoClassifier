"""路径 key 的统一工具（P0 优化 + 语义收敛）。

数据库里的照片路径已经是**绝对且解析过**的形式（``photos.path`` 由
``PhotoDatabase`` 写入）。因此：

- 读取数据库里的路径时用 :func:`db_key`：只做 ``normpath``，**不做文件系统查询**；
  1 万张照片的筛选热路径必须走这里。
- 处理用户输入 / 新文件路径时用 :func:`resolve_key`：
  ``Path.resolve()`` 可能触发文件系统调用，但只发生在写入或单点操作上。
"""

from __future__ import annotations

import os
from pathlib import Path


def db_key(path: str | Path) -> str:
    """数据库内路径 → 比较用 key（绝对路径、已解析，仅规范分隔符）。"""
    text = os.path.normpath(str(path))
    candidate = Path(text)
    if candidate.is_absolute():
        return text
    return str(candidate.resolve())


def resolve_key(path: str | Path) -> str:
    """用户输入 / 文件路径 → 与 ``photos.path`` 一致的绝对路径。"""
    return str(Path(path).resolve())
