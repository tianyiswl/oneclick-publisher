"""Emit a zero-action plan or passively observe Xiaohongshu response shapes."""

import asyncio
import inspect
import json
import os
import re
import secrets
import stat
import sys
import time
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit


_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[1]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))


_ALLOWED_REPORT_KEYS = frozenset({
    "schemaVersion", "platformType", "mode", "phases", "missingPhases",
    "status", "errorCode", "observedAt", "responses", "cleanup",
})
_ALLOWED_RESPONSE_KEYS = frozenset({
    "method", "path", "status", "contentType", "keyPaths",
    "fieldTypes", "listLengths", "paginationKeys",
})
_TEXT_LIMIT = 120
_RESPONSE_LIMIT = 100
_REQUEST_PHASE_LIMIT = _RESPONSE_LIMIT * 4
_KEY_PATH_LIMIT = 300
_REVIEW_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_REVIEW_ID_SEGMENT = re.compile(
    r"^(?:[0-9]+|[0-9a-f]{24}|[0-9a-f]{8}-[0-9a-f-]{27,})$", re.I)
_REVIEW_SENSITIVE_NAMES = frozenset({
    "authorization", "cookie", "cookies", "token", "ticket", "session",
    "sessionid", "password", "passwd", "secret", "phone", "mobile", "email",
})
_REVIEW_STATIC_HYPHENATED_NAMES = frozenset({"data-analysis", "note-detail"})
_MAX_RESPONSE_BODY_BYTES = 1_048_576
_MAX_TOTAL_RESPONSE_BODY_BYTES = 4_194_304
_KNOWN_CHUNKED_JSON_PATHS = frozenset({
    "/api/galaxy/v2/creator/datacenter/account/base",
    "/api/galaxy/creator/home/personal_info",
    "/api/galaxy/creator/datacenter/note/analyze/list",
    "/api/galaxy/creator/datacenter/note/base",
})
_ACCOUNT_SELECTION_REQUIRED = "xiaohongshu_account_selection_required"
_ARGUMENTS_INVALID = "xiaohongshu_arguments_invalid"
_REPORT_PATH_INVALID = "xiaohongshu_report_path_invalid"
_REPORT_WRITE_FAILED = "xiaohongshu_report_write_failed"
_REPOSITORY_ROOT = _BOOTSTRAP_ROOT
_REPORT_OUTPUT_DIRECTORY = (
    _REPOSITORY_ROOT
    / ".superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery"
)
_BUILTIN_PATH_TYPE = type(Path())
_STATIC_ENDPOINT_SEGMENTS = frozenset({
    "api", "data", "deep", "wide", "cycle", "deadline", "item",
    "eligible", "overview", "note", "notes", "content", "contents",
    "list", "lists", "feed", "feeds", "metrics", "stats", "creator",
    "home", "profile", "account", "accounts", "user", "users", "v1",
    "v2",
})
_REDACTED_ENDPOINT_SEGMENT = ":segment"
# Empty until a real passive run is reviewed and its complete path template is
# deliberately promoted. Discovery responses are still retained, but cannot
# become contract evidence merely because individual segments look familiar.
_REVIEWED_CONTRACT_PATH_TEMPLATES = MappingProxyType({
    "/api/galaxy/v2/creator/datacenter/account/base": "account_overview",
    "/api/galaxy/creator/home/personal_info": "account_overview",
    "/api/galaxy/creator/datacenter/note/analyze/list": "content_list",
    "/api/galaxy/creator/datacenter/note/base": "content_lifetime",
})
_DYNAMIC_ID_PARENTS = frozenset({
    "note", "notes", "item", "content", "contents", "user", "users",
    "account", "accounts",
})
_UUID_ENDPOINT_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_HEX_ENDPOINT_SEGMENT = re.compile(r"^[0-9a-f]{12,}$", re.IGNORECASE)
_HIGH_ENTROPY_ENDPOINT_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_CONTENT_LENGTH = re.compile(r"^[0-9]+$")
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_TEMPORARY_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_CREATOR_HOST = "creator.xiaohongshu.com"
_CREATOR_HOME = "https://creator.xiaohongshu.com/creator/home"
_DATA_ANALYSIS_URL = (
    "https://creator.xiaohongshu.com/statistics/data-analysis?source=official"
)
_NOTE_DETAIL_URL = "https://creator.xiaohongshu.com/statistics/note-detail"
_NOTE_ID = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)
_REVIEW_NAVIGATION_PATHS = MappingProxyType({
    "account_home": "/creator/home",
    "data_analysis": "/statistics/data-analysis",
    "content_lifetime": "/statistics/note-detail",
})
_ALLOWED_OBSERVATION_PHASES = frozenset(_REVIEW_NAVIGATION_PATHS)
_OBSERVATION_CONTRACT_CANDIDATES = MappingProxyType({
    "account_home": ("account_overview",),
    "data_analysis": ("account_overview", "content_list"),
    "content_lifetime": ("content_lifetime",),
})
_NETWORK_QUIET_TIMEOUT_MS = 30_000
_PASSIVE_CAPTURE_WAIT_MS = 6_000
_CONTENT_LIST_CAPTURE_POLL_MS = 250
_CONTENT_LIST_CAPTURE_POLL_ATTEMPTS = 32
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
_REQUIRED_CONTRACT_PHASES = (
    "account_overview", "content_list", "content_lifetime",
)
_CONTRACTS_UNOBSERVED = "xiaohongshu_contracts_unobserved"
_CONTRACTS_INCOMPLETE = "xiaohongshu_contracts_incomplete"
_LOGIN_REQUIRED = "login_required"
_VERIFICATION_REQUIRED = "verification_required"
_NAVIGATION_UNVERIFIED = "xiaohongshu_navigation_unverified"
_CONTENT_IDENTIFIERS = frozenset({"note_id", "item_id", "content_id", "id"})
_CONTENT_LIST_KEYS = frozenset({
    "items", "list", "notes", "feeds", "note_infos",
})
_METRIC_KEYS = frozenset({
    "view_count", "views", "like_count", "likes", "comment_count",
    "comments", "share_count", "shares", "collect_count", "collects",
})
_ACCOUNT_METRIC_KEYS = frozenset({
    "fans", "fans_count", "fan_count", "follower_count", "followers",
}) | _METRIC_KEYS
_ACCOUNT_SCOPE_KEYS = frozenset({
    "trend", "interval", "window", "range", "period", "daily",
    "seven", "thirty",
})
_CUMULATIVE_SCOPE_KEYS = frozenset({
    "lifetime", "cumulative", "all_time", "total",
})
_NON_LIFETIME_SCOPE_KEYS = frozenset({
    "interval", "window", "range", "period", "daily",
})
_ACCOUNT_CONTAINER_KEYS = frozenset({
    "account", "profile", "overview", "note_info",
})
_ALLOWED_MODES = frozenset({"plan", "execute"})
_ALLOWED_STATUSES = frozenset({
    "planned", "success", "partial_success", "failed",
})
_ALLOWED_ERROR_CODES = frozenset({
    "",
    _ACCOUNT_SELECTION_REQUIRED,
    _ARGUMENTS_INVALID,
    _REPORT_PATH_INVALID,
    _REPORT_WRITE_FAILED,
    _CONTRACTS_UNOBSERVED,
    _CONTRACTS_INCOMPLETE,
    _LOGIN_REQUIRED,
    _VERIFICATION_REQUIRED,
    _NAVIGATION_UNVERIFIED,
    "xiaohongshu_session_state_invalid",
    "xiaohongshu_probe_cleanup_incomplete",
    "xiaohongshu_probe_timeout",
    "xiaohongshu_probe_failed",
})
_ALLOWED_METHODS = frozenset({"GET", "POST"})
_ALLOWED_CONTENT_TYPES = frozenset({"application/json"})
_ALLOWED_FIELD_TYPES = frozenset({
    "dict", "list", "str", "int", "float", "bool", "NoneType",
})
_SENSITIVE_STRUCTURAL_KEYS = frozenset({
    "token", "cookie", "authorization", "set_cookie", "password",
    "secret", "title", "nickname", "phone", "mobile", "body", "content",
})
_SAFE_STATIC_STRUCTURAL_KEYS = frozenset({
    "data", "result", "success", "code", "message", "metrics", "stats",
}) | (
    _PAGINATION_KEYS
    | _CONTENT_IDENTIFIERS
    | _CONTENT_LIST_KEYS
    | _METRIC_KEYS
    | _ACCOUNT_METRIC_KEYS
    | _ACCOUNT_SCOPE_KEYS
    | _CUMULATIVE_SCOPE_KEYS
    | _ACCOUNT_CONTAINER_KEYS
)
_OBSERVED_AT = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


