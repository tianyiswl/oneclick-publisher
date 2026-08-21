# -*- coding: utf-8 -*-
"""小红书已审核官方响应的最小、纯 payload 转换器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
import re

from .platform_data_models import CollectionFailure, ContentRecord, MetricPoint


_CONTENT_ID = re.compile(r"^[0-9a-f]{24}$")
_CONTENT_LIMIT = 50
_CONTENT_METRICS = {
    "view_count": "views",
    "like_count": "likes",
    "comment_count": "comments",
    "share_count": "shares",
    "collect_count": "favorites",
}
_NOTE_INFO_METRICS = ("view_count", "like_count", "comment_count")
_DATA_METRICS = ("share_count", "collect_count")


def _invalid() -> None:
    raise CollectionFailure("metric_payload_invalid") from None


def _truncated() -> None:
    raise CollectionFailure("content_list_truncated") from None


def _mapping(value: object) -> dict:
    if type(value) is not dict:
        _invalid()
    return value


def _list(value: object) -> list:
    if type(value) is not list:
        _invalid()
    return value


def _required_text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        _invalid()
    return value


def _platform_day(value: object) -> str:
    result = _required_text(value)
    try:
        parsed = date.fromisoformat(result)
    except ValueError:
        _invalid()
    if parsed.isoformat() != result:
        _invalid()
    return result


def _metric_value(value: object) -> int | float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        _invalid()
    return value


@dataclass(frozen=True, slots=True)
class XhsContentIdentity:
    """已证实的作品 ID；其余元数据在本层保持不可得。"""

    content_id: str

    def __post_init__(self) -> None:
        if type(self.content_id) is not str or _CONTENT_ID.fullmatch(self.content_id) is None:
            _invalid()


def parse_account_overview(
    payload: object,
    account_id: int,
    observed_at: str,
    platform_day: str,
) -> tuple[MetricPoint, ...]:
    """只将已审核的 ``data.fans_count`` 转成指定主体的粉丝累计值。"""

    if type(account_id) is not int or account_id <= 0:
        _invalid()
    data = _mapping(_mapping(payload).get("data"))
    value = _metric_value(data.get("fans_count"))
    day = _platform_day(platform_day)
    observed = _required_text(observed_at)
    try:
        point = MetricPoint(
            entity_type="account",
            entity_key=f"account:{account_id}",
            metric_key="followers_total",
            raw_metric_key="fans_count",
            metric_value=value,
            metric_unit="count",
            metric_scope="lifetime_total",
            period_start=day,
            period_end=day,
            observed_at=observed,
        )
    except CollectionFailure:
        _invalid()
    return (point,)


def parse_content_list(payload: object) -> tuple[XhsContentIdentity, ...]:
    """只读取已审核列表容器中的精确作品 ID，并拒绝不完整列表。"""

    data = _mapping(_mapping(payload).get("data"))
    note_infos = _list(data.get("note_infos"))
    total = data.get("total")
    if type(total) is not int or total < 0:
        _invalid()
    if len(note_infos) > _CONTENT_LIMIT or total > _CONTENT_LIMIT or total != len(note_infos):
        _truncated()

    identities: list[XhsContentIdentity] = []
    content_ids: set[str] = set()
    for note_info in note_infos:
        identity = XhsContentIdentity(_mapping(note_info).get("id"))
        if identity.content_id in content_ids:
            _invalid()
        content_ids.add(identity.content_id)
        identities.append(identity)
    return tuple(identities)


def parse_content_lifetime(
    payload: object,
    identity: XhsContentIdentity,
    observed_at: str,
    platform_day: str,
) -> tuple[ContentRecord, tuple[MetricPoint, ...]]:
    """映射经审核的作品累计字段，保持其文本元数据不可得。"""

    if type(identity) is not XhsContentIdentity:
        _invalid()
    data = _mapping(_mapping(payload).get("data"))
    note_info = _mapping(data.get("note_info"))
    response_identity = XhsContentIdentity(note_info.get("id"))
    if response_identity != identity:
        _invalid()
    day = _platform_day(platform_day)
    observed = _required_text(observed_at)

    raw_values: list[tuple[str, int | float]] = []
    for raw_key in _NOTE_INFO_METRICS:
        if raw_key in note_info:
            raw_values.append((raw_key, _metric_value(note_info[raw_key])))
    for raw_key in _DATA_METRICS:
        if raw_key in data:
            raw_values.append((raw_key, _metric_value(data[raw_key])))
    if not raw_values:
        _invalid()

    try:
        content = ContentRecord(
            content_id=identity.content_id,
            title="",
            cover_url="",
            published_at="",
            content_status="unavailable",
            content_type="unavailable",
        )
        points = tuple(
            MetricPoint(
                entity_type="content",
                entity_key=identity.content_id,
                metric_key=_CONTENT_METRICS[raw_key],
                raw_metric_key=raw_key,
                metric_value=value,
                metric_unit="count",
                metric_scope="lifetime_total",
                period_start=day,
                period_end=day,
                observed_at=observed,
            )
            for raw_key, value in raw_values
        )
    except CollectionFailure:
        _invalid()
    return content, points
