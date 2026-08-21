# -*- coding: utf-8 -*-
"""快手创作者服务平台已登录会话的受限只读数据采集器。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
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


_CREATOR_HOST = "cp.kuaishou.com"
_HOME_PAGE = "https://cp.kuaishou.com/"
_HOME_PHASE = "home"
_AUTHORITY_PATH = "/rest/v2/creator/pc/authority/account/current"
_IDENTITY_PATH = "/rest/cp/creator/pc/home/userInfo"
_CONTENT_LIST_PATH = "/rest/cp/creator/analysis/pc/home/photo/list"
_BEIJING = ZoneInfo("Asia/Shanghai")
_CONTENT_METRICS = {
    "playCount": "views",
    "likeCount": "likes",
    "commentCount": "comments",
    "shareCount": "shares",
    "collectCount": "favorites",
}


KUAISHOU_CONFIG = DomesticCollectorConfig(
    platform_type=4,
    home_url=_HOME_PAGE,
    allowed_hosts=frozenset({_CREATOR_HOST}),
    endpoint_by_path={
        _AUTHORITY_PATH: "account_base",
        _IDENTITY_PATH: "account_home",
        _CONTENT_LIST_PATH: "content_list",
    },
    phase_by_path={
        _AUTHORITY_PATH: _HOME_PHASE,
        _IDENTITY_PATH: _HOME_PHASE,
        _CONTENT_LIST_PATH: _HOME_PHASE,
    },
    phase_settle_milliseconds=5_000,
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


def _successful(payload: Mapping[str, object]) -> Mapping[str, object]:
    if _integer(payload.get("result")) != 1:
        _invalid()
    return _mapping(payload.get("data"))


def _matching(
    captures: tuple[CapturedJson, ...], endpoint: str, path: str
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        capture.payload
        for capture in captures
        if capture.endpoint == endpoint
        and capture.phase == _HOME_PHASE
        and capture.path == path
    )


def _followers_total(captures: tuple[CapturedJson, ...]) -> int:
    payloads = _matching(captures, "account_home", _IDENTITY_PATH)
    if not payloads:
        _invalid()
    identities: set[tuple[int, str, int]] = set()
    for payload in payloads:
        core = _mapping(_successful(payload).get("coreUserInfo"))
        user_id = _integer(core.get("userId"))
        if user_id <= 0:
            _invalid()
        identities.add(
            (
                user_id,
                _required_text(core.get("userName")),
                _integer(core.get("fansNum")),
            )
        )
    if len(identities) != 1:
        _invalid()
    return identities.pop()[2]


def _require_authority(captures: tuple[CapturedJson, ...]) -> None:
    payloads = _matching(captures, "account_base", _AUTHORITY_PATH)
    if not payloads:
        _invalid()
    identities: set[tuple[int, str]] = set()
    for payload in payloads:
        raw_data = payload.get("data")
        data = raw_data if isinstance(raw_data, Mapping) else None
        raw_core = payload.get("coreUserInfo")
        if not isinstance(raw_core, Mapping) and data is not None:
            raw_core = data.get("coreUserInfo")
        core = raw_core if isinstance(raw_core, Mapping) else None
        login_url = payload.get("loginUrl")
        if type(login_url) is str and login_url.strip() and core is None:
            raise PlatformDataCollectionError("login_required") from None

        if _integer(payload.get("result")) != 1:
            _invalid()
        if core is not None:
            identity = core
        else:
            successful_data = _mapping(payload.get("data"))
            if successful_data.get("logined") is False:
                raise PlatformDataCollectionError("login_required") from None
            if successful_data.get("logined") is not True:
                _invalid()
            identity = successful_data
        user_id = _integer(identity.get("userId"))
        if user_id <= 0:
            _invalid()
        identities.add((user_id, _required_text(identity.get("userName"))))
    if len(identities) != 1:
        _invalid()


def _content_record(
    raw_item: object,
    *,
    observed_at: str,
    observed_day: str,
) -> tuple[ContentRecord, tuple[MetricPoint, ...]]:
    item = _mapping(raw_item)
    content_id = _required_text(item.get("photoId"))
    video = item.get("video")
    if type(video) is not bool:
        _invalid()
    content = ContentRecord(
        content_id=content_id,
        title="",
        cover_url="",
        published_at="",
        content_status="unavailable",
        content_type="video" if video else "image",
    )
    points: list[MetricPoint] = []
    for raw_key, metric_key in _CONTENT_METRICS.items():
        if raw_key not in item:
            continue
        points.append(
            MetricPoint(
                entity_type="content",
                entity_key=content_id,
                metric_key=metric_key,
                raw_metric_key=raw_key,
                metric_value=_integer(item.get(raw_key)),
                metric_unit="count",
                metric_scope="lifetime_total",
                period_start=observed_day,
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
    payloads = _matching(captures, "content_list", _CONTENT_LIST_PATH)
    if not payloads:
        return (), (), False, False

    records: dict[str, tuple[ContentRecord, tuple[MetricPoint, ...]]] = {}
    totals: set[int] = set()
    for payload in payloads:
        photo_list = _mapping(_successful(payload).get("photoList"))
        rows = _sequence(photo_list.get("photoItems"))
        total = _integer(photo_list.get("totalCount"))
        if total < len(rows):
            _invalid()
        totals.add(total)
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
    if len(totals) != 1:
        _invalid()
    expected_total = next(iter(totals))
    if len(records) > expected_total:
        _invalid()
    ordered = tuple(records.values())
    contents = tuple(item[0] for item in ordered)
    points = tuple(point for item in ordered for point in item[1])
    return contents, points, True, expected_total > len(contents)


def parse_captures(
    account_id: int, captures: tuple[CapturedJson, ...]
) -> CollectionBatch:
    if type(account_id) is not int or account_id <= 0 or type(captures) is not tuple:
        _invalid()
    _require_authority(captures)
    followers_total = _followers_total(captures)
    observed_at, observed_day = _observation()
    account_period_day = (
        datetime.fromisoformat(observed_day).date() - timedelta(days=1)
    ).isoformat()
    contents, content_points, content_available, truncated = _contents(
        captures,
        observed_at=observed_at,
        observed_day=observed_day,
    )
    account_point = MetricPoint(
        entity_type="account",
        entity_key=f"account:{account_id}",
        metric_key="followers_total",
        raw_metric_key="fansNum",
        metric_value=followers_total,
        metric_unit="count",
        metric_scope="lifetime_total",
        period_start=account_period_day,
        period_end=account_period_day,
        observed_at=observed_at,
    )
    warning = (
        "content_list_truncated"
        if truncated
        else "" if content_available else "content_list_unavailable"
    )
    return CollectionBatch(
        platform_type=4,
        source_mode="browser_signed",
        metrics=(account_point,) + content_points,
        contents=contents,
        account_metrics_available=True,
        content_data_available=content_available,
        platform_observed_at=observed_at,
        warning_code=warning,
    )


class KuaishouDataCollector:
    """只接受快手创作者服务平台已审核首页响应的采集器。"""

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
        self._collector = domestic_collector_factory(
            KUAISHOU_CONFIG,
            parse_captures=parse_captures,
            **dependencies,
        )

    def collect(self, account: dict) -> CollectionBatch:
        try:
            return self._collector.collect(account)
        except PlatformDataCollectionError as error:
            if error.error_code != "verification_required":
                raise
            raise PlatformDataCollectionError(
                "verification_required",
                retryable=True,
                cleanup_receipt=error.cleanup_receipt,
                failure_diagnostic=error.failure_diagnostic,
            ) from None
