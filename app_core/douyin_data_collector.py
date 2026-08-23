# -*- coding: utf-8 -*-
"""抖音创作者数据的登录会话只读采集器。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from .paths import COOKIE_DIR
from .platform_data_collection_errors import PlatformDataCollectionError
from .platform_data_comment_contract import (
    DouyinCommentContract,
    load_verified_contract,
)
from .platform_data_comment_models import CommentInsightFailure
from .platform_data_models import (
    ALLOWED_CONTENT_STATUSES,
    ALLOWED_CONTENT_TYPES,
    ALLOWED_METRIC_KEYS,
    CollectionBatch,
    ContentRecord,
    MetricPoint,
)


DOUYIN_DASHBOARD_URL = (
    "https://creator.douyin.com/janus/douyin/creator/data/overview/dashboard"
)
DOUYIN_DATA_PAGE_URL = "https://creator.douyin.com/creator-micro/home"
_BROWSER_RESPONSE_PATHS = frozenset(
    {
        "/aweme/janus/creator/data/overview/all/",
        "/janus/douyin/creator/data/overview/dashboard",
    }
)
_CONTENT_LIST_UNAVAILABLE_WARNING = "content_list_unavailable"
_REQUEST_TIMEOUT_SECONDS = 20.0
_BROWSER_TIMEOUT_SECONDS = 30.0
_CONTENT_RESPONSE_LIMIT = 50
_MISSING = object()
_RAW_METRIC_MAP = {
    "play": "views",
    "play_cnt": "views",
    "digg": "likes",
    "digg_cnt": "likes",
    "comment": "comments",
    "comment_cnt": "comments",
    "share": "shares",
    "share_cnt": "shares",
    "fans": "followers_total",
    "fans_cnt": "followers_total",
    "new_fans": "followers_net",
    "net_fans_cnt": "followers_net",
    "profile": "profile_visits",
    "profile_cnt": "profile_visits",
}
_DIRECT_DAY_FORMATS = ("%Y%m%d",)
_BROWSER_DAY_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d")


class DouyinDataCollectionError(PlatformDataCollectionError):
    def __init__(self, error_code: str, *, fallback_allowed: bool) -> None:
        super().__init__(
            error_code,
            fallback_allowed=fallback_allowed,
            retryable=fallback_allowed,
        )


def _is_douyin_cookie_domain(value: object) -> bool:
    if type(value) is not str:
        return False
    domain = value.strip().lower().lstrip(".")
    return domain == "douyin.com" or domain.endswith(".douyin.com")


def _required_account(account: object) -> tuple[int, Path]:
    if type(account) is not dict:
        raise DouyinDataCollectionError(
            "metric_payload_invalid", fallback_allowed=False
        )
    account_id = account.get("id")
    platform_type = account.get("type")
    file_path = account.get("filePath")
    if (
        type(account_id) is not int
        or account_id <= 0
        or type(platform_type) is not int
        or platform_type != 3
        or type(file_path) is not str
        or not file_path.strip()
    ):
        raise DouyinDataCollectionError(
            "metric_payload_invalid", fallback_allowed=False
        )
    state_path = COOKIE_DIR / Path(file_path).name
    if not state_path.is_file():
        raise DouyinDataCollectionError(
            "session_state_missing", fallback_allowed=False
        )
    return account_id, state_path


def _contains_login_rejection(value: object) -> bool:
    if isinstance(value, Mapping):
        status = value.get("status_code")
        if type(status) is int and status == 8:
            return True
        return any(_contains_login_rejection(item) for item in value.values())
    if type(value) is list:
        return any(_contains_login_rejection(item) for item in value)
    return False


def _local_observation_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _platform_day(value: object, *, formats: tuple[str, ...]) -> str:
    if type(value) is not str or value != value.strip():
        raise DouyinDataCollectionError(
            "metric_payload_invalid", fallback_allowed=False
        )
    for date_format in formats:
        try:
            parsed = datetime.strptime(value, date_format)
        except ValueError:
            continue
        if parsed.strftime(date_format) == value:
            return parsed.strftime("%Y-%m-%d")
    raise DouyinDataCollectionError(
        "metric_payload_invalid", fallback_allowed=False
    )


def _metric_number(value: object) -> int | float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise DouyinDataCollectionError(
            "metric_payload_invalid", fallback_allowed=False
        )
    return value


def _content_payload_invalid() -> None:
    raise DouyinDataCollectionError(
        "content_payload_invalid", fallback_allowed=False
    )


def _strict_path_value(root: object, path: object) -> object:
    if type(root) is not dict or type(path) is not str or not path:
        _content_payload_invalid()
    value = root
    for segment in path.split("."):
        if not segment or segment.endswith("[]") or type(value) is not dict:
            _content_payload_invalid()
        if segment not in value:
            return _MISSING
        value = value[segment]
    return value


def _content_rows(payload: object, list_field: object) -> tuple[dict, ...]:
    if type(list_field) is not str or not list_field.endswith("[]"):
        _content_payload_invalid()
    value = _strict_path_value(payload, list_field[:-2])
    if type(value) is not list:
        _content_payload_invalid()
    if not all(type(item) is dict for item in value):
        _content_payload_invalid()
    return tuple(value)


def _row_field(row: dict, list_field: str, field: object) -> object:
    prefix = f"{list_field}."
    if type(field) is not str or not field.startswith(prefix):
        _content_payload_invalid()
    relative_path = field[len(prefix) :]
    return _strict_path_value(row, relative_path)


def _stable_content_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
    ):
        _content_payload_invalid()
    return value


def _optional_controlled_text(value: object) -> str:
    if value is _MISSING or value is None:
        return ""
    if type(value) is not str or value != value.strip():
        return ""
    return value


def _observed_day(observed_at: object) -> str:
    if type(observed_at) is not str or observed_at != observed_at.strip():
        _content_payload_invalid()
    try:
        parsed = datetime.fromisoformat(observed_at)
    except ValueError:
        _content_payload_invalid()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _content_payload_invalid()
    return parsed.date().isoformat()


def parse_verified_content_payload(
    contract: DouyinCommentContract,
    payload: object,
    account_id: int,
    observed_at: str,
) -> tuple[tuple[ContentRecord, ...], tuple[MetricPoint, ...], str]:
    """只按已验证合同把一页作品 JSON 投影为公开模型。"""

    if (
        type(contract) is not DouyinCommentContract
        or contract.verified is not True
        or type(account_id) is not int
        or account_id <= 0
        or type(payload) is not dict
    ):
        _content_payload_invalid()
    observed_day = _observed_day(observed_at)
    rows = _content_rows(payload, contract.content_list_field)
    has_more = _strict_path_value(payload, contract.content_has_more_field)
    if type(has_more) is not bool:
        _content_payload_invalid()
    raw_cursor = _strict_path_value(payload, contract.content_cursor_field)
    if has_more:
        if (
            type(raw_cursor) is not str
            or not raw_cursor
            or raw_cursor != raw_cursor.strip()
        ):
            _content_payload_invalid()
        cursor = raw_cursor
    else:
        cursor = ""

    records: dict[
        str, tuple[ContentRecord, tuple[MetricPoint, ...]]
    ] = {}
    for row in rows:
        content_id = _stable_content_id(
            _row_field(
                row, contract.content_list_field, contract.content_id_field
            )
        )
        title = _optional_controlled_text(
            _row_field(
                row, contract.content_list_field, contract.content_title_field
            )
        )
        cover_url = _optional_controlled_text(
            _row_field(
                row, contract.content_list_field, contract.content_cover_field
            )
        )
        published_at = _optional_controlled_text(
            _row_field(
                row,
                contract.content_list_field,
                contract.content_published_at_field,
            )
        )
        raw_status = _row_field(
            row, contract.content_list_field, contract.content_status_field
        )
        content_status = (
            raw_status
            if type(raw_status) is str
            and raw_status in ALLOWED_CONTENT_STATUSES
            and raw_status != "unavailable"
            else "unavailable"
        )
        if not title or not cover_url or not published_at:
            content_status = "unavailable"
        raw_type = _row_field(
            row, contract.content_list_field, contract.content_type_field
        )
        content_type = (
            raw_type
            if type(raw_type) is str and raw_type in ALLOWED_CONTENT_TYPES
            else "unavailable"
        )
        content = ContentRecord(
            content_id=content_id,
            title=title,
            cover_url=cover_url,
            published_at=published_at,
            content_status=content_status,
            content_type=content_type,
        )
        points: list[MetricPoint] = []
        for metric_key, field in contract.content_metric_fields:
            if metric_key not in ALLOWED_METRIC_KEYS:
                continue
            value = _row_field(row, contract.content_list_field, field)
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
            ):
                continue
            points.append(
                MetricPoint(
                    entity_type="content",
                    entity_key=content_id,
                    metric_key=metric_key,
                    raw_metric_key=field,
                    metric_value=value,
                    metric_unit="count",
                    metric_scope="lifetime_total",
                    period_start=observed_day,
                    period_end=observed_day,
                    observed_at=observed_at,
                )
            )
        if not points:
            _content_payload_invalid()
        candidate = (content, tuple(points))
        existing = records.get(content_id)
        if existing is not None:
            if existing != candidate:
                _content_payload_invalid()
            continue
        records[content_id] = candidate

    contents = tuple(item[0] for item in records.values())
    metrics = tuple(
        point for _content, points in records.values() for point in points
    )
    return contents, metrics, cursor


def _daily_point_identity(point: MetricPoint) -> tuple[str, str, str]:
    return point.metric_key, point.metric_scope, point.period_start


def _parse_daily_points(
    *,
    raw_metric_key: str,
    metric_key: str,
    entries: object,
    account_id: int,
    date_key: str,
    value_key: str,
    day_formats: tuple[str, ...],
    observed_at: str,
) -> tuple[MetricPoint, ...]:
    if type(entries) is not list or not entries:
        raise DouyinDataCollectionError(
            "metric_payload_invalid", fallback_allowed=False
        )
    entry_snapshot = tuple(entries)
    points: list[MetricPoint] = []
    identities: set[tuple[str, str, str]] = set()
    for entry in entry_snapshot:
        if not isinstance(entry, Mapping):
            raise DouyinDataCollectionError(
                "metric_payload_invalid", fallback_allowed=False
            )
        platform_day = _platform_day(
            entry.get(date_key), formats=day_formats
        )
        point = MetricPoint(
            entity_type="account",
            entity_key=f"account:{account_id}",
            metric_key=metric_key,
            raw_metric_key=raw_metric_key,
            metric_value=_metric_number(entry.get(value_key)),
            metric_unit="count",
            metric_scope="daily_increment",
            period_start=platform_day,
            period_end=platform_day,
            observed_at=observed_at,
        )
        identity = _daily_point_identity(point)
        if identity in identities:
            raise DouyinDataCollectionError(
                "metric_payload_invalid", fallback_allowed=False
            )
        identities.add(identity)
        points.append(point)
    return tuple(points)


def _parse_daily_metric(
    metric: Mapping,
    account_id: int,
    *,
    observed_at: str,
) -> tuple[MetricPoint, ...]:
    raw_metric_key = metric.get("english_metric_name")
    if type(raw_metric_key) is not str:
        return ()
    metric_key = _RAW_METRIC_MAP.get(raw_metric_key)
    if metric_key is None:
        return ()
    return _parse_daily_points(
        raw_metric_key=raw_metric_key,
        metric_key=metric_key,
        entries=metric.get("trends"),
        account_id=account_id,
        date_key="date_time",
        value_key="value",
        day_formats=_DIRECT_DAY_FORMATS,
        observed_at=observed_at,
    )


def _default_playwright_factory():
    from playwright.async_api import async_playwright

    return async_playwright()


class DouyinDataCollector:
    def __init__(
        self,
        *,
        session_factory: Callable[[], object] = requests.Session,
        playwright_factory: Callable[[], object] = _default_playwright_factory,
        contract_loader: Callable[[], DouyinCommentContract] = load_verified_contract,
        browser_timeout_seconds: float = _BROWSER_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._playwright_factory = playwright_factory
        if not callable(contract_loader):
            raise DouyinDataCollectionError(
                "metric_payload_invalid", fallback_allowed=False
            )
        self._contract_loader = contract_loader
        if type(browser_timeout_seconds) not in (int, float):
            raise DouyinDataCollectionError(
                "metric_payload_invalid", fallback_allowed=False
            )
        self._browser_timeout_seconds = max(
            0.001, float(browser_timeout_seconds)
        )

    def _load_state(self, state_path: Path) -> dict:
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise DouyinDataCollectionError(
                "session_state_missing", fallback_allowed=False
            ) from None
        if type(payload) is not dict or type(payload.get("cookies")) is not list:
            raise DouyinDataCollectionError(
                "session_state_missing", fallback_allowed=False
            )
        return payload

    def _install_state(self, session: object, state: dict) -> None:
        cookies = getattr(session, "cookies", None)
        headers = getattr(session, "headers", None)
        if cookies is None or headers is None:
            raise DouyinDataCollectionError(
                "direct_request_rejected", fallback_allowed=False
            )
        for item in state["cookies"]:
            if not isinstance(item, Mapping):
                continue
            if not _is_douyin_cookie_domain(item.get("domain")):
                continue
            name = item.get("name")
            value = item.get("value")
            if type(name) is not str or not name or type(value) is not str:
                continue
            domain = str(item.get("domain") or "")
            path = item.get("path")
            cookies.set(
                name,
                value,
                domain=domain,
                path=path if type(path) is str and path else "/",
            )
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://creator.douyin.com",
                "Referer": (
                    "https://creator.douyin.com/"
                    "creator-micro/data-center/operation"
                ),
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                ),
            }
        )

    def _parse_payload(
        self,
        payload: object,
        account_id: int,
        *,
        source_mode: str = "direct_session",
        observed_at: str | None = None,
    ) -> CollectionBatch:
        if not isinstance(payload, Mapping):
            raise DouyinDataCollectionError(
                "metric_payload_invalid", fallback_allowed=False
            )
        if _contains_login_rejection(payload):
            raise DouyinDataCollectionError(
                "login_required", fallback_allowed=True
            )
        status_code = payload.get("status_code")
        if type(status_code) is not int or status_code != 0:
            raise DouyinDataCollectionError(
                "direct_request_rejected", fallback_allowed=True
            )
        metrics = payload.get("metrics")
        if type(metrics) is not list:
            raise DouyinDataCollectionError(
                "metric_payload_empty", fallback_allowed=True
            )
        response_observed_at = observed_at or _local_observation_timestamp()
        points: list[MetricPoint] = []
        identities: set[tuple[str, str, str]] = set()
        for metric in metrics:
            if not isinstance(metric, Mapping):
                continue
            for point in _parse_daily_metric(
                metric,
                account_id,
                observed_at=response_observed_at,
            ):
                identity = _daily_point_identity(point)
                if identity in identities:
                    raise DouyinDataCollectionError(
                        "metric_payload_invalid", fallback_allowed=False
                    )
                identities.add(identity)
                points.append(point)
        if not points:
            raise DouyinDataCollectionError(
                "metric_payload_empty", fallback_allowed=True
            )
        return CollectionBatch(
            platform_type=3,
            source_mode=source_mode,
            metrics=tuple(points),
            contents=(),
            account_metrics_available=True,
            content_data_available=False,
            platform_observed_at=response_observed_at,
            # 仅账号总览请求经过实测；不得猜测作品列表接口或发起补采。
            warning_code=_CONTENT_LIST_UNAVAILABLE_WARNING,
        )

    def _parse_current_overview(
        self,
        payload: object,
        account_id: int,
    ) -> CollectionBatch:
        if not isinstance(payload, Mapping):
            raise DouyinDataCollectionError(
                "metric_payload_invalid", fallback_allowed=False
            )
        if _contains_login_rejection(payload):
            raise DouyinDataCollectionError(
                "login_required", fallback_allowed=False
            )
        status_code = payload.get("status_code")
        if type(status_code) is not int or status_code != 0:
            raise DouyinDataCollectionError(
                "metric_payload_invalid", fallback_allowed=False
            )
        data = payload.get("data")
        response_observed_at = _local_observation_timestamp()
        if not isinstance(data, Mapping):
            return self._parse_payload(
                payload,
                account_id,
                source_mode="browser_signed",
                observed_at=response_observed_at,
            )
        points: list[MetricPoint] = []
        identities: set[tuple[str, str, str]] = set()
        for raw_key, raw_metric in data.items():
            if type(raw_key) is not str or not isinstance(raw_metric, Mapping):
                continue
            metric_key = _RAW_METRIC_MAP.get(raw_key)
            if metric_key is None:
                continue
            nested_status = raw_metric.get("status_code")
            if type(nested_status) is int and nested_status != 0:
                raise DouyinDataCollectionError(
                    "metric_payload_invalid", fallback_allowed=False
                )
            options = raw_metric.get("option_list")
            if options is None:
                continue
            if type(options) is not list or not options:
                raise DouyinDataCollectionError(
                    "metric_payload_invalid", fallback_allowed=False
                )
            if raw_key == "fans":
                daily_points = _parse_daily_points(
                    raw_metric_key=raw_key,
                    metric_key="followers_net",
                    entries=options,
                    account_id=account_id,
                    date_key="date",
                    value_key="count",
                    day_formats=_BROWSER_DAY_FORMATS,
                    observed_at=response_observed_at,
                )
                platform_day = daily_points[-1].period_start
                point = MetricPoint(
                    entity_type="account",
                    entity_key=f"account:{account_id}",
                    metric_key="followers_total",
                    raw_metric_key=raw_key,
                    metric_value=_metric_number(raw_metric.get("current_count")),
                    metric_unit="count",
                    metric_scope="lifetime_total",
                    period_start=platform_day,
                    period_end=platform_day,
                    observed_at=response_observed_at,
                )
                parsed_points = (*daily_points, point)
            else:
                parsed_points = _parse_daily_points(
                    raw_metric_key=raw_key,
                    metric_key=metric_key,
                    entries=options,
                    account_id=account_id,
                    date_key="date",
                    value_key="count",
                    day_formats=_BROWSER_DAY_FORMATS,
                    observed_at=response_observed_at,
                )
            for point in parsed_points:
                identity = _daily_point_identity(point)
                if identity in identities:
                    raise DouyinDataCollectionError(
                        "metric_payload_invalid", fallback_allowed=False
                    )
                identities.add(identity)
                points.append(point)
        if not points:
            raise DouyinDataCollectionError(
                "metric_payload_empty", fallback_allowed=False
            )
        return CollectionBatch(
            platform_type=3,
            source_mode="browser_signed",
            metrics=tuple(points),
            contents=(),
            account_metrics_available=True,
            content_data_available=False,
            platform_observed_at=response_observed_at,
            # 浏览器也只接纳已验证的账号总览响应，作品数据保持不可用。
            warning_code=_CONTENT_LIST_UNAVAILABLE_WARNING,
        )

    def collect_direct(self, account: dict) -> CollectionBatch:
        account_id, state_path = _required_account(account)
        state = self._load_state(state_path)
        session = None
        try:
            session = self._session_factory()
            self._install_state(session, state)
            response = session.post(
                DOUYIN_DASHBOARD_URL,
                json={"recent_days": 30},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if type(getattr(response, "status_code", None)) is not int:
                raise DouyinDataCollectionError(
                    "direct_request_rejected", fallback_allowed=False
                )
            if response.status_code != 200:
                raise DouyinDataCollectionError(
                    "direct_request_rejected",
                    fallback_allowed=response.status_code in (401, 403),
                )
            return self._parse_payload(response.json(), account_id)
        except DouyinDataCollectionError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise DouyinDataCollectionError(
                "direct_request_rejected", fallback_allowed=False
            ) from None
        finally:
            if session is not None:
                try:
                    session.close()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException:
                    pass

    @staticmethod
    async def _close_resource(resource: object | None) -> BaseException | None:
        if resource is None:
            return None
        close = getattr(resource, "close", None)
        if close is None:
            close = getattr(resource, "stop", None)
        if close is None:
            return RuntimeError("resource close unavailable")
        try:
            result = close()
            if hasattr(result, "__await__"):
                await result
        except BaseException as exc:
            return exc
        return None

    async def _collect_browser_signed_async(
        self,
        account: dict,
        report: Callable[[dict], None] | None,
    ) -> CollectionBatch:
        account_id, state_path = _required_account(account)
        playwright = None
        browser = None
        context = None
        page = None
        listener_tasks: set[asyncio.Task] = set()
        result: CollectionBatch | None = None
        caught: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future = loop.create_future()

        def emit(stage: str, message: str) -> None:
            if report is None:
                return
            try:
                report({"stage": stage, "message": message})
            except Exception:
                return

        async def consume_response(response: object) -> None:
            if response_future.done():
                return
            try:
                url = getattr(response, "url", "")
                parsed = urlparse(url if type(url) is str else "")
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != "creator.douyin.com"
                    or parsed.path not in _BROWSER_RESPONSE_PATHS
                ):
                    return
                payload = await response.json()
                batch = self._parse_current_overview(payload, account_id)
                if not response_future.done():
                    response_future.set_result(batch)
            except DouyinDataCollectionError as exc:
                if not response_future.done():
                    response_future.set_exception(exc)
            except (KeyboardInterrupt, SystemExit) as exc:
                if not response_future.done():
                    response_future.set_exception(exc)
            except BaseException:
                if not response_future.done():
                    response_future.set_exception(
                        DouyinDataCollectionError(
                            "metric_payload_invalid",
                            fallback_allowed=False,
                        )
                    )

        def observe_response(response: object) -> None:
            task = asyncio.create_task(consume_response(response))
            listener_tasks.add(task)
            task.add_done_callback(listener_tasks.discard)

        try:
            emit("browser_signed", "正在读取抖音官方数据")
            starter = self._playwright_factory()
            playwright = await starter.start()
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()
            page.on("response", observe_response)
            await page.goto(
                DOUYIN_DATA_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=self._browser_timeout_seconds * 1000,
            )
            current_url = str(getattr(page, "url", "") or "").lower()
            if any(
                marker in current_url
                for marker in ("/verification", "/login", "passport.")
            ):
                raise DouyinDataCollectionError(
                    "login_required", fallback_allowed=False
                )
            try:
                result = await asyncio.wait_for(
                    response_future,
                    timeout=self._browser_timeout_seconds,
                )
            except TimeoutError:
                raise DouyinDataCollectionError(
                    "browser_signature_timeout", fallback_allowed=False
                ) from None
        except BaseException as exc:
            caught = exc
        finally:
            for task in tuple(listener_tasks):
                if not task.done():
                    task.cancel()
            if listener_tasks:
                await asyncio.gather(*listener_tasks, return_exceptions=True)
            for resource in (page, context, browser, playwright):
                close_error = await self._close_resource(resource)
                if close_error is not None:
                    cleanup_errors.append(close_error)

        if isinstance(caught, (KeyboardInterrupt, SystemExit)):
            raise caught
        for cleanup_error in cleanup_errors:
            if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
                raise cleanup_error
        if cleanup_errors:
            raise DouyinDataCollectionError(
                "browser_cleanup_incomplete", fallback_allowed=False
            ) from None
        if isinstance(caught, DouyinDataCollectionError):
            raise caught
        if caught is not None:
            raise DouyinDataCollectionError(
                "metric_payload_invalid", fallback_allowed=False
            ) from None
        if result is None:
            raise DouyinDataCollectionError(
                "metric_payload_empty", fallback_allowed=False
            )
        return result

    async def _complete_content_data_async(
        self,
        account: dict,
        account_batch: CollectionBatch,
        contract: DouyinCommentContract,
        report: Callable[[dict], None] | None,
    ) -> CollectionBatch:
        account_id, state_path = _required_account(account)
        if (
            type(account_batch) is not CollectionBatch
            or account_batch.platform_type != 3
            or account_batch.content_data_available
            or any(
                point.entity_type != "account"
                or point.entity_key != f"account:{account_id}"
                for point in account_batch.metrics
            )
            or type(contract) is not DouyinCommentContract
            or contract.verified is not True
            or contract.creator_host != "creator.douyin.com"
            or "{content_id}" in contract.content_navigation_template
        ):
            _content_payload_invalid()

        playwright = None
        browser = None
        context = None
        page = None
        worker: asyncio.Task | None = None
        result: tuple[tuple[ContentRecord, ...], tuple[MetricPoint, ...]] | None = None
        caught: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        response_queue: asyncio.Queue[object] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future = loop.create_future()
        observed_at = _local_observation_timestamp()

        def observe_response(response: object) -> None:
            if not response_future.done():
                response_queue.put_nowait(response)

        async def consume_responses() -> None:
            records: dict[
                str, tuple[ContentRecord, tuple[MetricPoint, ...]]
            ] = {}
            seen_cursors: set[str] = set()
            accepted_pages = 0
            while not response_future.done():
                response = await response_queue.get()
                try:
                    url = getattr(response, "url", "")
                    parsed = urlparse(url if type(url) is str else "")
                    request = getattr(response, "request", None)
                    method = getattr(request, "method", None)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname != contract.creator_host
                        or parsed.path != contract.content_response_path
                        or type(method) is not str
                        or method != contract.content_response_method
                    ):
                        continue
                    accepted_pages += 1
                    if accepted_pages > _CONTENT_RESPONSE_LIMIT:
                        _content_payload_invalid()
                    payload = await response.json()
                    contents, points, cursor = parse_verified_content_payload(
                        contract,
                        payload,
                        account_id,
                        observed_at,
                    )
                    points_by_id = {
                        content.content_id: tuple(
                            point
                            for point in points
                            if point.entity_key == content.content_id
                        )
                        for content in contents
                    }
                    for content in contents:
                        candidate = (
                            content,
                            points_by_id[content.content_id],
                        )
                        existing = records.get(content.content_id)
                        if existing is not None:
                            if existing != candidate:
                                _content_payload_invalid()
                            continue
                        records[content.content_id] = candidate
                    if cursor:
                        if cursor in seen_cursors:
                            _content_payload_invalid()
                        seen_cursors.add(cursor)
                        continue
                    completed = (
                        tuple(item[0] for item in records.values()),
                        tuple(
                            point
                            for _content, content_points in records.values()
                            for point in content_points
                        ),
                    )
                    if not response_future.done():
                        response_future.set_result(completed)
                    return
                except DouyinDataCollectionError as exc:
                    if not response_future.done():
                        response_future.set_exception(exc)
                    return
                except (KeyboardInterrupt, SystemExit) as exc:
                    if not response_future.done():
                        response_future.set_exception(exc)
                    return
                except BaseException:
                    if not response_future.done():
                        response_future.set_exception(
                            DouyinDataCollectionError(
                                "content_payload_invalid",
                                fallback_allowed=False,
                            )
                        )
                    return

        try:
            starter = self._playwright_factory()
            playwright = await starter.start()
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()
            page.on("response", observe_response)
            worker = asyncio.create_task(consume_responses())
            navigation_url = (
                f"https://{contract.creator_host}"
                f"{contract.content_navigation_template}"
            )
            await page.goto(
                navigation_url,
                wait_until="domcontentloaded",
                timeout=self._browser_timeout_seconds * 1000,
            )
            current_url = str(getattr(page, "url", "") or "").lower()
            if "/verification" in current_url:
                raise DouyinDataCollectionError(
                    "verification_required", fallback_allowed=False
                )
            if "/login" in current_url or "passport." in current_url:
                raise DouyinDataCollectionError(
                    "login_required", fallback_allowed=False
                )
            if "/forbidden" in current_url:
                raise DouyinDataCollectionError(
                    "access_denied", fallback_allowed=False
                )
            try:
                result = await asyncio.wait_for(
                    response_future,
                    timeout=self._browser_timeout_seconds,
                )
            except TimeoutError:
                raise DouyinDataCollectionError(
                    "content_list_unavailable", fallback_allowed=False
                ) from None
        except BaseException as exc:
            caught = exc
        finally:
            if worker is not None and not worker.done():
                worker.cancel()
            if worker is not None:
                await asyncio.gather(worker, return_exceptions=True)
            for resource in (page, context, browser, playwright):
                close_error = await self._close_resource(resource)
                if close_error is not None:
                    cleanup_errors.append(close_error)

        if isinstance(caught, (KeyboardInterrupt, SystemExit)):
            raise caught
        for cleanup_error in cleanup_errors:
            if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
                raise cleanup_error
        if cleanup_errors:
            raise DouyinDataCollectionError(
                "browser_cleanup_incomplete", fallback_allowed=False
            ) from None
        if isinstance(caught, DouyinDataCollectionError):
            raise caught
        if caught is not None:
            raise DouyinDataCollectionError(
                "content_payload_invalid", fallback_allowed=False
            ) from None
        if result is None:
            raise DouyinDataCollectionError(
                "content_list_unavailable", fallback_allowed=False
            )
        contents, content_points = result
        return CollectionBatch(
            platform_type=3,
            source_mode="browser_signed",
            metrics=account_batch.metrics + content_points,
            contents=contents,
            account_metrics_available=True,
            content_data_available=True,
            platform_observed_at=observed_at,
            warning_code="",
        )

    def complete_content_data(
        self,
        account: dict,
        account_batch: CollectionBatch,
        report: Callable[[dict], None] | None = None,
    ) -> CollectionBatch:
        """仅在生产合同已验证时，以独立短会话补全本人作品列表。"""

        try:
            contract = self._contract_loader()
        except CommentInsightFailure as exc:
            if exc.error_code == "comment_content_unavailable":
                return account_batch
            raise DouyinDataCollectionError(
                "content_list_unavailable", fallback_allowed=False
            ) from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            return account_batch
        try:
            return asyncio.run(
                self._complete_content_data_async(
                    account,
                    account_batch,
                    contract,
                    report,
                )
            )
        except DouyinDataCollectionError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise DouyinDataCollectionError(
                "content_payload_invalid", fallback_allowed=False
            ) from None

    def collect_browser_signed(
        self,
        account: dict,
        report: Callable[[dict], None] | None = None,
    ) -> CollectionBatch:
        try:
            return asyncio.run(
                self._collect_browser_signed_async(account, report)
            )
        except DouyinDataCollectionError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise DouyinDataCollectionError(
                "metric_payload_invalid", fallback_allowed=False
            ) from None

    def collect(self, account: dict) -> CollectionBatch:
        try:
            return self.collect_direct(account)
        except DouyinDataCollectionError as exc:
            if not exc.fallback_allowed:
                raise
        return self.collect_browser_signed(account)
