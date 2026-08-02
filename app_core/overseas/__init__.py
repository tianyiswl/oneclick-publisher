# -*- coding: utf-8 -*-
"""海外平台增值包的稳定客户端边界。

本包只定义能力、权益和状态，不读取或保存 OAuth Token、Cookie 与密钥。
平台实现必须通过 :mod:`app_core.overseas.capability` 中的接口接入。
"""

from .capability import OverseasPlatformCapability
from .entitlements import evaluate_overseas_entitlement
from .models import (
    AuthorizationRequest,
    AuthorizationResult,
    CapabilityAvailability,
    CapabilityDescriptor,
    EntitlementDecision,
    EvidenceRecord,
    EvidenceStage,
    OverseasOperation,
    OverseasPlatform,
    PlatformCapabilityState,
    PublicationMode,
    ReadbackResult,
    UploadRequest,
    UploadResult,
)
from .registry import CapabilityRegistry, default_registry

__all__ = [
    "AuthorizationRequest",
    "AuthorizationResult",
    "CapabilityAvailability",
    "CapabilityDescriptor",
    "CapabilityRegistry",
    "EntitlementDecision",
    "EvidenceRecord",
    "EvidenceStage",
    "OverseasOperation",
    "OverseasPlatform",
    "OverseasPlatformCapability",
    "PlatformCapabilityState",
    "PublicationMode",
    "ReadbackResult",
    "UploadRequest",
    "UploadResult",
    "default_registry",
    "evaluate_overseas_entitlement",
]
