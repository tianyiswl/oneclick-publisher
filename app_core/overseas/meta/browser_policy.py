# -*- coding: utf-8 -*-
"""Meta Business Suite 浏览器发布的安全门禁。

浏览器适配器与官方 Graph API 是两条独立链路。本模块只负责浏览器链路的
显式确认和海外增值权益判断，不读取 Cookie，也不放宽官方 API 的开发中状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..entitlements import evaluate_overseas_entitlement
from ..models import CapabilityAvailability, CapabilityDescriptor, OverseasPlatform


META_BROWSER_PLATFORM_TYPES = frozenset({8, 9})
META_BROWSER_PUBLISH_CONFIRMED = "metaBrowserPublishConfirmed"
META_BROWSER_AUTOMATION_ACKNOWLEDGED = "metaBrowserAutomationAcknowledged"
META_BROWSER_CONFIRMATION_MESSAGE = (
    "Instagram/Facebook 浏览器正式发布需要在桌面端明确确认，并同意实验性浏览器自动化提示"
)

_PLATFORM_BY_TYPE = {
    8: OverseasPlatform.INSTAGRAM,
    9: OverseasPlatform.FACEBOOK,
}


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


def descriptor_for_platform_type(platform_type: int) -> CapabilityDescriptor:
    try:
        platform = _PLATFORM_BY_TYPE[int(platform_type)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"不是 Meta 浏览器发布平台：{platform_type}") from exc
    return CapabilityDescriptor(
        platform=platform,
        availability=CapabilityAvailability.DEVELOPMENT,
        operations=frozenset(),
    )


def browser_publish_entitlement_decision(
    platform_type: int,
    license_status: Mapping[str, Any],
):
    """复用统一海外增值包权益规则；不把浏览器实验能力标记为官方可用。"""

    return evaluate_overseas_entitlement(
        license_status,
        descriptor_for_platform_type(platform_type),
    )
