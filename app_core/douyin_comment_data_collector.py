# -*- coding: utf-8 -*-
"""抖音一级评论的短生命周期只读采集器。"""

from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
from pathlib import Path
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
_MAX_RESPONSE_PAGES = 50
_DEFAULT_TOTAL_TIMEOUT_SECONDS = 30.0
_REPORT_LOCK = threading.Lock()
_ACTIVE_REPORT_WORKER: threading.Thread | None = None


def _invalid() -> None:
    raise CommentInsightFailure("comment_payload_invalid")


def _strict_path(root: object, path: object) -> object:
    if type(root) is not dict or type(path) is not str or not path:
        _invalid()
    value = root
    for segment in path.split("."):
        if not segment or segment.endswith("[]") or type(value) is not dict:
            _invalid()
        if segment not in value:
            return _MISSING
        value = value[segment]
    return value


def _comment_rows(payload: object, list_field: object) -> tuple[object, ...]:
    if type(payload) is not dict or type(list_field) is not str:
        _invalid()
    if not list_field.endswith("[]"):
        _invalid()
    value = _strict_path(payload, list_field[:-2])
    if type(value) is not list:
        _invalid()
    return tuple(value)


def _row_value(row: object, list_field: str, field: object) -> object:
    prefix = f"{list_field}."
    if type(row) is not dict or type(field) is not str or not field.startswith(prefix):
        _invalid()
    relative = field[len(prefix) :]
    value = _strict_path(row, relative)
    if value is _MISSING:
        _invalid()
    return value


def _parse_comment_page_with_rejections(
    contract: DouyinCommentContract,
    payload: object,
    account_id: int,
    content_id: str,
    observed_at: str,
) -> tuple[CommentPage, int]:
    if (
        type(contract) is not DouyinCommentContract
        or contract.verified is not True
        or contract.creator_host != "creator.douyin.com"
        or type(account_id) is not int
        or account_id <= 0
        or type(content_id) is not str
        or not content_id.strip()
        or content_id != content_id.strip()
        or type(observed_at) is not str
    ):
        _invalid()

    rows = _comment_rows(payload, contract.comment_list_field)
    has_more = _strict_path(payload, contract.comment_has_more_field)
    raw_cursor = _strict_path(payload, contract.comment_cursor_field)
    if type(has_more) is not bool or raw_cursor is _MISSING:
        _invalid()
    if has_more:
        if (
            type(raw_cursor) is not str
            or not raw_cursor
            or raw_cursor != raw_cursor.strip()
        ):
            _invalid()
        next_cursor: str | None = raw_cursor
    else:
        if raw_cursor is not None and (
            type(raw_cursor) is not str or raw_cursor != raw_cursor.strip()
        ):
            _invalid()
        next_cursor = raw_cursor or ""

    records: dict[str, CommentRecord] = {}
    rejected_count = 0
    for raw_row in rows:
        try:
            parent_id = _row_value(
                raw_row,
                contract.comment_list_field,
                contract.comment_parent_id_field,
            )
            if parent_id is not None:
                continue
            row_content_id = _row_value(
                raw_row,
                contract.comment_list_field,
                contract.comment_content_id_field,
            )
            if type(row_content_id) is not str or row_content_id != content_id:
                _invalid()
            platform_comment_id = _row_value(
                raw_row,
                contract.comment_list_field,
                contract.comment_id_field,
            )
            if (
                type(platform_comment_id) is not str
                or not platform_comment_id.strip()
                or platform_comment_id != platform_comment_id.strip()
            ):
                _invalid()
            comment_key = derive_comment_key(
                account_id, content_id, platform_comment_id
            )
            del platform_comment_id
            record = CommentRecord(
                content_id=content_id,
                comment_key=comment_key,
                body=_row_value(
                    raw_row,
                    contract.comment_list_field,
                    contract.comment_body_field,
                ),
                like_count=_row_value(
                    raw_row,
                    contract.comment_list_field,
                    contract.comment_like_count_field,
                ),
                reply_count=_row_value(
                    raw_row,
                    contract.comment_list_field,
                    contract.comment_reply_count_field,
                ),
                commented_at=_row_value(
                    raw_row,
                    contract.comment_list_field,
                    contract.comment_commented_at_field,
                ),
                observed_at=observed_at,
            )
        except CommentInsightFailure:
            rejected_count += 1
            continue
        existing = records.get(record.comment_key)
        if existing is not None:
            if existing != record:
                _invalid()
            continue
        records[record.comment_key] = record

    platform_end = not has_more
    if not records and not platform_end:
        _invalid()
    return (
        CommentPage(
            comments=tuple(records.values()),
            has_more=has_more,
            next_cursor=next_cursor,
            platform_end=platform_end,
        ),
        rejected_count,
    )