class ProbeFailure(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def build_plan() -> dict[str, object]:
    return {
        "schemaVersion": "xiaohongshu-data-contract-probe/v1",
        "platformType": 1,
        "mode": "plan",
        "phases": list(_REQUIRED_CONTRACT_PHASES),
        "missingPhases": [],
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


def _review_path(value: object) -> str:
    if type(value) is not str:
        return ""
    if value.startswith("/") and "?" not in value and "#" not in value:
        raw_path = value
    else:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        if parsed.scheme != "https" or parsed.hostname != _CREATOR_HOST:
            return ""
        raw_path = parsed.path
    output: list[str] = []
    for segment in raw_path.split("/"):
        if not segment:
            continue
        lowered = segment.lower()
        if _REVIEW_ID_SEGMENT.fullmatch(lowered):
            output.append(":id")
        elif (
            (_REVIEW_SAFE_NAME.fullmatch(lowered)
             or lowered in _REVIEW_STATIC_HYPHENATED_NAMES)
            and lowered not in _REVIEW_SENSITIVE_NAMES
        ):
            output.append(lowered)
        else:
            output.append(":segment")
    return "/" + "/".join(output)


def _review_key_path(value: object) -> str:
    if type(value) is not str or len(value) > 192:
        return ""
    parts = value.replace("[]", ".[]").split(".")
    rebuilt: list[str] = []
    for part in parts:
        if part == "[]":
            if not rebuilt:
                return ""
            rebuilt[-1] += "[]"
        elif (
            _REVIEW_SAFE_NAME.fullmatch(part)
            and part not in _REVIEW_SENSITIVE_NAMES
        ):
            rebuilt.append(part)
        else:
            return ""
    return ".".join(rebuilt)


def _review_key_paths(value: object) -> list[str]:
    if type(value) is not list:
        return []
    reviewed: list[str] = []
    for item in value[:_KEY_PATH_LIMIT]:
        path = _review_key_path(item)
        if path:
            reviewed.append(path)
    return reviewed


def _review_field_types(value: object) -> dict[str, str]:
    if type(value) is not dict:
        return {}
    reviewed: dict[str, str] = {}
    for key, item in value.items():
        path = _review_key_path(key)
        if not path or type(item) is not str or item not in _ALLOWED_FIELD_TYPES:
            continue
        reviewed[path] = item
        if len(reviewed) >= _KEY_PATH_LIMIT:
            break
    return reviewed


def _review_list_lengths(value: object) -> dict[str, int]:
    if type(value) is not dict:
        return {}
    reviewed: dict[str, int] = {}
    for key, item in value.items():
        path = _review_key_path(key)
        if (
            not path
            or type(item) is not int
            or item < 0
            or item > _STRUCTURAL_NODE_LIMIT
        ):
            continue
        reviewed[path] = item
        if len(reviewed) >= _KEY_PATH_LIMIT:
            break
    return reviewed


def _review_navigation(value: object) -> list[dict[str, str]]:
    if type(value) is not list:
        return []
    reviewed: list[dict[str, str]] = []
    for source in value[:len(_REVIEW_NAVIGATION_PATHS)]:
        if type(source) is not dict:
            continue
        phase = source.get("phase")
        path = _review_path(source.get("path"))
        if (
            type(phase) is str
            and _REVIEW_NAVIGATION_PATHS.get(phase) == path
        ):
            reviewed.append({"phase": phase, "path": path})
    return reviewed


def _review_schema(report: object) -> dict[str, object]:
    if type(report) is not dict:
        return {"responses": [], "navigation": []}
    reviewed: list[dict[str, object]] = []
    responses = report.get("responses")
    for source in responses[:_RESPONSE_LIMIT] if type(responses) is list else ():
        if type(source) is not dict:
            continue
        url = source.get("url")
        path_value = url if type(url) is str else source.get("path")
        path = _review_path(path_value)
        if not path:
            continue
        row: dict[str, object] = {"path": path}
        observation_phase = source.get("observationPhase")
        if (
            type(observation_phase) is str
            and observation_phase in _ALLOWED_OBSERVATION_PHASES
        ):
            row["observationPhase"] = observation_phase
        method = source.get("method")
        if type(method) is str and method in _ALLOWED_METHODS:
            row["method"] = method
        status = source.get("status")
        if type(status) is int and 100 <= status <= 599:
            row["status"] = status
        content_type = source.get("contentType")
        if type(content_type) is str and content_type in _ALLOWED_CONTENT_TYPES:
            row["contentType"] = content_type
        structural_values = (
            (source.get("keyPaths"), list),
            (source.get("fieldTypes"), dict),
            (source.get("listLengths"), dict),
            (source.get("paginationKeys"), list),
        )
        if all(value is None or type(value) is expected_type
               for value, expected_type in structural_values):
            key_paths = _review_key_paths(source.get("keyPaths"))
            if key_paths:
                row["keyPaths"] = key_paths
            field_types = _review_field_types(source.get("fieldTypes"))
            if field_types:
                row["fieldTypes"] = field_types
            list_lengths = _review_list_lengths(source.get("listLengths"))
            if list_lengths:
                row["listLengths"] = list_lengths
            pagination_keys = _review_key_paths(source.get("paginationKeys"))
            if pagination_keys:
                row["paginationKeys"] = pagination_keys
        reviewed.append(row)
    return {
        "responses": reviewed,
        "navigation": _review_navigation(report.get("navigation")),
    }


def _enum_text(value: object, allowed: frozenset[str], default: str = "") -> str:
    if type(value) is str and value in allowed:
        return value
    return default


def _normalize_structural_key(value: object) -> str | None:
    """Return a static key name or a fixed template without retaining IDs."""
    if type(value) is not str or not value:
        return None
    if value in {":content_identifier", ":identifier", ":key"}:
        return ":key"
    lowered = value.lower()
    if lowered in _SAFE_STATIC_STRUCTURAL_KEYS:
        return lowered
    components = frozenset(lowered.split("_"))
    if lowered in _SENSITIVE_STRUCTURAL_KEYS or components & _SENSITIVE_STRUCTURAL_KEYS:
        return ":key"
    return ":key"


def _sanitize_structural_path(value: object) -> str | None:
    if type(value) is not str or not value or len(value) > 4_096:
        return None
    normalized: list[str] = []
    for segment in value.split("."):
        is_list = segment.endswith("[]")
        key = segment[:-2] if is_list else segment
        if key == "" and is_list:
            normalized.append("[]")
            continue
        safe_key = _normalize_structural_key(key)
        if safe_key is None:
            return None
        normalized.append(f"{safe_key}[]" if is_list else safe_key)
        if len(normalized) > _STRUCTURAL_DEPTH_LIMIT:
            break
    return ".".join(normalized)


def _sanitize_structural_list(value: object) -> list[str]:
    if type(value) is not list:
        return []
    result: list[str] = []
    for item in value[:_KEY_PATH_LIMIT]:
        safe_path = _sanitize_structural_path(item)
        if safe_path is not None:
            result.append(safe_path)
    return result


def _sanitize_field_types(value: object) -> dict[str, str]:
    if type(value) is not dict:
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        safe_path = _sanitize_structural_path(key)
        if safe_path is None:
            continue
        result[safe_path] = _enum_text(item, _ALLOWED_FIELD_TYPES)
        if len(result) >= _KEY_PATH_LIMIT:
            break
    return result


def _sanitize_list_lengths(value: object) -> dict[str, int]:
    if type(value) is not dict:
        return {}
    result: dict[str, int] = {}
    for key, item in value.items():
        safe_path = _sanitize_structural_path(key)
        if safe_path is None or type(item) is not int:
            continue
        result[safe_path] = max(0, min(item, _STRUCTURAL_NODE_LIMIT))
        if len(result) >= _KEY_PATH_LIMIT:
            break
    return result


def _sanitize_phases(value: object) -> list[str]:
    if type(value) is not list:
        return []
    return [
        phase for phase in _REQUIRED_CONTRACT_PHASES
        if phase in value
    ]


def _safe_observed_at(value: object) -> str:
    if type(value) is str and _OBSERVED_AT.fullmatch(value) is not None:
        return value
    return ""


def _endpoint_path(path: object) -> str | None:
    """Return a redacted endpoint shape, never a literal unknown segment."""
    if type(path) is not str:
        return ""
    try:
        raw_path = urlsplit(path).path
    except ValueError:
        return ""
    if not raw_path:
        return ""
    if not raw_path.startswith("/"):
        return None
    segments = raw_path.split("/")[1:]
    if not segments or any(not segment for segment in segments):
        return None

    normalized: list[str] = []
    for segment in segments:
        if normalized and normalized[-1] in _DYNAMIC_ID_PARENTS:
            normalized.append(":id")
            continue
        if segment in {":id", _REDACTED_ENDPOINT_SEGMENT}:
            normalized.append(segment)
            continue
        if segment in _STATIC_ENDPOINT_SEGMENTS:
            normalized.append(segment)
            continue
        is_dynamic_id = (
            segment.isdecimal()
            or _UUID_ENDPOINT_SEGMENT.fullmatch(segment) is not None
            or _HEX_ENDPOINT_SEGMENT.fullmatch(segment) is not None
            or _HIGH_ENTROPY_ENDPOINT_SEGMENT.fullmatch(segment) is not None
        )
        if is_dynamic_id:
            normalized.append(":id")
            continue
        normalized.append(_REDACTED_ENDPOINT_SEGMENT)
    return "/" + "/".join(normalized)


def _sanitize_response(value: object) -> dict[str, object] | None:
    source = value if type(value) is dict else {}
    source_url = source.get("url")
    if type(source_url) is str:
        try:
            source_path = urlsplit(source_url).path
        except ValueError:
            source_path = ""
    else:
        source_path = source.get("path")
    safe_path = (
        source_path
        if type(source_path) is str
        and source_path in _REVIEWED_CONTRACT_PATH_TEMPLATES
        else _endpoint_path(source_path)
    )
    if safe_path is None:
        return None
    status = source.get("status")
    return {
        "observationPhase": _enum_text(
            source.get("observationPhase"), _ALLOWED_OBSERVATION_PHASES
        ),
        "method": _enum_text(source.get("method"), _ALLOWED_METHODS),
        "path": safe_path,
        "status": status if type(status) is int and 100 <= status <= 599 else 0,
        "contentType": _enum_text(
            source.get("contentType"), _ALLOWED_CONTENT_TYPES
        ),
        "keyPaths": _sanitize_structural_list(
            source.get("keyPaths", source.get("keys"))
        ),
        "fieldTypes": _sanitize_field_types(source.get("fieldTypes")),
        "listLengths": _sanitize_list_lengths(source.get("listLengths")),
        "paginationKeys": _sanitize_structural_list(
            source.get("paginationKeys")
        ),
    }


def sanitize_probe_report(value: object) -> dict[str, object]:
    """Rebuild the report with only value-free, structural fields."""
    source = value if type(value) is dict else {}
    plan = build_plan()
    responses = source.get("responses")
    cleanup = source.get("cleanup")
    safe_cleanup = cleanup if type(cleanup) is dict else {}

    safe_responses: list[dict[str, object]] = []
    if type(responses) is list:
        for response in responses[:_RESPONSE_LIMIT]:
            safe_response = _sanitize_response(response)
            if safe_response is not None:
                safe_responses.append(safe_response)

    mode = _enum_text(source.get("mode"), _ALLOWED_MODES, plan["mode"])
    cleanup_closed = _safe_boolean(safe_cleanup.get("closed"))
    cleanup_alive = _safe_integer(safe_cleanup.get("aliveResourceCount"))
    error_code = _enum_text(source.get("errorCode"), _ALLOWED_ERROR_CODES)
    status = _enum_text(source.get("status"), _ALLOWED_STATUSES, plan["status"])

    if mode == "plan":
        phases = list(_REQUIRED_CONTRACT_PHASES)
        missing_phases = []
        status = "planned"
        error_code = ""
    else:
        phases, missing_phases, status, error_code = _contract_outcome(
            safe_responses
        )
        source_error = _enum_text(source.get("errorCode"), _ALLOWED_ERROR_CODES)
        if source_error not in {"", _CONTRACTS_UNOBSERVED, _CONTRACTS_INCOMPLETE}:
            status = "failed"
            error_code = source_error
            if error_code in {_LOGIN_REQUIRED, _VERIFICATION_REQUIRED}:
                safe_responses = []
                phases = []
                missing_phases = list(_REQUIRED_CONTRACT_PHASES)
        elif not cleanup_closed or cleanup_alive != 0:
            status = "failed"
            error_code = "xiaohongshu_probe_cleanup_incomplete"

    return {
        "schemaVersion": (
            plan["schemaVersion"]
            if source.get("schemaVersion") != plan["schemaVersion"]
            else source["schemaVersion"]
        ),
        "platformType": 1,
        "mode": mode,
        "phases": phases,
        "missingPhases": missing_phases,
        "status": status,
        "errorCode": error_code,
        "observedAt": _safe_observed_at(source.get("observedAt")),
        "responses": safe_responses,
        "cleanup": {
            "closed": cleanup_closed,
            "aliveResourceCount": cleanup_alive,
        },
    }


def _report_destination(path: object) -> Path:
    """Accept only a built-in path below the dedicated probe-report directory."""
    if type(path) is str:
        candidate = Path(path)
    elif type(path) is _BUILTIN_PATH_TYPE:
        candidate = path
    else:
        raise ProbeFailure(_REPORT_PATH_INVALID)

    if ".." in candidate.parts:
        raise ProbeFailure(_REPORT_PATH_INVALID)
    repository_root = Path(os.path.abspath(_REPOSITORY_ROOT))
    root = Path(os.path.abspath(_REPORT_OUTPUT_DIRECTORY))
    destination = Path(os.path.abspath(
        candidate if candidate.is_absolute() else repository_root / candidate
    ))
    if (
        root == repository_root
        or not root.is_relative_to(repository_root)
        or destination == root
        or not destination.is_relative_to(root)
    ):
        raise ProbeFailure(_REPORT_PATH_INVALID)

    relative = destination.relative_to(repository_root)
    current = repository_root
    for index, segment in enumerate(relative.parts):
        current = current / segment
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        is_destination = index == len(relative.parts) - 1
        if stat.S_ISLNK(mode):
            raise ProbeFailure(_REPORT_PATH_INVALID)
        if is_destination:
            if not stat.S_ISREG(mode):
                raise ProbeFailure(_REPORT_PATH_INVALID)
        elif not stat.S_ISDIR(mode):
            raise ProbeFailure(_REPORT_PATH_INVALID)
    return destination


def _open_repository_root() -> int:
    repository_root = Path(os.path.abspath(_REPOSITORY_ROOT))
    directory_fd = os.open(repository_root, _DIRECTORY_OPEN_FLAGS)
    try:
        opened = os.fstat(directory_fd)
        expected = os.stat(repository_root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
        ):
            raise NotADirectoryError
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_report_directory(destination: Path) -> int:
    """Create and open the report parent from the verified repository FD."""
    repository_root = Path(os.path.abspath(_REPOSITORY_ROOT))
    relative_parent = destination.parent.relative_to(repository_root)
    directory_fd = _open_repository_root()
    try:
        for segment in relative_parent.parts:
            try:
                next_fd = os.open(
                    segment, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd
                )
            except FileNotFoundError:
                try:
                    os.mkdir(segment, 0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(
                    segment, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd
                )
            try:
                if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                    raise NotADirectoryError
            except Exception:
                os.close(next_fd)
                raise
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_temporary_report_file(directory_fd: int, name: str) -> tuple[int, str]:
    for _ in range(100):
        temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
        try:
            return (
                os.open(
                    temporary_name,
                    _TEMPORARY_OPEN_FLAGS,
                    0o600,
                    dir_fd=directory_fd,
                ),
                temporary_name,
            )
        except FileExistsError:
            continue
    raise FileExistsError


def _write_report(path: Path, report: object) -> None:
    """Persist only the final rebuilt structural report at the approved path."""
    try:
        destination = _report_destination(path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise ProbeFailure(_REPORT_PATH_INVALID) from None

    directory_fd: int | None = None
    temporary_fd: int | None = None
    temporary_name: str | None = None
    try:
        serialized = json.dumps(
            sanitize_probe_report(report),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        destination = _report_destination(destination)
        directory_fd = _open_report_directory(destination)
        temporary_fd, temporary_name = _open_temporary_report_file(
            directory_fd, destination.name
        )
        temporary = os.fdopen(temporary_fd, mode="w", encoding="utf-8")
        temporary_fd = None
        with temporary:
            temporary.write(serialized)
            temporary.flush()
        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise ProbeFailure(_REPORT_WRITE_FAILED) from None
    finally:
        if temporary_fd is not None:
            try:
                os.close(temporary_fd)
            except Exception:
                pass
        if temporary_name is not None and directory_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except Exception:
                pass
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except Exception:
                pass


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


def _response_metadata(
    response: object,
    *,
    allow_known_missing_length: bool = False,
) -> tuple[str, str, int, str, int] | None:
    url = getattr(response, "url", None)
    if type(url) is not str:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != _CREATOR_HOST:
        return None
    endpoint_path = _endpoint_path(parsed.path)
    if not endpoint_path:
        return None
    if parsed.path in _REVIEWED_CONTRACT_PATH_TEMPLATES:
        endpoint_path = parsed.path

    headers = getattr(response, "headers", None)
    if type(headers) is not dict:
        return None
    raw_content_type = headers.get("content-type")
    if type(raw_content_type) is not str:
        return None
    content_type = raw_content_type.split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return None

    raw_content_length = headers.get("content-length")
    if (
        raw_content_length is None
        and allow_known_missing_length
        and parsed.path in _KNOWN_CHUNKED_JSON_PATHS
    ):
        content_length = 0
    else:
        if (
            type(raw_content_length) is not str
            or not 1 <= len(raw_content_length) <= 20
            or _CONTENT_LENGTH.fullmatch(raw_content_length) is None
        ):
            return None
        try:
            content_length = int(raw_content_length)
        except ValueError:
            return None
        if not 1 <= content_length <= _MAX_RESPONSE_BODY_BYTES:
            return None

    content_encoding = headers.get("content-encoding")
    if content_encoding is not None and (
        type(content_encoding) is not str
        or content_encoding.strip().lower() != "identity"
    ):
        return None

    request = getattr(response, "request", None)
    method = getattr(request, "method", "")
    status = getattr(response, "status", 0)
    if (
        type(method) is not str
        or method not in _ALLOWED_METHODS
        or type(status) is not int
        or not 200 <= status < 300
    ):
        return None
    return (
        endpoint_path,
        method,
        status,
        content_type,
        content_length,
    )


def _navigation_error_code(response: object) -> str | None:
    """Classify only controlled navigation metadata, never body or DOM text."""
    url = getattr(response, "url", None)
    status = getattr(response, "status", None)
    headers = getattr(response, "headers", None)
    if type(url) is not str or type(status) is not int or type(headers) is not dict:
        return _NAVIGATION_UNVERIFIED
    try:
        parsed = urlsplit(url)
    except ValueError:
        return _NAVIGATION_UNVERIFIED
    if parsed.scheme != "https" or parsed.hostname != _CREATOR_HOST:
        return _NAVIGATION_UNVERIFIED

    content_type_value = headers.get("content-type")
    if type(content_type_value) is not str:
        return _NAVIGATION_UNVERIFIED
    content_type = content_type_value.split(";", 1)[0].strip().lower()
    if content_type not in {"text/html", "application/json"}:
        return _NAVIGATION_UNVERIFIED

    segments = tuple(segment.lower() for segment in parsed.path.split("/") if segment)
    if any(
        segment in {"verification", "verify", "captcha", "slider"}
        for segment in segments
    ):
        return _VERIFICATION_REQUIRED
    if status == 401 or any(
        segment in {"login", "signin"} for segment in segments
    ):
        return _LOGIN_REQUIRED
    if parsed.path in {
        "/creator/home",
        "/statistics/data-analysis",
        "/statistics/note-detail",
    } and 200 <= status < 400:
        return None
    return _NAVIGATION_UNVERIFIED


def _first_note_id(payload: object) -> str | None:
    """Return one transient note id from the platform's own list response."""
    if type(payload) is not dict:
        return None
    data = payload.get("data")
    if type(data) is not dict:
        return None
    for collection_key in ("note_infos", "items"):
        collection = data.get(collection_key)
        if type(collection) is not list:
            continue
        for item in collection[:_LIST_ITEM_SAMPLE_LIMIT]:
            if type(item) is not dict:
                continue
            for identity_key in ("id", "note_id"):
                candidate = item.get(identity_key)
                if type(candidate) is str and _NOTE_ID.fullmatch(candidate):
                    return candidate
    return None


async def _response_note_id(
    response: object, *, deadline: float, monotonic
) -> str | None:
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
    return _first_note_id(payload)


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
                safe_key = _normalize_structural_key(key)
                if safe_key is None:
                    continue
                child_path = f"{path}.{safe_key}" if path else safe_key
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
    metadata: tuple[str, str, int, str, int],
    payload: object,
    *,
    deadline: float,
    monotonic,
) -> dict[str, object]:
    path, method, status, content_type, _content_length = metadata
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
    response: object,
    *,
    metadata: tuple[str, str, int, str, int] | None = None,
    deadline: float,
    monotonic,
) -> dict[str, object] | None:
    if metadata is None:
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


def _reviewed_contract_phase(path: object) -> str | None:
    if type(path) is not str or _REDACTED_ENDPOINT_SEGMENT in path.split("/"):
        return None
    phase = _REVIEWED_CONTRACT_PATH_TEMPLATES.get(path)
    return phase if phase in _REQUIRED_CONTRACT_PHASES else None


def _structural_evidence_paths(
    shape: dict[str, object],
) -> tuple[list[tuple[str, tuple[tuple[str, bool], ...]]], dict[str, str]]:
    paths = shape.get("keyPaths")
    raw_field_types = shape.get("fieldTypes")
    if type(paths) is not list or type(raw_field_types) is not dict:
        return [], {}

    field_types = {
        key: item
        for key, item in raw_field_types.items()
        if type(key) is str and item in _ALLOWED_FIELD_TYPES
    }
    evidence: list[tuple[str, tuple[tuple[str, bool], ...]]] = []
    for path in paths:
        if type(path) is not str or _sanitize_structural_path(path) != path:
            continue
        segments: list[tuple[str, bool]] = []
        for segment in path.split("."):
            is_list = segment.endswith("[]")
            key = segment[:-2] if is_list else segment
            if key.startswith(":") or key not in _SAFE_STATIC_STRUCTURAL_KEYS:
                segments = []
                break
            segments.append((key, is_list))
        if segments:
            evidence.append((path, tuple(segments)))
    return evidence, field_types


def _classify_account_shape(
    evidence: list[tuple[str, tuple[tuple[str, bool], ...]]],
    field_types: dict[str, str],
) -> bool:
    for path, segments in evidence:
        metric_key = segments[-1][0]
        scope_keys = {key for key, _is_list in segments[:-1]}
        if (
            metric_key in _ACCOUNT_METRIC_KEYS
            and field_types.get(path) in {"int", "float"}
            and scope_keys & _ACCOUNT_SCOPE_KEYS
        ):
            return True
    return False


def _content_identity_paths(
    evidence: list[tuple[str, tuple[tuple[str, bool], ...]]],
    field_types: dict[str, str],
) -> list[tuple[tuple[str, bool], ...]]:
    identities: list[tuple[tuple[str, bool], ...]] = []
    for path, segments in evidence:
        identity_key = segments[-1][0]
        generic_id_is_scoped = identity_key == "id" and any(
            key == "note_infos" and is_list
            for key, is_list in segments[:-1]
        )
        if (
            identity_key in _CONTENT_IDENTIFIERS
            and (identity_key != "id" or generic_id_is_scoped)
            and field_types.get(path) in {"str", "int"}
        ):
            identities.append(segments)
    return identities


def _classify_content_list_shape(
    evidence: list[tuple[str, tuple[tuple[str, bool], ...]]],
    field_types: dict[str, str],
) -> bool:
    for identity_path in _content_identity_paths(evidence, field_types):
        if any(
            is_list and key in _CONTENT_LIST_KEYS
            for key, is_list in identity_path[:-1]
        ):
            return True
    return False


def _classify_empty_paginated_content_list(
    shape: dict[str, object],
) -> bool:
    if shape.get("observationPhase") != "data_analysis":
        return False
    list_lengths = shape.get("listLengths")
    pagination_keys = shape.get("paginationKeys")
    field_types = shape.get("fieldTypes")
    if (
        type(list_lengths) is not dict
        or type(pagination_keys) is not list
        or type(field_types) is not dict
        or field_types.get("data.total") != "int"
        or "data.total" not in pagination_keys
    ):
        return False
    direct_lists = [
        path
        for path, length in list_lengths.items()
        if type(path) is str
        and path.startswith("data.")
        and path.count(".") == 1
        and path.removeprefix("data.") in _CONTENT_LIST_KEYS
        and type(length) is int
        and length == 0
    ]
    return len(direct_lists) == 1


def _classify_content_lifetime_shape(
    evidence: list[tuple[str, tuple[tuple[str, bool], ...]]],
    field_types: dict[str, str],
) -> bool:
    identities = _content_identity_paths(evidence, field_types)
    numeric_metrics = [
        (path, segments)
        for path, segments in evidence
        if segments[-1][0] in _METRIC_KEYS
        and field_types.get(path) in {"int", "float"}
    ]
    for identity_path in identities:
        entity_ancestor = identity_path[:-1]
        if not entity_ancestor:
            continue
        for _metric_path, metric_segments in numeric_metrics:
            if metric_segments[:len(entity_ancestor)] != entity_ancestor:
                continue
            scope_keys = {
                key
                for key, _is_list in metric_segments[
                    len(entity_ancestor):-1
                ]
            }
            if (
                scope_keys & _CUMULATIVE_SCOPE_KEYS
                and not scope_keys & _NON_LIFETIME_SCOPE_KEYS
            ):
                return True
    return False


def _classify_reviewed_content_lifetime_shape(
    field_types: dict[str, str],
) -> bool:
    """Accept the reviewed official base endpoint's sibling identity/metrics."""
    return any(
        field_types.get(f"data.{metric_key}") in {"int", "float"}
        for metric_key in _METRIC_KEYS
    )


def _classify_reviewed_account_shape(field_types: dict[str, str]) -> bool:
    return any(
        field_types.get(f"data.{metric_key}") in {"int", "float"}
        for metric_key in _ACCOUNT_METRIC_KEYS
    )


def _classify_shape(shape: dict[str, object]) -> str:
    reviewed_phase = _reviewed_contract_phase(shape.get("path"))
    observation_phase = shape.get("observationPhase")
    if reviewed_phase is not None:
        candidates = (reviewed_phase,)
    elif type(observation_phase) is str:
        candidates = _OBSERVATION_CONTRACT_CANDIDATES.get(
            observation_phase, ()
        )
    else:
        candidates = ()
    if not candidates:
        return "unclassified"
    evidence, field_types = _structural_evidence_paths(shape)
    if not evidence:
        return "unclassified"
    if (
        reviewed_phase == "account_overview"
        and _classify_reviewed_account_shape(field_types)
    ):
        return "account_overview"
    if (
        reviewed_phase == "content_lifetime"
        and _classify_reviewed_content_lifetime_shape(field_types)
    ):
        return "content_lifetime"
    classifiers = {
        "account_overview": _classify_account_shape,
        "content_list": _classify_content_list_shape,
        "content_lifetime": _classify_content_lifetime_shape,
    }
    matches = [
        phase
        for phase in candidates
        if classifiers[phase](evidence, field_types)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches and _classify_empty_paginated_content_list(shape):
        return "content_list"
    return "unclassified"


def _contract_outcome(
    shapes: list[dict[str, object]],
) -> tuple[list[str], list[str], str, str]:
    observed: list[str] = []
    for shape in shapes:
        if (
            type(shape.get("path")) is not str
            or not shape["path"]
            or shape.get("method") not in {"GET", "POST"}
            or type(shape.get("status")) is not int
            or not 200 <= shape["status"] < 300
            or shape.get("contentType") != "application/json"
            or type(shape.get("keyPaths")) is not list
            or not shape["keyPaths"]
        ):
            continue
        phase = _classify_shape(shape)
        if phase in _REQUIRED_CONTRACT_PHASES and phase not in observed:
            observed.append(phase)

    observed = [phase for phase in _REQUIRED_CONTRACT_PHASES if phase in observed]
    missing = [phase for phase in _REQUIRED_CONTRACT_PHASES if phase not in observed]
    if not observed:
        return observed, missing, "failed", _CONTRACTS_UNOBSERVED
    if missing:
        return observed, missing, "partial_success", _CONTRACTS_INCOMPLETE
    return observed, [], "success", ""


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
    observed_responses: list[
        tuple[object, tuple[str, str, int, str, int], str]
    ] = []
    retained_body_bytes = 0
    response_budget_exhausted = False
    retained_response_ids: set[int] = set()
    bounded_payloads: dict[int, object] = {}
    request_phases: dict[int, tuple[object, str]] = {}
    active_observation_phase = ""
    shapes: list[dict[str, object]] = []
    navigation: list[dict[str, str]] = []
    caught: BaseException | None = None
    cleanup_errors: list[BaseException] = []

    def retain_request(request: object) -> None:
        if (
            active_observation_phase not in _ALLOWED_OBSERVATION_PHASES
            or len(request_phases) >= _REQUEST_PHASE_LIMIT
        ):
            return
        request_phases[id(request)] = (request, active_observation_phase)

    def retain_response(response: object) -> None:
        nonlocal retained_body_bytes, response_budget_exhausted
        try:
            request = getattr(response, "request", None)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return
        request_entry = request_phases.pop(id(request), None)
        if request_entry is None or request_entry[0] is not request:
            return
        observation_phase = request_entry[1]
        response_identity = id(response)
        if (
            response_budget_exhausted
            or len(observed_responses) >= _RESPONSE_LIMIT
            or response_identity in retained_response_ids
        ):
            return
        try:
            metadata = _response_metadata(
                response, allow_known_missing_length=True
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return
        if metadata is None:
            return
        next_total = retained_body_bytes + metadata[4]
        if next_total > _MAX_TOTAL_RESPONSE_BODY_BYTES:
            observed_responses.clear()
            retained_body_bytes = 0
            response_budget_exhausted = True
            return
        retained_body_bytes = next_total
        retained_response_ids.add(response_identity)
        observed_responses.append((response, metadata, observation_phase))

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
        page.on("request", retain_request)
        async def navigate(phase: str, url: str) -> None:
            nonlocal active_observation_phase
            active_observation_phase = phase
            try:
                navigation_response = await _await_with_deadline(
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=_NETWORK_QUIET_TIMEOUT_MS,
                    ),
                    deadline=work_deadline,
                    monotonic=monotonic,
                )
                navigation_error = _navigation_error_code(navigation_response)
                if navigation_error is not None:
                    raise ProbeFailure(navigation_error)
                navigation.append({"phase": phase, "path": _review_path(url)})
                passive_wait = getattr(page, "wait_for_timeout", None)
                if callable(passive_wait):
                    await _await_with_deadline(
                        passive_wait(_PASSIVE_CAPTURE_WAIT_MS),
                        deadline=work_deadline,
                        monotonic=monotonic,
                    )
            finally:
                active_observation_phase = ""

        async def wait_for_data_analysis_note_id(
            start: int, *, poll_attempts: int
        ) -> str | None:
            cursor = start
            for attempt in range(poll_attempts + 1):
                pending = tuple(observed_responses[cursor:])
                cursor += len(pending)
                for response, metadata, observation_phase in pending:
                    if observation_phase != "data_analysis":
                        continue
                    if metadata[4] == 0:
                        payload = await load_bounded_chunked_payload(
                            response, metadata=metadata
                        )
                        note_id = _first_note_id(payload)
                    else:
                        note_id = await _response_note_id(
                            response,
                            deadline=work_deadline,
                            monotonic=monotonic,
                        )
                    if note_id is not None:
                        return note_id
                if attempt == poll_attempts:
                    break
                passive_wait = getattr(page, "wait_for_timeout", None)
                if not callable(passive_wait):
                    break
                await _await_with_deadline(
                    passive_wait(_CONTENT_LIST_CAPTURE_POLL_MS),
                    deadline=work_deadline,
                    monotonic=monotonic,
                )
            return None

        async def load_bounded_chunked_payload(
            response: object,
            *,
            metadata: tuple[str, str, int, str, int],
        ) -> object:
            nonlocal retained_body_bytes, response_budget_exhausted
            response_identity = id(response)
            if response_identity in bounded_payloads:
                return bounded_payloads[response_identity]
            if metadata[4] != 0 or response_budget_exhausted:
                return None
            loader = getattr(response, "body", None)
            if not callable(loader):
                return None
            try:
                body = loader()
                if inspect.isawaitable(body):
                    body = await _await_with_deadline(
                        body, deadline=work_deadline, monotonic=monotonic
                    )
            except TimeoutError:
                raise
            except Exception:
                return None
            if type(body) is not bytes or not 1 <= len(body) <= _MAX_RESPONSE_BODY_BYTES:
                return None
            next_total = retained_body_bytes + len(body)
            if next_total > _MAX_TOTAL_RESPONSE_BODY_BYTES:
                response_budget_exhausted = True
                bounded_payloads.clear()
                return None
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            retained_body_bytes = next_total
            bounded_payloads[response_identity] = payload
            return payload

        async def cache_chunked_responses(start: int) -> None:
            for response, metadata, _observation_phase in tuple(
                observed_responses[start:]
            ):
                if metadata[4] == 0:
                    await load_bounded_chunked_payload(
                        response, metadata=metadata
                    )

        async def activate_note_data_tab() -> bool:
            nonlocal active_observation_phase
            getter = getattr(page, "get_by_text", None)
            if not callable(getter):
                return False
            active_observation_phase = "data_analysis"
            try:
                locator = getter("笔记数据", exact=True)
                count_method = getattr(locator, "count", None)
                visible_method = getattr(locator, "is_visible", None)
                click_method = getattr(locator, "click", None)
                if not all(callable(method) for method in (
                    count_method, visible_method, click_method
                )):
                    return False
                count = await _await_with_deadline(
                    count_method(), deadline=work_deadline, monotonic=monotonic
                )
                visible = await _await_with_deadline(
                    visible_method(), deadline=work_deadline, monotonic=monotonic
                )
                if type(count) is not int or count != 1 or visible is not True:
                    return False
                await _await_with_deadline(
                    click_method(), deadline=work_deadline, monotonic=monotonic
                )
                return True
            except (KeyboardInterrupt, SystemExit, TimeoutError):
                raise
            except Exception:
                return False
            finally:
                active_observation_phase = ""

        account_response_start = len(observed_responses)
        await navigate("account_home", _CREATOR_HOME)
        await cache_chunked_responses(account_response_start)
        data_response_start = len(observed_responses)
        await navigate("data_analysis", _DATA_ANALYSIS_URL)
        note_id = await wait_for_data_analysis_note_id(
            data_response_start, poll_attempts=0
        )
        if note_id is None:
            post_activation_start = len(observed_responses)
            await activate_note_data_tab()
            note_id = await wait_for_data_analysis_note_id(
                post_activation_start,
                poll_attempts=_CONTENT_LIST_CAPTURE_POLL_ATTEMPTS,
            )
        if note_id is not None:
            await navigate(
                "content_lifetime", f"{_NOTE_DETAIL_URL}?noteId={note_id}"
            )
        for response, metadata, observation_phase in tuple(observed_responses):
            if metadata[4] == 0:
                payload = await load_bounded_chunked_payload(
                    response, metadata=metadata
                )
                shape = (
                    _shape_from_payload(
                        metadata,
                        payload,
                        deadline=work_deadline,
                        monotonic=monotonic,
                    )
                    if payload is not None
                    else None
                )
            else:
                shape = await _async_response_shape(
                    response,
                    metadata=metadata,
                    deadline=work_deadline,
                    monotonic=monotonic,
                )
            if shape is not None:
                shape["observationPhase"] = observation_phase
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
    phases, missing_phases, contract_status, contract_error = _contract_outcome(
        shapes
    )
    report = build_plan()
    report.update({
        "mode": "execute",
        "phases": phases,
        "missingPhases": missing_phases,
        "status": contract_status,
        "errorCode": contract_error,
        "observedAt": _observed_at(utc_now),
        "responses": shapes,
        "navigation": navigation,
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
    sanitized_report = sanitize_probe_report(report)
    sanitized_report["navigation"] = navigation
    return sanitized_report, caught


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
        sanitized_report = sanitize_probe_report(report)
        sanitized_report["navigation"] = []
        return sanitized_report


def _execute(*, browser_factory, utc_now) -> dict[str, object]:
    """Apply the account gate, then run one passive official-page probe."""
    return _probe_with_browser(
        _select_single_eligible_account(),
        playwright_factory=browser_factory,
        monotonic=time.monotonic,
        utc_now=utc_now,
    )


def _failed_execution_report(
    error_code: str, source: object | None = None
) -> dict[str, object]:
    report = sanitize_probe_report(
        build_plan() if source is None else source
    )
    report.update({
        "mode": "execute",
        "status": "failed",
        "errorCode": error_code,
    })
    return sanitize_probe_report(report)


def _execution_exit_code(report: object) -> int:
    if (
        type(report) is dict
        and report.get("mode") == "execute"
        and report.get("status") == "success"
    ):
        return 0
    return 1


def main(argv: list[str] | None = None, *, stdout=sys.stdout) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        payload = build_plan()
        json.dump(payload, stdout, ensure_ascii=False)
        stdout.write("\n")
        return 0

    review_schema = False
    if arguments == ["--execute"]:
        report_path = None
    elif arguments == ["--execute", "--review-schema"]:
        report_path = None
        review_schema = True
    elif (
        len(arguments) == 3
        and arguments[:2] == ["--execute", "--report"]
    ):
        report_path = arguments[2]
    elif (
        len(arguments) == 4
        and arguments[:3] == ["--execute", "--review-schema", "--report"]
    ):
        report_path = arguments[3]
        review_schema = True
    else:
        payload = _failed_execution_report(_ARGUMENTS_INVALID)
        json.dump(payload, stdout, ensure_ascii=False)
        stdout.write("\n")
        return 2

    if report_path is not None:
        try:
            report_path = _report_destination(report_path)
        except ProbeFailure as failure:
            payload = _failed_execution_report(failure.error_code)
            json.dump(payload, stdout, ensure_ascii=False)
            stdout.write("\n")
            return 1
        except Exception:
            payload = _failed_execution_report(_REPORT_PATH_INVALID)
            json.dump(payload, stdout, ensure_ascii=False)
            stdout.write("\n")
            return 1

    def browser_factory():
        from playwright.async_api import async_playwright

        return async_playwright()

    from datetime import datetime, timezone

    try:
        payload = _execute(
            browser_factory=browser_factory,
            utc_now=lambda: datetime.now(timezone.utc),
        )
    except ProbeFailure as failure:
        payload = _failed_execution_report(failure.error_code)
    if report_path is not None:
        try:
            _write_report(report_path, sanitize_probe_report(payload))
        except ProbeFailure as failure:
            payload = _failed_execution_report(
                failure.error_code, payload
            )
            json.dump(payload, stdout, ensure_ascii=False)
            stdout.write("\n")
            return 1
    output_payload = sanitize_probe_report(payload)
    if review_schema:
        output_payload["schemaReview"] = _review_schema(payload)
    json.dump(output_payload, stdout, ensure_ascii=False)
    stdout.write("\n")
    return _execution_exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())
