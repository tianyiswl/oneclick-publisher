"""Emit a zero-action Xiaohongshu data-contract probe plan."""

import json
import sys
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


def _safe_text_mapping(value: object) -> dict[str, str]:
    if type(value) is not dict:
        return {}
    return {
        _safe_text(key): _safe_text(item)
        for key, item in value.items()
        if type(key) is str
    }


def _safe_integer_mapping(value: object) -> dict[str, int]:
    if type(value) is not dict:
        return {}
    return {
        _safe_text(key): _safe_integer(item)
        for key, item in value.items()
        if type(key) is str
    }


def _sanitize_response(value: object) -> dict[str, object]:
    source = value if type(value) is dict else {}
    source_url = source.get("url")
    source_path = urlsplit(source_url).path if type(source_url) is str else source.get("path")
    return {
        "method": _safe_text(source.get("method")),
        "path": _safe_text(source_path),
        "status": _safe_integer(source.get("status")),
        "contentType": _safe_text(source.get("contentType")),
        "keyPaths": _safe_text_list(source.get("keyPaths", source.get("keys")), _KEY_PATH_LIMIT),
        "fieldTypes": _safe_text_mapping(source.get("fieldTypes")),
        "listLengths": _safe_integer_mapping(source.get("listLengths")),
        "paginationKeys": _safe_text_list(source.get("paginationKeys")),
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


def _execute() -> dict[str, object]:
    """Reserved for an explicit future execution mode."""
    return build_plan()


def main(argv: list[str] | None = None, *, stdout=sys.stdout) -> int:
    del argv
    json.dump(build_plan(), stdout, ensure_ascii=False)
    stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