def parse_comment_page(
    contract: DouyinCommentContract,
    payload: object,
    account_id: int,
    content_id: str,
    observed_at: str,
) -> CommentPage:
    """按已验证合同投影一页评论，不保留身份字段。"""

    page, _rejected_count = _parse_comment_page_with_rejections(
        contract,
        payload,
        account_id,
        content_id,
        observed_at,
    )
    return page


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
        or not account["filePath"].strip()
        or account["filePath"] != account["filePath"].strip()
        or type(content_id) is not str
        or not content_id.strip()
        or content_id != content_id.strip()
        or type(known_keys) is not frozenset
        or not all(
            type(key) is str and _KEY_RE.fullmatch(key) is not None
            for key in known_keys
        )
        or type(limit) is not int
        or limit < 1
        or limit > 100
        or (report is not None and not callable(report))
    ):
        _invalid()
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
    trigger = _TRIGGER_RE.fullmatch(value.comment_pagination_trigger)
    navigation = value.comment_navigation_template
    response_path = value.comment_response_path
    if (
        value.verified is not True
        or value.creator_host != "creator.douyin.com"
        or type(value.comment_response_method) is not str
        or not value.comment_response_method
        or value.comment_response_method != value.comment_response_method.upper()
        or type(response_path) is not str
        or not response_path.startswith("/")
        or any(marker in response_path for marker in ("?", "#", "\\", "//"))
        or type(navigation) is not str
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


def _emit_report(
    report: Callable[[dict], None] | None,
    payload: dict,
    deadline: float,
) -> bool:
    """报告回调最多只保留一个审计过的守护线程。"""

    global _ACTIVE_REPORT_WORKER
    if report is None:
        return True
    completed = threading.Event()

    def invoke() -> None:
        global _ACTIVE_REPORT_WORKER
        try:
            report(payload)
        except BaseException:
            pass
        finally:
            completed.set()
            current = threading.current_thread()
            with _REPORT_LOCK:
                if _ACTIVE_REPORT_WORKER is current:
                    _ACTIVE_REPORT_WORKER = None

    with _REPORT_LOCK:
        active = _ACTIVE_REPORT_WORKER
        if active is not None and active.is_alive():
            return False
        worker = threading.Thread(
            target=invoke,
            name="douyin-comment-report",
            daemon=True,
        )
        _ACTIVE_REPORT_WORKER = worker
        try:
            worker.start()
        except BaseException:
            _ACTIVE_REPORT_WORKER = None
            return False
    worker.join(_remaining(deadline))
    return completed.is_set()


def _navigation_error(value: object, expected_path: str) -> str | None:
    if type(value) is not str:
        return "comment_login_required"
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
        matched = _TRIGGER_RE.fullmatch(contract.comment_pagination_trigger)
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
    ) -> CommentCollectionBatch:
        playwright = None
        browser = None
        context = None
        page = None
        worker: asyncio.Task | None = None
        result: tuple[tuple[CommentRecord, ...], int, int, str, str] | None = None
        caught: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        records: dict[str, CommentRecord] = {}
        rejected_count = 0
        page_count = 0
        response_queue: asyncio.Queue[object] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future = loop.create_future()
        operation_tasks: set[asyncio.Future] = set()
        cleanup_reservation = min(
            2.0, self._total_timeout_seconds * 0.25
        )
        operation_deadline = total_deadline - cleanup_reservation
        seen_cursors: set[str] = set()

        async def await_operation(awaitable):
            task = asyncio.ensure_future(awaitable)
            operation_tasks.add(task)
            try:
                remaining = operation_deadline - time.monotonic()
                if remaining > 0:
                    done, _pending = await asyncio.wait((task,), timeout=remaining)
                    if task in done:
                        return task.result()
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
                    operation_tasks.discard(task)

        def observe_response(response: object) -> None:
            if not response_future.done():
                response_queue.put_nowait(response)

        def finish(
            comments: tuple[CommentRecord, ...],
            stop_reason: str,
            warning_code: str,
        ) -> None:
            if not response_future.done():
                response_future.set_result(
                    (
                        comments,
                        rejected_count,
                        page_count,
                        stop_reason,
                        warning_code,
                    )
                )

        async def consume_responses() -> None:
            nonlocal rejected_count, page_count
            while not response_future.done():
                response = await response_queue.get()
                try:
                    raw_url = getattr(response, "url", None)
                    parsed = urlsplit(raw_url if type(raw_url) is str else "")
                    request = getattr(response, "request", None)
                    method = getattr(request, "method", None)
                    if (
                        parsed.scheme != "https"
                        or parsed.netloc != contract.creator_host
                        or parsed.path != contract.comment_response_path
                        or type(method) is not str
                        or method != contract.comment_response_method
                    ):
                        continue
                    status = getattr(response, "status", None)
                    if status == 401:
                        raise CommentInsightFailure("comment_login_required")
                    if status == 403:
                        raise CommentInsightFailure("comment_access_denied")
                    page_count += 1
                    if page_count > _MAX_RESPONSE_PAGES:
                        _invalid()
                    reader = getattr(response, "json", None)
                    if not callable(reader) or not inspect.iscoroutinefunction(reader):
                        _invalid()
                    payload = await await_operation(reader())
                    parsed_page, rejected = _parse_comment_page_with_rejections(
                        contract,
                        payload,
                        account_id,
                        content_id,
                        observed_at,
                    )
                    rejected_count += rejected
                    for record in parsed_page.comments:
                        existing = records.get(record.comment_key)
                        if existing is not None:
                            if existing != record:
                                _invalid()
                            continue
                        records[record.comment_key] = record
                        if record.comment_key in known_keys:
                            finish(tuple(records.values()), "known_comment", "")
                            return
                        if len(records) >= limit:
                            if limit == 100:
                                finish(
                                    tuple(records.values()),
                                    "limit_reached",
                                    "comment_limit_reached",
                                )
                            else:
                                finish(tuple(records.values()), "", "")
                            return
                    if parsed_page.platform_end:
                        finish(tuple(records.values()), "platform_end", "")
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
                        response_future.set_exception(exc)
                    raise
                except CommentInsightFailure as exc:
                    if not response_future.done():
                        response_future.set_exception(exc)
                    return
                except TimeoutError as exc:
                    if not response_future.done():
                        response_future.set_exception(exc)
                    return
                except BaseException:
                    if not response_future.done():
                        response_future.set_exception(
                            CommentInsightFailure("comment_payload_invalid")
                        )
                    return

        try:
            starter = self._playwright_factory()
            playwright = await await_operation(starter.start())
            browser = await await_operation(
                playwright.chromium.launch(headless=True)
            )
            context = await await_operation(
                browser.new_context(storage_state=str(state_path))
            )
            page = await await_operation(context.new_page())
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
                    except BaseException as exc:
                        return exc
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
                except BaseException as exc:
                    cleanup_steps -= 1
                    return exc
                return await settle_cleanup(close_result)

            if page is not None:
                remove_listener = getattr(page, "remove_listener", None)
                if remove_listener is None:
                    remove_listener = getattr(page, "off", None)
                if callable(remove_listener):
                    try:
                        removal = remove_listener("response", observe_response)
                    except BaseException as exc:
                        cleanup_errors.append(exc)
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
                    cleanup_errors.append(future_error)
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
                cleanup_steps -= 1

            for resource in (page, context, browser, playwright):
                close_error = await close_bounded(resource)
                if close_error is not None:
                    cleanup_errors.append(close_error)

        cleanup = CleanupReceipt(
            closed=not cleanup_errors,
            alive_resource_count=len(cleanup_errors),
        )
        if isinstance(caught, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise caught
        for cleanup_error in cleanup_errors:
            if isinstance(
                cleanup_error,
                (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
            ):
                raise cleanup_error
        if cleanup_errors:
            failure = _failure_with_cleanup(
                "comment_sync_cancelled", cleanup
            )
            _emit_report(
                report,
                _report_payload(
                    status="failed",
                    error_code=failure.error_code,
                    accepted_count=0,
                    rejected_count=rejected_count,
                    page_count=page_count,
                    stop_reason="",
                    cleanup=cleanup,
                ),
                total_deadline,
            )
            raise failure from None
        if isinstance(caught, TimeoutError):
            failure = _failure_with_cleanup(
                "comment_sync_timeout", cleanup, retryable=True
            )
            _emit_report(
                report,
                _report_payload(
                    status="failed",
                    error_code=failure.error_code,
                    accepted_count=0,
                    rejected_count=rejected_count,
                    page_count=page_count,
                    stop_reason="",
                    cleanup=cleanup,
                ),
                total_deadline,
            )
            raise failure from None
        if isinstance(caught, CommentInsightFailure):
            caught.cleanup_receipt = cleanup
            _emit_report(
                report,
                _report_payload(
                    status="failed",
                    error_code=caught.error_code,
                    accepted_count=0,
                    rejected_count=rejected_count,
                    page_count=page_count,
                    stop_reason="",
                    cleanup=cleanup,
                ),
                total_deadline,
            )
            raise caught from None
        if caught is not None:
            failure = _failure_with_cleanup("comment_payload_invalid", cleanup)
            _emit_report(
                report,
                _report_payload(
                    status="failed",
                    error_code=failure.error_code,
                    accepted_count=0,
                    rejected_count=rejected_count,
                    page_count=page_count,
                    stop_reason="",
                    cleanup=cleanup,
                ),
                total_deadline,
            )
            raise failure from None
        if result is None:
            raise _failure_with_cleanup(
                "comment_sync_timeout", cleanup, retryable=True
            ) from None
        comments, rejected_count, page_count, stop_reason, warning_code = result
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
        return batch

    @staticmethod
    def _run_short_lived(coroutine) -> CommentCollectionBatch:
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
        account_id, state_path, content_id, known_keys, limit = _required_inputs(
            account, content_id, known_keys, limit, report
        )
        try:
            contract = _required_contract(self._contract_loader())
        except CommentInsightFailure:
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise CommentInsightFailure("comment_content_unavailable") from None
        try:
            observed_at = self._observed_at_factory()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            _invalid()
        total_deadline = time.monotonic() + self._total_timeout_seconds
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
        try:
            return self._run_short_lived(coroutine)
        except CommentInsightFailure:
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise CommentInsightFailure("comment_payload_invalid") from None
