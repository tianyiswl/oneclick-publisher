"""Emit a zero-action plan or passively observe Xiaohongshu response shapes."""

import asyncio
import inspect
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


_ALLOWED_REPORT_KEYS = frozenset({
    "schemaVersion", "platformType", "mode", "phases", "status",
    "errorCode", "observedAt", "responses", "cleanup",
})
_ALLOWED_RESPONSE_KEYS = frozenset({
    "method", "path", "status", "contentType", "keyPaths",
    "fieldTypes", "listLengths", "paginationKeys",
})
_TEXT_LIMIT = 120
_RESPONSE_LIMIT = 100
_KEY_PATH_LIMIT = 300
_ACCOUNT_SELECTION_REQUIRED = "xiaohongshu_account_selection_required"
_CREATOR_HOST = "creator.xiaohongshu.com"
_CREATOR_HOME = "https://creator.xiaohongshu.com/creator/home"
_NETWORK_QUIET_TIMEOUT_MS = 30_000
_TOTAL_TIMEOUT_SECONDS = 60.0
_MAX_CLEANUP_BUDGET_SECONDS = 5.0
_STRUCTURAL_DEPTH_LIMIT = 40
_STRUCTURAL_NODE_LIMIT = 300
_DICT_KEY_SAMPLE_LIMIT = 100
_LIST_ITEM_SAMPLE_LIMIT = 25
_SYNC_SHAPE_BUDGET_SECONDS = 1.0
_PAGINATION_KEYS = frozenset({
    "cursor", "next_cursor", "nextcursor", "has_more", "hasmore",
    "page", "page_no", "page_num", "page_size", "pagesize", "total",
})
_CONTENT_IDENTIFIERS = frozenset({"note_id", "item_id", "content_id"})
_CONTENT_LIST_KEYS = frozenset({"items", "list", "notes", "feeds"})
_LIFETIME_METRIC_KEYS = frozenset({
    "view_count", "views", "like_count", "likes", "comment_count",
    "comments", "share_count", "shares", "collect_count", "collects",
})
_ACCOUNT_KEYS = frozenset({
    "account", "profile", "fans", "fan_count", "follower_count",
    "followers", "overview", "trend",
})


