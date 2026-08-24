# -*- coding: utf-8 -*-
"""抖音评论合同的只读结构观察与失败关闭加载。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import unquote, urlsplit

from .paths import COOKIE_DIR
from .platform_data_collection_errors import CleanupReceipt
from .platform_data_comment_models import CommentInsightFailure


DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parent
    / "platform_data_douyin_comment_contract.json"
)
_CREATOR_HOST = "creator.douyin.com"
_SAFE_TYPE_NAMES = frozenset(
    {"dict", "list", "str", "int", "float", "bool", "null"}
)
_IDENTITY_SEGMENTS = frozenset(
    {
        "author",
        "author_id",
        "author_info",
        "avatar",
        "avatar_url",
        "account",
        "account_id",
        "account_info",
        "creator",
        "creator_id",
        "creator_info",
        "display_name",
        "douyin_id",
        "nickname",
        "openid",
        "owner",
        "owner_id",
        "owner_info",
        "profile",
        "sec_uid",
        "uid",
        "unique_id",
        "user",
        "user_id",
        "user_info",
        "username",
    }
)
_IDENTITY_ACTOR_TOKENS = frozenset(
    {
        "account",
        "author",
        "avatar",
        "creator",
        "nickname",
        "openid",
        "owner",
        "reviewer",
        "user",
        "username",
    }
)
_COMPACT_IDENTITY_RE = re.compile(
    r"^(?:account|author|avatar|creator|nickname|openid|owner|reviewer|user)"
    r"(?:detail|id|info|metadata|name|profile|uri|url)?$"
)
_PATH_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_PATH_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_KEY_PATH_RE = re.compile(r"^[A-Za-z0-9_$.-]+(?:\[\])?(?:\.[A-Za-z0-9_$-]+(?:\[\])?)*$")
_METRIC_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PAGINATION_TRIGGER_RE = re.compile(
    r"^(?:click|scroll):[A-Za-z0-9_.:-]{1,200}$"
)
_OFFICIAL_ROOT = "https://creator.douyin.com"
_MAX_RESPONSES = 50
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_MAX_KEY_PATHS = 2048
_TOTAL_WALL_SECONDS = 60.0
_OBSERVATION_WINDOW_MS = 15_000.0
_SHAPE_WORKER_LOCK = threading.Lock()
_ACTIVE_SHAPE_WORKER: threading.Thread | None = None
_PAGINATION_NAMES = frozenset(
    {
        "cursor",
        "has_more",
        "hasmore",
        "next_cursor",
        "next_page_token",
        "offset",
        "page",
        "page_token",
    }
)


def _default_playwright_factory():
    from playwright.async_api import async_playwright

    return async_playwright()


_PLAYWRIGHT_FACTORY = _default_playwright_factory


@dataclass(frozen=True, slots=True)
class ContractResponseShape:
    scheme: str
    host: str
    path: str
    method: str
    key_paths: tuple[str, ...]
    field_types: tuple[tuple[str, str], ...]
    pagination_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.scheme) is not str
            or self.scheme != "https"
            or type(self.host) is not str
            or self.host != _CREATOR_HOST
            or not _is_fixed_path(self.path)
            or type(self.method) is not str
            or not self.method
            or self.method != self.method.upper()
            or type(self.key_paths) is not tuple
            or not self.key_paths
            or type(self.field_types) is not tuple
            or type(self.pagination_fields) is not tuple
        ):
            _unavailable()
        if any(_safe_key_path(item) != item for item in self.key_paths):
            _unavailable()
        if len(set(self.key_paths)) != len(self.key_paths):
            _unavailable()
        for item in self.field_types:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or item[0] not in self.key_paths
                or type(item[1]) is not str
                or item[1] not in _SAFE_TYPE_NAMES
            ):
                _unavailable()
        if len(set(self.field_types)) != len(self.field_types):
            _unavailable()
        if any(
            type(item) is not str or item not in self.key_paths
            for item in self.pagination_fields
        ):
            _unavailable()
        if len(set(self.pagination_fields)) != len(self.pagination_fields):
            _unavailable()


@dataclass(frozen=True, slots=True)
class ContractObservation:
    verified: bool
    responses: tuple[ContractResponseShape, ...]
    navigation_templates: tuple[str, ...]
    pagination_triggers: tuple[str, ...]
    cleanup: CleanupReceipt

    def __post_init__(self) -> None:
        if (
            type(self.responses) is not tuple
            or not all(type(item) is ContractResponseShape for item in self.responses)
            or type(self.navigation_templates) is not tuple
            or not all(
                type(item) is str and _is_navigation_template(item)
                for item in self.navigation_templates
            )
            or type(self.pagination_triggers) is not tuple
            or not all(
                _is_pagination_trigger(item)
                for item in self.pagination_triggers
            )
            or type(self.cleanup) is not CleanupReceipt
        ):
            _unavailable()
        if (
            len(set(self.navigation_templates))
            != len(self.navigation_templates)
            or len(set(self.pagination_triggers))
            != len(self.pagination_triggers)
        ):
            _unavailable()


@dataclass(frozen=True, slots=True)
class ContractSelection:
    content_response_path: str
    content_navigation_template: str
    content_list_field: str
    content_id_field: str
    content_title_field: str
    content_cover_field: str
    content_published_at_field: str
    content_status_field: str
    content_type_field: str
    content_metric_fields: tuple[tuple[str, str], ...]
    content_cursor_field: str
    content_has_more_field: str
    comment_response_path: str
    comment_navigation_template: str
    comment_list_field: str
    comment_id_field: str
    comment_content_id_field: str
    comment_parent_id_field: str
    comment_body_field: str
    comment_like_count_field: str
    comment_reply_count_field: str
    comment_commented_at_field: str
    comment_cursor_field: str
    comment_has_more_field: str
    comment_pagination_trigger: str


@dataclass(frozen=True, slots=True)
class DouyinCommentContract:
    schema_version: int
    verified: bool
    creator_host: str
    content_response_method: str
    comment_response_method: str
    content_response_path: str
    content_navigation_template: str
    content_list_field: str
    content_id_field: str
    content_title_field: str
    content_cover_field: str
    content_published_at_field: str
    content_status_field: str
    content_type_field: str
    content_metric_fields: tuple[tuple[str, str], ...]
    content_cursor_field: str
    content_has_more_field: str
    comment_response_path: str
    comment_navigation_template: str
    comment_list_field: str
    comment_id_field: str
    comment_content_id_field: str
    comment_parent_id_field: str
    comment_body_field: str
    comment_like_count_field: str
    comment_reply_count_field: str
    comment_commented_at_field: str
    comment_cursor_field: str
    comment_has_more_field: str
    comment_pagination_trigger: str

    @property
    def comment_time_field(self) -> str:
        """兼容采集计划中对评论时间字段的简写。"""

        return self.comment_commented_at_field


def _unavailable() -> None:
    raise CommentInsightFailure("comment_content_unavailable")


def _path_syntax_is_safe(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith("/")
        and "?" not in value
        and "#" not in value
        and "\\" not in value
        and "//" not in value
    )


def _is_concrete_identifier_segment(value: str) -> bool:
    decoded = unquote(value)
    if decoded == "{content_id}":
        return False
    if _PATH_UUID_RE.fullmatch(decoded) is not None:
        return True
    if decoded.isdecimal() and len(decoded) >= 6:
        return True
    return (
        len(decoded) >= 12
        and _PATH_OPAQUE_RE.fullmatch(decoded) is not None
        and any(character.isdigit() for character in decoded)
    )


def _sanitize_path(value: object) -> str | None:
    if not _path_syntax_is_safe(value):
        return None
    assert type(value) is str
    segments = value.split("/")
    safe_segments = [
        "{content_id}" if _is_concrete_identifier_segment(item) else item
        for item in segments
    ]
    sanitized = "/".join(safe_segments)
    braces_removed = sanitized.replace("{content_id}", "")
    if "{" in braces_removed or "}" in braces_removed:
        return None
    return sanitized


def _is_fixed_path(value: object) -> bool:
    return type(value) is str and _sanitize_path(value) == value


def _is_navigation_template(value: object) -> bool:
    if not _is_fixed_path(value):
        return False
    braces_removed = value.replace("{content_id}", "")
    return "{" not in braces_removed and "}" not in braces_removed


def _is_pagination_trigger(value: object) -> bool:
    return (
        type(value) is str
        and _PAGINATION_TRIGGER_RE.fullmatch(value) is not None
    )


def _safe_key_path(value: object) -> str | None:
    if type(value) is not str or not value or _KEY_PATH_RE.fullmatch(value) is None:
        return None
    for part in value.split("."):
        segment = part.removesuffix("[]")
        snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", segment).lower()
        tokens = tuple(
            token for token in re.split(r"[^a-z0-9]+", snake_case) if token
        )
        normalized = "_".join(tokens)
        compact = "".join(tokens)
        if (
            normalized in _IDENTITY_SEGMENTS
            or any(token in _IDENTITY_ACTOR_TOKENS for token in tokens)
            or _COMPACT_IDENTITY_RE.fullmatch(compact) is not None
            or ("profile" in tokens and tokens != ("profile", "visits"))
        ):
            return None
    return value


def _safe_response(value: object) -> dict | None:
    if type(value) is not dict:
        return None
    raw_url = value.get("url")
    if type(raw_url) is not str:
        return None
    parsed = urlsplit(raw_url)
    if parsed.scheme != "https" or parsed.hostname != _CREATOR_HOST:
        return None
    method = value.get("method")
    if type(method) is not str or not method or method != method.upper():
        return None
    keys_value = value.get("keys")
    if type(keys_value) not in (list, tuple):
        return None
    keys = tuple(
        safe
        for item in keys_value
        if (safe := _safe_key_path(item)) is not None
    )
    types_value = value.get("fieldTypes")
    field_types: list[list[str]] = []
    if type(types_value) in (list, tuple):
        for item in types_value:
            if type(item) not in (list, tuple) or len(item) != 2:
                continue
            key = _safe_key_path(item[0])
            type_name = item[1]
            if (
                key is not None
                and key in keys
                and type(type_name) is str
                and type_name in _SAFE_TYPE_NAMES
            ):
                field_types.append([key, type_name])
    path = _sanitize_path(parsed.path or "/")
    if path is None:
        return None
    return {
        "scheme": "https",
        "host": _CREATOR_HOST,
        "path": path,
        "method": method,
        "keyPaths": list(dict.fromkeys(keys)),
        "fieldTypes": field_types,
    }


def _response_payload(response: ContractResponseShape) -> dict:
    return {
        "scheme": response.scheme,
        "host": response.host,
        "path": response.path,
        "method": response.method,
        "keyPaths": list(response.key_paths),
        "fieldTypes": [list(item) for item in response.field_types],
        "paginationFields": list(response.pagination_fields),
    }


def sanitize_contract_observation(value: object) -> dict:
    """只保留官方响应的无值结构；所有原始值和自定义容器均丢弃。"""

    if type(value) is ContractObservation:
        return {
            "verified": (
                value.verified if type(value.verified) is bool else False
            ),
            "responses": [
                _response_payload(response) for response in value.responses
            ],
            "navigationTemplates": list(value.navigation_templates),
            "paginationTriggers": list(value.pagination_triggers),
            "cleanup": value.cleanup.public_payload(),
        }
    if type(value) is not dict:
        return {"responses": []}
    responses = value.get("responses")
    if type(responses) not in (list, tuple):
        return {"responses": []}
    safe_responses = [
        safe
        for item in responses
        if (safe := _safe_response(item)) is not None
    ]
    return {"responses": safe_responses}


def _required_state(account: object) -> Path:
    if type(account) is not dict:
        _unavailable()
    if (
        type(account.get("id")) is not int
        or account["id"] <= 0
        or type(account.get("type")) is not int
        or account["type"] != 3
        or type(account.get("filePath")) is not str
        or not account["filePath"].strip()
    ):
        _unavailable()
    state = COOKIE_DIR / Path(account["filePath"]).name
    try:
        if state.is_symlink() or not state.is_file():
            raise FileNotFoundError
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise CommentInsightFailure("comment_login_required") from None
    return state


def _builtin_type_name(value: object) -> str | None:
    if type(value) is dict:
        return "dict"
    if type(value) is list:
        return "list"
    if type(value) is str:
        return "str"
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "float"
    if value is None:
        return "null"
    return None


def _json_shape(
    payload: object,
    *,
    max_paths: int | None = None,
    deadline: float | None = None,
) -> tuple[
    tuple[str, ...], tuple[tuple[str, str], ...], tuple[str, ...]
]:
    path_limit = _MAX_KEY_PATHS if max_paths is None else max_paths
    if type(path_limit) is not int or path_limit < 0:
        raise CommentInsightFailure("comment_payload_invalid")
    paths: list[str] = []
    types: list[tuple[str, str]] = []

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError

    def remember(path: str, type_name: str) -> None:
        if path not in paths:
            paths.append(path)
        pair = (path, type_name)
        if pair not in types:
            types.append(pair)
        if len(paths) > path_limit:
            raise CommentInsightFailure("comment_payload_invalid")

    def visit(value: object, path: str) -> None:
        check_deadline()
        type_name = _builtin_type_name(value)
        if type_name is None:
            raise CommentInsightFailure("comment_payload_invalid")
        if type(value) is dict:
            if path:
                remember(path, type_name)
            for key, child in value.items():
                if type(key) is not str or not key:
                    raise CommentInsightFailure("comment_payload_invalid")
                child_path = f"{path}.{key}" if path else key
                if _safe_key_path(child_path) != child_path:
                    continue
                visit(child, child_path)
            return
        if type(value) is list:
            array_path = f"{path}[]"
            if _safe_key_path(array_path) != array_path:
                return
            remember(array_path, type_name)
            for child in value:
                visit(child, array_path)
            return
        if not path or _safe_key_path(path) != path:
            return
        remember(path, type_name)

    visit(payload, "")
    pagination = tuple(
        path
        for path in paths
        if path.rsplit(".", 1)[-1].removesuffix("[]").lower()
        in _PAGINATION_NAMES
    )
    return tuple(paths), tuple(types), pagination


def _content_type(headers: object) -> str:
    if type(headers) is not dict:
        return ""
    for key, value in headers.items():
        if (
            type(key) is str
            and key.lower() == "content-type"
            and type(value) is str
        ):
            return value.split(";", 1)[0].strip().lower()
    return ""


def _json_content_type(value: str) -> bool:
    return value == "application/json" or value.endswith("+json")


def _navigation_path(value: object) -> str | None:
    if type(value) is not str:
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != _CREATOR_HOST:
        return None
    path = _sanitize_path(parsed.path or "/")
    if path is None:
        return None
    return path if _is_navigation_template(path) else None


def _navigation_failure(value: object) -> str | None:
    if type(value) is not str:
        return "comment_login_required"
    lowered = value.lower()
    if any(marker in lowered for marker in ("captcha", "verification", "verify")):
        return "comment_verification_required"
    if any(marker in lowered for marker in ("forbidden", "no_permission", "unauthorized")):
        return "comment_access_denied"
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _CREATOR_HOST
        or any(marker in lowered for marker in ("/login", "passport."))
    ):
        return "comment_login_required"
    return None


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _run_shape_before_deadline(
    operation: Callable[[], object], deadline: float
) -> object:
    global _ACTIVE_SHAPE_WORKER
    completed = threading.Event()
    outcome: list[object] = []

    def invoke() -> None:
        global _ACTIVE_SHAPE_WORKER
        try:
            result = operation()
            if time.monotonic() >= deadline:
                outcome.append(TimeoutError())
            else:
                outcome.append(result)
        except BaseException as exc:
            outcome.append(exc)
        finally:
            completed.set()
            current = threading.current_thread()
            with _SHAPE_WORKER_LOCK:
                if _ACTIVE_SHAPE_WORKER is current:
                    _ACTIVE_SHAPE_WORKER = None

    worker = threading.Thread(
        target=invoke,
        name="douyin-comment-contract-shape",
        daemon=True,
    )
    with _SHAPE_WORKER_LOCK:
        active = _ACTIVE_SHAPE_WORKER
        if active is not None and active.is_alive():
            raise TimeoutError
        _ACTIVE_SHAPE_WORKER = worker
        try:
            worker.start()
        except BaseException:
            _ACTIVE_SHAPE_WORKER = None
            raise
    worker.join(_remaining_seconds(deadline))
    if not completed.is_set():
        raise TimeoutError
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def _active_shape_workers() -> tuple[threading.Thread, ...]:
    with _SHAPE_WORKER_LOCK:
        worker = _ACTIVE_SHAPE_WORKER
        if worker is None or not worker.is_alive():
            return ()
        return (worker,)


def _parse_json_shape_before_deadline(
    body: bytes,
    *,
    max_paths: int,
    deadline: float,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[str, ...]]:
    try:
        payload = json.loads(body)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise CommentInsightFailure("comment_payload_invalid") from None
    if time.monotonic() >= deadline:
        raise TimeoutError
    return _json_shape(
        payload,
        max_paths=max_paths,
        deadline=deadline,
    )


async def _await_before_deadline(value: object, deadline: float) -> object:
    if not hasattr(value, "__await__"):
        return value
    remaining = _remaining_seconds(deadline)
    if remaining <= 0:
        close = getattr(value, "close", None)
        if callable(close):
            close()
        raise TimeoutError
    return await asyncio.wait_for(value, timeout=remaining)


async def _close_resource(
    resource: object | None, deadline: float
) -> BaseException | None:
    if resource is None:
        return None
    close = getattr(resource, "close", None)
    if close is None:
        close = getattr(resource, "stop", None)
    if close is None:
        return RuntimeError("close unavailable")
    try:
        if inspect.iscoroutinefunction(close):
            result = close()
        else:
            completed = threading.Event()
            outcome: list[object] = []

            def invoke() -> None:
                try:
                    outcome.append(close())
                except BaseException as exc:
                    outcome.append(exc)
                finally:
                    completed.set()

            worker = threading.Thread(
                target=invoke,
                name="douyin-comment-contract-close",
                daemon=True,
            )
            worker.start()
            worker.join(_remaining_seconds(deadline))
            if not completed.is_set():
                return TimeoutError()
            result = outcome[0]
            if isinstance(result, BaseException):
                return result
        if hasattr(result, "__await__"):
            await _await_before_deadline(result, deadline)
    except BaseException as exc:
        return exc
    return None


def _emit_report(
    report: Callable[[dict], None] | None,
    *,
    deadline: float,
    status: str,
    error_code: str,
    responses: tuple[ContractResponseShape, ...],
    navigation_templates: tuple[str, ...],
    pagination_triggers: tuple[str, ...],
    cleanup: CleanupReceipt,
) -> bool:
    if report is None:
        return True
    field_names = sorted(
        {
            path
            for response in responses
            for path in response.key_paths
        }
    )
    payload = {
        "status": status,
        "errorCode": error_code,
        "responseCount": len(responses),
        "responsePaths": [response.path for response in responses],
        "fieldNames": field_names,
        "paginationFields": sorted(
            {
                field
                for response in responses
                for field in response.pagination_fields
            }
        ),
        "navigationTemplates": list(navigation_templates),
        "paginationTriggers": list(pagination_triggers),
        "cleanup": cleanup.public_payload(),
    }
    completed = threading.Event()

    def invoke() -> None:
        try:
            report(payload)
        except Exception:
            pass
        finally:
            completed.set()

    worker = threading.Thread(
        target=invoke,
        name="douyin-comment-contract-report",
        daemon=True,
    )
    worker.start()
    worker.join(_remaining_seconds(deadline))
    return completed.is_set()


def _failure_with_cleanup(
    error_code: str,
    cleanup: CleanupReceipt,
    *,
    retryable: bool = False,
) -> CommentInsightFailure:
    failure = CommentInsightFailure(error_code, retryable=retryable)
    failure.cleanup_receipt = cleanup
    return failure


async def _observe_contract_responses_async(
    account: dict,
    report: Callable[[dict], None] | None,
    hard_deadline: float,
) -> ContractObservation:
    state_path = _required_state(account)
    playwright = None
    browser = None
    context = None
    page = None
    caught: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    responses: list[ContractResponseShape] = []
    navigation_paths: list[str] = []
    listener_tasks: set[asyncio.Task] = set()
    seen_response_ids: set[int] = set()
    response_count = 0
    total_bytes = 0
    total_key_paths = 0
    fatal_error: BaseException | None = None
    limits_lock = asyncio.Lock()
    body_budget_lock = asyncio.Lock()
    shape_budget_lock = asyncio.Lock()
    initial_remaining = _remaining_seconds(hard_deadline)
    cleanup_reserve = min(5.0, initial_remaining * 0.5)
    operation_deadline = hard_deadline - cleanup_reserve
    cleanup_deadline = hard_deadline - (cleanup_reserve / 3)

    async def consume_response(response: object) -> None:
        nonlocal response_count, total_bytes, total_key_paths, fatal_error
        if fatal_error is not None:
            return
        try:
            url = getattr(response, "url", None)
            if type(url) is not str:
                return
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != _CREATOR_HOST:
                return
            content_type = _content_type(getattr(response, "headers", None))
            if not _json_content_type(content_type):
                return
            status = getattr(response, "status", None)
            if type(status) is int and status in (401, 403):
                raise CommentInsightFailure(
                    "comment_login_required"
                    if status == 401
                    else "comment_access_denied"
                )
            request = getattr(response, "request", None)
            method = getattr(request, "method", None)
            if type(method) is not str or not method or method != method.upper():
                raise CommentInsightFailure("comment_payload_invalid")
            async with limits_lock:
                response_count += 1
                if response_count > _MAX_RESPONSES:
                    raise CommentInsightFailure("comment_payload_invalid")
            async with body_budget_lock:
                body_reader = getattr(response, "body", None)
                if not callable(body_reader):
                    raise CommentInsightFailure("comment_payload_invalid")
                if not inspect.iscoroutinefunction(body_reader):
                    raise CommentInsightFailure("comment_payload_invalid")
                if total_bytes >= _MAX_TOTAL_BYTES:
                    raise CommentInsightFailure("comment_payload_invalid")
                body = body_reader()
                if hasattr(body, "__await__"):
                    body = await _await_before_deadline(
                        body, operation_deadline
                    )
                remaining_bytes = _MAX_TOTAL_BYTES - total_bytes
                if (
                    type(body) is not bytes
                    or len(body) > _MAX_RESPONSE_BYTES
                    or len(body) > remaining_bytes
                ):
                    raise CommentInsightFailure("comment_payload_invalid")
                total_bytes += len(body)
            async with shape_budget_lock:
                remaining_paths = _MAX_KEY_PATHS - total_key_paths
                key_paths, field_types, pagination_fields = (
                    _run_shape_before_deadline(
                        lambda: _parse_json_shape_before_deadline(
                            body,
                            max_paths=remaining_paths,
                            deadline=operation_deadline,
                        ),
                        operation_deadline,
                    )
                )
                total_key_paths += len(key_paths)
            safe_path = _sanitize_path(parsed.path or "/")
            if safe_path is None:
                raise CommentInsightFailure("comment_payload_invalid")
            shape = ContractResponseShape(
                scheme="https",
                host=_CREATOR_HOST,
                path=safe_path,
                method=method,
                key_paths=key_paths,
                field_types=field_types,
                pagination_fields=pagination_fields,
            )
            if shape not in responses:
                responses.append(shape)
        except BaseException as exc:
            fatal_error = exc

    def observe_response(response: object) -> None:
        identity = id(response)
        if identity in seen_response_ids:
            return
        seen_response_ids.add(identity)
        task = asyncio.create_task(consume_response(response))
        listener_tasks.add(task)
        task.add_done_callback(listener_tasks.discard)

    def observe_navigation(frame: object) -> None:
        navigation = _navigation_path(getattr(frame, "url", None))
        if navigation is not None and navigation not in navigation_paths:
            navigation_paths.append(navigation)

    try:
        starter = _PLAYWRIGHT_FACTORY()
        playwright = await _await_before_deadline(
            starter.start(), operation_deadline
        )
        browser = await _await_before_deadline(
            playwright.chromium.launch(headless=False), operation_deadline
        )
        context = await _await_before_deadline(
            browser.new_context(storage_state=str(state_path)),
            operation_deadline,
        )
        page = await _await_before_deadline(
            context.new_page(), operation_deadline
        )
        page.on("response", observe_response)
        page.on("framenavigated", observe_navigation)
        navigation_timeout_ms = _remaining_seconds(operation_deadline) * 1000
        await _await_before_deadline(
            page.goto(
                _OFFICIAL_ROOT,
                wait_until="domcontentloaded",
                timeout=navigation_timeout_ms,
            ),
            operation_deadline,
        )
        navigation_error = _navigation_failure(getattr(page, "url", None))
        if navigation_error is not None:
            raise CommentInsightFailure(navigation_error)
        current_navigation = _navigation_path(getattr(page, "url", None))
        if (
            current_navigation is not None
            and current_navigation not in navigation_paths
        ):
            navigation_paths.append(current_navigation)
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if not callable(wait_for_timeout):
            raise CommentInsightFailure("comment_content_unavailable")
        observation_ms = min(
            _OBSERVATION_WINDOW_MS,
            _remaining_seconds(operation_deadline) * 1000,
        )
        await _await_before_deadline(
            wait_for_timeout(observation_ms), operation_deadline
        )
        if listener_tasks:
            await _await_before_deadline(
                asyncio.gather(
                    *tuple(listener_tasks), return_exceptions=True
                ),
                operation_deadline,
            )
        if fatal_error is not None:
            raise fatal_error
        if not responses:
            raise CommentInsightFailure("comment_content_unavailable")
    except BaseException as exc:
        caught = exc
    finally:
        for task in tuple(listener_tasks):
            if not task.done():
                task.cancel()
        if listener_tasks:
            try:
                await _await_before_deadline(
                    asyncio.gather(
                        *tuple(listener_tasks), return_exceptions=True
                    ),
                    cleanup_deadline,
                )
            except BaseException as exc:
                if caught is None:
                    caught = exc
        resources = (page, context, browser, playwright)
        shape_workers = _active_shape_workers()
        for index, worker in enumerate(shape_workers):
            remaining_targets = len(shape_workers) - index + len(resources)
            worker_deadline = time.monotonic() + (
                _remaining_seconds(cleanup_deadline) / remaining_targets
            )
            worker.join(_remaining_seconds(worker_deadline))
            if worker.is_alive():
                cleanup_errors.append(TimeoutError())
        for index, resource in enumerate(resources):
            resource_count = len(resources) - index
            close_deadline = time.monotonic() + (
                _remaining_seconds(cleanup_deadline) / resource_count
            )
            close_error = await _close_resource(resource, close_deadline)
            if close_error is not None:
                cleanup_errors.append(close_error)

    cleanup = CleanupReceipt(
        closed=not cleanup_errors,
        alive_resource_count=len(cleanup_errors),
    )
    response_tuple = tuple(responses)
    navigation_tuple = tuple(navigation_paths)
    # 响应字段只证明“平台存在分页状态”，不能冒充已验证的 UI 翻页动作。
    pagination_tuple: tuple[str, ...] = ()
    if isinstance(caught, (KeyboardInterrupt, SystemExit)):
        raise caught
    for cleanup_error in cleanup_errors:
        if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
            raise cleanup_error
    if cleanup_errors:
        report_completed = _emit_report(
            report,
            deadline=hard_deadline,
            status="failed",
            error_code="comment_sync_cancelled",
            responses=response_tuple,
            navigation_templates=navigation_tuple,
            pagination_triggers=pagination_tuple,
            cleanup=cleanup,
        )
        if not report_completed:
            raise _failure_with_cleanup(
                "comment_sync_timeout", cleanup, retryable=True
            ) from None
        raise _failure_with_cleanup(
            "comment_sync_cancelled", cleanup
        ) from None
    if isinstance(caught, asyncio.CancelledError):
        report_completed = _emit_report(
            report,
            deadline=hard_deadline,
            status="failed",
            error_code="comment_sync_cancelled",
            responses=response_tuple,
            navigation_templates=navigation_tuple,
            pagination_triggers=pagination_tuple,
            cleanup=cleanup,
        )
        if not report_completed:
            raise _failure_with_cleanup(
                "comment_sync_timeout", cleanup, retryable=True
            ) from None
        raise _failure_with_cleanup(
            "comment_sync_cancelled", cleanup, retryable=True
        ) from None
    if isinstance(caught, TimeoutError):
        _emit_report(
            report,
            deadline=hard_deadline,
            status="failed",
            error_code="comment_sync_timeout",
            responses=response_tuple,
            navigation_templates=navigation_tuple,
            pagination_triggers=pagination_tuple,
            cleanup=cleanup,
        )
        raise _failure_with_cleanup(
            "comment_sync_timeout", cleanup, retryable=True
        ) from None
    if isinstance(caught, CommentInsightFailure):
        report_completed = _emit_report(
            report,
            deadline=hard_deadline,
            status="failed",
            error_code=caught.error_code,
            responses=response_tuple,
            navigation_templates=navigation_tuple,
            pagination_triggers=pagination_tuple,
            cleanup=cleanup,
        )
        if not report_completed:
            raise _failure_with_cleanup(
                "comment_sync_timeout", cleanup, retryable=True
            ) from None
        caught.cleanup_receipt = cleanup
        raise caught
    if caught is not None:
        report_completed = _emit_report(
            report,
            deadline=hard_deadline,
            status="failed",
            error_code="comment_payload_invalid",
            responses=response_tuple,
            navigation_templates=navigation_tuple,
            pagination_triggers=pagination_tuple,
            cleanup=cleanup,
        )
        if not report_completed:
            raise _failure_with_cleanup(
                "comment_sync_timeout", cleanup, retryable=True
            ) from None
        raise _failure_with_cleanup(
            "comment_payload_invalid", cleanup
        ) from None
    observation = ContractObservation(
        verified=False,
        responses=response_tuple,
        navigation_templates=navigation_tuple,
        pagination_triggers=pagination_tuple,
        cleanup=cleanup,
    )
    report_completed = _emit_report(
        report,
        deadline=hard_deadline,
        status="observed_unverified",
        error_code="comment_content_unavailable",
        responses=response_tuple,
        navigation_templates=navigation_tuple,
        pagination_triggers=pagination_tuple,
        cleanup=cleanup,
    )
    if not report_completed:
        raise _failure_with_cleanup(
            "comment_sync_timeout", cleanup, retryable=True
        ) from None
    return observation


def observe_contract_responses(
    account: dict,
    report: Callable[[dict], None] | None = None,
) -> ContractObservation:
    """用现有登录态被动观察官网 JSON；从不重放或合成平台请求。"""

    hard_deadline = time.monotonic() + _TOTAL_WALL_SECONDS
    try:
        return asyncio.run(
            _observe_contract_responses_async(
                account, report, hard_deadline
            )
        )
    except CommentInsightFailure:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except TimeoutError:
        raise CommentInsightFailure(
            "comment_sync_timeout", retryable=True
        ) from None
    except asyncio.CancelledError:
        raise CommentInsightFailure(
            "comment_sync_cancelled", retryable=True
        ) from None
    except BaseException:
        raise CommentInsightFailure("comment_payload_invalid") from None


def _selection_fields(selection: ContractSelection, prefix: str) -> tuple[str, ...]:
    if prefix == "content":
        return (
            selection.content_list_field,
            selection.content_id_field,
            selection.content_title_field,
            selection.content_cover_field,
            selection.content_published_at_field,
            selection.content_status_field,
            selection.content_type_field,
            *(field for _metric_key, field in selection.content_metric_fields),
            selection.content_cursor_field,
            selection.content_has_more_field,
        )
    return (
        selection.comment_list_field,
        selection.comment_id_field,
        selection.comment_content_id_field,
        selection.comment_parent_id_field,
        selection.comment_body_field,
        selection.comment_like_count_field,
        selection.comment_reply_count_field,
        selection.comment_commented_at_field,
        selection.comment_cursor_field,
        selection.comment_has_more_field,
    )


def _validate_selection_shape(selection: object) -> ContractSelection:
    if type(selection) is not ContractSelection:
        _unavailable()
    text_fields = (
        selection.content_response_path,
        selection.content_navigation_template,
        selection.content_list_field,
        selection.content_id_field,
        selection.content_title_field,
        selection.content_cover_field,
        selection.content_published_at_field,
        selection.content_status_field,
        selection.content_type_field,
        selection.content_cursor_field,
        selection.content_has_more_field,
        selection.comment_response_path,
        selection.comment_navigation_template,
        selection.comment_list_field,
        selection.comment_id_field,
        selection.comment_content_id_field,
        selection.comment_parent_id_field,
        selection.comment_body_field,
        selection.comment_like_count_field,
        selection.comment_reply_count_field,
        selection.comment_commented_at_field,
        selection.comment_cursor_field,
        selection.comment_has_more_field,
        selection.comment_pagination_trigger,
    )
    if not all(type(item) is str and item for item in text_fields):
        _unavailable()
    if (
        not _is_fixed_path(selection.content_response_path)
        or not _is_fixed_path(selection.comment_response_path)
        or not _is_navigation_template(selection.content_navigation_template)
        or not _is_navigation_template(selection.comment_navigation_template)
    ):
        _unavailable()
    if any(
        _safe_key_path(item) != item
        for item in (
            *_selection_fields(selection, "content"),
            *_selection_fields(selection, "comment"),
        )
    ):
        _unavailable()
    if not _is_pagination_trigger(selection.comment_pagination_trigger):
        _unavailable()
    if type(selection.content_metric_fields) is not tuple:
        _unavailable()
    metric_keys: list[str] = []
    for item in selection.content_metric_fields:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or _METRIC_KEY_RE.fullmatch(item[0]) is None
            or type(item[1]) is not str
        ):
            _unavailable()
        metric_keys.append(item[0])
    if len(set(metric_keys)) != len(metric_keys):
        _unavailable()
    return selection


def _matching_methods(
    observation: ContractObservation,
    response_path: str,
    fields: tuple[str, ...],
    pagination_fields: tuple[str, str],
) -> tuple[str, ...]:
    path_methods = tuple(
        dict.fromkeys(
            response.method
            for response in observation.responses
            if response.path == response_path
        )
    )
    if len(path_methods) != 1:
        return path_methods
    required = set(fields)
    required_pagination = set(pagination_fields)
    matched = any(
        response.path == response_path
        and response.method == path_methods[0]
        and required.issubset(response.key_paths)
        and required_pagination.issubset(response.pagination_fields)
        for response in observation.responses
    )
    return path_methods if matched else ()


def _contract_from_selection(
    selection: ContractSelection,
    *,
    content_method: str,
    comment_method: str,
) -> DouyinCommentContract:
    if (
        type(content_method) is not str
        or not content_method
        or content_method != content_method.upper()
        or type(comment_method) is not str
        or not comment_method
        or comment_method != comment_method.upper()
    ):
        _unavailable()
    return DouyinCommentContract(
        schema_version=1,
        verified=True,
        creator_host=_CREATOR_HOST,
        content_response_method=content_method,
        comment_response_method=comment_method,
        **{
            field: getattr(selection, field)
            for field in ContractSelection.__dataclass_fields__
        },
    )


def _manifest_payload(contract: DouyinCommentContract) -> dict:
    return {
        "schemaVersion": contract.schema_version,
        "verified": contract.verified,
        "creatorHost": contract.creator_host,
        "contentList": {
            "method": contract.content_response_method,
            "responsePath": contract.content_response_path,
            "navigationTemplate": contract.content_navigation_template,
            "listField": contract.content_list_field,
            "idField": contract.content_id_field,
            "titleField": contract.content_title_field,
            "coverField": contract.content_cover_field,
            "publishedAtField": contract.content_published_at_field,
            "statusField": contract.content_status_field,
            "typeField": contract.content_type_field,
            "metricFields": [
                {"metricKey": metric_key, "field": field}
                for metric_key, field in contract.content_metric_fields
            ],
            "cursorField": contract.content_cursor_field,
            "hasMoreField": contract.content_has_more_field,
        },
        "commentList": {
            "method": contract.comment_response_method,
            "responsePath": contract.comment_response_path,
            "navigationTemplate": contract.comment_navigation_template,
            "listField": contract.comment_list_field,
            "idField": contract.comment_id_field,
            "contentIdField": contract.comment_content_id_field,
            "parentIdField": contract.comment_parent_id_field,
            "bodyField": contract.comment_body_field,
            "likeCountField": contract.comment_like_count_field,
            "replyCountField": contract.comment_reply_count_field,
            "commentedAtField": contract.comment_commented_at_field,
            "cursorField": contract.comment_cursor_field,
            "hasMoreField": contract.comment_has_more_field,
            "paginationTrigger": contract.comment_pagination_trigger,
        },
    }


def freeze_verified_contract(
    observation: ContractObservation,
    selection: ContractSelection,
    destination: Path,
) -> DouyinCommentContract:
    """仅从同一份已验证、已清理观察中原子冻结生产合同。"""

    if (
        type(observation) is not ContractObservation
        or type(observation.verified) is not bool
        or observation.verified is not True
        or type(observation.cleanup) is not CleanupReceipt
        or not observation.cleanup.closed
        or observation.cleanup.alive_resource_count != 0
    ):
        _unavailable()
    selected = _validate_selection_shape(selection)
    content_methods = _matching_methods(
        observation,
        selected.content_response_path,
        _selection_fields(selected, "content"),
        (
            selected.content_cursor_field,
            selected.content_has_more_field,
        ),
    )
    comment_methods = _matching_methods(
        observation,
        selected.comment_response_path,
        _selection_fields(selected, "comment"),
        (
            selected.comment_cursor_field,
            selected.comment_has_more_field,
        ),
    )
    if (
        selected.content_navigation_template
        not in observation.navigation_templates
        or selected.comment_navigation_template
        not in observation.navigation_templates
        or selected.comment_pagination_trigger
        not in observation.pagination_triggers
        or len(content_methods) != 1
        or len(comment_methods) != 1
    ):
        _unavailable()
    contract = _contract_from_selection(
        selected,
        content_method=content_methods[0],
        comment_method=comment_methods[0],
    )
    target = Path(destination)
    temporary_path: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                _manifest_payload(contract),
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        _unavailable()
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except BaseException:
                pass
    return contract


def _exact_dict(value: object, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        _unavailable()
    return value


def _manifest_contract(payload: object) -> DouyinCommentContract:
    root = _exact_dict(
        payload,
        {
            "schemaVersion",
            "verified",
            "creatorHost",
            "contentList",
            "commentList",
        },
    )
    if (
        type(root["schemaVersion"]) is not int
        or root["schemaVersion"] != 1
        or type(root["verified"]) is not bool
        or root["verified"] is not True
        or type(root["creatorHost"]) is not str
        or root["creatorHost"] != _CREATOR_HOST
    ):
        _unavailable()
    content = _exact_dict(
        root["contentList"],
        {
            "method",
            "responsePath",
            "navigationTemplate",
            "listField",
            "idField",
            "titleField",
            "coverField",
            "publishedAtField",
            "statusField",
            "typeField",
            "metricFields",
            "cursorField",
            "hasMoreField",
        },
    )
    comment = _exact_dict(
        root["commentList"],
        {
            "method",
            "responsePath",
            "navigationTemplate",
            "listField",
            "idField",
            "contentIdField",
            "parentIdField",
            "bodyField",
            "likeCountField",
            "replyCountField",
            "commentedAtField",
            "cursorField",
            "hasMoreField",
            "paginationTrigger",
        },
    )
    metric_values = content["metricFields"]
    if type(metric_values) is not list:
        _unavailable()
    metrics: list[tuple[str, str]] = []
    for item in metric_values:
        metric = _exact_dict(item, {"metricKey", "field"})
        metrics.append((metric["metricKey"], metric["field"]))
    selection = ContractSelection(
        content_response_path=content["responsePath"],
        content_navigation_template=content["navigationTemplate"],
        content_list_field=content["listField"],
        content_id_field=content["idField"],
        content_title_field=content["titleField"],
        content_cover_field=content["coverField"],
        content_published_at_field=content["publishedAtField"],
        content_status_field=content["statusField"],
        content_type_field=content["typeField"],
        content_metric_fields=tuple(metrics),
        content_cursor_field=content["cursorField"],
        content_has_more_field=content["hasMoreField"],
        comment_response_path=comment["responsePath"],
        comment_navigation_template=comment["navigationTemplate"],
        comment_list_field=comment["listField"],
        comment_id_field=comment["idField"],
        comment_content_id_field=comment["contentIdField"],
        comment_parent_id_field=comment["parentIdField"],
        comment_body_field=comment["bodyField"],
        comment_like_count_field=comment["likeCountField"],
        comment_reply_count_field=comment["replyCountField"],
        comment_commented_at_field=comment["commentedAtField"],
        comment_cursor_field=comment["cursorField"],
        comment_has_more_field=comment["hasMoreField"],
        comment_pagination_trigger=comment["paginationTrigger"],
    )
    return _contract_from_selection(
        _validate_selection_shape(selection),
        content_method=content["method"],
        comment_method=comment["method"],
    )


def load_verified_contract(
    path: Path = DEFAULT_CONTRACT_PATH,
) -> DouyinCommentContract:
    """加载生产合同；缺失、未验证或结构不完整时固定失败。"""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        _unavailable()
    try:
        return _manifest_contract(payload)
    except CommentInsightFailure:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        _unavailable()
