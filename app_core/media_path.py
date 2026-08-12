# -*- coding: utf-8 -*-
"""本地媒体路径的共享规范化。"""

from __future__ import annotations


def normalize_media_path(value: object) -> str:
    """仅移除首尾空白；路径内部的连续空格等字符必须原样保留。"""

    return str(value or "").strip()