class ProbeFailure(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def build_plan() -> dict[str, object]:
    return {
        "schemaVersion": "xiaohongshu-data-contract-probe/v1",
        "platformType": 1,
        "mode": "plan",
        "phases": ["account_overview", "content_list", "content_lifetime"],
        "status": "planned",
        "errorCode": "",
        "observedAt": "",
        "responses": [],
        "cleanup": {"closed": True, "aliveResourceCount": 0},
    }


def _safe_text(value: object, default: str = "") -> str:
    if type(value) is str:
        return value[:_TEXT_LIMIT]
    return default


def _safe_integer(value: object, default: int = 0) -> int:
    if type(value) is int:
        return value
    return default


def _safe_boolean(value: object, default: bool = True) -> bool:
    if type(value) is bool:
        return value
    return default


def _safe_text_list(value: object, limit: int | None = None) -> list[str]:
    if type(value) is not list:
        return []
    items = value if limit is None else value[:limit]
    return [_safe_text(item) for item in items]


def _safe_text_mapping(
    value: object, limit: int | None = None
) -> dict[str, str]:
    if type(value) is not dict:
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if type(key) is not str:
            continue
        result[_safe_text(key)] = _safe_text(item)
        if limit is not None and len(result) >= limit:
            break
    return result


def _safe_integer_mapping(
    value: object, limit: int | None = None
) -> dict[str, int]:
    if type(value) is not dict:
        return {}
    result: dict[str, int] = {}
    for key, item in value.items():
        if type(key) is not str:
            continue
        result[_safe_text(key)] = _safe_integer(item)
        if limit is not None and len(result) >= limit:
            break
    return result


def _sanitize_response(value: object) -> dict[str, object]:
    source = value if type(value) is dict else {}
    source_url = source.get("url")
    if type(source_url) is str:
        try:
            source_path = urlsplit(source_url).path
        except ValueError:
            source_path = ""
    else:
        source_path = source.get("path")
    return {
        "method": _safe_text(source.get("method")),
        "path": _safe_text(source_path),
        "status": _safe_integer(source.get("status")),
        "contentType": _safe_text(source.get("contentType")),
        "keyPaths": _safe_text_list(source.get("keyPaths", source.get("keys")), _KEY_PATH_LIMIT),
        "fieldTypes": _safe_text_mapping(source.get("fieldTypes"), _KEY_PATH_LIMIT),
        "listLengths": _safe_integer_mapping(
            source.get("listLengths"), _KEY_PATH_LIMIT
        ),
        "paginationKeys": _safe_text_list(
            source.get("paginationKeys"), _KEY_PATH_LIMIT
        ),
    }


def sanitize_probe_report(value: object) -> dict[str, object]:
    """Rebuild the report with only value-free, structural fields."""
    source = value if type(value) is dict else {}
    plan = build_plan()
    responses = source.get("responses")
    cleanup = source.get("cleanup")
    phases = source.get("phases")
    safe_cleanup = cleanup if type(cleanup) is dict else {}

    return {
        "schemaVersion": _safe_text(source.get("schemaVersion"), plan["schemaVersion"]),
        "platformType": _safe_integer(source.get("platformType"), plan["platformType"]),
        "mode": _safe_text(source.get("mode"), plan["mode"]),
        "phases": _safe_text_list(phases) if type(phases) is list else list(plan["phases"]),
        "status": _safe_text(source.get("status"), plan["status"]),
        "errorCode": _safe_text(source.get("errorCode")),
        "observedAt": _safe_text(source.get("observedAt")),
        "responses": [
            _sanitize_response(response)
            for response in responses[:_RESPONSE_LIMIT]
        ] if type(responses) is list else [],
        "cleanup": {
            "closed": _safe_boolean(safe_cleanup.get("closed")),
            "aliveResourceCount": _safe_integer(safe_cleanup.get("aliveResourceCount")),
        },
    }


def _select_single_eligible_account() -> dict[str, object]:
    try:
        from app_core import account_service

        accounts = account_service.list_accounts()
    except Exception:
        raise ProbeFailure(_ACCOUNT_SELECTION_REQUIRED) from None

    if type(accounts) is not list:
        raise ProbeFailure(_ACCOUNT_SELECTION_REQUIRED)

    eligible_accounts: list[dict[str, object]] = []
    for account in accounts:
        if type(account) is not dict:
            continue
        account_id = account.get("id")
        platform_type = account.get("type")
        status = account.get("status")
        if (
            type(account_id) is int
            and account_id > 0
            and type(platform_type) is int
            and platform_type == 1
            and type(status) is int
            and status == 1
        ):
            eligible_accounts.append(account)

    if len(eligible_accounts) != 1:
        raise ProbeFailure(_ACCOUNT_SELECTION_REQUIRED)

    selected = eligible_accounts[0]
    file_path = selected.get("filePath")
    return {
        "id": selected["id"],
        "type": selected["type"],
        "status": selected["status"],
        "filePath": file_path if type(file_path) is str else "",
    }


def _response_metadata(response: object) -> tuple[str, str, int, str] | None:
    url = getattr(response, "url", None)
    if type(url) is not str:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != _CREATOR_HOST:
        return None

    headers = getattr(response, "headers", None)
    if type(headers) is not dict:
        return None
    raw_content_type = headers.get("content-type")
    if type(raw_content_type) is not str:
        return None
    content_type = raw_content_type.split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return None

    request = getattr(response, "request", None)
    method = getattr(request, "method", "")
    status = getattr(response, "status", 0)
    return (
        parsed.path,
        method if type(method) is str else "",
        status if type(status) is int else 0,
        content_type,
    )


def _structural_fields(
    payload: object, *, deadline: float, monotonic
) -> tuple[
    list[str], dict[str, str], dict[str, int], list[str]
]:
    key_paths: list[str] = []
    field_types: dict[str, str] = {}
    list_lengths: dict[str, int] = {}
    pagination_keys: list[str] = []

    seen_containers: set[int] = set()
    stack: list[tuple[object, str, int]] = [(payload, "", 0)]
    visited_nodes = 0

    while stack and visited_nodes < _STRUCTURAL_NODE_LIMIT:
        if monotonic() >= deadline:
            raise TimeoutError
        value, path, depth = stack.pop()
        visited_nodes += 1
        value_type = type(value)
        if path:
            key_paths.append(path)
            if value_type in (dict, list, str, int, float, bool, type(None)):
                field_types[path] = value_type.__name__

        if value_type in (dict, list):
            identity = id(value)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
        if depth >= _STRUCTURAL_DEPTH_LIMIT:
            continue

        children: list[tuple[object, str, int]] = []
        if value_type is dict:
            for key, child in value.items():
                if type(key) is not str:
                    continue
                child_path = f"{path}.{key}" if path else key
                if key.lower() in _PAGINATION_KEYS:
                    pagination_keys.append(child_path)
                children.append((child, child_path, depth + 1))
                if len(children) >= _DICT_KEY_SAMPLE_LIMIT:
                    break
        elif value_type is list:
            if path:
                list_lengths[path] = len(value)
            child_path = f"{path}[]" if path else "[]"
            children.extend(
                (child, child_path, depth + 1)
                for child in value[:_LIST_ITEM_SAMPLE_LIMIT]
            )
        stack.extend(reversed(children))

    return (
        list(dict.fromkeys(key_paths)),
        field_types,
        list_lengths,
        list(dict.fromkeys(pagination_keys)),
    )


def _shape_from_payload(
    metadata: tuple[str, str, int, str],
    payload: object,
    *,
    deadline: float,
    monotonic,
) -> dict[str, object]:
    path, method, status, content_type = metadata
    key_paths, field_types, list_lengths, pagination_keys = _structural_fields(
        payload, deadline=deadline, monotonic=monotonic
    )
    return {
        "method": method,
        "path": path,
        "status": status,
        "contentType": content_type,
        "keyPaths": key_paths,
        "fieldTypes": field_types,
        "listLengths": list_lengths,
        "paginationKeys": pagination_keys,
    }


def _response_shape(response) -> dict[str, object] | None:
    """Return only an eligible synchronous response's structural contract."""
    metadata = _response_metadata(response)
    if metadata is None:
        return None
    loader = getattr(response, "json", None)
    if not callable(loader):
        return None
    try:
        payload = loader()
    except Exception:
        return None
    if inspect.isawaitable(payload):
        close = getattr(payload, "close", None)
        if callable(close):
            close()
        return None
    started_at = time.monotonic()
    try:
        return _shape_from_payload(
            metadata,
            payload,
            deadline=started_at + _SYNC_SHAPE_BUDGET_SECONDS,
            monotonic=time.monotonic,
        )
    except TimeoutError:
        return None


async def _async_response_shape(
    response: object, *, deadline: float, monotonic
) -> dict[str, object] | None:
    metadata = _response_metadata(response)
    if metadata is None:
        return None
    loader = getattr(response, "json", None)
    if not callable(loader):
        return None
    try:
        payload = loader()
        if inspect.isawaitable(payload):
            payload = await _await_with_deadline(
                payload, deadline=deadline, monotonic=monotonic
            )
    except TimeoutError:
        raise
    except Exception:
        return None
    return _shape_from_payload(
        metadata, payload, deadline=deadline, monotonic=monotonic
    )


def _classify_shape(shape: dict[str, object]) -> str:
    paths = shape.get("keyPaths")
    if type(paths) is not list:
        return "unclassified"
    tokens = {
        token.lower().removesuffix("[]")
        for path in paths
        if type(path) is str
        for token in path.split(".")
    }
    if tokens & _CONTENT_IDENTIFIERS and tokens & _CONTENT_LIST_KEYS:
        return "content_list"
    if tokens & _CONTENT_IDENTIFIERS and tokens & _LIFETIME_METRIC_KEYS:
        return "content_lifetime"
    if tokens & _ACCOUNT_KEYS:
        return "account_overview"
    return "unclassified"


async def _await_with_deadline(value: object, *, deadline: float, monotonic) -> object:
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
    resource: object, *, deadline: float, monotonic
) -> BaseException | None:
    try:
        close = getattr(resource, "close", None)
        if not callable(close):
            close = getattr(resource, "stop", None)
        exit_context = False
        if not callable(close):
            close = getattr(resource, "__aexit__", None)
            exit_context = callable(close)
        if not callable(close):
            return RuntimeError("resource_close_unavailable")
        await _await_with_deadline(
            close(None, None, None) if exit_context else close(),
            deadline=deadline,
            monotonic=monotonic,
        )
    except BaseException as exc:
        return exc
    return None


