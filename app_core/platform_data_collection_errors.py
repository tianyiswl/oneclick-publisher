# -*- coding: utf-8 -*-
"""平台数据采集器的公共异常协议。"""

from __future__ import annotations

from dataclasses import dataclass

from .platform_data_models import CollectionFailure


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
    ) -> None:
        self.fallback_allowed = (
            type(fallback_allowed) is bool and fallback_allowed
        )
        self.cleanup_receipt = (
            cleanup_receipt if type(cleanup_receipt) is CleanupReceipt else None
        )
        super().__init__(error_code, retryable=retryable)
