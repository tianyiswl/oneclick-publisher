# -*- coding: utf-8 -*-
"""Meta Business Suite 浏览器正式发布的安全门禁。

本模块只保存浏览器执行器需要的两项一次性确认规则，不包含开发者应用、
OAuth、Graph API、令牌或官方接口能力。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


META_BROWSER_PLATFORM_TYPES = frozenset({8, 9})
META_BROWSER_PUBLISH_CONFIRMED = "metaBrowserPublishConfirmed"
META_BROWSER_AUTOMATION_ACKNOWLEDGED = "metaBrowserAutomationAcknowledged"
META_BROWSER_CONFIRMATION_MESSAGE = (
    "Instagram/Facebook 浏览器正式发布需要在桌面端明确确认，"
    "并同意实验性浏览器自动化提示"
)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def browser_publish_confirmation_valid(payload: Mapping[str, Any]) -> bool:
    """只有两个独立确认字段同时为真时才允许进入最终发布路径。"""

    return _as_bool(payload.get(META_BROWSER_PUBLISH_CONFIRMED)) and _as_bool(
        payload.get(META_BROWSER_AUTOMATION_ACKNOWLEDGED)
    )
