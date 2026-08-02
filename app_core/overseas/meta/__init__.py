# -*- coding: utf-8 -*-
"""Meta 官方授权、资产发现与 Reels 发布能力。"""

from .api import MetaApiClient, MetaApiError
from .browser_policy import (
    META_BROWSER_AUTOMATION_ACKNOWLEDGED,
    META_BROWSER_CONFIRMATION_MESSAGE,
    META_BROWSER_PLATFORM_TYPES,
    META_BROWSER_PUBLISH_CONFIRMED,
    browser_publish_confirmation_valid,
    browser_publish_entitlement_decision,
)
from .capability import FacebookCapability, InstagramCapability
from .credentials import (
    META_REQUIRED_SCOPES,
    MetaBrokerConfig,
    MetaCredentialStore,
    MetaPageAsset,
    MetaTokenRecord,
)
from .oauth import (
    CALLBACK_SECURITY_HEADERS,
    MetaOAuthBrokerClient,
    MetaOAuthBrokerError,
)

__all__ = [
    "META_REQUIRED_SCOPES",
    "CALLBACK_SECURITY_HEADERS",
    "FacebookCapability",
    "InstagramCapability",
    "MetaApiClient",
    "MetaApiError",
    "META_BROWSER_AUTOMATION_ACKNOWLEDGED",
    "META_BROWSER_CONFIRMATION_MESSAGE",
    "META_BROWSER_PLATFORM_TYPES",
    "META_BROWSER_PUBLISH_CONFIRMED",
    "browser_publish_confirmation_valid",
    "browser_publish_entitlement_decision",
    "MetaBrokerConfig",
    "MetaCredentialStore",
    "MetaOAuthBrokerClient",
    "MetaOAuthBrokerError",
    "MetaPageAsset",
    "MetaTokenRecord",
]
