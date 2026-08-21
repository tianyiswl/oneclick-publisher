# -*- coding: utf-8 -*-
"""国内平台已登录短会话采集的共用安全骨架。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import inspect
import json
import math
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from .paths import COOKIE_DIR
from .platform_data_collection_errors import (
    CleanupReceipt,
    PlatformDataCollectionError,
)
from .platform_data_models import CollectionBatch, CollectionFailure


_FIXED_ENDPOINTS = frozenset(
    {"account_home", "account_base", "content_list", "content_detail", "runtime"}
)
_CONTENT_LENGTH = re.compile(r"^[1-9][0-9]{0,19}$")
_POLL_MILLISECONDS = 25


def _default_browser_factory():
    from playwright.async_api import async_playwright

    return async_playwright()


def _error(
    error_code: str,
    *,
    cleanup_receipt: CleanupReceipt | None = None,
    failure_diagnostic: dict | None = None,
) -> PlatformDataCollectionError:
    return PlatformDataCollectionError(
        error_code,
        cleanup_receipt=cleanup_receipt,
        failure_diagnostic=failure_diagnostic,
    )


def _invalid(*, endpoint: str = "runtime", stage: str = "runtime", reason: str = "operation_failed") -> PlatformDataCollectionError:
    return _error(
        "metric_payload_invalid",
        failure_diagnostic={"endpoint": endpoint, "stage": stage, "reason": reason},
    )


def _controlled_text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CollectionFailure("metric_payload_invalid")
    return value


def _immutable_mapping(value: object, *, keys: frozenset[str] | None = None) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise CollectionFailure("metric_payload_invalid")
    result: dict[str, str] = {}
    for key, item in value.items():
        checked_key = _controlled_text(key)
        checked_value = _controlled_text(item)
        if keys is not None and checked_key not in keys:
            raise CollectionFailure("metric_payload_invalid")
        result[checked_key] = checked_value
    if not result:
        raise CollectionFailure("metric_payload_invalid")
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class DomesticCollectorConfig:
    platform_type: int
    home_url: str
    allowed_hosts: frozenset[str]
    endpoint_by_path: Mapping[str, str]
    phase_by_path: Mapping[str, str]
    total_timeout_seconds: float = 60.0
    max_response_bytes: int = 1_048_576
    max_total_response_bytes: int = 4_194_304
    max_responses: int = 100
    max_requests: int = 400
    navigation_by_phase: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if type(self.platform_type) is not int or self.platform_type <= 0:
            raise CollectionFailure("metric_payload_invalid")
        if not isinstance(self.allowed_hosts, frozenset) or not self.allowed_hosts:
            raise CollectionFailure("metric_payload_invalid")
        hosts = frozenset(_controlled_text(host).lower() for host in self.allowed_hosts)
        if any("/" in host or ":" in host for host in hosts):
            raise CollectionFailure("metric_payload_invalid")
        try:
            home = urlsplit(_controlled_text(self.home_url))
        except ValueError:
            raise CollectionFailure("metric_payload_invalid") from None
        if home.scheme != "https" or home.hostname not in hosts:
            raise CollectionFailure("metric_payload_invalid")
        endpoints = _immutable_mapping(self.endpoint_by_path)
        phases = _immutable_mapping(self.phase_by_path)
        if set(endpoints) != set(phases):
            raise CollectionFailure("metric_payload_invalid")
        if any(not path.startswith("/") or "?" in path or "#" in path for path in endpoints):
            raise CollectionFailure("metric_payload_invalid")
        if any(endpoint not in _FIXED_ENDPOINTS for endpoint in endpoints.values()):
            raise CollectionFailure("metric_payload_invalid")
        configured_navigation = self.navigation_by_phase
        if configured_navigation is None:
            configured_phases = tuple(dict.fromkeys(phases.values()))
            if len(configured_phases) != 1:
                raise CollectionFailure("metric_payload_invalid")
            navigation = MappingProxyType({configured_phases[0]: self.home_url})
        else:
            navigation = _immutable_mapping(configured_navigation)
        if set(navigation) != set(phases.values()):
            raise CollectionFailure("metric_payload_invalid")
        for navigation_url in navigation.values():
            try:
                parsed_navigation = urlsplit(navigation_url)
            except ValueError:
                raise CollectionFailure("metric_payload_invalid") from None
            if (
                parsed_navigation.scheme != "https"
                or parsed_navigation.hostname not in hosts
            ):
                raise CollectionFailure("metric_payload_invalid")
        if type(self.total_timeout_seconds) not in (int, float) or not math.isfinite(float(self.total_timeout_seconds)) or self.total_timeout_seconds <= 0:
            raise CollectionFailure("metric_payload_invalid")
        for limit in (
            self.max_response_bytes,
            self.max_total_response_bytes,
            self.max_responses,
            self.max_requests,
        ):
            if type(limit) is not int or limit <= 0:
                raise CollectionFailure("metric_payload_invalid")
        if self.max_total_response_bytes < self.max_response_bytes:
            raise CollectionFailure("metric_payload_invalid")
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "endpoint_by_path", endpoints)
        object.__setattr__(self, "phase_by_path", phases)
        object.__setattr__(self, "navigation_by_phase", navigation)


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError
            result[key] = _freeze_json(item)
        return MappingProxyType(result)
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise ValueError


@dataclass(frozen=True, slots=True)
class CapturedJson:
    endpoint: str
    phase: str
    path: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if _controlled_text(self.endpoint) not in _FIXED_ENDPOINTS:
            raise CollectionFailure("metric_payload_invalid")
        _controlled_text(self.phase)
        if not _controlled_text(self.path).startswith("/") or not isinstance(self.payload, Mapping):
            raise CollectionFailure("metric_payload_invalid")
        try:
            frozen = _freeze_json(dict(self.payload))
        except (TypeError, ValueError):
            raise CollectionFailure("metric_payload_invalid") from None
        if not isinstance(frozen, Mapping):
            raise CollectionFailure("metric_payload_invalid")
        object.__setattr__(self, "payload", frozen)


def parse_captures(_account_id: int, _captures: tuple[CapturedJson, ...]) -> CollectionBatch:
    """平台实现必须注入审核过的字段解析器；共用层不解释业务字段。"""

    raise _error("metric_payload_invalid")


def _state_path(account: object, config: DomesticCollectorConfig) -> tuple[int, Path]:
    if type(account) is not dict:
        raise _error("metric_payload_invalid")
    account_id = account.get("id")
    platform_type = account.get("type")
    file_path = account.get("filePath")
    if (
        type(account_id) is not int
        or account_id <= 0
        or type(platform_type) is not int
        or platform_type != config.platform_type
        or type(file_path) is not str
        or not file_path.strip()
    ):
        raise _error("metric_payload_invalid")
    path = COOKIE_DIR / Path(file_path).name
    try:
        if path.is_symlink() or not path.is_file():
            raise _error("session_state_missing")
    except PlatformDataCollectionError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise _error("session_state_missing") from None
    return account_id, path


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


async def _close_resource(resource: object | None, *, deadline: float, monotonic: Callable[[], float]) -> BaseException | None:
    if resource is None:
        return None
    close = getattr(resource, "close", None)
    if not callable(close):
        close = getattr(resource, "stop", None)
    if not callable(close):
        return RuntimeError("resource_close_unavailable")
    try:
        await _await_until(close(), deadline=deadline, monotonic=monotonic)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:
        return error
    return None


class DomesticBrowserCollector:
    """只保留配置中审核过的 HTTPS JSON 响应，并在每次采集后关闭资源。"""

    def __init__(
        self,
        config: DomesticCollectorConfig,
        *,
        browser_factory: Callable[[], object] = _default_browser_factory,
        parse_captures: Callable[[int, tuple[CapturedJson, ...]], CollectionBatch] = parse_captures,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if type(config) is not DomesticCollectorConfig or not callable(browser_factory) or not callable(parse_captures):
            raise CollectionFailure("metric_payload_invalid")
        self._config = config
        self._browser_factory = browser_factory
        self._parse_captures = parse_captures
        self._monotonic = monotonic or time.monotonic

    def collect(self, account: dict) -> CollectionBatch:
        try:
            batch, _receipt = asyncio.run(self._collect_async(account))
            return batch
        except PlatformDataCollectionError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise _error("metric_payload_invalid") from None

    async def _collect_async(self, account: dict) -> tuple[CollectionBatch, CleanupReceipt]:
        account_id, state_path = _state_path(account, self._config)
        started = self._monotonic()
        deadline = started + float(self._config.total_timeout_seconds)
        cleanup_reserve = min(5.0, float(self._config.total_timeout_seconds) / 5)
        work_deadline = deadline - cleanup_reserve
        manager = playwright = browser = context = page = None
        caught: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        captures: list[CapturedJson] = []
        response_tasks: list[asyncio.Task] = []
        request_paths: dict[int, tuple[object, str, str]] = {}
        seen_requests: dict[int, object] = {}
        seen_responses: set[int] = set()
        total_request_count = response_count = total_bytes = 0
        saw_unreviewed_response = False
        capture_error: PlatformDataCollectionError | None = None
        current_phase = ""

        def reviewed_path(value: object) -> str | None:
            if type(value) is not str:
                return None
            try:
                parsed = urlsplit(value)
            except ValueError:
                return None
            if (
                parsed.scheme != "https"
                or parsed.hostname not in self._config.allowed_hosts
                or parsed.path not in self._config.endpoint_by_path
            ):
                return None
            return parsed.path

        def remember_request(request: object) -> None:
            nonlocal total_request_count, capture_error
            request_id = id(request)
            if seen_requests.get(request_id) is request:
                return
            seen_requests[request_id] = request
            total_request_count += 1
            if total_request_count > self._config.max_requests:
                capture_error = _invalid(
                    stage="request_binding", reason="request_limit_exceeded"
                )
                return
            path = reviewed_path(getattr(request, "url", None))
            if path is None:
                return
            expected_phase = self._config.phase_by_path[path]
            if not current_phase or expected_phase != current_phase:
                capture_error = _invalid(
                    endpoint=self._config.endpoint_by_path[path],
                    stage="request_binding",
                    reason="request_mismatch",
                )
                return
            request_paths[request_id] = (request, path, current_phase)

        async def cache_response(response: object, path: str) -> None:
            nonlocal total_bytes
            endpoint = self._config.endpoint_by_path[path]
            status = getattr(response, "status", None)
            headers = getattr(response, "headers", None)
            content_type = headers.get("content-type") if isinstance(headers, Mapping) else None
            if (
                type(status) is not int
                or not 200 <= status < 300
                or type(content_type) is not str
                or content_type.split(";", 1)[0].strip().lower() != "application/json"
            ):
                return
            declared = headers.get("content-length") if isinstance(headers, Mapping) else None
            if declared is None:
                reserved = 0
            elif type(declared) is str and _CONTENT_LENGTH.fullmatch(declared):
                reserved = int(declared)
            else:
                raise _invalid(endpoint=endpoint, stage="response_headers", reason="invalid_content_length")
            if reserved > self._config.max_response_bytes or total_bytes + reserved > self._config.max_total_response_bytes:
                raise _invalid(endpoint=endpoint, stage="response_headers", reason="invalid_content_length")
            loader = getattr(response, "body", None)
            if not callable(loader):
                raise _invalid(endpoint=endpoint, stage="response_body", reason="body_unavailable")
            total_bytes += reserved
            try:
                body = await _await_until(loader(), deadline=work_deadline, monotonic=self._monotonic)
            except TimeoutError:
                raise _error("browser_signature_timeout") from None
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                raise _invalid(endpoint=endpoint, stage="response_body", reason="body_read_failed") from None
            if type(body) is not bytes or not 1 <= len(body) <= self._config.max_response_bytes:
                raise _invalid(endpoint=endpoint, stage="response_body", reason="body_size_invalid")
            total_bytes += max(0, len(body) - reserved)
            if total_bytes > self._config.max_total_response_bytes:
                raise _invalid(endpoint=endpoint, stage="response_body", reason="body_size_invalid")
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise _invalid(endpoint=endpoint, stage="json_decode", reason="invalid_json") from None
            if type(payload) is not dict:
                raise _invalid(endpoint=endpoint, stage="json_decode", reason="payload_shape_invalid")
            captures.append(
                CapturedJson(
                    endpoint=endpoint,
                    phase=self._config.phase_by_path[path],
                    path=path,
                    payload=payload,
                )
            )

        def remember_response(response: object) -> None:
            nonlocal capture_error, response_count, saw_unreviewed_response
            request = getattr(response, "request", None)
            if request is None:
                return
            if id(request) not in request_paths:
                remember_request(request)
            entry = request_paths.pop(id(request), None)
            if entry is None or entry[0] is not request:
                saw_unreviewed_response = True
                return
            path = reviewed_path(getattr(response, "url", None))
            if (
                path is None
                or path != entry[1]
                or entry[2] != self._config.phase_by_path[path]
            ):
                capture_error = _invalid(
                    endpoint=self._config.endpoint_by_path[entry[1]],
                    stage="request_binding",
                    reason="request_mismatch",
                )
                saw_unreviewed_response = True
                return
            response_id = id(response)
            if response_id in seen_responses:
                capture_error = _invalid(
                    endpoint=self._config.endpoint_by_path[path],
                    stage="response_capture",
                    reason="duplicate_response",
                )
                return
            seen_responses.add(response_id)
            response_count += 1
            if response_count > self._config.max_responses:
                capture_error = _invalid(
                    stage="response_capture", reason="response_limit_exceeded"
                )
                return
            task = asyncio.create_task(cache_response(response, path))
            response_tasks.append(task)

        async def flush_responses() -> None:
            if capture_error is not None:
                raise capture_error
            if response_tasks:
                await asyncio.gather(*tuple(response_tasks))
            if capture_error is not None:
                raise capture_error

        async def navigate(phase: str, navigation_url: str) -> None:
            nonlocal current_phase
            current_phase = phase
            await _await_until(
                page.goto(
                    navigation_url,
                    wait_until="domcontentloaded",
                    timeout=max(1, int((work_deadline - self._monotonic()) * 1000)),
                ),
                deadline=work_deadline,
                monotonic=self._monotonic,
            )
            current_url = str(getattr(page, "url", "") or "").lower()
            try:
                current = urlsplit(current_url)
            except ValueError:
                raise _invalid(stage="navigation", reason="invalid_navigation") from None
            current_path = current.path
            if any(marker in current_path for marker in ("/verification", "/captcha", "/challenge")):
                raise _error("verification_required")
            if current_path.startswith("/login") or (current.hostname or "").startswith("passport."):
                raise _error("login_required")
            if current.scheme != "https" or current.hostname not in self._config.allowed_hosts:
                raise _invalid(stage="navigation", reason="invalid_navigation")
            await flush_responses()

        try:
            manager = await _await_until(self._browser_factory(), deadline=work_deadline, monotonic=self._monotonic)
            start = getattr(manager, "start", None)
            playwright = await _await_until(start(), deadline=work_deadline, monotonic=self._monotonic) if callable(start) else manager
            browser = await _await_until(playwright.chromium.launch(headless=True), deadline=work_deadline, monotonic=self._monotonic)
            context = await _await_until(browser.new_context(storage_state=str(state_path)), deadline=work_deadline, monotonic=self._monotonic)
            page = await _await_until(context.new_page(), deadline=work_deadline, monotonic=self._monotonic)
            page.on("request", remember_request)
            page.on("response", remember_response)
            for phase, navigation_url in self._config.navigation_by_phase.items():
                await navigate(phase, navigation_url)
            if saw_unreviewed_response and not response_tasks:
                raise _error("metric_payload_empty")
            while not captures:
                await flush_responses()
                if captures:
                    break
                if work_deadline - self._monotonic() <= 0:
                    raise _error("browser_signature_timeout")
                waiter = getattr(page, "wait_for_timeout", None)
                if not callable(waiter):
                    raise _error("browser_signature_timeout")
                await _await_until(waiter(_POLL_MILLISECONDS), deadline=work_deadline, monotonic=self._monotonic)
            await flush_responses()
            result = self._parse_captures(account_id, tuple(captures))
            if type(result) is not CollectionBatch:
                raise _error("metric_payload_invalid")
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            caught = error
        finally:
            for task in response_tasks:
                if not task.done():
                    task.cancel()
            if response_tasks:
                await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
            resources = (page, context, browser, playwright if playwright is not None else manager)
            for resource in resources:
                try:
                    close_error = await _close_resource(resource, deadline=deadline, monotonic=self._monotonic)
                except (KeyboardInterrupt, SystemExit):
                    raise
                if close_error is not None:
                    cleanup_errors.append(close_error)

        receipt = CleanupReceipt(closed=not cleanup_errors, alive_resource_count=len(cleanup_errors))
        if cleanup_errors:
            raise _error("browser_cleanup_incomplete", cleanup_receipt=receipt) from None
        if isinstance(caught, TimeoutError):
            raise _error("browser_signature_timeout", cleanup_receipt=receipt) from None
        if isinstance(caught, PlatformDataCollectionError):
            raise _error(caught.error_code, cleanup_receipt=receipt, failure_diagnostic=caught.failure_diagnostic) from None
        if caught is not None:
            raise _error("metric_payload_invalid", cleanup_receipt=receipt) from None
        if not captures:
            raise _error("metric_payload_empty", cleanup_receipt=receipt)
        return result, receipt
