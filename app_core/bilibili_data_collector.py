# -*- coding: utf-8 -*-
"""B站创作中心已登录会话的受限只读数据采集器。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .domestic_data_collector import (
    CapturedJson,
    DomesticBrowserCollector,
    DomesticCollectorConfig,
)
from .platform_data_collection_errors import PlatformDataCollectionError
from .platform_data_models import (
    CollectionBatch,
    CollectionFailure,
    ContentRecord,
    MetricPoint,
)


_MEMBER_HOST = "member.bilibili.com"
_API_HOST = "api.bilibili.com"
_HOME_PAGE = "https://member.bilibili.com/platform/home"
_SUBMISSION_PAGE = "https://member.bilibili.com/platform/upload-manager/article"
_ACCOUNT_PHASE = "account"
_SUBMISSION_PHASE = "submission"
_IDENTITY_PATH = "/x/web-interface/nav"
_ACCOUNT_PATH = "/x/web/data/index/stat"
_ARCHIVES_PATH = "/x/web/archives"
_BEIJING = ZoneInfo("Asia/Shanghai")
_CONTENT_METRICS = {
    "view": "views",
    "like": "likes",
    "reply": "comments",
    "favorite": "favorites",
    "share": "shares",
}


BILIBILI_ACCOUNT_CONFIG = DomesticCollectorConfig(
    platform_type=5,
    home_url=_HOME_PAGE,
    allowed_hosts=frozenset({_MEMBER_HOST, _API_HOST}),
    endpoint_by_path={
        _IDENTITY_PATH: "account_home",
        _ACCOUNT_PATH: "account_base",
    },
    phase_by_path={
        _IDENTITY_PATH: _ACCOUNT_PHASE,
        _ACCOUNT_PATH: _ACCOUNT_PHASE,
    },
    phase_settle_milliseconds=4_000,
)

BILIBILI_CONTENT_CONFIG = DomesticCollectorConfig(
    platform_type=5,
    home_url=_HOME_PAGE,
    allowed_hosts=frozenset({_MEMBER_HOST}),
    endpoint_by_path={_ARCHIVES_PATH: "content_list"},
    phase_by_path={_ARCHIVES_PATH: _SUBMISSION_PHASE},
    navigation_by_phase={
        "bootstrap": _HOME_PAGE,
        _SUBMISSION_PHASE: _SUBMISSION_PAGE,
    },
    phase_settle_milliseconds=4_000,
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


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        _invalid()
    return value


def _required_text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        _invalid()
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _observation() -> tuple[str, str]:
    observed = _utc_now()
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        _invalid()
    normalized = observed.astimezone(timezone.utc)
    return (
        normalized.isoformat(timespec="seconds"),
        normalized.astimezone(_BEIJING).date().isoformat(),
    )


def _timestamp(value: object) -> tuple[str, str]:
    timestamp = _integer(value)
    try:
        parsed = datetime.fromtimestamp(timestamp, tz=_BEIJING)
    except (OverflowError, OSError, ValueError):
        _invalid()
    return parsed.isoformat(timespec="seconds"), parsed.date().isoformat()


def _successful(payload: Mapping[str, object]) -> Mapping[str, object]:
    if _integer(payload.get("code")) != 0:
        _invalid()
    return _mapping(payload.get("data"))


def _matching(
    captures: tuple[CapturedJson, ...], endpoint: str, path: str, phase: str
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        capture.payload
        for capture in captures
        if capture.endpoint == endpoint
        and capture.phase == phase
        and capture.path == path
    )


def _require_identity(captures: tuple[CapturedJson, ...], *, phase: str) -> None:
    payloads = _matching(captures, "account_home", _IDENTITY_PATH, phase)
    if not payloads:
        _invalid()
    for payload in payloads:
        data = _successful(payload)
        if data.get("isLogin") is not True:
            _invalid()
        _integer(data.get("mid"))


def _followers_total(captures: tuple[CapturedJson, ...], *, phase: str) -> int:
    payloads = _matching(captures, "account_base", _ACCOUNT_PATH, phase)
    if not payloads:
        _invalid()
    totals = {_integer(_successful(payload).get("total_fans")) for payload in payloads}
    if len(totals) != 1:
        _invalid()
    return totals.pop()


def _content_record(
    raw_item: object,
    *,
    observed_at: str,
    observed_day: str,
) -> tuple[ContentRecord, tuple[MetricPoint, ...]]:
    item = _mapping(raw_item)
    archive = _mapping(item.get("Archive"))
    stat = _mapping(item.get("stat"))
    content_id = _required_text(archive.get("bvid"))
    title = _required_text(archive.get("title"))
    published_at, published_day = _timestamp(archive.get("ptime"))
    if published_day > observed_day:
        _invalid()
    aid = _integer(archive.get("aid"))
    if _integer(stat.get("aid")) != aid:
        _invalid()

    # 封面域名尚未进入公开服务白名单，不将原始 URL 带出采集层。
    content = ContentRecord(
        content_id=content_id,
        title=title,
        cover_url="",
        published_at=published_at,
        content_status="unavailable",
        content_type="video",
    )
    points: list[MetricPoint] = []
    for raw_key, metric_key in _CONTENT_METRICS.items():
        if raw_key not in stat:
            continue
        points.append(
            MetricPoint(
                entity_type="content",
                entity_key=content_id,
                metric_key=metric_key,
                raw_metric_key=raw_key,
                metric_value=_integer(stat.get(raw_key)),
                metric_unit="count",
                metric_scope="lifetime_total",
                period_start=published_day,
                period_end=observed_day,
                observed_at=observed_at,
            )
        )
    if not points:
        _invalid()
    return content, tuple(points)


def _contents(
    captures: tuple[CapturedJson, ...],
    *,
    observed_at: str,
    observed_day: str,
) -> tuple[tuple[ContentRecord, ...], tuple[MetricPoint, ...], bool, bool]:
    payloads = _matching(
        captures, "content_list", _ARCHIVES_PATH, _SUBMISSION_PHASE
    )
    if not payloads:
        return (), (), False, False

    records: dict[str, tuple[ContentRecord, tuple[MetricPoint, ...]]] = {}
    pagination: tuple[int, int, int] | None = None
    for payload in payloads:
        data = _successful(payload)
        page = _mapping(data.get("page"))
        count = _integer(page.get("count"))
        page_number = _integer(page.get("pn"))
        page_size = _integer(page.get("ps"))
        if page_number != 1 or page_size <= 0:
            _invalid()
        current_pagination = (count, page_number, page_size)
        if pagination is not None and pagination != current_pagination:
            _invalid()
        pagination = current_pagination
        rows = _sequence(data.get("arc_audits"))
        if len(rows) > page_size:
            _invalid()
        for raw_item in rows:
            candidate = _content_record(
                raw_item,
                observed_at=observed_at,
                observed_day=observed_day,
            )
            content_id = candidate[0].content_id
            existing = records.get(content_id)
            if existing is not None and existing != candidate:
                _invalid()
            records[content_id] = candidate

    ordered = tuple(records.values())
    contents = tuple(item[0] for item in ordered)
    points = tuple(point for item in ordered for point in item[1])
    expected_count = pagination[0] if pagination is not None else 0
    return contents, points, True, expected_count > len(contents)


def parse_captures(
    account_id: int, captures: tuple[CapturedJson, ...]
) -> CollectionBatch:
    if type(account_id) is not int or account_id <= 0 or type(captures) is not tuple:
        _invalid()
    _require_identity(captures, phase=_SUBMISSION_PHASE)
    followers_total = _followers_total(captures, phase=_SUBMISSION_PHASE)
    observed_at, observed_day = _observation()
    contents, content_points, content_available, truncated = _contents(
        captures,
        observed_at=observed_at,
        observed_day=observed_day,
    )
    account_point = MetricPoint(
        entity_type="account",
        entity_key=f"account:{account_id}",
        metric_key="followers_total",
        raw_metric_key="total_fans",
        metric_value=followers_total,
        metric_unit="count",
        metric_scope="lifetime_total",
        period_start=observed_day,
        period_end=observed_day,
        observed_at=observed_at,
    )
    warning = (
        "content_list_truncated"
        if truncated
        else "" if content_available else "content_list_unavailable"
    )
    return CollectionBatch(
        platform_type=5,
        source_mode="browser_signed",
        metrics=(account_point,) + content_points,
        contents=contents,
        account_metrics_available=True,
        content_data_available=content_available,
        platform_observed_at=observed_at,
        warning_code=warning,
    )


def _account_only_captures(
    account_id: int, captures: tuple[CapturedJson, ...]
) -> CollectionBatch:
    if type(account_id) is not int or account_id <= 0 or type(captures) is not tuple:
        _invalid()
    _require_identity(captures, phase=_ACCOUNT_PHASE)
    followers_total = _followers_total(captures, phase=_ACCOUNT_PHASE)
    observed_at, observed_day = _observation()
    return CollectionBatch(
        platform_type=5,
        source_mode="browser_signed",
        metrics=(
            MetricPoint(
                entity_type="account",
                entity_key=f"account:{account_id}",
                metric_key="followers_total",
                raw_metric_key="total_fans",
                metric_value=followers_total,
                metric_unit="count",
                metric_scope="lifetime_total",
                period_start=observed_day,
                period_end=observed_day,
                observed_at=observed_at,
            ),
        ),
        contents=(),
        account_metrics_available=True,
        content_data_available=False,
        platform_observed_at=observed_at,
        warning_code="content_list_unavailable",
    )


def _followers_from_batch(batch: CollectionBatch) -> int:
    values = {
        _integer(point.metric_value)
        for point in batch.metrics
        if point.entity_type == "account"
        and point.metric_key == "followers_total"
    }
    if len(values) != 1:
        _invalid()
    return values.pop()


def _content_captures(
    account_id: int,
    captures: tuple[CapturedJson, ...],
    *,
    account_batch: CollectionBatch,
) -> CollectionBatch:
    if type(account_id) is not int or account_id <= 0 or type(captures) is not tuple:
        _invalid()
    followers_total = _followers_from_batch(account_batch)
    observed_at, observed_day = _observation()
    contents, content_points, content_available, truncated = _contents(
        captures,
        observed_at=observed_at,
        observed_day=observed_day,
    )
    if not content_available:
        _invalid()
    account_point = MetricPoint(
        entity_type="account",
        entity_key=f"account:{account_id}",
        metric_key="followers_total",
        raw_metric_key="total_fans",
        metric_value=followers_total,
        metric_unit="count",
        metric_scope="lifetime_total",
        period_start=observed_day,
        period_end=observed_day,
        observed_at=observed_at,
    )
    return CollectionBatch(
        platform_type=5,
        source_mode="browser_signed",
        metrics=(account_point,) + content_points,
        contents=contents,
        account_metrics_available=True,
        content_data_available=True,
        platform_observed_at=observed_at,
        warning_code="content_list_truncated" if truncated else "",
    )


class BilibiliDataCollector:
    """只接受 B站创作中心已审核稿件页响应的采集器。"""

    parse_captures = staticmethod(parse_captures)

    def __init__(
        self,
        *,
        browser_factory=None,
        monotonic=None,
        domestic_collector_factory=DomesticBrowserCollector,
    ) -> None:
        dependencies = {}
        if browser_factory is not None:
            dependencies["browser_factory"] = browser_factory
        if monotonic is not None:
            dependencies["monotonic"] = monotonic
        self._domestic_collector_factory = domestic_collector_factory
        self._dependencies = dependencies

    def collect(self, account: dict) -> CollectionBatch:
        account_collector = self._domestic_collector_factory(
            BILIBILI_ACCOUNT_CONFIG,
            parse_captures=_account_only_captures,
            **self._dependencies,
        )
        account_batch = account_collector.collect(account)

        def content_parser(
            account_id: int, captures: tuple[CapturedJson, ...]
        ) -> CollectionBatch:
            return _content_captures(
                account_id,
                captures,
                account_batch=account_batch,
            )

        content_collector = self._domestic_collector_factory(
            BILIBILI_CONTENT_CONFIG,
            parse_captures=content_parser,
            **self._dependencies,
        )
        try:
            return content_collector.collect(account)
        except PlatformDataCollectionError as error:
            if error.error_code == "metric_payload_empty":
                return account_batch
            raise
