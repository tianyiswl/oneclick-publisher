# -*- coding: utf-8 -*-
"""平台数据采集器协议与注册表。"""

from __future__ import annotations

from typing import Protocol

from .platform_data_models import CollectionBatch, CollectionFailure


class PlatformDataCollector(Protocol):
    def collect(self, account: dict) -> CollectionBatch: ...


def collector_for_platform(platform_type: int, **dependencies) -> PlatformDataCollector:
    if type(platform_type) is not int or platform_type != 3:
        raise CollectionFailure("collector_not_available")
    from .douyin_data_collector import DouyinDataCollector

    return DouyinDataCollector(**dependencies)
