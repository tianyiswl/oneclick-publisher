# -*- coding: utf-8 -*-
"""抖音一级评论的短生命周期只读采集器。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import inspect
from pathlib import Path
import queue
import re
import threading
import time
from typing import Callable
from urllib.parse import quote, urlsplit

from .paths import COOKIE_DIR
from .platform_data_collection_errors import CleanupReceipt
from .platform_data_comment_contract import (
    DouyinCommentContract,
    load_verified_contract,
)
from .platform_data_comment_models import (
    CommentCollectionBatch,
    CommentInsightFailure,
    CommentPage,
    CommentRecord,
    derive_comment_key,
)


_MISSING = object()
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_TRIGGER_RE = re.compile(r"^(click|scroll):([A-Za-z0-9_.:-]{1,200})$")
_JSON_MEDIA_TYPE_RE = re.compile(
    r"^application/(?:json|[a-z0-9][a-z0-9!#$&^_.-]*\+json)$"
)
_CHARSET_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_MAX_RESPONSE_PAGES = 50
_MAX_QUEUED_RESPONSES = 8
_MAX_RAW_ROWS = 256
_MAX_REJECTED_ROWS = 128
_MAX_PARSED_COMMENTS = 101
_MAX_CONTAINER_KEYS = 64
_MAX_HOST_LENGTH = 253
_MAX_METHOD_LENGTH = 16
_MAX_RESPONSE_PATH_LENGTH = 2_048
_MAX_NAVIGATION_TEMPLATE_LENGTH = 4_096
_MAX_CONTRACT_FIELD_LENGTH = 512
_MAX_PAGINATION_TRIGGER_LENGTH = 207
_MAX_RUNTIME_URL_LENGTH = 8_192
_MAX_HEADER_NAME_LENGTH = 128
_MAX_CONTENT_TYPE_LENGTH = 256
_MAX_CONTENT_ID_LENGTH = 512
_MAX_STATE_FILE_PATH_LENGTH = 1_024
_MAX_COMMENT_ID_LENGTH = 512
_MAX_COMMENT_BODY_LENGTH = 20_000
_MAX_COMMENT_TIME_LENGTH = 128
_MAX_CURSOR_LENGTH = 2_048
_DEFAULT_TOTAL_TIMEOUT_SECONDS = 30.0
_REPORT_LOCK = threading.Lock()
_REPORT_QUEUE: queue.Queue = queue.Queue(maxsize=1)
_REPORT_WORKER: threading.Thread | None = None
_REPORT_BUSY = False
_PROJECTION_LOCK = threading.Lock()
_PROJECTION_QUEUE: queue.Queue = queue.Queue(maxsize=1)
_PROJECTION_WORKER: threading.Thread | None = None
_PROJECTION_BUSY = False


@dataclass(frozen=True, slots=True)
class _ParseOutcome:
    page: CommentPage | None = None
    rejected_count: int = 0
    error_code: str = ""
    control_kind: str = ""


@dataclass(frozen=True, slots=True)
class _PreparedPage:
    rows: tuple[tuple[str, str, int, int, str], ...]
    rejected_count: int
    has_more: bool
    next_cursor: str
    platform_end: bool


@dataclass(slots=True)
class _ProjectionJob:
    prepared: _PreparedPage | None
    account_id: int
    content_id: str
    observed_at: str
    completed: threading.Event = field(default_factory=threading.Event)
    outcome: _ParseOutcome | None = None


@dataclass(frozen=True, slots=True)
class _CollectionOutcome:
    batch: CommentCollectionBatch | None
    error_code: str
    retryable: bool
    control_kind: str
    cleanup_receipt: CleanupReceipt


class _ParseDeadlineExceeded(RuntimeError):
    pass


class _ContentBindingMismatch(RuntimeError):
    pass


class _OversizedInput(RuntimeError):
    pass


def _control_kind(value: BaseException) -> str:
    if isinstance(value, asyncio.CancelledError):
        return "cancelled"
    if isinstance(value, KeyboardInterrupt):
        return "keyboard_interrupt"
    if isinstance(value, SystemExit):
        return "system_exit"
    return ""


def _raise_fresh_control(kind: str) -> None:
    if kind == "cancelled":
        raise asyncio.CancelledError() from None
    if kind == "keyboard_interrupt":
        raise KeyboardInterrupt() from None
    if kind == "system_exit":
        raise SystemExit() from None
    _invalid()


def _invalid() -> None:
    raise CommentInsightFailure("comment_payload_invalid")


def _bounded_text(
    value: object,
    maximum: int,
    *,
    preserve_edges: bool = False,
) -> str:
    if type(value) is not str or len(value) < 1:
        _invalid()
    if len(value) > maximum:
        raise _OversizedInput
    stripped = value.strip()
    if not stripped or (not preserve_edges and value != stripped):
        _invalid()
    return value


def _bounded_count(value: object) -> int:
    if type(value) is not int or value < 0:
        _invalid()
    return value


def _is_bounded_builtin_text(value: object, maximum: int) -> bool:
    return type(value) is str and 0 < len(value) <= maximum


def _strict_path(root: object, path: object) -> object:
    if (
        type(root) is not dict
        or not _is_bounded_builtin_text(path, _MAX_CONTRACT_FIELD_LENGTH)
    ):
        _invalid()
    value = root
    for segment in path.split("."):
        if (
            not segment
            or segment.endswith("[]")
            or type(value) is not dict
            or len(value) > _MAX_CONTAINER_KEYS
        ):
            _invalid()
        if segment not in value:
            return _MISSING
        value = value[segment]
    return value


def _comment_rows(payload: object, list_field: object) -> list:
    if (
        type(payload) is not dict
        or not _is_bounded_builtin_text(
            list_field, _MAX_CONTRACT_FIELD_LENGTH
        )
    ):
        _invalid()
    if not list_field.endswith("[]"):
        _invalid()
    value = _strict_path(payload, list_field[:-2])
    if type(value) is not list:
        _invalid()
    return value


def _row_value(row: object, list_field: str, field: object) -> object:
    if (
        type(row) is not dict
        or not _is_bounded_builtin_text(
            list_field, _MAX_CONTRACT_FIELD_LENGTH
        )
        or not _is_bounded_builtin_text(
            field, _MAX_CONTRACT_FIELD_LENGTH
        )
    ):
        _invalid()
    prefix = f"{list_field}."
    if not field.startswith(prefix):
        _invalid()
    relative = field[len(prefix) :]
    value = _strict_path(row, relative)
    if value is _MISSING:
        _invalid()
    return value


def _prepare_comment_page_unsafe(
    contract: DouyinCommentContract,
    payload: object,
    account_id: int,
    content_id: str,
    observed_at: str,
    *,
    deadline: float | None = None,
) -> _PreparedPage:
    if (
        type(contract) is not DouyinCommentContract
        or contract.verified is not True
        or contract.creator_host != "creator.douyin.com"
        or type(account_id) is not int
        or account_id <= 0
    ):
        _invalid()
    _bounded_text(content_id, _MAX_CONTENT_ID_LENGTH)
    _bounded_text(observed_at, _MAX_COMMENT_TIME_LENGTH)

    rows = _comment_rows(payload, contract.comment_list_field)
    if len(rows) > _MAX_RAW_ROWS:
        raise _OversizedInput
    has_more = _strict_path(payload, contract.comment_has_more_field)
    raw_cursor = _strict_path(payload, contract.comment_cursor_field)
    if type(has_more) is not bool or raw_cursor is _MISSING:
        _invalid()
    if has_more:
        next_cursor = _bounded_text(raw_cursor, _MAX_CURSOR_LENGTH)
    else:
        if raw_cursor is None:
            next_cursor = ""
        elif type(raw_cursor) is str and raw_cursor == "":
            next_cursor = ""
        else:
            next_cursor = _bounded_text(raw_cursor, _MAX_CURSOR_LENGTH)

    for raw_row in rows:
        if deadline is not None and time.monotonic() >= deadline:
            raise _ParseDeadlineExceeded
        try:
            if type(raw_row) is not dict or len(raw_row) > _MAX_CONTAINER_KEYS:
                raise _ContentBindingMismatch
            row_content_id = _bounded_text(
                _row_value(
                    raw_row,
                    contract.comment_list_field,
                    contract.comment_content_id_field,
                ),
                _MAX_CONTENT_ID_LENGTH,
            )
        except (CommentInsightFailure, _OversizedInput):
            raise _ContentBindingMismatch from None
        if row_content_id != content_id:
            raise _ContentBindingMismatch

    projection_rows: list[tuple[str, str, int, int, str]] = []
    rejected_count = 0
    truncated_rows = False
    for index, raw_row in enumerate(rows):
        if deadline is not None and time.monotonic() >= deadline:
            raise _ParseDeadlineExceeded
        try:
            if type(raw_row) is not dict or len(raw_row) > _MAX_CONTAINER_KEYS:
                _invalid()
            parent_id = _row_value(
                raw_row,
                contract.comment_list_field,
                contract.comment_parent_id_field,
            )
            if parent_id is not None:
                continue
            platform_comment_id = _row_value(
                raw_row,
                contract.comment_list_field,
                contract.comment_id_field,
            )
            platform_comment_id = _bounded_text(
                platform_comment_id, _MAX_COMMENT_ID_LENGTH
            )
            body = _bounded_text(
                _row_value(
                    raw_row,
                    contract.comment_list_field,
                    contract.comment_body_field,
                ),
                _MAX_COMMENT_BODY_LENGTH,
                preserve_edges=True,
            )
            like_count = _bounded_count(
                _row_value(
                    raw_row,
                    contract.comment_list_field,
                    contract.comment_like_count_field,
                )
            )
            reply_count = _bounded_count(
                _row_value(
                    raw_row,
                    contract.comment_list_field,
                    contract.comment_reply_count_field,
                )
            )
            commented_at = _bounded_text(
                _row_value(
                    raw_row,
                    contract.comment_list_field,
                    contract.comment_commented_at_field,
                ),
                _MAX_COMMENT_TIME_LENGTH,
            )
            projection_rows.append(
                (
                    platform_comment_id,
                    body,
                    like_count,
                    reply_count,
                    commented_at,
                )
            )
            platform_comment_id = None
            body = None
        except CommentInsightFailure:
            rejected_count += 1
            if rejected_count > _MAX_REJECTED_ROWS:
                _invalid()
            continue
        if len(projection_rows) >= _MAX_PARSED_COMMENTS:
            truncated_rows = index + 1 < len(rows)
            break

    platform_end = not has_more and not truncated_rows
    if not projection_rows and not platform_end:
        _invalid()
    return _PreparedPage(
        rows=tuple(projection_rows),
        rejected_count=rejected_count,
        has_more=has_more,
        next_cursor=next_cursor,
        platform_end=platform_end,
    )


def _project_prepared_page_unsafe(
    prepared: _PreparedPage,
    account_id: int,
    content_id: str,
    observed_at: str,
) -> tuple[CommentPage, int]:
    records: dict[str, CommentRecord] = {}
    rejected_count = prepared.rejected_count
    for (
        platform_comment_id,
        body,
        like_count,
        reply_count,
        commented_at,
    ) in prepared.rows:
        try:
            comment_key = derive_comment_key(
                account_id, content_id, platform_comment_id
            )
            platform_comment_id = None
            record = CommentRecord(
                content_id=content_id,
                comment_key=comment_key,
                body=body,
                like_count=like_count,
                reply_count=reply_count,
                commented_at=commented_at,
                observed_at=observed_at,
            )
            body = None
        except CommentInsightFailure:
            rejected_count += 1
            if rejected_count > _MAX_REJECTED_ROWS:
                _invalid()
            continue
        existing = records.get(record.comment_key)
        if existing is not None:
            if existing != record:
                _invalid()
            continue
        records[record.comment_key] = record

    if not records and not prepared.platform_end:
        _invalid()
    return (
        CommentPage(
            comments=tuple(records.values()),
            has_more=prepared.has_more,
            next_cursor=prepared.next_cursor,
            platform_end=prepared.platform_end,
        ),
        rejected_count,
    )


def _project_prepared_page_outcome(
    prepared: _PreparedPage,
    account_id: int,
    content_id: str,
    observed_at: str,
) -> _ParseOutcome:
    try:
        page, rejected_count = _project_prepared_page_unsafe(
            prepared, account_id, content_id, observed_at
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
        return _ParseOutcome(control_kind=_control_kind(exc))
    except CommentInsightFailure as exc:
        return _ParseOutcome(error_code=exc.error_code)
    except BaseException:
        return _ParseOutcome(error_code="comment_payload_invalid")
    return _ParseOutcome(page=page, rejected_count=rejected_count)


def _prepare_comment_page_outcome(
    contract: DouyinCommentContract,
    payload: object,
    account_id: int,
    content_id: str,
    observed_at: str,
    *,
    deadline: float | None = None,
) -> tuple[_PreparedPage | None, str, str]:
    try:
        prepared = _prepare_comment_page_unsafe(
            contract,
            payload,
            account_id,
            content_id,
            observed_at,
            deadline=deadline,
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
        return None, "", _control_kind(exc)
    except CommentInsightFailure as exc:
        return None, exc.error_code, ""
    except _ParseDeadlineExceeded:
        return None, "comment_sync_timeout", ""
    except BaseException:
        return None, "comment_payload_invalid", ""
    return prepared, "", ""


def _parse_comment_page_outcome(
    contract: DouyinCommentContract,
    payload: object,
    account_id: int,
    content_id: str,
    observed_at: str,
    *,
    deadline: float | None = None,
) -> _ParseOutcome:
    """在原始载荷边界内消化异常，只返回脱敏结果。"""

    prepared, error_code, control_kind = _prepare_comment_page_outcome(
        contract,
        payload,
        account_id,
        content_id,
        observed_at,
        deadline=deadline,
    )
    if control_kind:
        return _ParseOutcome(control_kind=control_kind)
    if error_code or prepared is None:
        return _ParseOutcome(error_code=error_code or "comment_payload_invalid")
    return _project_prepared_page_outcome(
        prepared, account_id, content_id, observed_at
    )


def parse_comment_page(
    contract: DouyinCommentContract,
    payload: object,
    account_id: int,
    content_id: str,
    observed_at: str,
) -> CommentPage:
    """按已验证合同投影一页评论，不保留身份字段。"""

    outcome = _parse_comment_page_outcome(
        contract,
        payload,
        account_id,
        content_id,
        observed_at,
    )
    payload = None
    if outcome.control_kind:
        _raise_fresh_control(outcome.control_kind)
    if outcome.error_code:
        raise CommentInsightFailure(outcome.error_code) from None
    if outcome.page is None:
        raise CommentInsightFailure("comment_payload_invalid") from None
    return outcome.page


def _projection_worker_loop() -> None:
    """单一投影 worker；任务完成后立即丢弃有界原始标量。"""

    global _PROJECTION_BUSY
    while True:
        job = _PROJECTION_QUEUE.get()
        prepared = job.prepared
        job.prepared = None
        if prepared is None:
            outcome = _ParseOutcome(error_code="comment_payload_invalid")
        else:
            outcome = _project_prepared_page_outcome(
                prepared,
                job.account_id,
                job.content_id,
                job.observed_at,
            )
        prepared = None
        job.outcome = outcome
        outcome = None
        with _PROJECTION_LOCK:
            _PROJECTION_BUSY = False
        job.completed.set()
        job = None


def _start_projection_job(
    prepared: _PreparedPage,
    account_id: int,
    content_id: str,
    observed_at: str,
) -> tuple[_ProjectionJob | None, str]:
    global _PROJECTION_BUSY, _PROJECTION_WORKER
    job = _ProjectionJob(
        prepared=prepared,
        account_id=account_id,
        content_id=content_id,
        observed_at=observed_at,
    )
    with _PROJECTION_LOCK:
        if _PROJECTION_BUSY:
            job.prepared = None
            return None, "busy"
        worker = _PROJECTION_WORKER
        if worker is None or not worker.is_alive():
            worker = threading.Thread(
                target=_projection_worker_loop,
                name="douyin-comment-projector",
                daemon=True,
            )
            _PROJECTION_WORKER = worker
            try:
                worker.start()
            except BaseException:
                _PROJECTION_WORKER = None
                job.prepared = None
                return None, "unavailable"
        _PROJECTION_BUSY = True
        try:
            _PROJECTION_QUEUE.put_nowait(job)
        except queue.Full:
            _PROJECTION_BUSY = False
            job.prepared = None
            return None, "unavailable"
    return job, ""


def _projection_worker_is_busy() -> bool:
    with _PROJECTION_LOCK:
        return _PROJECTION_BUSY


async def _project_prepared_page_bounded(
    prepared: _PreparedPage,
    account_id: int,
    content_id: str,
    observed_at: str,
    deadline: float,
    own_job: Callable[[_ProjectionJob | None], None],
) -> tuple[_ParseOutcome, _ProjectionJob | None, bool]:
    job, start_error = _start_projection_job(
        prepared, account_id, content_id, observed_at
    )
    prepared = None
    if start_error == "busy":
        return (
            _ParseOutcome(error_code="comment_sync_timeout"),
            None,
            True,
        )
    if start_error or job is None:
        return (
            _ParseOutcome(error_code="comment_payload_invalid"),
            None,
            False,
        )
    own_job(job)
    while not job.completed.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return (
                _ParseOutcome(error_code="comment_sync_timeout"),
                job,
                False,
            )
        await asyncio.sleep(min(0.001, remaining))
    outcome = job.outcome
    own_job(None)
    if outcome is None:
        outcome = _ParseOutcome(error_code="comment_payload_invalid")
    return outcome, None, False


def _observed_at() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_playwright_factory():
    from playwright.async_api import async_playwright

    return async_playwright()


def _required_inputs(
    account: object,
    content_id: object,
    known_keys: object,
    limit: object,
    report: object,
) -> tuple[int, Path, str, frozenset[str], int]:
    if (
        type(account) is not dict
        or type(account.get("id")) is not int
        or account["id"] <= 0
        or type(account.get("type")) is not int
        or account["type"] != 3
        or type(account.get("filePath")) is not str
        or type(content_id) is not str
        or type(known_keys) is not frozenset
        or len(known_keys) > 10_000
        or not all(
            type(key) is str
            and len(key) == 64
            and _KEY_RE.fullmatch(key) is not None
            for key in known_keys
        )
        or type(limit) is not int
        or limit < 1
        or limit > 100
        or (report is not None and not callable(report))
    ):
        _invalid()
    _bounded_text(account["filePath"], _MAX_STATE_FILE_PATH_LENGTH)
    _bounded_text(content_id, _MAX_CONTENT_ID_LENGTH)
    state_path = COOKIE_DIR / Path(account["filePath"]).name
    try:
        if state_path.is_symlink() or not state_path.is_file():
            raise FileNotFoundError
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise CommentInsightFailure("comment_login_required") from None
    return account["id"], state_path, content_id, known_keys, limit


def _required_contract(value: object) -> DouyinCommentContract:
    if type(value) is not DouyinCommentContract:
        raise CommentInsightFailure("comment_content_unavailable")
    comment_fields = (
        value.comment_list_field,
        value.comment_id_field,
        value.comment_content_id_field,
        value.comment_parent_id_field,
        value.comment_body_field,
        value.comment_like_count_field,
        value.comment_reply_count_field,
        value.comment_commented_at_field,
        value.comment_cursor_field,
        value.comment_has_more_field,
    )
    navigation = value.comment_navigation_template
    response_path = value.comment_response_path
    method = value.comment_response_method
    trigger_value = value.comment_pagination_trigger
    if (
        value.verified is not True
        or not _is_bounded_builtin_text(
            value.creator_host, _MAX_HOST_LENGTH
        )
        or value.creator_host != "creator.douyin.com"
        or not _is_bounded_builtin_text(method, _MAX_METHOD_LENGTH)
        or not _is_bounded_builtin_text(
            response_path, _MAX_RESPONSE_PATH_LENGTH
        )
        or not _is_bounded_builtin_text(
            navigation, _MAX_NAVIGATION_TEMPLATE_LENGTH
        )
        or not _is_bounded_builtin_text(
            trigger_value, _MAX_PAGINATION_TRIGGER_LENGTH
        )
        or any(
            not _is_bounded_builtin_text(
                field, _MAX_CONTRACT_FIELD_LENGTH
            )
            for field in comment_fields
        )
    ):
        raise CommentInsightFailure("comment_content_unavailable")
    trigger = _TRIGGER_RE.fullmatch(trigger_value)
    if (
        method != method.upper()
        or not response_path.startswith("/")
        or any(marker in response_path for marker in ("?", "#", "\\", "//"))
        or navigation.count("{content_id}") != 1
        or "{" in navigation.replace("{content_id}", "")
        or "}" in navigation.replace("{content_id}", "")
        or not navigation.startswith("/")
        or any(marker in navigation for marker in ("?", "#", "\\", "//"))
        or trigger is None
    ):
        raise CommentInsightFailure("comment_content_unavailable")
    return value


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _failure_with_cleanup(
    error_code: str,
    cleanup: CleanupReceipt,
    *,
    retryable: bool = False,
) -> CommentInsightFailure:
    failure = CommentInsightFailure(error_code, retryable=retryable)
    failure.cleanup_receipt = cleanup
    return failure


def _raise_fresh_failure(
    error_code: str,
    *,
    retryable: bool = False,
    cleanup: CleanupReceipt | None = None,
) -> None:
    failure = CommentInsightFailure(error_code, retryable=retryable)
    if cleanup is not None:
        failure.cleanup_receipt = cleanup
    raise failure from None


def _report_payload(
    *,
    status: str,
    error_code: str,
    accepted_count: int,
    rejected_count: int,
    page_count: int,
    stop_reason: str,
    cleanup: CleanupReceipt,
) -> dict:
    return {
        "status": status,
        "errorCode": error_code,
        "acceptedCount": accepted_count,
        "rejectedCount": rejected_count,
        "pageCount": page_count,
        "stopReason": stop_reason,
        "cleanup": cleanup.public_payload(),
    }


def _report_worker_loop() -> None:
    """单一 worker 串行执行最多一个报告，不保留积压回调。"""

    global _REPORT_BUSY
    while True:
        report, payload, completed = _REPORT_QUEUE.get()
        try:
            report(payload)
        except BaseException:
            pass
        finally:
            completed.set()
            with _REPORT_LOCK:
                _REPORT_BUSY = False
            report = None
            payload = None
            completed = None


def _emit_report(
    report: Callable[[dict], None] | None,
    payload: dict,
    deadline: float,
) -> bool:
    """仅投递到单一有界 worker；忙时直接丢弃。"""

    global _REPORT_BUSY, _REPORT_WORKER
    if report is None:
        return True
    remaining = _remaining(deadline)
    if remaining <= 0:
        return False
    completed = threading.Event()
    with _REPORT_LOCK:
        if _REPORT_BUSY:
            return False
        worker = _REPORT_WORKER
        if worker is None or not worker.is_alive():
            worker = threading.Thread(
                target=_report_worker_loop,
                name="douyin-comment-report",
                daemon=True,
            )
            _REPORT_WORKER = worker
            try:
                worker.start()
            except BaseException:
                _REPORT_WORKER = None
                return False
        _REPORT_BUSY = True
        try:
            _REPORT_QUEUE.put_nowait((report, payload, completed))
        except queue.Full:
            _REPORT_BUSY = False
            return False
    completed.wait(remaining)
    return completed.is_set()


def _navigation_error(value: object, expected_path: str) -> str | None:
    if type(value) is not str:
        return "comment_login_required"
    if len(value) > _MAX_RUNTIME_URL_LENGTH:
        return "comment_payload_invalid"
    if not _is_bounded_builtin_text(
        expected_path, _MAX_NAVIGATION_TEMPLATE_LENGTH
    ):
        return "comment_payload_invalid"
    lowered = value.lower()
    if any(marker in lowered for marker in ("captcha", "verification", "verify")):
        return "comment_verification_required"
    if any(
        marker in lowered
        for marker in ("forbidden", "no_permission", "unauthorized")
    ):
        return "comment_access_denied"
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "creator.douyin.com"
        or any(marker in lowered for marker in ("/login", "passport."))
    ):
        return "comment_login_required"
    if parsed.path != expected_path:
        return "comment_content_unavailable"
    return None


def _response_contract_status(
    response: object,
    contract: DouyinCommentContract,
) -> str:
    raw_url = getattr(response, "url", None)
    request = getattr(response, "request", None)
    method = getattr(request, "method", None)
    if (
        not _is_bounded_builtin_text(raw_url, _MAX_RUNTIME_URL_LENGTH)
        or not _is_bounded_builtin_text(method, _MAX_METHOD_LENGTH)
    ):
        return "invalid"
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme == "https"
        and parsed.netloc == contract.creator_host
        and parsed.path == contract.comment_response_path
        and method == contract.comment_response_method
    ):
        return "match"
    return "ignore"


def _response_content_type_status(response: object, deadline: float) -> str:
    if time.monotonic() >= deadline:
        return "timeout"
    headers = getattr(response, "headers", None)
    if type(headers) is not dict or len(headers) > _MAX_CONTAINER_KEYS:
        return "invalid"
    content_type: object = _MISSING
    for name in headers:
        if time.monotonic() >= deadline:
            return "timeout"
        if (
            type(name) is not str
            or len(name) > _MAX_HEADER_NAME_LENGTH
        ):
            return "invalid"
        if name.lower() != "content-type":
            continue
        if content_type is not _MISSING:
            return "invalid"
        content_type = headers[name]
    if (
        type(content_type) is not str
        or len(content_type) == 0
        or len(content_type) > _MAX_CONTENT_TYPE_LENGTH
    ):
        return "invalid"
    if time.monotonic() >= deadline:
        return "timeout"
    parts = content_type.split(";")
    if len(parts) not in (1, 2):
        return "invalid"
    media_type = parts[0].strip().lower()
    if _JSON_MEDIA_TYPE_RE.fullmatch(media_type) is None:
        return "invalid"
    if len(parts) == 2:
        parameter = parts[1].strip()
        if parameter.count("=") != 1:
            return "invalid"
        name, charset = parameter.split("=", 1)
        if name.strip().lower() != "charset":
            return "invalid"
        charset = charset.strip()
        if (
            len(charset) >= 2
            and charset.startswith('"')
            and charset.endswith('"')
        ):
            charset = charset[1:-1]
        if _CHARSET_RE.fullmatch(charset) is None:
            return "invalid"
    if time.monotonic() >= deadline:
        return "timeout"
    return "valid"


class DouyinCommentDataCollector:
    def __init__(
        self,
        *,
        contract_loader: Callable[[], DouyinCommentContract] = load_verified_contract,
        playwright_factory: Callable[[], object] = _default_playwright_factory,
        total_timeout_seconds: float = _DEFAULT_TOTAL_TIMEOUT_SECONDS,
        observed_at_factory: Callable[[], str] = _observed_at,
    ) -> None:
        if (
            not callable(contract_loader)
            or not callable(playwright_factory)
            or type(total_timeout_seconds) not in (int, float)
            or total_timeout_seconds <= 0
            or not callable(observed_at_factory)
        ):
            _invalid()
        self._contract_loader = contract_loader
        self._playwright_factory = playwright_factory
        self._total_timeout_seconds = float(total_timeout_seconds)
        self._observed_at_factory = observed_at_factory

    async def _trigger_pagination(
        self,
        page: object,
        contract: DouyinCommentContract,
        await_operation,
    ) -> None:
        trigger_value = contract.comment_pagination_trigger
        if not _is_bounded_builtin_text(
            trigger_value, _MAX_PAGINATION_TRIGGER_LENGTH
        ):
            _invalid()
        matched = _TRIGGER_RE.fullmatch(trigger_value)
        if matched is None:
            _invalid()
        action, target = matched.groups()
        locator_factory = getattr(page, "locator", None)
        if not callable(locator_factory):
            _invalid()
        try:
            locator = locator_factory(target)
            if action == "click":
                trigger = getattr(locator, "click", None)
            else:
                trigger = getattr(locator, "scroll_into_view_if_needed", None)
            if not callable(trigger):
                _invalid()
            await await_operation(trigger(timeout=self._total_timeout_seconds * 1000))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except CommentInsightFailure:
            raise
        except BaseException:
            _invalid()

    async def _collect_async(
        self,
        *,
        account_id: int,
        state_path: Path,
        content_id: str,
        known_keys: frozenset[str],
        limit: int,
        contract: DouyinCommentContract,
        observed_at: str,
        report: Callable[[dict], None] | None,
        total_deadline: float,
    ) -> _CollectionOutcome:
        playwright = None
        browser = None
        context = None
        page = None
        worker: asyncio.Task | None = None
        projection_job: _ProjectionJob | None = None
        projection_blocked_by_worker = False
        result: tuple[object, ...] | None = None
        caught: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        records: dict[str, CommentRecord] = {}
        rejected_count = 0
        page_count = 0
        response_queue: asyncio.Queue[object] = asyncio.Queue(
            maxsize=_MAX_QUEUED_RESPONSES
        )
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future = loop.create_future()
        operation_tasks: dict[asyncio.Future, str] = {}
        owned_resources: dict[str, object] = {}
        cleanup_reservation = min(
            2.0, self._total_timeout_seconds * 0.25
        )
        operation_deadline = total_deadline - cleanup_reservation
        seen_cursors: set[str] = set()

        async def await_operation(awaitable, *, owner: str = ""):
            task = asyncio.ensure_future(awaitable)
            operation_tasks[task] = owner
            try:
                remaining = operation_deadline - time.monotonic()
                if remaining > 0:
                    done, _pending = await asyncio.wait((task,), timeout=remaining)
                    if task in done:
                        value = task.result()
                        if owner:
                            owned_resources[owner] = value
                        return value
                task.cancel()
                if isinstance(task, asyncio.Task):
                    task._log_destroy_pending = False
                raise TimeoutError
            except BaseException:
                if not task.done():
                    task.cancel()
                    if isinstance(task, asyncio.Task):
                        task._log_destroy_pending = False
                raise
            finally:
                if task.done():
                    operation_tasks.pop(task, None)

        def observe_response(response: object) -> None:
            if response_future.done():
                return
            try:
                metadata_status = _response_contract_status(
                    response, contract
                )
                if metadata_status == "invalid":
                    response_future.set_result(
                        ("failure", "comment_payload_invalid")
                    )
                    return
                if metadata_status != "match":
                    return
                status = getattr(response, "status", None)
                if status == 401:
                    response_future.set_result(
                        ("failure", "comment_login_required")
                    )
                    return
                if status == 403:
                    response_future.set_result(
                        ("failure", "comment_access_denied")
                    )
                    return
                if type(status) is not int or status < 200 or status >= 300:
                    response_future.set_result(
                        ("failure", "comment_payload_invalid")
                    )
                    return
                if time.monotonic() >= operation_deadline:
                    response_future.set_result(("timeout",))
                    return
                content_type_status = _response_content_type_status(
                    response, operation_deadline
                )
                if content_type_status == "timeout":
                    response_future.set_result(("timeout",))
                    return
                if content_type_status != "valid":
                    response_future.set_result(
                        ("failure", "comment_payload_invalid")
                    )
                    return
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
                response_future.set_result(("control", _control_kind(exc)))
                return
            except BaseException:
                response_future.set_result(
                    ("failure", "comment_payload_invalid")
                )
                return
            try:
                response_queue.put_nowait(response)
            except asyncio.QueueFull:
                response_future.set_result(
                    ("failure", "comment_payload_invalid")
                )

        def finish(
            comments: tuple[CommentRecord, ...],
            stop_reason: str,
            warning_code: str,
        ) -> None:
            if not response_future.done():
                response_future.set_result(
                    (
                        "success",
                        comments,
                        rejected_count,
                        page_count,
                        stop_reason,
                        warning_code,
                    )
                )

        async def consume_responses() -> None:
            nonlocal rejected_count, page_count
            nonlocal projection_job, projection_blocked_by_worker
            while not response_future.done():
                response = await response_queue.get()
                try:
                    metadata_status = _response_contract_status(
                        response, contract
                    )
                    if metadata_status == "invalid":
                        _invalid()
                    if metadata_status != "match":
                        continue
                    status = getattr(response, "status", None)
                    if status == 401:
                        raise CommentInsightFailure("comment_login_required")
                    if status == 403:
                        raise CommentInsightFailure("comment_access_denied")
                    if type(status) is not int or status < 200 or status >= 300:
                        _invalid()
                    content_type_status = _response_content_type_status(
                        response, operation_deadline
                    )
                    if content_type_status == "timeout":
                        raise TimeoutError
                    if content_type_status != "valid":
                        _invalid()
                    page_count += 1
                    if page_count > _MAX_RESPONSE_PAGES:
                        _invalid()
                    reader = getattr(response, "json", None)
                    if not callable(reader) or not inspect.iscoroutinefunction(reader):
                        _invalid()
                    payload = await await_operation(reader())
                    prepared, prepare_error, prepare_control = (
                        _prepare_comment_page_outcome(
                            contract,
                            payload,
                            account_id,
                            content_id,
                            observed_at,
                            deadline=operation_deadline,
                        )
                    )
                    payload = None
                    response = None
                    if prepare_control:
                        response_future.set_result(
                            ("control", prepare_control)
                        )
                        return
                    if prepare_error or prepared is None:
                        response_future.set_result(
                            (
                                "failure",
                                prepare_error or "comment_payload_invalid",
                            )
                        )
                        return
                    def own_projection_job(
                        value: _ProjectionJob | None,
                    ) -> None:
                        nonlocal projection_job
                        projection_job = value

                    (
                        parse_outcome,
                        returned_projection_job,
                        projection_blocked_by_worker,
                    ) = await _project_prepared_page_bounded(
                        prepared,
                        account_id,
                        content_id,
                        observed_at,
                        operation_deadline,
                        own_projection_job,
                    )
                    projection_job = returned_projection_job
                    prepared = None
                    if parse_outcome.control_kind:
                        response_future.set_result(
                            ("control", parse_outcome.control_kind)
                        )
                        return
                    if parse_outcome.error_code or parse_outcome.page is None:
                        response_future.set_result(
                            (
                                "failure",
                                parse_outcome.error_code
                                or "comment_payload_invalid",
                            )
                        )
                        return
                    parsed_page = parse_outcome.page
                    rejected_count += parse_outcome.rejected_count
                    page_exceeds_limit = False
                    for record in parsed_page.comments:
                        existing = records.get(record.comment_key)
                        if existing is not None:
                            if existing != record:
                                _invalid()
                            continue
                        if limit == 100 and len(records) >= limit:
                            page_exceeds_limit = True
                            break
                        records[record.comment_key] = record
                        if record.comment_key in known_keys:
                            finish(tuple(records.values()), "known_comment", "")
                            return
                        if len(records) >= limit:
                            if limit < 100:
                                finish(tuple(records.values()), "", "")
                                return
                    if (
                        parsed_page.platform_end
                        and not page_exceeds_limit
                    ):
                        finish(tuple(records.values()), "platform_end", "")
                        return
                    if len(records) >= 100:
                        finish(
                            tuple(records.values()),
                            "limit_reached",
                            "comment_limit_reached",
                        )
                        return
                    cursor = parsed_page.next_cursor
                    if type(cursor) is not str or not cursor or cursor in seen_cursors:
                        _invalid()
                    seen_cursors.add(cursor)
                    await self._trigger_pagination(
                        page, contract, await_operation
                    )
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
                    if not response_future.done():
                        response_future.set_result(
                            ("control", _control_kind(exc))
                        )
                    return
                except CommentInsightFailure as exc:
                    if not response_future.done():
                        response_future.set_result(
                            ("failure", exc.error_code)
                        )
                    return
                except TimeoutError:
                    if not response_future.done():
                        response_future.set_result(("timeout",))
                    return
                except BaseException:
                    if not response_future.done():
                        response_future.set_result(
                            ("failure", "comment_payload_invalid")
                        )
                    return

        try:
            if time.monotonic() >= operation_deadline:
                raise TimeoutError
            starter = self._playwright_factory()
            playwright = await await_operation(
                starter.start(), owner="playwright"
            )
            browser = await await_operation(
                playwright.chromium.launch(headless=True), owner="browser"
            )
            context = await await_operation(
                browser.new_context(storage_state=str(state_path)),
                owner="context",
            )
            page = await await_operation(context.new_page(), owner="page")
            page.on("response", observe_response)
            worker = asyncio.create_task(consume_responses())
            encoded_content_id = quote(content_id, safe="")
            navigation_path = contract.comment_navigation_template.replace(
                "{content_id}", encoded_content_id
            )
            navigation_url = f"https://{contract.creator_host}{navigation_path}"
            await await_operation(
                page.goto(
                    navigation_url,
                    wait_until="domcontentloaded",
                    timeout=self._total_timeout_seconds * 1000,
                )
            )
            navigation_error = _navigation_error(
                getattr(page, "url", None), navigation_path
            )
            if navigation_error is not None:
                raise CommentInsightFailure(navigation_error)
            result = await await_operation(response_future)
        except BaseException as exc:
            caught = exc
        finally:
            cleanup_steps = 8

            async def settle_cleanup(awaitable) -> BaseException | None:
                nonlocal cleanup_steps
                try:
                    remaining = total_deadline - time.monotonic()
                    task = asyncio.ensure_future(awaitable)
                    timeout = remaining / cleanup_steps if remaining > 0 else 0
                    done, _pending = await asyncio.wait((task,), timeout=timeout)
                    if task not in done:
                        task.cancel()
                        if isinstance(task, asyncio.Task):
                            task._log_destroy_pending = False
                        return TimeoutError()
                    try:
                        task.result()
                    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
                        return type(exc)()
                    except BaseException:
                        return RuntimeError("cleanup failed")
                    return None
                finally:
                    cleanup_steps -= 1

            async def close_bounded(resource: object | None) -> BaseException | None:
                nonlocal cleanup_steps
                if resource is None:
                    cleanup_steps -= 1
                    return None
                close = getattr(resource, "close", None)
                if close is None:
                    close = getattr(resource, "stop", None)
                if close is None or not inspect.iscoroutinefunction(close):
                    cleanup_steps -= 1
                    return RuntimeError("resource close unavailable")
                try:
                    close_result = close()
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
                    cleanup_steps -= 1
                    return type(exc)()
                except BaseException:
                    cleanup_steps -= 1
                    return RuntimeError("cleanup failed")
                return await settle_cleanup(close_result)

            if page is not None:
                remove_listener = getattr(page, "remove_listener", None)
                if remove_listener is None:
                    remove_listener = getattr(page, "off", None)
                if callable(remove_listener):
                    try:
                        removal = remove_listener("response", observe_response)
                    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
                        cleanup_errors.append(type(exc)())
                        cleanup_steps -= 1
                    except BaseException:
                        cleanup_errors.append(RuntimeError("listener removal failed"))
                        cleanup_steps -= 1
                    else:
                        if hasattr(removal, "__await__"):
                            removal_error = await settle_cleanup(removal)
                            if removal_error is not None:
                                cleanup_errors.append(removal_error)
                        else:
                            cleanup_steps -= 1
                else:
                    cleanup_errors.append(RuntimeError("listener removal unavailable"))
                    cleanup_steps -= 1
            else:
                cleanup_steps -= 1

            if not response_future.done():
                response_future.cancel()
            if response_future.done():
                try:
                    future_error = response_future.exception()
                except asyncio.CancelledError:
                    future_error = None
                if isinstance(
                    future_error,
                    (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
                ):
                    cleanup_errors.append(type(future_error)())
            cleanup_steps -= 1

            worker_cancelled = False
            if worker is not None and not worker.done():
                worker.cancel()
                worker_cancelled = True
            if worker is None:
                cleanup_steps -= 1
            else:
                worker_error = await settle_cleanup(worker)
                if worker_error is not None and not (
                    worker_cancelled
                    and isinstance(worker_error, asyncio.CancelledError)
                ):
                    cleanup_errors.append(worker_error)

            pending_operations = tuple(
                task for task in operation_tasks if not task.done()
            )
            for task in pending_operations:
                task.cancel()
                if isinstance(task, asyncio.Task):
                    task._log_destroy_pending = False
            if pending_operations:
                operation_error = await settle_cleanup(
                    asyncio.gather(
                        *pending_operations, return_exceptions=True
                    )
                )
                if operation_error is not None:
                    cleanup_errors.append(operation_error)
                else:
                    for task in pending_operations:
                        owner = operation_tasks.pop(task, "")
                        try:
                            late_value = task.result()
                        except asyncio.CancelledError:
                            continue
                        except (KeyboardInterrupt, SystemExit) as exc:
                            cleanup_errors.append(type(exc)())
                        except BaseException:
                            cleanup_errors.append(RuntimeError("operation failed"))
                        else:
                            if owner:
                                owned_resources[owner] = late_value
            else:
                cleanup_steps -= 1

            page = owned_resources.get("page", page)
            context = owned_resources.get("context", context)
            browser = owned_resources.get("browser", browser)
            playwright = owned_resources.get("playwright", playwright)

            for resource in (page, context, browser, playwright):
                close_error = await close_bounded(resource)
                if close_error is not None:
                    cleanup_errors.append(close_error)

            projection_still_running = (
                projection_job is not None
                and not projection_job.completed.is_set()
            ) or (
                projection_blocked_by_worker
                and _projection_worker_is_busy()
            )
            if projection_still_running:
                cleanup_errors.append(
                    RuntimeError("projection worker active")
                )
            projection_job = None
            projection_blocked_by_worker = False

        cleanup = CleanupReceipt(
            closed=not cleanup_errors,
            alive_resource_count=len(cleanup_errors),
        )
        caught_control = _control_kind(caught) if caught is not None else ""
        cleanup_control = ""
        for cleanup_error in cleanup_errors:
            cleanup_control = _control_kind(cleanup_error)
            if cleanup_control:
                break

        def failed_outcome(
            error_code: str,
            *,
            retryable: bool = False,
        ) -> _CollectionOutcome:
            _emit_report(
                report,
                _report_payload(
                    status="failed",
                    error_code=error_code,
                    accepted_count=0,
                    rejected_count=rejected_count,
                    page_count=page_count,
                    stop_reason="",
                    cleanup=cleanup,
                ),
                total_deadline,
            )
            return _CollectionOutcome(
                batch=None,
                error_code=error_code,
                retryable=retryable,
                control_kind="",
                cleanup_receipt=cleanup,
            )

        if caught_control or cleanup_control:
            control_kind = caught_control or cleanup_control
            caught = None
            cleanup_errors.clear()
            return _CollectionOutcome(
                batch=None,
                error_code="",
                retryable=False,
                control_kind=control_kind,
                cleanup_receipt=cleanup,
            )
        if cleanup_errors:
            caught = None
            cleanup_errors.clear()
            return failed_outcome("comment_sync_cancelled")
        if isinstance(caught, TimeoutError):
            caught = None
            return failed_outcome("comment_sync_timeout", retryable=True)
        if isinstance(caught, CommentInsightFailure):
            error_code = caught.error_code
            caught = None
            return failed_outcome(error_code)
        if caught is not None:
            caught = None
            return failed_outcome("comment_payload_invalid")
        if result is None:
            return failed_outcome("comment_sync_timeout", retryable=True)
        result_kind = result[0]
        if result_kind == "control":
            return _CollectionOutcome(
                batch=None,
                error_code="",
                retryable=False,
                control_kind=str(result[1]),
                cleanup_receipt=cleanup,
            )
        if result_kind == "timeout":
            return failed_outcome("comment_sync_timeout", retryable=True)
        if result_kind == "failure":
            return failed_outcome(str(result[1]))
        if result_kind != "success" or len(result) != 6:
            return failed_outcome("comment_payload_invalid")
        (
            _status,
            comments,
            rejected_count,
            page_count,
            stop_reason,
            warning_code,
        ) = result
        batch = CommentCollectionBatch(
            platform_type=3,
            source_mode="browser_signed",
            content_id=content_id,
            comments=comments,
            accepted_count=len(comments),
            rejected_count=rejected_count,
            page_count=page_count,
            stop_reason=stop_reason,
            warning_code=warning_code,
            platform_observed_at=observed_at,
            cleanup_receipt=cleanup,
        )
        _emit_report(
            report,
            _report_payload(
                status="success",
                error_code="",
                accepted_count=batch.accepted_count,
                rejected_count=batch.rejected_count,
                page_count=batch.page_count,
                stop_reason=batch.stop_reason,
                cleanup=cleanup,
            ),
            total_deadline,
        )
        return _CollectionOutcome(
            batch=batch,
            error_code="",
            retryable=False,
            control_kind="",
            cleanup_receipt=cleanup,
        )

    @staticmethod
    def _run_short_lived(coroutine) -> _CollectionOutcome:
        """不用 asyncio.run，避免退出时无界等待抗取消任务。"""

        loop = asyncio.new_event_loop()
        loop.set_exception_handler(lambda _loop, _context: None)
        main_task: asyncio.Task | None = None
        try:
            main_task = loop.create_task(coroutine)
            process_control: BaseException | None = None
            while True:
                try:
                    value = loop.run_until_complete(main_task)
                except (KeyboardInterrupt, SystemExit) as exc:
                    if process_control is None:
                        process_control = exc
                    if main_task.done():
                        raise
                    continue
                if process_control is not None:
                    raise process_control
                return value
        finally:
            for task in tuple(asyncio.all_tasks(loop)):
                if task.done():
                    try:
                        task.exception()
                    except asyncio.CancelledError:
                        pass
                    continue
                task.cancel()
                task._log_destroy_pending = False
                try:
                    task.get_coro().close()
                except BaseException:
                    pass
            if main_task is None and hasattr(coroutine, "close"):
                coroutine.close()
            loop.close()

    def collect(
        self,
        account: dict,
        content_id: str,
        known_keys: frozenset[str],
        limit: int = 100,
        report=None,
    ) -> CommentCollectionBatch:
        total_deadline = time.monotonic() + self._total_timeout_seconds
        early_error = ""
        early_control = ""
        account_id = 0
        state_path: Path | None = None
        try:
            account_id, state_path, content_id, known_keys, limit = _required_inputs(
                account, content_id, known_keys, limit, report
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
            early_control = _control_kind(exc)
        except CommentInsightFailure as exc:
            early_error = exc.error_code
        except BaseException:
            early_error = "comment_payload_invalid"
        account = None
        if (
            not early_control
            and not early_error
            and time.monotonic() >= total_deadline
        ):
            early_error = "comment_sync_timeout"
        if early_control:
            report = None
            _raise_fresh_control(early_control)
        if early_error:
            report = None
            _raise_fresh_failure(early_error)

        contract: DouyinCommentContract | None = None
        try:
            contract = _required_contract(self._contract_loader())
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
            early_control = _control_kind(exc)
        except CommentInsightFailure as exc:
            early_error = exc.error_code
        except BaseException:
            early_error = "comment_content_unavailable"
        if (
            not early_control
            and not early_error
            and time.monotonic() >= total_deadline
        ):
            early_error = "comment_sync_timeout"
        if early_control:
            report = None
            contract = None
            _raise_fresh_control(early_control)
        if early_error:
            report = None
            contract = None
            _raise_fresh_failure(early_error)

        observed_at = ""
        try:
            observed_at = self._observed_at_factory()
            observed_at = _bounded_text(
                observed_at, _MAX_COMMENT_TIME_LENGTH
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
            early_control = _control_kind(exc)
        except BaseException:
            early_error = "comment_payload_invalid"
        if (
            not early_control
            and not early_error
            and time.monotonic() >= total_deadline
        ):
            early_error = "comment_sync_timeout"
        if early_control:
            report = None
            contract = None
            state_path = None
            _raise_fresh_control(early_control)
        if early_error:
            report = None
            contract = None
            state_path = None
            _raise_fresh_failure(early_error)

        assert state_path is not None
        assert contract is not None
        coroutine = self._collect_async(
            account_id=account_id,
            state_path=state_path,
            content_id=content_id,
            known_keys=known_keys,
            limit=limit,
            contract=contract,
            observed_at=observed_at,
            report=report,
            total_deadline=total_deadline,
        )
        outcome: _CollectionOutcome | None = None
        unexpected_error = False
        unexpected_control = ""
        try:
            outcome = self._run_short_lived(coroutine)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
            unexpected_control = _control_kind(exc)
        except BaseException:
            unexpected_error = True
        coroutine = None
        contract = None
        state_path = None
        report = None
        if unexpected_control:
            _raise_fresh_control(unexpected_control)
        if unexpected_error or outcome is None:
            _raise_fresh_failure("comment_payload_invalid")
        if outcome.control_kind:
            _raise_fresh_control(outcome.control_kind)
        if outcome.batch is not None:
            return outcome.batch
        _raise_fresh_failure(
            outcome.error_code or "comment_payload_invalid",
            retryable=outcome.retryable,
            cleanup=outcome.cleanup_receipt,
        )