def _storage_state_path(account: object) -> Path:
    if type(account) is not dict:
        raise ProbeFailure("xiaohongshu_session_state_invalid")
    file_path = account.get("filePath")
    if type(file_path) is not str or not file_path.strip():
        raise ProbeFailure("xiaohongshu_session_state_invalid")

    supplied = Path(file_path)
    if supplied.is_absolute():
        state_path = supplied
    else:
        from app_core.paths import COOKIE_DIR

        state_path = Path(COOKIE_DIR) / supplied.name
    if not state_path.is_file():
        raise ProbeFailure("xiaohongshu_session_state_invalid")
    return state_path


def _observed_at(utc_now) -> str:
    try:
        value = utc_now()
        rendered = value.isoformat()
    except Exception:
        return ""
    return rendered if type(rendered) is str else ""


async def _run_probe(
    account: dict,
    *,
    playwright_factory,
    monotonic,
    utc_now,
    work_deadline: float,
    total_deadline: float,
) -> tuple[dict[str, object], BaseException | None]:
    playwright_manager = None
    playwright = None
    browser = None
    context = None
    page = None
    observed_responses: list[object] = []
    shapes: list[dict[str, object]] = []
    caught: BaseException | None = None
    cleanup_errors: list[BaseException] = []

    def retain_response(response: object) -> None:
        if len(observed_responses) >= _RESPONSE_LIMIT:
            return
        try:
            eligible = _response_metadata(response) is not None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return
        if eligible:
            observed_responses.append(response)

    try:
        state_path = _storage_state_path(account)
        playwright_manager = await _await_with_deadline(
            playwright_factory(), deadline=work_deadline, monotonic=monotonic
        )
        start = getattr(playwright_manager, "start", None)
        playwright = await _await_with_deadline(
            start(), deadline=work_deadline, monotonic=monotonic
        ) if callable(start) else playwright_manager
        browser = await _await_with_deadline(
            playwright.chromium.launch(headless=True),
            deadline=work_deadline,
            monotonic=monotonic,
        )
        context = await _await_with_deadline(
            browser.new_context(storage_state=str(state_path)),
            deadline=work_deadline,
            monotonic=monotonic,
        )
        page = await _await_with_deadline(
            context.new_page(), deadline=work_deadline, monotonic=monotonic
        )
        page.on("response", retain_response)
        await _await_with_deadline(
            page.goto(
                _CREATOR_HOME,
                wait_until="networkidle",
                timeout=_NETWORK_QUIET_TIMEOUT_MS,
            ),
            deadline=work_deadline,
            monotonic=monotonic,
        )
        for response in observed_responses:
            shape = await _async_response_shape(
                response, deadline=work_deadline, monotonic=monotonic
            )
            if shape is not None:
                shapes.append(shape)
    except BaseException as exc:
        caught = exc
    finally:
        resources = (
            page,
            context,
            browser,
            playwright if playwright is not None else playwright_manager,
        )
        for index, resource in enumerate(resources):
            if resource is None:
                continue
            remaining_count = len(resources) - index
            remaining_cleanup = max(0.0, total_deadline - monotonic())
            resource_deadline = (
                monotonic() + (remaining_cleanup / remaining_count)
            )
            close_error = await _close_resource(
                resource, deadline=resource_deadline, monotonic=monotonic
            )
            if close_error is not None:
                cleanup_errors.append(close_error)

    process_error = next(
        (
            error for error in ((caught,) + tuple(cleanup_errors))
            if isinstance(error, (KeyboardInterrupt, SystemExit))
        ),
        None,
    )
    if process_error is not None:
        raise process_error

    alive_count = sum(1 for error in cleanup_errors if error is not None)
    report = build_plan()
    report.update({
        "mode": "execute",
        "phases": list(dict.fromkeys(_classify_shape(shape) for shape in shapes)),
        "status": "success",
        "errorCode": "",
        "observedAt": _observed_at(utc_now),
        "responses": shapes,
        "cleanup": {
            "closed": alive_count == 0,
            "aliveResourceCount": int(alive_count),
        },
    })
    if cleanup_errors:
        report["status"] = "failed"
        report["errorCode"] = "xiaohongshu_probe_cleanup_incomplete"
    elif isinstance(caught, (TimeoutError, asyncio.CancelledError)):
        report["status"] = "failed"
        report["errorCode"] = "xiaohongshu_probe_timeout"
    elif isinstance(caught, ProbeFailure):
        report["status"] = "failed"
        report["errorCode"] = caught.error_code
    elif caught is not None:
        report["status"] = "failed"
        report["errorCode"] = "xiaohongshu_probe_failed"
    return sanitize_probe_report(report), caught


