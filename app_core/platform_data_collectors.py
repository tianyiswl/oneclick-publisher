# -*- coding: utf-8 -*-
"""平台数据采集器协议与注册表。"""

from __future__ import annotations

from typing import Protocol

from .platform_data_models import CollectionBatch, CollectionFailure


class PlatformDataCollector(Protocol):
    def collect(self, account: dict) -> CollectionBatch: ...


def _douyin_factory(**dependencies) -> PlatformDataCollector:
    from .douyin_data_collector import DouyinDataCollector

    return DouyinDataCollector(**dependencies)


_COLLECTOR_FACTORIES = {3: _douyin_factory}


def registered_platform_types() -> tuple[int, ...]:
    return tuple(sorted(_COLLECTOR_FACTORIES))


def collector_for_platform(platform_type: int, **dependencies) -> PlatformDataCollector:
    if type(platform_type) is not int:
        raise CollectionFailure("collector_not_available") from None
    factory = _COLLECTOR_FACTORIES.get(platform_type)
    if factory is None:
        raise CollectionFailure("collector_not_available") from None

    return factory(**dependencies)
