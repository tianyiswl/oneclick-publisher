# -*- coding: utf-8 -*-
"""Offline safety contracts for Facebook post-click evidence."""

from __future__ import annotations

import hashlib
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace

from uploader.meta_uploader.post_click_diagnostics import (
    FacebookPostClickDiagnosticRecorder,
    project_facebook_post_click_diagnostic,
)


class _Element:
    def __init__(self, text: str, *, enabled: bool = True) -> None:
        self.text = text
        self.enabled = enabled

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self) -> str:
        return self.text

    async def is_enabled(self) -> bool:
        return self.enabled


class _Locator:
    def __init__(self, elements: list[_Element]) -> None:
        self.elements = elements

    async def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> _Element:
        return self.elements[index]


class _Page:
    def __init__(self) -> None:
        self.url = (
            "https://business.facebook.com/latest/reels_composer/"
            "?asset_id=1001&access_token=SECRET_TOKEN#SECRET_FRAGMENT"
        )
        self.handlers: dict[str, list] = {}
        self.roles = {
            "dialog": [_Element("确认发布 SECRET_CAPTION")],
            "alertdialog": [_Element("安全提示 SECRET_COOKIE")],
            "alert": [_Element("你的 Reel 正在发布")],
            "status": [_Element("SECRET_STATUS_BODY")],
        }
        self.final_button = _Element("分享", enabled=False)

    def on(self, event: str, callback) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback) -> None:
        self.handlers.get(event, []).remove(callback)

    def emit(self, event: str, value) -> None:
        for callback in tuple(self.handlers.get(event, [])):
            callback(value)

    def get_by_role(self, role: str, *, name=None, exact=None) -> _Locator:
        if role == "button" and name == "分享" and exact is True:
            return _Locator([self.final_button])
        return _Locator(list(self.roles.get(role, [])))


class FacebookPostClickDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_keeps_only_allowlisted_metadata(self) -> None:
        page = _Page()
        recorder = FacebookPostClickDiagnosticRecorder(
            page,
            expected_page_id="1001",
            final_button_label="分享",
        )
        recorder.start()
        request = SimpleNamespace(
            method="POST",
            resource_type="xhr",
            headers={"cookie": "SECRET_COOKIE"},
            post_data="SECRET_CAPTION",
        )
        page.emit(
            "response",
            SimpleNamespace(
                url=(
                    "https://business.facebook.com/api/graphql/"
                    "?access_token=SECRET_TOKEN&variables=SECRET_CAPTION"
                ),
                status=200,
                ok=True,
                request=request,
            ),
        )
        page.emit(
            "response",
            SimpleNamespace(
                url=(
                    "https://business.facebook.com/api/"
                    "AbCdEfGhJkLmNoPq/"
                ),
                status=204,
                ok=True,
                request=request,
            ),
        )
        page.emit(
            "popup",
            SimpleNamespace(
                url=(
                    "https://business.facebook.com/latest/notifications/"
                    "?token=SECRET_TOKEN"
                )
            ),
        )

        await recorder.sample(
            "post_click_observed",
            post_click_state="confirmation_pending",
        )
        snapshot = recorder.finish()
        recorder.stop()

        self.assertEqual(
            snapshot["schemaVersion"],
            "facebook-post-click-diagnostic/v2",
        )
        self.assertEqual(snapshot["pageId"], "1001")
        self.assertIn("popupUrls", snapshot)
        self.assertEqual(
            snapshot["popupUrls"],
            ["https://business.facebook.com/latest/notifications/"],
        )
        self.assertEqual(
            snapshot["networkResults"],
            [
                {
                    "method": "POST",
                    "host": "business.facebook.com",
                    "path": "/api/graphql/",
                    "resourceType": "xhr",
                    "status": 200,
                    "ok": True,
                },
                {
                    "method": "POST",
                    "host": "business.facebook.com",
                    "path": "/api/:redacted/",
                    "resourceType": "xhr",
                    "status": 204,
                    "ok": True,
                },
            ],
        )
        self.assertEqual(snapshot["networkResultCount"], 2)
        self.assertEqual(snapshot["networkDroppedCount"], 0)
        self.assertEqual(snapshot["graphqlResults"], [])
        self.assertEqual(snapshot["graphqlResultCount"], 0)
        self.assertEqual(snapshot["graphqlDroppedCount"], 0)
        sample = snapshot["samples"][0]
        self.assertEqual(sample["phase"], "post_click_observed")
        self.assertEqual(sample["captureStatus"], "ok")
        self.assertEqual(
            sample["pageUrl"],
            (
                "https://business.facebook.com/latest/reels_composer/"
                "?asset_id=1001"
            ),
        )
        self.assertEqual(sample["postClickState"], "confirmation_pending")
        self.assertEqual(
            sample["finalButton"],
            {"label": "分享", "visibleCount": 1, "enabled": False},
        )
        self.assertEqual(
            [item["role"] for item in sample["dialogs"]],
            ["dialog", "alertdialog"],
        )
        self.assertEqual(
            sample["dialogs"][0]["textSha256"],
            hashlib.sha256(
                "确认发布 SECRET_CAPTION".encode("utf-8")
            ).hexdigest(),
        )
        self.assertEqual(
            [item["classification"] for item in sample["notices"]],
            ["accepted", "unknown"],
        )

        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        for secret in (
            "SECRET_TOKEN",
            "SECRET_COOKIE",
            "SECRET_CAPTION",
            "SECRET_STATUS_BODY",
            "access_token",
            "variables",
            "AbCdEfGhJkLmNoPq",
        ):
            self.assertNotIn(secret, serialized)

        self.assertEqual(
            project_facebook_post_click_diagnostic(
                snapshot,
                expected_page_id="1001",
            ),
            snapshot,
        )

        with self.subTest("extra top-level credential field"):
            compromised = deepcopy(snapshot)
            compromised["cookie"] = "SECRET_COOKIE"
            with self.assertRaises(ValueError):
                project_facebook_post_click_diagnostic(compromised)

        with self.subTest("raw request URL"):
            compromised = deepcopy(snapshot)
            compromised["networkResults"][0]["url"] = (
                "https://business.facebook.com/api/graphql/"
                "?access_token=SECRET_TOKEN"
            )
            with self.assertRaises(ValueError):
                project_facebook_post_click_diagnostic(compromised)

        with self.subTest("unexpected page identity"):
            with self.assertRaises(ValueError):
                project_facebook_post_click_diagnostic(
                    snapshot,
                    expected_page_id="1002",
                )

    async def test_publish_mutation_response_is_classified_without_body_persistence(
        self,
    ) -> None:
        page = _Page()
        recorder = FacebookPostClickDiagnosticRecorder(
            page,
            expected_page_id="1001",
            final_button_label="分享",
        )
        recorder.start()
        recorder.mark_final_action_started()
        operation_name = "CometBusinessReelsCreateMutation"
        document_id = "987654321"
        request = SimpleNamespace(
            method="POST",
            resource_type="xhr",
            post_data_json={
                "fb_api_req_friendly_name": operation_name,
                "doc_id": document_id,
                "variables": (
                    '{"caption":"SECRET_CAPTION",'
                    '"access_token":"SECRET_TOKEN"}'
                ),
            },
        )
        response_json_calls: list[str] = []

        async def response_json() -> object:
            response_json_calls.append("called")
            return {
                "data": None,
                "errors": [
                    {
                        "code": 1357001,
                        "is_transient": True,
                        "message": "SECRET_META_ERROR_BODY",
                    }
                ],
                "extensions": {"token": "SECRET_RESPONSE_TOKEN"},
            }

        page.emit(
            "response",
            SimpleNamespace(
                url="https://business.facebook.com/api/graphql/",
                status=200,
                ok=True,
                request=request,
                json=response_json,
            ),
        )

        await recorder.drain()
        await recorder.sample(
            "post_click_observed",
            post_click_state="composer_unchanged",
        )
        snapshot = recorder.finish()
        recorder.stop()

        self.assertEqual(response_json_calls, ["called"])
        self.assertEqual(
            snapshot["schemaVersion"],
            "facebook-post-click-diagnostic/v2",
        )
        self.assertEqual(snapshot["graphqlResultCount"], 1)
        self.assertEqual(snapshot["graphqlDroppedCount"], 0)
        self.assertEqual(
            snapshot["graphqlResults"],
            [
                {
                    "operationClass": "reel_publish_mutation",
                    "operationNameSha256": hashlib.sha256(
                        operation_name.encode("utf-8")
                    ).hexdigest(),
                    "operationNameLength": len(operation_name),
                    "documentIdSha256": hashlib.sha256(
                        document_id.encode("utf-8")
                    ).hexdigest(),
                    "documentIdLength": len(document_id),
                    "applicationOutcome": "errors",
                    "dataPresent": False,
                    "errorCount": 1,
                    "errorCodes": [1357001],
                    "transientErrorCount": 1,
                    "errorMessages": [
                        {
                            "textSha256": hashlib.sha256(
                                "SECRET_META_ERROR_BODY".encode("utf-8")
                            ).hexdigest(),
                            "textLength": len("SECRET_META_ERROR_BODY"),
                        }
                    ],
                    "captureStatus": "ok",
                }
            ],
        )
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        for secret in (
            operation_name,
            document_id,
            "SECRET_CAPTION",
            "SECRET_TOKEN",
            "SECRET_META_ERROR_BODY",
            "SECRET_RESPONSE_TOKEN",
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual(
            project_facebook_post_click_diagnostic(
                snapshot,
                expected_page_id="1001",
            ),
            snapshot,
        )
        with self.subTest("malformed GraphQL error code"):
            compromised = deepcopy(snapshot)
            compromised["graphqlResults"][0]["errorCodes"] = [
                {"token": "SECRET_TOKEN"}
            ]
            with self.assertRaises(ValueError):
                project_facebook_post_click_diagnostic(compromised)
        with self.subTest("raw GraphQL response body"):
            compromised = deepcopy(snapshot)
            compromised["graphqlResults"][0]["responseBody"] = (
                "SECRET_META_ERROR_BODY"
            )
            with self.assertRaises(ValueError):
                project_facebook_post_click_diagnostic(compromised)

    async def test_all_mutations_are_inspected_but_queries_are_not(self) -> None:
        page = _Page()
        recorder = FacebookPostClickDiagnosticRecorder(
            page,
            expected_page_id="1001",
            final_button_label="分享",
        )
        recorder.start()
        recorder.mark_final_action_started()
        body_calls: list[str] = []

        async def mutation_json() -> object:
            body_calls.append("mutation")
            return {"data": {"submit": {"id": "SECRET_PLATFORM_ID"}}}

        async def query_json() -> object:
            body_calls.append("query")
            return {"data": {"caption": "SECRET_QUERY_BODY"}}

        for operation_name, response_json in (
            ("CometComposerSubmitMutation", mutation_json),
            ("CometBusinessReelsComposerQuery", query_json),
        ):
            page.emit(
                "response",
                SimpleNamespace(
                    url="https://business.facebook.com/api/graphql/",
                    status=200,
                    ok=True,
                    request=SimpleNamespace(
                        method="POST",
                        resource_type="fetch",
                        post_data_json={
                            "fb_api_req_friendly_name": operation_name,
                            "doc_id": "123456789",
                            "variables": "SECRET_REQUEST_VARIABLES",
                        },
                    ),
                    json=response_json,
                ),
            )

        await recorder.drain()
        await recorder.sample(
            "post_click_observed",
            post_click_state="composer_unchanged",
        )
        snapshot = recorder.finish()
        recorder.stop()

        self.assertEqual(body_calls, ["mutation"])
        self.assertEqual(snapshot["graphqlResultCount"], 2)
        self.assertEqual(
            [
                (item["operationClass"], item["applicationOutcome"])
                for item in snapshot["graphqlResults"]
            ],
            [
                ("other_mutation", "data"),
                ("query", "not_inspected"),
            ],
        )
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        for secret in (
            "CometComposerSubmitMutation",
            "CometBusinessReelsComposerQuery",
            "SECRET_PLATFORM_ID",
            "SECRET_QUERY_BODY",
            "SECRET_REQUEST_VARIABLES",
        ):
            self.assertNotIn(secret, serialized)

    async def test_malformed_graphql_errors_degrade_without_raw_body(self) -> None:
        page = _Page()
        recorder = FacebookPostClickDiagnosticRecorder(
            page,
            expected_page_id="1001",
            final_button_label="分享",
        )
        recorder.start()
        recorder.mark_final_action_started()

        async def response_json() -> object:
            return {
                "data": None,
                "errors": ["SECRET_MALFORMED_ERROR_BODY"],
            }

        page.emit(
            "response",
            SimpleNamespace(
                url="https://business.facebook.com/api/graphql/",
                status=200,
                ok=True,
                request=SimpleNamespace(
                    method="POST",
                    resource_type="xhr",
                    post_data_json={
                        "fb_api_req_friendly_name": (
                            "CometBusinessReelsCreateMutation"
                        ),
                        "doc_id": "123456789",
                    },
                ),
                json=response_json,
            ),
        )

        await recorder.drain()
        await recorder.sample(
            "post_click_observed",
            post_click_state="composer_unchanged",
        )
        snapshot = recorder.finish()
        recorder.stop()

        result = snapshot["graphqlResults"][0]
        self.assertEqual(result["applicationOutcome"], "unreadable")
        self.assertEqual(result["captureStatus"], "partial")
        self.assertEqual(result["errorCount"], 0)
        self.assertNotIn(
            "SECRET_MALFORMED_ERROR_BODY",
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
        )

    async def test_graphql_capture_survives_general_network_result_cap(self) -> None:
        page = _Page()
        recorder = FacebookPostClickDiagnosticRecorder(
            page,
            expected_page_id="1001",
            final_button_label="分享",
        )
        recorder.start()
        recorder.mark_final_action_started()
        for _ in range(32):
            page.emit(
                "response",
                SimpleNamespace(
                    url="https://business.facebook.com/ajax/background/",
                    status=200,
                    ok=True,
                    request=SimpleNamespace(
                        method="GET",
                        resource_type="fetch",
                    ),
                ),
            )

        async def mutation_json() -> object:
            return {"data": {"submit": {"id": "SECRET_PLATFORM_ID"}}}

        page.emit(
            "response",
            SimpleNamespace(
                url="https://business.facebook.com/api/graphql/",
                status=200,
                ok=True,
                request=SimpleNamespace(
                    method="POST",
                    resource_type="xhr",
                    post_data_json={
                        "fb_api_req_friendly_name": (
                            "CometBusinessReelsCreateMutation"
                        ),
                        "doc_id": "123456789",
                    },
                ),
                json=mutation_json,
            ),
        )

        await recorder.drain()
        await recorder.sample(
            "post_click_observed",
            post_click_state="composer_unchanged",
        )
        snapshot = recorder.finish()
        recorder.stop()

        self.assertEqual(snapshot["networkResultCount"], 33)
        self.assertEqual(snapshot["networkDroppedCount"], 1)
        self.assertEqual(snapshot["graphqlResultCount"], 1)
        self.assertEqual(snapshot["graphqlDroppedCount"], 0)
        self.assertEqual(
            snapshot["graphqlResults"][0]["applicationOutcome"],
            "data",
        )

    async def test_drain_seals_new_responses_before_finish(self) -> None:
        page = _Page()
        recorder = FacebookPostClickDiagnosticRecorder(
            page,
            expected_page_id="1001",
            final_button_label="分享",
        )
        recorder.start()
        recorder.mark_final_action_started()

        async def late_json() -> object:
            return {"data": {"late": True}}

        late_response = SimpleNamespace(
            url="https://business.facebook.com/api/graphql/",
            status=200,
            ok=True,
            request=SimpleNamespace(
                method="POST",
                resource_type="xhr",
                post_data_json={
                    "fb_api_req_friendly_name": "LateBackgroundMutation",
                    "doc_id": "222222222",
                },
            ),
            json=late_json,
        )

        async def first_json() -> object:
            page.emit("response", late_response)
            return {"data": {"submit": True}}

        page.emit(
            "response",
            SimpleNamespace(
                url="https://business.facebook.com/api/graphql/",
                status=200,
                ok=True,
                request=SimpleNamespace(
                    method="POST",
                    resource_type="xhr",
                    post_data_json={
                        "fb_api_req_friendly_name": (
                            "CometBusinessReelsCreateMutation"
                        ),
                        "doc_id": "111111111",
                    },
                ),
                json=first_json,
            ),
        )

        await recorder.drain()
        await recorder.sample(
            "readback_finished",
            post_click_state="composer_unchanged",
        )
        snapshot = recorder.finish()
        recorder.stop()

        self.assertEqual(snapshot["graphqlResultCount"], 1)
        self.assertEqual(len(snapshot["graphqlResults"]), 1)
        self.assertEqual(page.handlers["response"], [])
        self.assertEqual(page.handlers["popup"], [])

    async def test_malformed_urls_degrade_to_partial_without_listener_failure(
        self,
    ) -> None:
        page = _Page()
        page.url = "https://business.facebook.com:bad/latest/SECRET_BODY"
        recorder = FacebookPostClickDiagnosticRecorder(
            page,
            expected_page_id="1001",
            final_button_label="分享",
        )
        recorder.start()
        page.emit(
            "response",
            SimpleNamespace(
                url="https://business.facebook.com:bad/api/SECRET_BODY",
                status=200,
                ok=True,
                request=SimpleNamespace(method="GET", resource_type="fetch"),
            ),
        )

        await recorder.sample(
            "post_click_observed",
            post_click_state="unknown",
        )
        snapshot = recorder.finish()
        recorder.stop()

        self.assertEqual(snapshot["samples"][0]["captureStatus"], "partial")
        self.assertEqual(snapshot["samples"][0]["pageUrl"], "")
        self.assertEqual(snapshot["networkResults"], [])
        self.assertNotIn("SECRET_BODY", json.dumps(snapshot, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
