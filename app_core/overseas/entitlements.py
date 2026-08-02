# -*- coding: utf-8 -*-
"""海外增值包客户端权益门禁。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import CapabilityDescriptor, EntitlementDecision


OVERSEAS_ADDON_PLAN_ID = "overseas_addon"


def _normalized_features(status: Mapping[str, Any]) -> frozenset[str]:
    raw_features = status.get("features")
    if not isinstance(raw_features, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        str(feature).strip()
        for feature in raw_features
        if isinstance(feature, str) and feature.strip()
    )


def evaluate_overseas_entitlement(
    status: Mapping[str, Any],
    descriptor: CapabilityDescriptor,
) -> EntitlementDecision:
    """从 ``activation_service.license_status`` 的已验证结果判断权益。

    本函数不解析或信任原始 Token。生产调用方必须传入激活服务完成签名校验后
    返回的状态。七天本地试用按现有产品规则拥有全功能；其他模式必须包含平台
    对应的服务端签名功能标识，缺失时失败关闭。
    """

    required_feature = descriptor.entitlement_feature
    if not bool(status.get("accessAllowed")):
        return EntitlementDecision(
            allowed=False,
            code="license_access_denied",
            message=str(status.get("statusText") or "软件试用或授权当前不可用"),
            required_feature=required_feature,
            source=str(status.get("mode") or ""),
        )

    mode = str(status.get("mode") or "").strip().lower()
    if mode == "trial":
        return EntitlementDecision(
            allowed=True,
            code="trial_full_access",
            message="七天全功能试用已包含海外平台增值能力",
            required_feature=required_feature,
            source="trial",
        )

    features = _normalized_features(status)
    if required_feature in features:
        return EntitlementDecision(
            allowed=True,
            code="feature_granted",
            message="海外平台增值权益已验证",
            required_feature=required_feature,
            source=str(status.get("planId") or "signed_feature"),
        )

    return EntitlementDecision(
        allowed=False,
        code="overseas_addon_required",
        message="当前授权不含海外平台增值包，请升级 overseas_addon 或 pro 套餐",
        required_feature=required_feature,
        source=str(status.get("planId") or status.get("mode") or ""),
    )
