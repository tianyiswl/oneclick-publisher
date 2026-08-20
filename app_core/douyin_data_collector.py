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
from .platform_data_models import CollectionBatch, CollectionFailure, MetricPoint


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


class DouyinDataCollectionError(CollectionFailure):
    def __init__(self, error_code: str, *, fallback_allowed: bool) -> None:
        self.fallback_allowed = bool(fallback_allowed)
        super().__init__(error_code, retryable=fallback_allowed)


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
            observed_at=f"{platform_day}T00:00:00+08:00",
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
        browser_timeout_seconds: float = _BROWSER_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._playwright_factory = playwright_factory
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
        points: list[MetricPoint] = []
        identities: set[tuple[str, str, str]] = set()
        for metric in metrics:
            if not isinstance(metric, Mapping):
                continue
            for point in _parse_daily_metric(metric, account_id):
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
            platform_observed_at=_local_observation_timestamp(),
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
        if not isinstance(data, Mapping):
            return self._parse_payload(
                payload,
                account_id,
                source_mode="browser_signed",
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
                    observed_at=f"{platform_day}T00:00:00+08:00",
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
            platform_observed_at=_local_observation_timestamp(),
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