def _probe_with_browser(
    account: dict,
    *,
    playwright_factory,
    monotonic,
    utc_now,
) -> dict[str, object]:
    """Run one bounded passive probe with no page-side requests or DOM access."""
    started_at = monotonic()
    total_deadline = started_at + _TOTAL_TIMEOUT_SECONDS
    cleanup_budget = min(
        _MAX_CLEANUP_BUDGET_SECONDS,
        _TOTAL_TIMEOUT_SECONDS / 2,
    )
    work_deadline = total_deadline - cleanup_budget

    async def bounded_probe() -> dict[str, object]:
        report, _caught = await _run_probe(
            account,
            playwright_factory=playwright_factory,
            monotonic=monotonic,
            utc_now=utc_now,
            work_deadline=work_deadline,
            total_deadline=total_deadline,
        )
        return report

    try:
        return asyncio.run(bounded_probe())
    except (KeyboardInterrupt, SystemExit):
        raise
    except TimeoutError:
        report = build_plan()
        report.update({
            "mode": "execute",
            "status": "failed",
            "errorCode": "xiaohongshu_probe_timeout",
            "observedAt": _observed_at(utc_now),
        })
        return sanitize_probe_report(report)


def _execute(*, browser_factory, utc_now) -> dict[str, object]:
    """Apply the account gate, then run one passive official-page probe."""
    return _probe_with_browser(
        _select_single_eligible_account(),
        playwright_factory=browser_factory,
        monotonic=time.monotonic,
        utc_now=utc_now,
    )


def main(argv: list[str] | None = None, *, stdout=sys.stdout) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--execute"]:
        def browser_factory():
            from playwright.async_api import async_playwright

            return async_playwright()

        from datetime import datetime, timezone

        payload = _execute(
            browser_factory=browser_factory,
            utc_now=lambda: datetime.now(timezone.utc),
        )
    else:
        payload = build_plan()
    json.dump(payload, stdout, ensure_ascii=False)
    stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
