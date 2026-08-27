# -*- coding: utf-8 -*-
"""微信公众号已登录会话的受限只读数据采集器。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .domestic_data_collector import (
    CapturedJson,
    DomesticBrowserCollector,
    DomesticCollectorConfig,
)
from .platform_data_models import (
    CollectionBatch,
    CollectionFailure,
    ContentRecord,
    MetricPoint,
)


_WECHAT_HOST = "mp.weixin.qq.com"
_BEIJING = ZoneInfo("Asia/Shanghai")
_CONTENT_QUERY_KEYS = frozenset(
    {
        "action",
        "ajax",
        "article_source",
        "begin_timestamp",
        "count",
        "end_timestamp",
        "f",
        "fingerprint",
        "lang",
        "offset",
        "token",
    }
)


def _next_content_page_url(
    current_url: str, payload: Mapping[str, object]
) -> str | None:
    """只使用公众号已返回的游标翻页，不读取或记录登录凭据。"""

    if type(current_url) is not str or not isinstance(payload, Mapping):
        _invalid()
    try:
        parsed = urlsplit(current_url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (ValueError, TypeError):
        _invalid()
    keys = [key for key, _value in pairs]
    if (
        parsed.scheme != "https"
        or parsed.hostname != _WECHAT_HOST
        or parsed.path != "/misc/appmsganalysis"
        or parsed.fragment
        or frozenset(keys) != _CONTENT_QUERY_KEYS
        or len(keys) != len(_CONTENT_QUERY_KEYS)
    ):
        _invalid()
    values = dict(pairs)
    current_offset = values.get("offset", "")
    if not current_offset.isascii() or not current_offset.isdigit():
        _invalid()
    next_offset = payload.get("next_offset")
    if next_offset is None or next_offset == 0:
        return None
    if type(next_offset) is not int or next_offset < 0:
        _invalid()
    if next_offset <= int(current_offset):
        return None
    updated = [
        (key, str(next_offset) if key == "offset" else value)
        for key, value in pairs
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(updated), "")
    )

WECHAT_CONFIG = DomesticCollectorConfig(
    platform_type=10,
    home_url="https://mp.weixin.qq.com/",
    allowed_hosts=frozenset({_WECHAT_HOST}),
    endpoint_by_path={
        "/misc/useranalysis": "account_base",
        "/misc/appmsganalysis": "content_list",
    },
    phase_by_path={
        "/misc/useranalysis": "account",
        "/misc/appmsganalysis": "content",
    },
    navigation_by_phase={
        "bootstrap": "https://mp.weixin.qq.com/",
        "account": "/misc/useranalysis",
        "content": "/misc/appmsganalysis",
    },
    pagination_phase="content",
    pagination_url_builder=_next_content_page_url,
    pagination_max_pages=20,
)


def _invalid() -> None:
    raise CollectionFailure("metric_payload_invalid") from None


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _invalid()
    return value


def _sequence(value: object) -> tuple:
    if type(value) is not tuple:
        _invalid()
    return value


def _integer(value: object, *, nonnegative: bool = True) -> int:
    if type(value) is not int or (nonnegative and value < 0):
        _invalid()
    return value


def _required_text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        _invalid()
    return value


def _beijing_day(value: object) -> str:
    result = _required_text(value)
    formats = ()
    if len(result) == 8 and result.isascii() and result.isdigit():
        formats = ("%Y%m%d",)
    elif len(result) == 10 and result[4] == "/" and result[7] == "/":
        formats = ("%Y/%m/%d",)
    if formats:
        try:
            parsed = datetime.strptime(result, formats[0]).date()
        except ValueError:
            _invalid()
        if parsed.strftime(formats[0]) != result:
            _invalid()
        return parsed.isoformat()
    try:
        parsed = date.fromisoformat(result)
    except ValueError:
        _invalid()
    if parsed.isoformat() != result:
        _invalid()
    return result


def _observed_at(payload: Mapping[str, object]) -> str:
    base = _mapping(payload.get("base_resp"))
    if _integer(base.get("ret"), nonnegative=False) != 0:
        _invalid()
    timestamp = _integer(base.get("svr_time"))
    try:
        return datetime.fromtimestamp(timestamp, tz=_BEIJING).isoformat()
    except (OverflowError, OSError, ValueError):
        _invalid()


def _observed_beijing_day(observed_at: str) -> str:
    try:
        parsed = datetime.fromisoformat(observed_at)
    except ValueError:
        _invalid()
    if parsed.tzinfo is None:
        _invalid()
    return parsed.astimezone(_BEIJING).date().isoformat()


def _successful(payload: Mapping[str, object]) -> None:
    base = _mapping(payload.get("base_resp"))
    if _integer(base.get("ret"), nonnegative=False) != 0:
        _invalid()


def _account_points(
    payload: Mapping[str, object], account_id: int
) -> tuple[tuple[MetricPoint, ...], str, str]:
    observed_at = _observed_at(payload)
    categories = _sequence(payload.get("category_list"))
    if len(categories) != 1:
        _invalid()
    rows = _sequence(_mapping(categories[0]).get("list"))
    if not rows:
        _invalid()
    points: list[MetricPoint] = []
    days: set[str] = set()
    for raw_row in rows:
        row = _mapping(raw_row)
        day = _beijing_day(row.get("date"))
        if day in days:
            _invalid()
        days.add(day)
        total = _integer(row.get("cumulate_user"))
        net = _integer(row.get("netgain_user"), nonnegative=False)
        points.extend(
            (
                MetricPoint(
                    entity_type="account",
                    entity_key=f"account:{account_id}",
                    metric_key="followers_total",
                    raw_metric_key="cumulate_user",
                    metric_value=total,
                    metric_unit="count",
                    metric_scope="lifetime_total",
                    period_start=day,
                    period_end=day,
                    observed_at=observed_at,
                ),
                MetricPoint(
                    entity_type="account",
                    entity_key=f"account:{account_id}",
                    metric_key="followers_net",
                    raw_metric_key="netgain_user",
                    metric_value=net,
                    metric_unit="count",
                    metric_scope="daily_increment",
                    period_start=day,
                    period_end=day,
                    observed_at=observed_at,
                ),
            )
        )
    return tuple(points), observed_at, max(days)


def _content_rows(
    captures: tuple[CapturedJson, ...], observed_at: str, observed_day: str
) -> tuple[tuple[ContentRecord, ...], tuple[MetricPoint, ...], bool]:
    contents: list[ContentRecord] = []
    points: list[MetricPoint] = []
    records_by_id: dict[str, tuple[ContentRecord, MetricPoint]] = {}
    truncated = False
    saw_content_contract = False
    for capture in captures:
        if (
            capture.endpoint != "content_list"
            or capture.phase != "content"
            or capture.path != "/misc/appmsganalysis"
        ):
            continue
        payload = _mapping(capture.payload)
        _successful(payload)
        article_list = payload.get("article_list")
        if article_list is None:
            continue
        rows = _sequence(article_list)
        if not rows:
            continue
        saw_content_contract = True
        next_offset = _integer(payload.get("next_offset"), nonnegative=False)
        truncated = next_offset > 0
        for raw_row in rows:
            row = _mapping(raw_row)
            message_id = _integer(row.get("msg_id"))
            item_index = _integer(row.get("item_idx"))
            content_id = f"{message_id}:{item_index}"
            title = _required_text(row.get("title"))
            published_day = _beijing_day(row.get("ref_date"))
            views = _integer(row.get("total_read_uv"))
            content = ContentRecord(
                content_id=content_id,
                title=title,
                cover_url="",
                published_at=published_day,
                content_status="unavailable",
                content_type="unavailable",
            )
            point = MetricPoint(
                entity_type="content",
                entity_key=content_id,
                metric_key="views",
                raw_metric_key="total_read_uv",
                metric_value=views,
                metric_unit="count",
                metric_scope="lifetime_total",
                period_start=observed_day,
                period_end=observed_day,
                observed_at=observed_at,
            )
            existing = records_by_id.get(content_id)
            if existing is not None:
                if existing != (content, point):
                    _invalid()
                continue
            records_by_id[content_id] = (content, point)
            contents.append(content)
            points.append(point)
    if not saw_content_contract:
        return (), (), False
    return tuple(contents), tuple(points), truncated


def parse_captures(
    account_id: int, captures: tuple[CapturedJson, ...]
) -> CollectionBatch:
    if type(account_id) is not int or account_id <= 0 or type(captures) is not tuple:
        _invalid()
    account_payloads = [
        capture.payload
        for capture in captures
        if capture.endpoint == "account_base"
        and capture.phase == "account"
        and capture.path == "/misc/useranalysis"
        and "category_list" in capture.payload
    ]
    if not account_payloads:
        _invalid()
    completed_payloads: list[Mapping[str, object]] = []
    for payload in account_payloads:
        checked = _mapping(payload)
        load_done = _integer(checked.get("load_done"), nonnegative=False)
        categories = _sequence(checked.get("category_list"))
        if load_done == 1 and len(categories) == 1:
            completed_payloads.append(checked)
    if not completed_payloads:
        _invalid()
    parsed_accounts = tuple(
        _account_points(payload, account_id) for payload in completed_payloads
    )
    observed_at = max(parsed[1] for parsed in parsed_accounts)
    observed_day = _observed_beijing_day(observed_at)
    merged_points: dict[tuple[str, str, str, str, str], MetricPoint] = {}
    for points, _payload_observed_at, _payload_day in parsed_accounts:
        for point in points:
            identity = (
                point.entity_type,
                point.entity_key,
                point.metric_key,
                point.metric_scope,
                point.period_start,
            )
            existing = merged_points.get(identity)
            normalized = replace(point, observed_at=observed_at)
            if existing is not None and existing != normalized:
                _invalid()
            merged_points[identity] = normalized
    account_points = tuple(merged_points.values())
    contents, content_points, truncated = _content_rows(
        captures, observed_at, observed_day
    )
    content_available = bool(contents)
    warning_code = (
        "content_list_truncated"
        if content_available and truncated
        else "" if content_available else "content_list_unavailable"
    )
    return CollectionBatch(
        platform_type=10,
        source_mode="browser_signed",
        metrics=account_points + content_points,
        contents=contents,
        account_metrics_available=True,
        content_data_available=content_available,
        platform_observed_at=observed_at,
        warning_code=warning_code,
    )


class WechatDataCollector:
    def __init__(self, *, browser_factory=None) -> None:
        dependencies = {}
        if browser_factory is not None:
            dependencies["browser_factory"] = browser_factory
        self._collector = DomesticBrowserCollector(
            WECHAT_CONFIG,
            parse_captures=parse_captures,
            **dependencies,
        )

    @staticmethod
    def parse_captures(
        account_id: int, captures: tuple[CapturedJson, ...]
    ) -> CollectionBatch:
        return parse_captures(account_id, captures)

    def collect(self, account: dict) -> CollectionBatch:
        return self._collector.collect(account)
