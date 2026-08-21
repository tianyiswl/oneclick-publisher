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
        self.fallback_allowed = (
            type(fallback_allowed) is bool and fallback_allowed
        )
        self.cleanup_receipt = (
            cleanup_receipt if type(cleanup_receipt) is CleanupReceipt else None
        )
        self.failure_diagnostic = public_failure_diagnostic(failure_diagnostic)
        super().__init__(error_code, retryable=retryable)
