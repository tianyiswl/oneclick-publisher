# -*- coding: utf-8 -*-
"""Credential-free evidence gathered after one Facebook final click."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit


_SCHEMA_VERSION_V1 = "facebook-post-click-diagnostic/v1"
_SCHEMA_VERSION_V2 = "facebook-post-click-diagnostic/v2"
_SCHEMA_VERSION = _SCHEMA_VERSION_V2
_META_HOSTS = frozenset(
    {"business.facebook.com", "facebook.com", "www.facebook.com"}
)
_FINAL_ACTION_LABELS = frozenset(
    {
        "Publish",
        "Publish now",
        "Share",
        "Share now",
        "Post",
        "发布",
        "立即发布",
        "分享",
        "立即分享",
    }
)
_SAMPLE_PHASES = frozenset({"post_click_observed", "readback_finished"})
_POST_CLICK_STATES = frozenset(
    {
        "confirmation_pending",
        "composer_unchanged",
        "transitioned_unknown",
        "unknown",
    }
)
_NOTICE_ACCEPTED = frozenset(
    {
        "your reel is being published",
        "your reel was submitted for publishing",
        "你的 reel 正在发布",
        "你的 reel 已提交发布",
    }
)
_NOTICE_REJECTED = frozenset(
    {
        "your reel wasn't published",
        "we couldn't publish your reel",
        "你的 reel 未发布",
        "无法发布你的 reel",
    }
)
_NETWORK_RESOURCE_TYPES = frozenset({"document", "xhr", "fetch"})
_NETWORK_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)
_SAFE_PATH_SEGMENTS = frozenset(
    {
        "ajax",
        "api",
        "business",
        "content",
        "graphql",
        "home",
        "latest",
        "notifications",
        "posts",
        "reel",
        "reels",
        "reels_composer",
    }
)
_MAX_SAMPLES = 3
_MAX_DIALOGS_PER_SAMPLE = 8
_MAX_NOTICES_PER_SAMPLE = 8
_MAX_POPUPS = 8
_MAX_NETWORK_RESULTS = 32
_MAX_GRAPHQL_RESULTS = 16
_MAX_GRAPHQL_ERRORS = 8
_GRAPHQL_RESPONSE_TIMEOUT_SECONDS = 5.0
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _text_evidence(value: object) -> dict[str, object]:
    text = _canonical_text(value)
    return {
        "textSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "textLength": len(text),
    }


def _identity_evidence(value: object, *, max_length: int) -> dict[str, object]:
    text = _canonical_text(value)
    if not text or len(text) > max_length:
        return {"sha256": "", "length": 0}
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "length": len(text),
    }


def _graphql_request_values(request: Any) -> tuple[str, str]:
    try:
        body = getattr(request, "post_data_json", None)
    except Exception:
        return "", ""
    if not isinstance(body, Mapping):
        return "", ""
    operation_name = _canonical_text(body.get("fb_api_req_friendly_name"))
    document_id = _canonical_text(body.get("doc_id"))
    if len(operation_name) > 256:
        operation_name = ""
    if (
        len(document_id) > 64
        or not document_id.isascii()
        or (document_id and not document_id.isdigit())
    ):
        document_id = ""
    return operation_name, document_id


def _graphql_operation_class(operation_name: str) -> str:
    folded = str(operation_name or "").casefold()
    if not folded:
        return "unknown"
    if "mutation" not in folded:
        return "query"
    content_marker = any(
        marker in folded for marker in ("reel", "shortform", "video")
    )
    action_marker = any(
        marker in folded for marker in ("create", "publish", "share", "post")
    )
    if content_marker and action_marker:
        return "reel_publish_mutation"
    return "other_mutation"


def _graphql_error_code(value: object) -> int | None:
    if type(value) is int and 0 <= value <= 2_147_483_647:
        return value
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and len(value) <= 10
    ):
        parsed = int(value)
        return parsed if parsed <= 2_147_483_647 else None
    return None


def _classify_graphql_payload(payload: object) -> dict[str, object] | None:
    if isinstance(payload, Mapping):
        entries: list[Mapping[str, object]] = [payload]
    elif (
        type(payload) is list
        and payload
        and len(payload) <= 64
        and all(isinstance(item, Mapping) for item in payload)
    ):
        entries = list(payload)
    else:
        return None

    data_present = any(
        "data" in entry and entry.get("data") is not None for entry in entries
    )
    errors: list[Mapping[str, object]] = []
    for entry in entries:
        raw_errors = entry.get("errors")
        if raw_errors is None:
            continue
        if (
            type(raw_errors) is not list
            or len(raw_errors) > 100_000
            or not all(isinstance(item, Mapping) for item in raw_errors)
        ):
            return None
        errors.extend(raw_errors)
    error_count = len(errors)
    outcome = (
        "mixed"
        if data_present and error_count
        else "data"
        if data_present
        else "errors"
        if error_count
        else "empty"
    )
    error_codes: list[int] = []
    error_messages: list[dict[str, object]] = []
    transient_error_count = 0
    for error in errors[:_MAX_GRAPHQL_ERRORS]:
        code = _graphql_error_code(error.get("code"))
        extensions = error.get("extensions")
        if code is None and isinstance(extensions, Mapping):
            code = _graphql_error_code(extensions.get("code"))
        if code is not None and code not in error_codes:
            error_codes.append(code)
        message = _canonical_text(error.get("message"))
        if message:
            error_messages.append(_text_evidence(message))
        if error.get("is_transient") is True:
            transient_error_count += 1
    return {
        "applicationOutcome": outcome,
        "dataPresent": data_present,
        "errorCount": error_count,
        "errorCodes": error_codes,
        "transientErrorCount": transient_error_count,
        "errorMessages": error_messages,
        "captureStatus": "ok",
    }


async def _safe_graphql_result(response: Any) -> dict[str, object]:
    request = getattr(response, "request", None)
    operation_name, document_id = _graphql_request_values(request)
    operation_class = _graphql_operation_class(operation_name)
    operation_evidence = _identity_evidence(operation_name, max_length=256)
    document_evidence = _identity_evidence(document_id, max_length=64)
    result: dict[str, object] = {
        "operationClass": operation_class,
        "operationNameSha256": operation_evidence["sha256"],
        "operationNameLength": operation_evidence["length"],
        "documentIdSha256": document_evidence["sha256"],
        "documentIdLength": document_evidence["length"],
        "applicationOutcome": "not_inspected",
        "dataPresent": False,
        "errorCount": 0,
        "errorCodes": [],
        "transientErrorCount": 0,
        "errorMessages": [],
        "captureStatus": "ok" if operation_name else "partial",
    }
    if operation_class not in {"reel_publish_mutation", "other_mutation"}:
        return result
    response_json = getattr(response, "json", None)
    if not callable(response_json):
        result["applicationOutcome"] = "unreadable"
        result["captureStatus"] = "partial"
        return result
    try:
        payload = await asyncio.wait_for(
            response_json(),
            timeout=_GRAPHQL_RESPONSE_TIMEOUT_SECONDS,
        )
    except Exception:
        result["applicationOutcome"] = "unreadable"
        result["captureStatus"] = "partial"
        return result
    classified = _classify_graphql_payload(payload)
    if classified is None:
        result["applicationOutcome"] = "unreadable"
        result["captureStatus"] = "partial"
        return result
    result.update(classified)
    return result


def _page_url_value(page: Any) -> str:
    value = getattr(page, "url", "")
    if callable(value):
        value = value()
    return str(value or "")


def _sanitized_path(value: str) -> str:
    parts: list[str] = []
    for raw_part in value.split("/"):
        if not raw_part:
            parts.append("")
            continue
        decoded = unquote(raw_part).casefold()
        parts.append(
            decoded
            if decoded in _SAFE_PATH_SEGMENTS or decoded == ":redacted"
            else ":redacted"
        )
    result = "/".join(parts)
    return result if result.startswith("/") else f"/{result}"


def _safe_meta_page_url(value: object, *, expected_page_id: str) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        hostname = str(parsed.hostname or "").casefold()
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError:
        return ""
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in _META_HOSTS
        or port is not None
        or username is not None
        or password is not None
    ):
        return ""
    safe_query = ""
    if query.get("asset_id") == [expected_page_id]:
        safe_query = urlencode({"asset_id": expected_page_id})
    path = _sanitized_path(parsed.path or "/")
    return urlunsplit(("https", hostname, path, safe_query, ""))


def _safe_network_endpoint(value: object) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(str(value or ""))
        hostname = str(parsed.hostname or "").casefold()
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in _META_HOSTS
        or port is not None
        or username is not None
        or password is not None
    ):
        return None
    return (
        hostname,
        _sanitized_path(parsed.path or "/"),
    )


async def _visible(locator: Any, *, limit: int) -> list[Any]:
    elements: list[Any] = []
    count = min(int(await locator.count()), int(limit))
    for index in range(count):
        element = locator.nth(index)
        if await element.is_visible():
            elements.append(element)
    return elements


class FacebookPostClickDiagnosticRecorder:
    """Observe one clicked composer without bodies, headers, cookies or tokens."""

    def __init__(
        self,
        page: Any,
        *,
        expected_page_id: str,
        final_button_label: str,
    ) -> None:
        page_id = str(expected_page_id or "").strip()
        label = _canonical_text(final_button_label)
        if not page_id.isascii() or not page_id.isdigit():
            raise ValueError("Facebook diagnostic page ID is invalid")
        if label not in _FINAL_ACTION_LABELS:
            raise ValueError("Facebook diagnostic final button is invalid")
        if not callable(getattr(page, "on", None)):
            raise TypeError("Facebook diagnostic page does not expose events")
        self.page = page
        self.expected_page_id = page_id
        self.final_button_label = label
        self._samples: list[dict[str, object]] = []
        self._popup_pages: list[Any] = []
        self._network_results: list[dict[str, object]] = []
        self._network_result_count = 0
        self._graphql_results: dict[int, dict[str, object]] = {}
        self._graphql_result_count = 0
        self._pending_response_tasks: set[asyncio.Task[None]] = set()
        self._final_action_started = False
        self._started = False
        self._sealed = False
        self._stopped = False

    def start(self) -> None:
        if self._started:
            return
        self.page.on("response", self._on_response)
        self.page.on("popup", self._on_popup)
        self._started = True

    def mark_final_action_started(self) -> None:
        if not self._started or self._stopped:
            raise RuntimeError("Facebook diagnostic listener is not active")
        self._final_action_started = True

    def _on_popup(self, popup: Any) -> None:
        if self._stopped or self._sealed or len(self._popup_pages) >= _MAX_POPUPS:
            return
        self._popup_pages.append(popup)

    def _on_response(self, response: Any) -> None:
        if self._stopped or self._sealed:
            return
        request = getattr(response, "request", None)
        resource_type = str(getattr(request, "resource_type", "") or "").casefold()
        if resource_type not in _NETWORK_RESOURCE_TYPES:
            return
        endpoint = _safe_network_endpoint(getattr(response, "url", ""))
        if endpoint is None:
            return
        method = str(getattr(request, "method", "") or "").upper()
        if method not in _NETWORK_METHODS:
            method = "OTHER"
        status = getattr(response, "status", 0)
        if type(status) is not int or status < 0 or status > 599:
            status = 0
        ok = getattr(response, "ok", None)
        if type(ok) is not bool:
            ok = 200 <= status < 400
        host, path = endpoint
        self._network_result_count += 1
        if len(self._network_results) < _MAX_NETWORK_RESULTS:
            self._network_results.append(
                {
                    "method": method,
                    "host": host,
                    "path": path,
                    "resourceType": resource_type,
                    "status": status,
                    "ok": ok,
                }
            )
        if (
            not self._final_action_started
            or method != "POST"
            or resource_type not in {"xhr", "fetch"}
            or host != "business.facebook.com"
            or path != "/api/graphql/"
        ):
            return
        result_index = self._graphql_result_count
        self._graphql_result_count += 1
        if result_index >= _MAX_GRAPHQL_RESULTS:
            return

        async def capture() -> None:
            self._graphql_results[result_index] = await _safe_graphql_result(
                response
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._graphql_results[result_index] = {
                "operationClass": "unknown",
                "operationNameSha256": "",
                "operationNameLength": 0,
                "documentIdSha256": "",
                "documentIdLength": 0,
                "applicationOutcome": "unreadable",
                "dataPresent": False,
                "errorCount": 0,
                "errorCodes": [],
                "transientErrorCount": 0,
                "errorMessages": [],
                "captureStatus": "partial",
            }
            return
        task = loop.create_task(capture())
        self._pending_response_tasks.add(task)
        task.add_done_callback(self._pending_response_tasks.discard)

    async def drain(self) -> None:
        self._sealed = True
        self._detach_listeners()
        while self._pending_response_tasks:
            pending = tuple(self._pending_response_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    async def sample(self, phase: str, *, post_click_state: str) -> None:
        normalized_phase = str(phase or "").strip()
        normalized_state = str(post_click_state or "").strip()
        if normalized_phase not in _SAMPLE_PHASES:
            raise ValueError("Facebook diagnostic sample phase is invalid")
        if normalized_state not in _POST_CLICK_STATES:
            raise ValueError("Facebook diagnostic post-click state is invalid")
        if len(self._samples) >= _MAX_SAMPLES:
            return

        partial = False
        page_url = _safe_meta_page_url(
            _page_url_value(self.page),
            expected_page_id=self.expected_page_id,
        )
        if not page_url:
            partial = True

        dialogs: list[dict[str, object]] = []
        for role in ("dialog", "alertdialog"):
            try:
                elements = await _visible(
                    self.page.get_by_role(role),
                    limit=_MAX_DIALOGS_PER_SAMPLE - len(dialogs),
                )
                for element in elements:
                    dialogs.append(
                        {
                            "role": role,
                            **_text_evidence(await element.inner_text()),
                        }
                    )
            except Exception:
                partial = True

        notices: list[dict[str, object]] = []
        for role in ("alert", "status"):
            try:
                elements = await _visible(
                    self.page.get_by_role(role),
                    limit=_MAX_NOTICES_PER_SAMPLE - len(notices),
                )
                for element in elements:
                    text = _canonical_text(await element.inner_text())
                    folded = text.casefold()
                    classification = (
                        "accepted"
                        if folded in _NOTICE_ACCEPTED
                        else "rejected"
                        if folded in _NOTICE_REJECTED
                        else "unknown"
                    )
                    notices.append(
                        {
                            "role": role,
                            "classification": classification,
                            **_text_evidence(text),
                        }
                    )
            except Exception:
                partial = True

        final_button: dict[str, object] = {
            "label": self.final_button_label,
            "visibleCount": 0,
            "enabled": None,
        }
        try:
            buttons = await _visible(
                self.page.get_by_role(
                    "button",
                    name=self.final_button_label,
                    exact=True,
                ),
                limit=2,
            )
            final_button["visibleCount"] = len(buttons)
            if len(buttons) == 1:
                final_button["enabled"] = bool(await buttons[0].is_enabled())
        except Exception:
            partial = True

        self._samples.append(
            {
                "phase": normalized_phase,
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "captureStatus": "partial" if partial else "ok",
                "pageUrl": page_url,
                "postClickState": normalized_state,
                "dialogs": dialogs,
                "notices": notices,
                "finalButton": final_button,
            }
        )

    def finish(self) -> dict[str, object]:
        if self._pending_response_tasks:
            raise RuntimeError("Facebook diagnostic responses must be drained")
        popup_urls: list[str] = []
        for popup in self._popup_pages:
            safe_url = _safe_meta_page_url(
                _page_url_value(popup),
                expected_page_id=self.expected_page_id,
            )
            if safe_url and safe_url not in popup_urls:
                popup_urls.append(safe_url)
        return {
            "schemaVersion": _SCHEMA_VERSION,
            "pageId": self.expected_page_id,
            "samples": [dict(sample) for sample in self._samples],
            "popupUrls": popup_urls,
            "networkResults": [dict(item) for item in self._network_results],
            "networkResultCount": self._network_result_count,
            "networkDroppedCount": max(
                0,
                self._network_result_count - len(self._network_results),
            ),
            "graphqlResults": [
                dict(self._graphql_results[index])
                for index in sorted(self._graphql_results)
            ],
            "graphqlResultCount": self._graphql_result_count,
            "graphqlDroppedCount": max(
                0,
                self._graphql_result_count - len(self._graphql_results),
            ),
        }

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._sealed = True
        for task in tuple(self._pending_response_tasks):
            task.cancel()
        self._pending_response_tasks.clear()
        self._detach_listeners()

    def _detach_listeners(self) -> None:
        if self._started:
            remove_listener = getattr(self.page, "remove_listener", None)
            if callable(remove_listener):
                for event, callback in (
                    ("response", self._on_response),
                    ("popup", self._on_popup),
                ):
                    try:
                        remove_listener(event, callback)
                    except Exception:
                        pass


def _exact_mapping(value: object, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("Facebook post-click diagnostic shape is invalid")
    return value


def _safe_text_record(
    value: object,
    *,
    allowed_roles: frozenset[str],
    include_classification: bool,
) -> dict[str, object]:
    keys = {"role", "textSha256", "textLength"}
    if include_classification:
        keys.add("classification")
    item = _exact_mapping(value, frozenset(keys))
    role = str(item.get("role") or "")
    text_hash = str(item.get("textSha256") or "")
    text_length = item.get("textLength")
    if (
        role not in allowed_roles
        or _SHA256.fullmatch(text_hash) is None
        or type(text_length) is not int
        or text_length < 0
        or text_length > 1_000_000
    ):
        raise ValueError("Facebook post-click text evidence is invalid")
    result: dict[str, object] = {
        "role": role,
        "textSha256": text_hash,
        "textLength": text_length,
    }
    if include_classification:
        classification = str(item.get("classification") or "")
        if classification not in {"accepted", "rejected", "unknown"}:
            raise ValueError("Facebook post-click notice classification is invalid")
        result["classification"] = classification
    return result


def project_facebook_post_click_diagnostic(
    value: object,
    *,
    expected_page_id: str | None = None,
) -> dict[str, object]:
    """Validate the exact credential-free shape used by storage and APIs."""

    if not isinstance(value, Mapping):
        raise ValueError("Facebook post-click diagnostic shape is invalid")
    schema_version = str(value.get("schemaVersion") or "")
    keys = {
        "schemaVersion",
        "pageId",
        "samples",
        "popupUrls",
        "networkResults",
        "networkResultCount",
        "networkDroppedCount",
    }
    if schema_version == _SCHEMA_VERSION_V2:
        keys.update(
            {
                "graphqlResults",
                "graphqlResultCount",
                "graphqlDroppedCount",
            }
        )
    diagnostic = _exact_mapping(value, frozenset(keys))
    page_id = str(diagnostic.get("pageId") or "")
    if (
        schema_version not in {_SCHEMA_VERSION_V1, _SCHEMA_VERSION_V2}
        or not page_id.isascii()
        or not page_id.isdigit()
        or (expected_page_id is not None and page_id != str(expected_page_id))
    ):
        raise ValueError("Facebook post-click diagnostic identity is invalid")

    raw_samples = diagnostic.get("samples")
    if (
        type(raw_samples) is not list
        or not raw_samples
        or len(raw_samples) > _MAX_SAMPLES
    ):
        raise ValueError("Facebook post-click diagnostic samples are invalid")
    samples: list[dict[str, object]] = []
    sample_phases: set[str] = set()
    for raw_sample in raw_samples:
        sample = _exact_mapping(
            raw_sample,
            frozenset(
                {
                    "phase",
                    "capturedAt",
                    "captureStatus",
                    "pageUrl",
                    "postClickState",
                    "dialogs",
                    "notices",
                    "finalButton",
                }
            ),
        )
        phase = str(sample.get("phase") or "")
        captured_at = str(sample.get("capturedAt") or "")
        capture_status = str(sample.get("captureStatus") or "")
        page_url = str(sample.get("pageUrl") or "")
        post_click_state = str(sample.get("postClickState") or "")
        try:
            captured = datetime.fromisoformat(captured_at)
        except ValueError as exc:
            raise ValueError("Facebook diagnostic timestamp is invalid") from exc
        if (
            phase not in _SAMPLE_PHASES
            or phase in sample_phases
            or captured.tzinfo is None
            or captured.utcoffset() is None
            or capture_status not in {"ok", "partial"}
            or post_click_state not in _POST_CLICK_STATES
            or (
                page_url
                and _safe_meta_page_url(page_url, expected_page_id=page_id)
                != page_url
            )
            or (not page_url and capture_status != "partial")
        ):
            raise ValueError("Facebook diagnostic sample is invalid")
        sample_phases.add(phase)

        raw_dialogs = sample.get("dialogs")
        raw_notices = sample.get("notices")
        if (
            type(raw_dialogs) is not list
            or len(raw_dialogs) > _MAX_DIALOGS_PER_SAMPLE
            or type(raw_notices) is not list
            or len(raw_notices) > _MAX_NOTICES_PER_SAMPLE
        ):
            raise ValueError("Facebook diagnostic DOM evidence is invalid")
        dialogs = [
            _safe_text_record(
                item,
                allowed_roles=frozenset({"dialog", "alertdialog"}),
                include_classification=False,
            )
            for item in raw_dialogs
        ]
        notices = [
            _safe_text_record(
                item,
                allowed_roles=frozenset({"alert", "status"}),
                include_classification=True,
            )
            for item in raw_notices
        ]
        raw_button = _exact_mapping(
            sample.get("finalButton"),
            frozenset({"label", "visibleCount", "enabled"}),
        )
        label = str(raw_button.get("label") or "")
        visible_count = raw_button.get("visibleCount")
        enabled = raw_button.get("enabled")
        if (
            label not in _FINAL_ACTION_LABELS
            or type(visible_count) is not int
            or visible_count < 0
            or visible_count > 2
            or (enabled is not None and type(enabled) is not bool)
            or (visible_count != 1 and enabled is not None)
        ):
            raise ValueError("Facebook diagnostic final button is invalid")
        samples.append(
            {
                "phase": phase,
                "capturedAt": captured.astimezone(timezone.utc).isoformat(),
                "captureStatus": capture_status,
                "pageUrl": page_url,
                "postClickState": post_click_state,
                "dialogs": dialogs,
                "notices": notices,
                "finalButton": {
                    "label": label,
                    "visibleCount": visible_count,
                    "enabled": enabled,
                },
            }
        )

    raw_popup_urls = diagnostic.get("popupUrls")
    if type(raw_popup_urls) is not list or len(raw_popup_urls) > _MAX_POPUPS:
        raise ValueError("Facebook diagnostic popup URLs are invalid")
    popup_urls: list[str] = []
    for raw_url in raw_popup_urls:
        url = str(raw_url or "")
        if (
            not url
            or _safe_meta_page_url(url, expected_page_id=page_id) != url
            or url in popup_urls
        ):
            raise ValueError("Facebook diagnostic popup URL is invalid")
        popup_urls.append(url)

    raw_network = diagnostic.get("networkResults")
    if type(raw_network) is not list or len(raw_network) > _MAX_NETWORK_RESULTS:
        raise ValueError("Facebook diagnostic network evidence is invalid")
    network_results: list[dict[str, object]] = []
    for raw_result in raw_network:
        result = _exact_mapping(
            raw_result,
            frozenset(
                {"method", "host", "path", "resourceType", "status", "ok"}
            ),
        )
        method = str(result.get("method") or "")
        host = str(result.get("host") or "").casefold()
        path = str(result.get("path") or "")
        resource_type = str(result.get("resourceType") or "")
        status = result.get("status")
        ok = result.get("ok")
        if (
            method not in _NETWORK_METHODS | {"OTHER"}
            or host not in _META_HOSTS
            or not path.startswith("/")
            or "?" in path
            or "#" in path
            or _sanitized_path(path) != path
            or resource_type not in _NETWORK_RESOURCE_TYPES
            or type(status) is not int
            or status < 0
            or status > 599
            or type(ok) is not bool
        ):
            raise ValueError("Facebook diagnostic network result is invalid")
        network_results.append(
            {
                "method": method,
                "host": host,
                "path": path,
                "resourceType": resource_type,
                "status": status,
                "ok": ok,
            }
        )

    total_count = diagnostic.get("networkResultCount")
    dropped_count = diagnostic.get("networkDroppedCount")
    if (
        type(total_count) is not int
        or total_count < len(network_results)
        or total_count > 100_000
        or type(dropped_count) is not int
        or dropped_count != total_count - len(network_results)
    ):
        raise ValueError("Facebook diagnostic network counts are invalid")
    projected = {
        "schemaVersion": schema_version,
        "pageId": page_id,
        "samples": samples,
        "popupUrls": popup_urls,
        "networkResults": network_results,
        "networkResultCount": total_count,
        "networkDroppedCount": dropped_count,
    }
    if schema_version == _SCHEMA_VERSION_V1:
        return projected

    raw_graphql = diagnostic.get("graphqlResults")
    if type(raw_graphql) is not list or len(raw_graphql) > _MAX_GRAPHQL_RESULTS:
        raise ValueError("Facebook diagnostic GraphQL evidence is invalid")
    graphql_results: list[dict[str, object]] = []
    for raw_result in raw_graphql:
        result = _exact_mapping(
            raw_result,
            frozenset(
                {
                    "operationClass",
                    "operationNameSha256",
                    "operationNameLength",
                    "documentIdSha256",
                    "documentIdLength",
                    "applicationOutcome",
                    "dataPresent",
                    "errorCount",
                    "errorCodes",
                    "transientErrorCount",
                    "errorMessages",
                    "captureStatus",
                }
            ),
        )
        operation_class = str(result.get("operationClass") or "")
        operation_hash = str(result.get("operationNameSha256") or "")
        operation_length = result.get("operationNameLength")
        document_hash = str(result.get("documentIdSha256") or "")
        document_length = result.get("documentIdLength")
        application_outcome = str(result.get("applicationOutcome") or "")
        data_present = result.get("dataPresent")
        error_count = result.get("errorCount")
        transient_count = result.get("transientErrorCount")
        capture_status = str(result.get("captureStatus") or "")
        if (
            operation_class
            not in {
                "reel_publish_mutation",
                "other_mutation",
                "query",
                "unknown",
            }
            or type(operation_length) is not int
            or operation_length < 0
            or operation_length > 256
            or (
                (operation_length == 0 and operation_hash)
                or (
                    operation_length > 0
                    and _SHA256.fullmatch(operation_hash) is None
                )
            )
            or type(document_length) is not int
            or document_length < 0
            or document_length > 64
            or (
                (document_length == 0 and document_hash)
                or (
                    document_length > 0
                    and _SHA256.fullmatch(document_hash) is None
                )
            )
            or application_outcome
            not in {"data", "errors", "mixed", "empty", "unreadable", "not_inspected"}
            or type(data_present) is not bool
            or type(error_count) is not int
            or error_count < 0
            or error_count > 100_000
            or type(transient_count) is not int
            or transient_count < 0
            or transient_count > error_count
            or capture_status not in {"ok", "partial"}
        ):
            raise ValueError("Facebook diagnostic GraphQL result is invalid")
        raw_codes = result.get("errorCodes")
        raw_messages = result.get("errorMessages")
        if (
            type(raw_codes) is not list
            or len(raw_codes) > _MAX_GRAPHQL_ERRORS
            or any(_graphql_error_code(code) != code for code in raw_codes)
            or len(set(raw_codes)) != len(raw_codes)
            or type(raw_messages) is not list
            or len(raw_messages) > _MAX_GRAPHQL_ERRORS
        ):
            raise ValueError("Facebook diagnostic GraphQL errors are invalid")
        messages = []
        for raw_message in raw_messages:
            message = _exact_mapping(
                raw_message,
                frozenset({"textSha256", "textLength"}),
            )
            text_hash = str(message.get("textSha256") or "")
            text_length = message.get("textLength")
            if (
                _SHA256.fullmatch(text_hash) is None
                or type(text_length) is not int
                or text_length <= 0
                or text_length > 1_000_000
            ):
                raise ValueError("Facebook diagnostic GraphQL message is invalid")
            messages.append(
                {"textSha256": text_hash, "textLength": text_length}
            )
        if (
            (operation_class == "unknown" and operation_length != 0)
            or (operation_class != "unknown" and operation_length == 0)
            or (
                operation_class in {"reel_publish_mutation", "other_mutation"}
                and application_outcome == "not_inspected"
            )
            or (
                operation_class not in {"reel_publish_mutation", "other_mutation"}
                and application_outcome != "not_inspected"
            )
            or (
                application_outcome == "data"
                and (not data_present or error_count)
            )
            or (
                application_outcome == "errors"
                and (data_present or error_count == 0)
            )
            or (
                application_outcome == "mixed"
                and (not data_present or error_count == 0)
            )
            or (
                application_outcome in {"empty", "unreadable", "not_inspected"}
                and (data_present or error_count)
            )
            or (
                application_outcome == "unreadable"
                and capture_status != "partial"
            )
        ):
            raise ValueError("Facebook diagnostic GraphQL outcome is invalid")
        graphql_results.append(
            {
                "operationClass": operation_class,
                "operationNameSha256": operation_hash,
                "operationNameLength": operation_length,
                "documentIdSha256": document_hash,
                "documentIdLength": document_length,
                "applicationOutcome": application_outcome,
                "dataPresent": data_present,
                "errorCount": error_count,
                "errorCodes": list(raw_codes),
                "transientErrorCount": transient_count,
                "errorMessages": messages,
                "captureStatus": capture_status,
            }
        )
    graphql_total = diagnostic.get("graphqlResultCount")
    graphql_dropped = diagnostic.get("graphqlDroppedCount")
    if (
        type(graphql_total) is not int
        or graphql_total < len(graphql_results)
        or graphql_total > 100_000
        or type(graphql_dropped) is not int
        or graphql_dropped != graphql_total - len(graphql_results)
    ):
        raise ValueError("Facebook diagnostic GraphQL counts are invalid")
    projected.update(
        {
            "graphqlResults": graphql_results,
            "graphqlResultCount": graphql_total,
            "graphqlDroppedCount": graphql_dropped,
        }
    )
    return projected
