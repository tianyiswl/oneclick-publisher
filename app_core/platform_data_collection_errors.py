# -*- coding: utf-8 -*-
"""平台数据采集器的公共异常协议。"""

from __future__ import annotations

from dataclasses import dataclass

from .platform_data_models import CollectionFailure


_FAILURE_ENDPOINTS = frozenset(
    {"account_home", "account_base", "content_list", "content_detail", "runtime"}
)
_FAILURE_STAGES = frozenset(
    {
        "response_capture", "response_headers", "response_body", "request_binding",
        "json_decode", "account_parse", "content_list_parse", "content_detail_parse",
        "visible_readback", "browser_start", "context_create", "page_create",
        "navigation", "runtime",
    }
)
_FAILURE_REASONS = frozenset(
    {
        "duplicate_response", "invalid_content_length", "body_unavailable",
        "body_read_failed", "body_size_invalid", "request_mismatch", "invalid_json",
        "payload_shape_invalid", "readback_mismatch", "operation_failed",
        "invalid_navigation", "request_limit_exceeded", "response_limit_exceeded",
    }
)

PLATFORM_DATA_COLLECTION_ERROR_CODES = frozenset(
    {
        "account_trends_unavailable",
        "browser_cleanup_incomplete",
        "browser_signature_timeout",
        "collector_not_available",
        "content_list_truncated",
        "content_list_unavailable",
        "content_payload_invalid",
        "direct_request_rejected",
        "login_required",
        "metric_payload_empty",
        "metric_payload_invalid",
        "session_state_missing",
        "sync_persist_failed",
        "validation_readback_mismatch",
        "verification_required",
    }
)

# 页面只能使用这组固定文案，绝不拼接浏览器、网络或平台异常原文。
PUBLIC_PLATFORM_DATA_ERROR_TEXT = {
    "account_trends_unavailable": "平台暂未返回账号数据",
    "browser_cleanup_incomplete": "浏览器会话未能完整关闭",
    "browser_signature_timeout": "平台数据读取超时",
    "collector_not_available": "当前平台暂不支持数据同步",
    "content_list_truncated": "作品列表未完整取得",
    "content_list_unavailable": "平台暂未返回作品数据",
    "content_payload_invalid": "平台页面或数据接口已变化",
    "direct_request_rejected": "平台暂不支持当前读取方式",
    "metric_payload_empty": "平台暂未返回可用数据",
    "metric_payload_invalid": "平台页面或数据接口已变化",
    "session_state_missing": "未找到可用登录状态",
    "sync_persist_failed": "本地数据保存失败",
    "validation_readback_mismatch": "本地数据读回失败",
}


def public_failure_diagnostic(value: object) -> dict | None:
    """只接受内部固定枚举，避免带出平台正文或异常原文。"""

    if type(value) is not dict or set(value) != {"endpoint", "stage", "reason"}:
        return None
    endpoint = value.get("endpoint")
    stage = value.get("stage")
    reason = value.get("reason")
    if (
        type(endpoint) is not str or endpoint not in _FAILURE_ENDPOINTS
        or type(stage) is not str or stage not in _FAILURE_STAGES
        or type(reason) is not str or reason not in _FAILURE_REASONS
    ):
        return None
    return {"endpoint": endpoint, "stage": stage, "reason": reason}


@dataclass(frozen=True, slots=True)
class CleanupReceipt:
    """仅公开资源是否关闭及未关闭资源数的受控回执。"""

    closed: bool
    alive_resource_count: int

    def __post_init__(self) -> None:
        if type(self.closed) is not bool:
            raise CollectionFailure("metric_payload_invalid")
        if type(self.alive_resource_count) is not int or self.alive_resource_count < 0:
            raise CollectionFailure("metric_payload_invalid")
        if self.closed != (self.alive_resource_count == 0):
            raise CollectionFailure("metric_payload_invalid")

    def public_payload(self) -> dict:
        return {
            "closed": self.closed,
            "aliveResourceCount": self.alive_resource_count,
        }


class PlatformDataCollectionError(CollectionFailure):
    """允许采集编排层读取回退语义的固定错误。"""

    def __init__(
        self,
        error_code: str,
        *,
        fallback_allowed: bool = False,
        retryable: bool = False,
        cleanup_receipt: CleanupReceipt | None = None,
        failure_diagnostic: dict | None = None,
    ) -> None:
        if error_code not in PLATFORM_DATA_COLLECTION_ERROR_CODES:
            error_code = "metric_payload_invalid"
        self.fallback_allowed = (
            type(fallback_allowed) is bool and fallback_allowed
        )
        self.cleanup_receipt = (
            cleanup_receipt if type(cleanup_receipt) is CleanupReceipt else None
        )
        self.failure_diagnostic = public_failure_diagnostic(failure_diagnostic)
        super().__init__(error_code, retryable=retryable)
