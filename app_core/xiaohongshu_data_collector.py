# -*- coding: utf-8 -*-
"""小红书已登录会话的受限只读数据采集器。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urlsplit

from .paths import COOKIE_DIR
from .platform_data_collection_errors import CleanupReceipt, PlatformDataCollectionError
from .platform_data_models import CollectionBatch, CollectionFailure
from .xiaohongshu_data_contract import (
    parse_account_overview,
    parse_content_lifetime,
    parse_content_list,
)


CREATOR_HOME = "https://creator.xiaohongshu.com/creator/home"
DATA_ANALYSIS_URL = (
    "https://creator.xiaohongshu.com/statistics/data-analysis?source=official"
)
_NOTE_DETAIL_URL = "https://creator.xiaohongshu.com/statistics/note-detail"
_CREATOR_HOST = "creator.xiaohongshu.com"
_TOTAL_TIMEOUT_SECONDS = 60.0
_CLEANUP_RESERVE_SECONDS = 5.0
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_TOTAL_RESPONSE_BYTES = 4_194_304
_MAX_RESPONSES = 100
_MAX_REQUESTS = _MAX_RESPONSES * 4
_RESPONSE_CAPTURE_POLL_MS = 250
_ACCOUNT_PATHS = frozenset(
    {
        "/api/galaxy/v2/creator/datacenter/account/base",
        "/api/galaxy/creator/home/personal_info",
    }
)
_CONTENT_LIST_PATH = "/api/galaxy/creator/datacenter/note/analyze/list"
_CONTENT_DETAIL_PATH = "/api/galaxy/creator/datacenter/note/base"
_REVIEWED_PATHS = _ACCOUNT_PATHS | {_CONTENT_LIST_PATH, _CONTENT_DETAIL_PATH}
_PHASE_ACCOUNT = "account"
_PHASE_LIST = "list"
_PHASE_DETAIL = "detail"
_PATH_PHASES = {
    **{path: _PHASE_ACCOUNT for path in _ACCOUNT_PATHS},
    _CONTENT_LIST_PATH: _PHASE_LIST,
    _CONTENT_DETAIL_PATH: _PHASE_DETAIL,
}
_PATH_ENDPOINTS = {
    "/api/galaxy/creator/home/personal_info": "account_home",
    "/api/galaxy/v2/creator/datacenter/account/base": "account_base",
    _CONTENT_LIST_PATH: "content_list",
    _CONTENT_DETAIL_PATH: "content_detail",
}
_CONTENT_LENGTH = re.compile(r"^[1-9][0-9]{0,19}$")


def _default_browser_factory():
    from playwright.async_api import async_playwright

    return async_playwright()


def _collection_error(
    error_code: str,
    *,
    fallback_allowed: bool = False,
    cleanup_receipt: CleanupReceipt | None = None,
    failure_diagnostic: dict | None = None,
) -> PlatformDataCollectionError:
    return PlatformDataCollectionError(
        error_code,
        fallback_allowed=fallback_allowed,
        retryable=fallback_allowed,
        cleanup_receipt=cleanup_receipt,
        failure_diagnostic=failure_diagnostic,
    )


def _payload_error(endpoint: str, stage: str, reason: str) -> PlatformDataCollectionError:
    return _collection_error(
        "metric_payload_invalid",
        failure_diagnostic={"endpoint": endpoint, "stage": stage, "reason": reason},
    )


@dataclass(frozen=True, slots=True)
class XiaohongshuCollectionOutcome:
    """采集批次与非持久化的清理回执。"""

    batch: CollectionBatch
    cleanup: CleanupReceipt
    validation: dict | None = None

    def public_diagnostics(self) -> dict:
        result = {"cleanup": self.cleanup.public_payload()}
        if self.validation is not None:
            result["validation"] = self.validation
        return result


def _visible_readback_payload(value: object, batch: CollectionBatch) -> dict | None:
    if type(value) is not dict or set(value) != {"account", "content"}:
        return None
    account = value.get("account")
    content = value.get("content")
    if (
        type(account) is not dict
        or set(account) != {"followersTotal"}
        or type(content) is not dict
        or set(content) != {"contentId", "views"}
    ):
        return None
    followers_total = account.get("followersTotal")
    content_id = content.get("contentId")
    views = content.get("views")
    if (
        type(followers_total) not in (int, float)
        or type(content_id) is not str
        or type(views) not in (int, float)
        or not batch.contents
        or content_id != batch.contents[0].content_id
    ):
        return None
    account_values = {
        point.metric_key: point.metric_value
        for point in batch.metrics
        if point.entity_type == "account"
    }
    content_values = {
        point.metric_key: point.metric_value
        for point in batch.metrics
        if point.entity_type == "content" and point.entity_key == content_id
    }
    if (
        account_values.get("followers_total") != followers_total
        or content_values.get("views") != views
    ):
        return None
    return {
        "account": {"followersTotal": followers_total},
        "content": {"contentId": content_id, "views": views},
    }


async def official_visible_readback(page: object, batch: CollectionBatch) -> dict | None:
    """从官方可见卡片投影三项验收数字，不回传页面文本或节点。"""

    if type(batch) is not CollectionBatch or not batch.contents:
        return None
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        value = await evaluate(
            """(contentId) => {
                const number = (node) => {
                  const text = typeof node === "string" ? node : node?.textContent;
                  if (typeof text !== "string") return null;
                  const compact = text.replace(/[\\s,，]/g, "");
                  if (!/^\\d+(?:\\.\\d+)?$/.test(compact)) return null;
                  const parsed = Number(compact);
                  return Number.isFinite(parsed) ? parsed : null;
                };
                const firstNumber = (selectors) => {
                  for (const selector of selectors) {
                    const value = number(document.querySelector(selector));
                    if (value !== null) return value;
                  }
                  return null;
                };
                const followersTotal = firstNumber([
                  '[data-testid="fans-count"]',
                  '[data-fans-count]',
                  '.fans-count',
                ]);
                const note = Array.from(document.querySelectorAll('[data-note-id]'))
                  .find((node) => node.getAttribute('data-note-id') === contentId);
                const views = note === undefined ? null : firstNumber([
                  `[data-note-id="${CSS.escape(contentId)}"] [data-testid="view-count"]`,
                  `[data-note-id="${CSS.escape(contentId)}"] [data-view-count]`,
                  `[data-note-id="${CSS.escape(contentId)}"] .view-count`,
                ]);
                return {
                  account: {followersTotal},
                  content: {contentId, views},
                };
            }""",
            batch.contents[0].content_id,
        )
    except BaseException:
        return None
    if type(value) is not dict:
        return None
    account = value.get("account")
    content = value.get("content")
    if type(account) is not dict or type(content) is not dict:
        return None
    return _visible_readback_payload(
        {
            "account": {"followersTotal": account.get("followersTotal")},
            "content": {
                "contentId": content.get("contentId"),
                "views": content.get("views"),
            },
        },
        batch,
    )


def _state_path(account: object) -> tuple[int, Path]:
    if type(account) is not dict:
        raise _collection_error("metric_payload_invalid") from None
    account_id = account.get("id")
    platform_type = account.get("type")
    file_path = account.get("filePath")
    if (
        type(account_id) is not int
        or account_id <= 0
        or type(platform_type) is not int
        or platform_type != 1
        or type(file_path) is not str
        or not file_path.strip()
    ):
        raise _collection_error("metric_payload_invalid") from None
    path = COOKIE_DIR / Path(file_path).name
    try:
        if path.is_symlink() or not path.is_file():
            raise _collection_error("session_state_missing")
    except PlatformDataCollectionError:
        raise
    except BaseException:
        raise _collection_error("session_state_missing") from None
    return account_id, path


def _response_path(response: object) -> str | None:
    url = getattr(response, "url", None)
    if type(url) is not str:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != _CREATOR_HOST
        or parsed.path not in _REVIEWED_PATHS
    ):
        return None
    headers = getattr(response, "headers", None)
    status = getattr(response, "status", None)
    content_type = headers.get("content-type") if type(headers) is dict else None
    if (
        type(status) is not int
        or not 200 <= status < 300
        or type(content_type) is not str
        or content_type.split(";", 1)[0].strip().lower() != "application/json"
    ):
        return None
    return parsed.path


def _declared_response_bytes(response: object) -> int | None:
    """校验可选长度头；白名单 JSON 的分块响应以 0 表示尚未声明长度。"""

    headers = getattr(response, "headers", None)
    if type(headers) is not dict:
        return None
    value = headers.get("content-length")
    if value is None:
        return 0
    if type(value) is not str or _CONTENT_LENGTH.fullmatch(value) is None:
        return None
    try:
        result = int(value)
    except ValueError:
        return None
    if not 1 <= result <= _MAX_RESPONSE_BYTES:
        return None
    return result


def _detail_request_matches(request: object, content_id: str) -> bool:
    """请求只锁定官方详情端点；作品身份由响应正文严格回读。"""

    url = getattr(request, "url", None)
    if type(url) is not str:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if type(content_id) is not str or not content_id:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == _CREATOR_HOST
        and parsed.path == _CONTENT_DETAIL_PATH
    )


async def _await_until(value: object, *, deadline: float, monotonic: Callable[[], float]) -> object:
    if not inspect.isawaitable(value):
        return value
    remaining = deadline - monotonic()
    if remaining <= 0:
        close = getattr(value, "close", None)
        if callable(close):
            close()
        raise TimeoutError
    return await asyncio.wait_for(value, timeout=remaining)


async def _close_resource(
    resource: object | None,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> BaseException | None:
    if resource is None:
        return None
    close = getattr(resource, "close", None)
    if not callable(close):
        close = getattr(resource, "stop", None)
    if not callable(close):
        return RuntimeError("resource_close_unavailable")
    try:
        await _await_until(close(), deadline=deadline, monotonic=monotonic)
    except BaseException as error:
        return error
    return None


class XiaohongshuDataCollector:
    """只接受审核端点的单页短会话采集器。"""

    def __init__(
        self,
        *,
        browser_factory: Callable[[], object] = _default_browser_factory,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] | None = None,
        visible_readback: Callable[[object, CollectionBatch], object] | None = None,
    ) -> None:
        import time

        self._browser_factory = browser_factory
        self._utc_now = utc_now
        self._monotonic = monotonic or time.monotonic
        self._visible_readback = visible_readback

    def collect_direct(self, account: dict) -> CollectionBatch:
        _state_path(account)
        raise _collection_error("direct_request_rejected", fallback_allowed=True) from None

    def collect_browser_signed(
        self, account: dict, *, validation_mode: bool = False
    ) -> CollectionBatch:
        try:
            return asyncio.run(
                self._collect_browser_signed_async(account, validation_mode=validation_mode)
            ).batch
        except PlatformDataCollectionError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise _collection_error("metric_payload_invalid") from None

    def collect_browser_signed_with_diagnostics(
        self, account: dict, *, validation_mode: bool = False
    ) -> XiaohongshuCollectionOutcome:
        try:
            return asyncio.run(
                self._collect_browser_signed_async(account, validation_mode=validation_mode)
            )
        except PlatformDataCollectionError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise _collection_error("metric_payload_invalid") from None

    def collect(self, account: dict) -> CollectionBatch:
        try:
            return self.collect_direct(account)
        except PlatformDataCollectionError as error:
            if not error.fallback_allowed:
                raise
        return self.collect_browser_signed(account)

    async def _collect_browser_signed_async(
        self, account: dict, *, validation_mode: bool
    ) -> XiaohongshuCollectionOutcome:
        if type(validation_mode) is not bool:
            raise _collection_error("metric_payload_invalid") from None
        account_id, state_path = _state_path(account)
        started = self._monotonic()
        total_deadline = started + _TOTAL_TIMEOUT_SECONDS
        work_deadline = total_deadline - _CLEANUP_RESERVE_SECONDS
        manager = None
        playwright = None
        browser = None
        context = None
        page = None
        phase = ""
        request_phases: dict[int, tuple[object, str]] = {}
        response_tasks: set[asyncio.Task] = set()
        payloads: dict[str, object] = {}
        transient_failures: dict[str, PlatformDataCollectionError] = {}
        seen_response_ids: set[int] = set()
        scheduled_response_ids: set[int] = set()
        selected_response_sequence: dict[str, int] = {}
        retained_request_count = 0
        retained_response_count = 0
        response_sequence = 0
        reserved_bytes = 0
        selected_content_id: str | None = None
        capture_error: PlatformDataCollectionError | None = None
        caught: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        browser_close_error: BaseException | None = None
        runtime_close_error: BaseException | None = None
        batch: CollectionBatch | None = None
        validation = None
        runtime_stage = "browser_start"

        def remember_request(request: object) -> None:
            nonlocal retained_request_count, capture_error
            if not phase:
                return
            retained_request_count += 1
            if retained_request_count > _MAX_REQUESTS:
                capture_error = _payload_error(
                    "runtime", "response_capture", "request_limit_exceeded"
                )
                return
            request_phases[id(request)] = (request, phase)

        async def cache_response(
            response: object, response_phase: str, sequence: int
        ) -> None:
            nonlocal reserved_bytes, capture_error
            path = _response_path(response)
            if path is None or _PATH_PHASES[path] != response_phase:
                return
            endpoint = _PATH_ENDPOINTS[path]
            response_id = id(response)
            declared_bytes = _declared_response_bytes(response)
            if declared_bytes is None or reserved_bytes + declared_bytes > _MAX_TOTAL_RESPONSE_BYTES:
                capture_error = _payload_error(endpoint, "response_headers", "invalid_content_length")
                return
            if path == _CONTENT_DETAIL_PATH and (
                selected_content_id is None
                or not _detail_request_matches(
                    getattr(response, "request", None), selected_content_id
                )
            ):
                capture_error = _payload_error(endpoint, "request_binding", "request_mismatch")
                return
            loader = getattr(response, "body", None)
            if not callable(loader):
                capture_error = _payload_error(endpoint, "response_body", "body_unavailable")
                return
            reserved_bytes += declared_bytes
            try:
                body = await _await_until(
                    loader(), deadline=work_deadline, monotonic=self._monotonic
                )
            except TimeoutError:
                capture_error = _collection_error("browser_signature_timeout")
                return
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                transient_failures[path] = _payload_error(
                    endpoint, "response_body", "body_read_failed"
                )
                return
            if type(body) is not bytes or not 1 <= len(body) <= _MAX_RESPONSE_BYTES:
                capture_error = _payload_error(endpoint, "response_body", "body_size_invalid")
                return
            reserved_bytes += max(0, len(body) - declared_bytes)
            if reserved_bytes > _MAX_TOTAL_RESPONSE_BYTES:
                capture_error = _payload_error(endpoint, "response_body", "body_size_invalid")
                return
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                capture_error = _payload_error(endpoint, "json_decode", "invalid_json")
                return
            seen_response_ids.add(response_id)
            previous_sequence = selected_response_sequence.get(path)
            if previous_sequence is None or sequence < previous_sequence:
                payloads[path] = payload
                selected_response_sequence[path] = sequence
            transient_failures.pop(path, None)

        def remember_response(response: object) -> None:
            nonlocal capture_error, retained_response_count, response_sequence
            request = getattr(response, "request", None)
            entry = request_phases.pop(id(request), None)
            if entry is None or entry[0] is not request:
                return
            path = _response_path(response)
            if path is None or _PATH_PHASES[path] != entry[1]:
                return
            response_id = id(response)
            if response_id in scheduled_response_ids or response_id in seen_response_ids:
                capture_error = _payload_error(
                    _PATH_ENDPOINTS[path], "response_capture", "duplicate_response"
                )
                return
            scheduled_response_ids.add(response_id)
            retained_response_count += 1
            if retained_response_count > _MAX_RESPONSES:
                capture_error = _payload_error(
                    "runtime", "response_capture", "response_limit_exceeded"
                )
                return
            response_sequence += 1
            task = asyncio.create_task(
                cache_response(response, entry[1], response_sequence)
            )
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        async def flush_responses() -> None:
            if response_tasks:
                completed = tuple(response_tasks)
                await asyncio.gather(*completed)
            if capture_error is not None:
                raise capture_error

        async def navigate(current_phase: str, url: str) -> None:
            nonlocal phase, runtime_stage
            phase = current_phase
            runtime_stage = "navigation"
            try:
                await _await_until(
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000),
                    deadline=work_deadline,
                    monotonic=self._monotonic,
                )
                current_url = str(getattr(page, "url", "") or "").lower()
                try:
                    current = urlsplit(current_url)
                except ValueError:
                    raise _payload_error(
                        "runtime", "navigation", "invalid_navigation"
                    ) from None
                current_path = current.path.lower()
                if (
                    any(
                        marker in current_path
                        for marker in ("/verification", "/captcha", "/challenge")
                    )
                    or current.hostname == "captcha.xiaohongshu.com"
                ):
                    raise _collection_error("verification_required") from None
                if (
                    current_path.startswith("/login")
                    or (current.hostname or "").startswith("passport.")
                ):
                    raise _collection_error("login_required") from None
                if current.scheme != "https" or current.hostname != _CREATOR_HOST:
                    raise _payload_error(
                        "runtime", "navigation", "invalid_navigation"
                    ) from None
                required_paths = {
                    _PHASE_ACCOUNT: _ACCOUNT_PATHS,
                    _PHASE_LIST: {_CONTENT_LIST_PATH},
                    _PHASE_DETAIL: {_CONTENT_DETAIL_PATH},
                }[current_phase]

                def required_transient_failure() -> PlatformDataCollectionError | None:
                    return next(
                        (
                            transient_failures[path]
                            for path in required_paths
                            if path in transient_failures
                        ),
                        None,
                    )

                while True:
                    await flush_responses()
                    if any(path in payloads for path in required_paths):
                        break
                    waiter = getattr(page, "wait_for_timeout", None)
                    if not callable(waiter):
                        if (failure := required_transient_failure()) is not None:
                            raise failure
                        raise _collection_error("browser_signature_timeout") from None
                    remaining = work_deadline - self._monotonic()
                    if remaining <= 0:
                        if (failure := required_transient_failure()) is not None:
                            raise failure
                        raise _collection_error("browser_signature_timeout") from None
                    await _await_until(
                        waiter(
                            max(
                                1,
                                min(
                                    _RESPONSE_CAPTURE_POLL_MS,
                                    int(remaining * 1_000),
                                ),
                            )
                        ),
                        deadline=work_deadline,
                        monotonic=self._monotonic,
                    )
            finally:
                phase = ""

        try:
            runtime_stage = "browser_start"
            manager = await _await_until(
                self._browser_factory(), deadline=work_deadline, monotonic=self._monotonic
            )
            start = getattr(manager, "start", None)
            playwright = (
                await _await_until(start(), deadline=work_deadline, monotonic=self._monotonic)
                if callable(start)
                else manager
            )
            browser = await _await_until(
                playwright.chromium.launch(headless=not validation_mode),
                deadline=work_deadline,
                monotonic=self._monotonic,
            )
            runtime_stage = "context_create"
            context = await _await_until(
                browser.new_context(storage_state=str(state_path)),
                deadline=work_deadline,
                monotonic=self._monotonic,
            )
            runtime_stage = "page_create"
            page = await _await_until(
                context.new_page(), deadline=work_deadline, monotonic=self._monotonic
            )
            page.on("request", remember_request)
            page.on("response", remember_response)
            await navigate(_PHASE_ACCOUNT, CREATOR_HOME)
            await navigate(_PHASE_LIST, DATA_ANALYSIS_URL)
            list_payload = payloads.get(_CONTENT_LIST_PATH)
            content_warning = ""
            identities = ()
            if list_payload is None:
                content_warning = "content_list_unavailable"
            else:
                try:
                    identities = parse_content_list(list_payload)
                except CollectionFailure as error:
                    if error.error_code == "content_list_truncated":
                        raise _collection_error(error.error_code) from None
                    content_warning = "content_payload_invalid"
            if identities:
                selected_content_id = identities[0].content_id
                await navigate(
                    _PHASE_DETAIL,
                    f"{_NOTE_DETAIL_URL}?noteId={selected_content_id}",
                )
            batch = self._batch_from_payloads(
                account_id=account_id,
                payloads=payloads,
                content_warning=content_warning,
                identities=identities,
            )
            if validation_mode:
                runtime_stage = "visible_readback"
                reader = self._visible_readback
                if not callable(reader):
                    raise _payload_error(
                        "runtime", "visible_readback", "readback_mismatch"
                    ) from None
                visible = _visible_readback_payload(
                    await _await_until(
                        reader(page, batch),
                        deadline=work_deadline,
                        monotonic=self._monotonic,
                    ),
                    batch,
                )
                if visible is None:
                    raise _payload_error(
                        "runtime", "visible_readback", "readback_mismatch"
                    ) from None
                validation = {
                    "mode": "official_visible_readback",
                    "officialVisible": visible,
                }
        except BaseException as error:
            caught = error
        finally:
            for task in tuple(response_tasks):
                if not task.done():
                    task.cancel()
            if response_tasks:
                await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
            resources = (page, context, browser, playwright if playwright is not None else manager)
            for index, resource in enumerate(resources):
                if resource is None:
                    continue
                remaining = len(resources) - index
                remaining_budget = max(0.0, total_deadline - self._monotonic())
                cleanup_deadline = self._monotonic() + (remaining_budget / remaining)
                close_error = await _close_resource(
                    resource, deadline=cleanup_deadline, monotonic=self._monotonic
                )
                if close_error is not None:
                    cleanup_errors.append(close_error)
                    if index == 2:
                        browser_close_error = close_error
                    elif index == 3:
                        runtime_close_error = close_error

        process_error = next(
            (
                error
                for error in ((caught,) + tuple(cleanup_errors))
                if isinstance(error, (KeyboardInterrupt, SystemExit))
            ),
            None,
        )
        if process_error is not None:
            raise process_error
        receipt = CleanupReceipt(
            closed=browser_close_error is None and runtime_close_error is None,
            alive_resource_count=(
                int(browser_close_error is not None)
                + int(runtime_close_error is not None)
            ),
        )
        if receipt.alive_resource_count:
            raise _collection_error(
                "browser_cleanup_incomplete", cleanup_receipt=receipt
            ) from None
        if isinstance(caught, TimeoutError):
            raise _collection_error(
                "browser_signature_timeout", cleanup_receipt=receipt
            ) from None
        if isinstance(caught, PlatformDataCollectionError):
            raise _collection_error(
                caught.error_code,
                fallback_allowed=caught.fallback_allowed,
                cleanup_receipt=receipt,
                failure_diagnostic=caught.failure_diagnostic,
            ) from None
        if caught is not None:
            raise _collection_error(
                "metric_payload_invalid",
                cleanup_receipt=receipt,
                failure_diagnostic={
                    "endpoint": "runtime",
                    "stage": runtime_stage,
                    "reason": "operation_failed",
                },
            ) from None

        if batch is None:
            raise _collection_error("metric_payload_invalid", cleanup_receipt=receipt) from None
        return XiaohongshuCollectionOutcome(
            batch=batch,
            cleanup=receipt,
            validation=validation,
        )

    def _batch_from_payloads(
        self,
        *,
        account_id: int,
        payloads: dict[str, object],
        content_warning: str,
        identities: tuple,
    ) -> CollectionBatch:
        observed_at, platform_day = self._observation_values()
        account_payload = payloads.get("/api/galaxy/creator/home/personal_info")
        if account_payload is None:
            account_payload = payloads.get("/api/galaxy/v2/creator/datacenter/account/base")
        if account_payload is None:
            raise _payload_error("runtime", "account_parse", "payload_shape_invalid") from None
        try:
            account_metrics = parse_account_overview(
                account_payload,
                account_id=account_id,
                observed_at=observed_at,
                platform_day=platform_day,
            )
        except CollectionFailure:
            endpoint = (
                "account_home"
                if "/api/galaxy/creator/home/personal_info" in payloads
                else "account_base"
            )
            raise _payload_error(endpoint, "account_parse", "payload_shape_invalid") from None

        if content_warning:
            batch = CollectionBatch(
                platform_type=1,
                source_mode="browser_signed",
                metrics=account_metrics,
                contents=(),
                account_metrics_available=True,
                content_data_available=False,
                platform_observed_at=observed_at,
                warning_code=content_warning,
            )
            return batch
        if not identities:
            batch = CollectionBatch(
                platform_type=1,
                source_mode="browser_signed",
                metrics=account_metrics,
                contents=(),
                account_metrics_available=True,
                content_data_available=True,
                platform_observed_at=observed_at,
            )
            return batch
        detail_payload = payloads.get(_CONTENT_DETAIL_PATH)
        if detail_payload is None:
            batch = CollectionBatch(
                platform_type=1,
                source_mode="browser_signed",
                metrics=account_metrics,
                contents=(),
                account_metrics_available=True,
                content_data_available=False,
                platform_observed_at=observed_at,
                warning_code="content_payload_invalid",
            )
            return batch
        try:
            content, content_metrics = parse_content_lifetime(
                detail_payload,
                identities[0],
                observed_at=observed_at,
                platform_day=platform_day,
            )
        except CollectionFailure:
            batch = CollectionBatch(
                platform_type=1,
                source_mode="browser_signed",
                metrics=account_metrics,
                contents=(),
                account_metrics_available=True,
                content_data_available=False,
                platform_observed_at=observed_at,
                warning_code="content_payload_invalid",
            )
            return batch
        batch = CollectionBatch(
            platform_type=1,
            source_mode="browser_signed",
            metrics=account_metrics + content_metrics,
            contents=(content,),
            account_metrics_available=True,
            content_data_available=True,
            platform_observed_at=observed_at,
        )
        return batch

    def _observation_values(self) -> tuple[str, str]:
        try:
            value = self._utc_now()
            if not isinstance(value, datetime):
                raise TypeError
            observed_at = value.isoformat()
            platform_day = value.date().isoformat()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise _collection_error("metric_payload_invalid") from None
        if type(observed_at) is not str or type(platform_day) is not str:
            raise _collection_error("metric_payload_invalid") from None
        return observed_at, platform_day
