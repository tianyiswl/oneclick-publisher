# -*- coding: utf-8 -*-
"""平台数据采集器的公共异常协议。"""

from __future__ import annotations

from .platform_data_models import CollectionFailure


class PlatformDataCollectionError(CollectionFailure):
    """允许采集编排层读取回退语义的固定错误。"""

    def __init__(
        self,
        error_code: str,
        *,
        fallback_allowed: bool = False,
        retryable: bool = False,
    ) -> None:
        self.fallback_allowed = (
            type(fallback_allowed) is bool and fallback_allowed
        )
        super().__init__(error_code, retryable=retryable)
