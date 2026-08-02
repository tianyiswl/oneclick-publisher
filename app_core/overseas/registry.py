# -*- coding: utf-8 -*-
"""海外平台能力注册表与统一调用门禁。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .capability import OverseasPlatformCapability
from .entitlements import evaluate_overseas_entitlement
from .models import (
    CapabilityAvailability,
    CapabilityDescriptor,
    OverseasPlatform,
    PlatformCapabilityState,
)


PLATFORM_ORDER = (
    OverseasPlatform.YOUTUBE,
    OverseasPlatform.TIKTOK,
    OverseasPlatform.INSTAGRAM,
    OverseasPlatform.FACEBOOK,
)


class CapabilityUnavailableError(RuntimeError):
    """平台仍在开发中或尚未注册可用实现。"""


class OverseasEntitlementError(PermissionError):
    """当前授权不包含目标海外平台能力。"""


class CapabilityRegistry:
    """维护平台描述与实现，避免平台逻辑散落到公共 UI。"""

    def __init__(self, descriptors: Iterable[CapabilityDescriptor] = ()) -> None:
        self._descriptors = {
            descriptor.platform: descriptor for descriptor in descriptors
        }
        self._capabilities: dict[OverseasPlatform, OverseasPlatformCapability] = {}

    def register(self, capability: OverseasPlatformCapability) -> None:
        descriptor = capability.descriptor
        if descriptor.availability is not CapabilityAvailability.AVAILABLE:
            raise ValueError("只有达到可用门槛的平台实现才能注册")
        self._descriptors[descriptor.platform] = descriptor
        self._capabilities[descriptor.platform] = capability

    def descriptor(self, platform: OverseasPlatform) -> CapabilityDescriptor:
        try:
            return self._descriptors[platform]
        except KeyError as exc:
            raise KeyError(f"海外平台注册表中不存在：{platform.value}") from exc

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(
            self._descriptors[platform]
            for platform in PLATFORM_ORDER
            if platform in self._descriptors
        )

    def states(
        self,
        license_status: Mapping[str, Any],
    ) -> tuple[PlatformCapabilityState, ...]:
        return tuple(
            PlatformCapabilityState(
                descriptor=descriptor,
                entitlement=evaluate_overseas_entitlement(
                    license_status,
                    descriptor,
                ),
            )
            for descriptor in self.descriptors()
        )

    def require(
        self,
        platform: OverseasPlatform,
        license_status: Mapping[str, Any],
    ) -> OverseasPlatformCapability:
        descriptor = self.descriptor(platform)
        if descriptor.availability is not CapabilityAvailability.AVAILABLE:
            raise CapabilityUnavailableError(f"{descriptor.display_name} 仍在开发中")
        capability = self._capabilities.get(platform)
        if capability is None:
            raise CapabilityUnavailableError(
                f"{descriptor.display_name} 尚未注册可用平台实现"
            )
        decision = evaluate_overseas_entitlement(license_status, descriptor)
        if not decision.allowed:
            raise OverseasEntitlementError(decision.message)
        return capability


def default_registry() -> CapabilityRegistry:
    """返回已验收平台与保持失败关闭的开发中平台描述。"""

    # 延迟导入避免海外包初始化时形成循环依赖。
    from .meta import FacebookCapability, InstagramCapability
    from .youtube import YouTubeCapability

    meta_descriptors = {
        capability.descriptor.platform: capability.descriptor
        for capability in (InstagramCapability(), FacebookCapability())
    }

    registry = CapabilityRegistry(
        meta_descriptors.get(
            platform,
            CapabilityDescriptor(
                platform=platform,
                availability=CapabilityAvailability.DEVELOPMENT,
                operations=frozenset(),
            ),
        )
        for platform in PLATFORM_ORDER
        if platform is not OverseasPlatform.YOUTUBE
    )
    registry.register(YouTubeCapability())
    return registry
