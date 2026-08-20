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
_ACCOUNT_SELECTION_REQUIRED = "xiaohongshu_account_selection_required"


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


def _execute(*, browser_factory, utc_now) -> dict[str, object]:
    """Apply the explicit account-selection gate without browser activity."""
    del browser_factory, utc_now
    _select_single_eligible_account()
    return build_plan()


def main(argv: list[str] | None = None, *, stdout=sys.stdout) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--execute"]:
        def browser_factory():
            from playwright.async_api import async_playwright

            return async_playwright

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
