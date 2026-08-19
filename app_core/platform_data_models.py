# -*- coding: utf-8 -*-
"""平台数据采集的公开模型与固定错误。"""

from __future__ import annotations

from dataclasses import dataclass
import math


ALLOWED_METRIC_KEYS = frozenset(
    {
        "views",
        "likes",
        "comments",
        "shares",
        "followers_total",
        "followers_net",
        "profile_visits",
    }
)
ALLOWED_METRIC_UNITS = frozenset({"count", "ratio"})
ALLOWED_SOURCE_MODES = frozenset({"direct_session", "browser_signed"})


class CollectionFailure(RuntimeError):
    """只向调用方公开固定错误码。"""

    def __init__(self, error_code: str, *, retryable: bool = False) -> None:
        code = str(error_code or "").strip()
        if not code:
            code = "metric_payload_invalid"
        self.error_code = code
        self.retryable = bool(retryable)
        super().__init__(code)


def _required_text(value: object) -> str:
    if type(value) is not str:
        raise CollectionFailure("metric_payload_invalid")
    result = value.strip()
    if not result:
        raise CollectionFailure("metric_payload_invalid")
    return result


@dataclass(frozen=True, slots=True)
class MetricPoint:
    entity_type: str
    entity_key: str
    metric_key: str
    raw_metric_key: str
    metric_value: int | float
    metric_unit: str
    observed_at: str

    def __post_init__(self) -> None:
        if _required_text(self.entity_type) != "account":
            raise CollectionFailure("metric_payload_invalid")
        _required_text(self.entity_key)
        if _required_text(self.metric_key) not in ALLOWED_METRIC_KEYS:
            raise CollectionFailure("metric_payload_invalid")
        _required_text(self.raw_metric_key)
        if type(self.metric_value) not in (int, float):
            raise CollectionFailure("metric_payload_invalid")
        if not math.isfinite(float(self.metric_value)):
            raise CollectionFailure("metric_payload_invalid")
        if _required_text(self.metric_unit) not in ALLOWED_METRIC_UNITS:
            raise CollectionFailure("metric_payload_invalid")
        _required_text(self.observed_at)


@dataclass(frozen=True, slots=True)
class CollectionBatch:
    platform_type: int
    source_mode: str
    metrics: tuple[MetricPoint, ...]

    def __post_init__(self) -> None:
        if type(self.platform_type) is not int or self.platform_type <= 0:
            raise CollectionFailure("metric_payload_invalid")
        if _required_text(self.source_mode) not in ALLOWED_SOURCE_MODES:
            raise CollectionFailure("metric_payload_invalid")
        if type(self.metrics) is not tuple or not self.metrics:
            raise CollectionFailure("metric_payload_empty")
        if not all(type(point) is MetricPoint for point in self.metrics):
            raise CollectionFailure("metric_payload_invalid")
        identities = {
            (point.entity_type, point.entity_key, point.metric_key)
            for point in self.metrics
        }
        if len(identities) != len(self.metrics):
            raise CollectionFailure("metric_payload_invalid")
