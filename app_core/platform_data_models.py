# -*- coding: utf-8 -*-
"""平台数据采集的公开模型与固定错误。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
        "favorites",
        "downloads",
    }
)
ALLOWED_METRIC_UNITS = frozenset({"count", "ratio"})
ALLOWED_SOURCE_MODES = frozenset({"direct_session", "browser_signed"})
ALLOWED_ENTITY_TYPES = frozenset({"account", "content"})
ALLOWED_METRIC_SCOPES = frozenset({"daily_increment", "lifetime_total"})
ALLOWED_CONTENT_STATUSES = frozenset(
    {"published", "scheduled", "private", "unavailable"}
)
ALLOWED_CONTENT_TYPES = frozenset({"video", "image"})


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


def _controlled_text(value: object) -> str:
    result = _required_text(value)
    if value != result:
        raise CollectionFailure("metric_payload_invalid")
    return result


def _date_only(value: object) -> str:
    result = _controlled_text(value)
    try:
        parsed = datetime.strptime(result, "%Y-%m-%d")
    except ValueError as error:
        raise CollectionFailure("metric_payload_invalid") from error
    if parsed.strftime("%Y-%m-%d") != result:
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
    metric_scope: str
    period_start: str
    period_end: str
    observed_at: str

    def __post_init__(self) -> None:
        entity_type = _controlled_text(self.entity_type)
        if entity_type not in ALLOWED_ENTITY_TYPES:
            raise CollectionFailure("metric_payload_invalid")
        _required_text(self.entity_key)
        metric_key = _controlled_text(self.metric_key)
        if metric_key not in ALLOWED_METRIC_KEYS:
            raise CollectionFailure("metric_payload_invalid")
        _required_text(self.raw_metric_key)
        if type(self.metric_value) not in (int, float):
            raise CollectionFailure("metric_payload_invalid")
        if not math.isfinite(float(self.metric_value)):
            raise CollectionFailure("metric_payload_invalid")
        if _controlled_text(self.metric_unit) not in ALLOWED_METRIC_UNITS:
            raise CollectionFailure("metric_payload_invalid")
        metric_scope = _controlled_text(self.metric_scope)
        if metric_scope not in ALLOWED_METRIC_SCOPES:
            raise CollectionFailure("metric_payload_invalid")
        period_start = _date_only(self.period_start)
        period_end = _date_only(self.period_end)
        if period_start > period_end:
            raise CollectionFailure("metric_payload_invalid")
        if entity_type == "account" and not (
            metric_scope == "daily_increment"
            or (
                metric_key == "followers_total"
                and metric_scope == "lifetime_total"
            )
        ):
            raise CollectionFailure("metric_payload_invalid")
        if entity_type == "content" and metric_scope != "lifetime_total":
            raise CollectionFailure("metric_payload_invalid")
        _required_text(self.observed_at)


@dataclass(frozen=True, slots=True)
class ContentRecord:
    content_id: str
    title: str
    cover_url: str
    published_at: str
    content_status: str
    content_type: str

    def __post_init__(self) -> None:
        _required_text(self.content_id)
        _required_text(self.title)
        _required_text(self.cover_url)
        _required_text(self.published_at)
        if _controlled_text(self.content_status) not in ALLOWED_CONTENT_STATUSES:
            raise CollectionFailure("metric_payload_invalid")
        if _controlled_text(self.content_type) not in ALLOWED_CONTENT_TYPES:
            raise CollectionFailure("metric_payload_invalid")


@dataclass(frozen=True, slots=True)
class CollectionBatch:
    platform_type: int
    source_mode: str
    metrics: tuple[MetricPoint, ...]
    contents: tuple[ContentRecord, ...]
    account_metrics_available: bool
    content_data_available: bool
    platform_observed_at: str
    warning_code: str = ""

    def __post_init__(self) -> None:
        if type(self.platform_type) is not int or self.platform_type <= 0:
            raise CollectionFailure("metric_payload_invalid")
        if _controlled_text(self.source_mode) not in ALLOWED_SOURCE_MODES:
            raise CollectionFailure("metric_payload_invalid")
        if type(self.metrics) is not tuple:
            raise CollectionFailure("metric_payload_invalid")
        if type(self.account_metrics_available) is not bool:
            raise CollectionFailure("metric_payload_invalid")
        if self.account_metrics_available and not self.metrics:
            raise CollectionFailure("metric_payload_empty")
        if not all(type(point) is MetricPoint for point in self.metrics):
            raise CollectionFailure("metric_payload_invalid")
        if type(self.contents) is not tuple:
            raise CollectionFailure("metric_payload_invalid")
        if type(self.content_data_available) is not bool:
            raise CollectionFailure("metric_payload_invalid")
        if self.content_data_available and not self.contents:
            raise CollectionFailure("metric_payload_empty")
        if not all(type(content) is ContentRecord for content in self.contents):
            raise CollectionFailure("metric_payload_invalid")
        _required_text(self.platform_observed_at)
        if type(self.warning_code) is not str:
            raise CollectionFailure("metric_payload_invalid")
        identities = {
            (
                point.entity_type,
                point.entity_key,
                point.metric_key,
                point.metric_scope,
                point.period_start,
                point.period_end,
            )
            for point in self.metrics
        }
        if len(identities) != len(self.metrics):
            raise CollectionFailure("metric_payload_invalid")
