"""发布配置的最小本地实现。

展示版其他配置功能仍保持无副作用占位，但话题解析会直接进入发布
载荷，不能用通用占位值吞掉内容包标签。
"""

from __future__ import annotations

import re


TAG_PATTERN = re.compile(r"#?([\w\u4e00-\u9fff-]+)")


def parse_tags(text: str) -> list[str]:
    """把用户输入解析为去重话题列表。"""

    tags: list[str] = []
    for match in TAG_PATTERN.finditer(text or ""):
        tag = match.group(1).strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def __getattr__(_name):
    return lambda *_args, **_kwargs: []
