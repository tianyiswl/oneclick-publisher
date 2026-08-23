# -*- coding: utf-8 -*-
"""抖音评论合同只读探测、脱敏和失败关闭测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app_core.platform_data_comment_contract as contract_module
from app_core.platform_data_comment_contract import (
    load_verified_contract,
    sanitize_contract_observation,
)
from app_core.platform_data_collection_errors import CleanupReceipt
from app_core.platform_data_comment_models import CommentInsightFailure


def response_shape(
    *,
    url: str,
    keys: tuple[str, ...],
    sample: object,
) -> dict:
    return {
        "url": url,
        "method": "GET",
        "keys": keys,
        "fieldTypes": tuple((key, "str") for key in keys),
        "sample": sample,
        "headers": {"Cookie": "private-cookie"},
    }


def valid_response(
    path: str,
    keys: tuple[str, ...],
    pagination: tuple[str, ...],
) -> object:
    return contract_module.ContractResponseShape(
        scheme="https",
        host="creator.douyin.com",
        path=path,
        method="GET",
        key_paths=keys,
        field_types=tuple((key, "str") for key in keys),
        pagination_fields=pagination,
    )


def valid_observation(*, verified: object = True) -> object:
    return contract_module.ContractObservation(
        verified=verified,
        responses=(
            valid_response(
                "/verified/content/list",
                (
                    "data.items[]",
                    "data.items[].id",
                    "data.items[].title",
                    "data.items[].cover",
                    "data.items[].published_at",
                    "data.items[].status",
                    "data.items[].type",
                    "data.items[].metrics.views",
                    "data.items[].metrics.likes",
                    "data.cursor",
                    "data.has_more",
                ),
                ("data.cursor", "data.has_more"),
            ),
            valid_response(
                "/verified/comment/list",
                (
                    "data.comments[]",
                    "data.comments[].comment_id",
                    "data.comments[].content_id",
                    "data.comments[].parent_id",
                    "data.comments[].text",
                    "data.comments[].like_count",
                    "data.comments[].reply_count",
                    "data.comments[].commented_at",
                    "data.cursor",
                    "data.has_more",
                ),
                ("data.cursor", "data.has_more"),
            ),
        ),
        navigation_templates=(
            "/verified/content",
            "/verified/content/{content_id}/comments",
        ),
        pagination_triggers=("click:verified-comment-load-more",),
        cleanup=CleanupReceipt(closed=True, alive_resource_count=0),
    )


def valid_selection(**overrides: object) -> object:
    values: dict[str, object] = {
        "content_response_path": "/verified/content/list",
        "content_navigation_template": "/verified/content",
        "content_list_field": "data.items[]",
        "content_id_field": "data.items[].id",
        "content_title_field": "data.items[].title",
        "content_cover_field": "data.items[].cover",
        "content_published_at_field": "data.items[].published_at",
        "content_status_field": "data.items[].status",
        "content_type_field": "data.items[].type",
        "content_metric_fields": (
            ("views", "data.items[].metrics.views"),
            ("likes", "data.items[].metrics.likes"),
        ),
        "content_cursor_field": "data.cursor",
        "content_has_more_field": "data.has_more",
        "comment_response_path": "/verified/comment/list",
        "comment_navigation_template": (
            "/verified/content/{content_id}/comments"
        ),
        "comment_list_field": "data.comments[]",
        "comment_id_field": "data.comments[].comment_id",
        "comment_content_id_field": "data.comments[].content_id",
        "comment_parent_id_field": "data.comments[].parent_id",
        "comment_body_field": "data.comments[].text",
        "comment_like_count_field": "data.comments[].like_count",
        "comment_reply_count_field": "data.comments[].reply_count",
        "comment_commented_at_field": "data.comments[].commented_at",
        "comment_cursor_field": "data.cursor",
        "comment_has_more_field": "data.has_more",
        "comment_pagination_trigger": "click:verified-comment-load-more",
    }
    values.update(overrides)
    return contract_module.ContractSelection(**values)


class FakeRequest:
    def __init__(self, method: str = "GET") -> None:
        self.method = method


class FakeResponse:
    def __init__(
        self,
        url: str,
        payload: object,
        *,
        content_type: str = "application/json; charset=utf-8",
        status: int = 200,
        body_error: BaseException | None = None,
        method: str = "GET",
    ) -> None:
        self.url = url
        self.request = FakeRequest(method)
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._body_error = body_error

    async def body(self) -> bytes:
        if self._body_error is not None:
            raise self._body_error
        return self._body


class FakePage:
    def __init__(
        self,
        responses: tuple[FakeResponse, ...] = (),
        *,
        goto_error: BaseException | None = None,
        final_url: str = "https://creator.douyin.com/",
        close_error: BaseException | None = None,
        close_delay: float = 0.0,
        block_navigation: bool = False,
    ) -> None:
        self.responses = responses
        self.goto_error = goto_error
        self.url = final_url
        self.close_error = close_error
        self.close_delay = close_delay
        self.block_navigation = block_navigation
        self.handlers: dict[str, object] = {}
        self.goto_calls: list[tuple[str, str, float]] = []
        self.closed = 0

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler

    async def goto(
        self, url: str, *, wait_until: str, timeout: float
    ) -> None:
        if "response" not in self.handlers:
            raise AssertionError("response listener must precede navigation")
        self.goto_calls.append((url, wait_until, timeout))
        if self.block_navigation:
            await asyncio.Event().wait()
        if self.goto_error is not None:
            raise self.goto_error
        for response in self.responses:
            self.handlers["response"](response)
        await asyncio.sleep(0)

    async def wait_for_timeout(self, _milliseconds: float) -> None:
        await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_error is not None:
            raise self.close_error


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = 0

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        self.closed += 1


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.closed = 0
        self.storage_states: list[str] = []

    async def new_context(self, *, storage_state: str) -> FakeContext:
        self.storage_states.append(storage_state)
        return self.context

    async def close(self) -> None:
        self.closed += 1


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.headless_values: list[bool] = []

    async def launch(self, *, headless: bool) -> FakeBrowser:
        self.headless_values.append(headless)
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stopped = 0

    async def stop(self) -> None:
        self.stopped += 1


class FakeStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> FakePlaywright:
        return self.playwright


class ObserverHarness:
    def __init__(self, root: Path, page: FakePage) -> None:
        self.state = root / "session.json"
        self.state.write_text(
            json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
        )
        self.account = {
            "id": 12,
            "type": 3,
            "filePath": self.state.name,
            "userName": "绝不进入报告",
        }
        self.context = FakeContext(page)
        self.browser = FakeBrowser(self.context)
        self.playwright = FakePlaywright(self.browser)
        self.starter = FakeStarter(self.playwright)
        self.page = page

    def patches(self):
        return (
            patch.object(contract_module, "COOKIE_DIR", self.state.parent),
            patch.object(
                contract_module,
                "_PLAYWRIGHT_FACTORY",
                return_value=self.starter,
            ),
        )


class ContractSanitizerTests(unittest.TestCase):
    def test_sanitized_observation_has_structure_but_no_values(self) -> None:
        """保存样本、查询串或身份字段会把私人数据写进探测报告。"""

        source = response_shape(
            url=(
                "https://creator.douyin.com/verified/comment/list"
                "?cursor=secret"
            ),
            keys=(
                "data.comments[].comment_id",
                "data.comments[].text",
                "data.comments[].author_info",
                "data.comments[].author_info.uid",
                "data.cursor",
            ),
            sample={"text": "private comment", "author": {"uid": "private"}},
        )

        safe = sanitize_contract_observation({"responses": [source]})

        encoded = json.dumps(safe, ensure_ascii=False)
        self.assertIn("/verified/comment/list", encoded)
        self.assertIn("data.comments[].text", encoded)
        for forbidden in (
            "secret",
            "private comment",
            "private",
            "uid",
            "author_info",
            "Cookie",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_custom_containers_and_builtin_values_do_not_survive(self) -> None:
        """接受容器子类或字段值会绕过只保留结构的边界。"""

        class SneakyDict(dict):
            pass

        class SneakyList(list):
            pass

        safe = sanitize_contract_observation(
            {
                "responses": [
                    SneakyDict(
                        response_shape(
                            url="https://creator.douyin.com/private",
                            keys=("data.value",),
                            sample="must-disappear",
                        )
                    ),
                    {
                        **response_shape(
                            url=(
                                "https://creator.douyin.com/verified/list"
                                "?content_id=private-id"
                            ),
                            keys=(
                                "data.value",
                                "data.author.uid",
                                "data.author_info",
                            ),
                            sample=123456,
                        ),
                        "keys": SneakyList(
                            [
                                "data.value",
                                "data.author.uid",
                                "data.author_info",
                            ]
                        ),
                    },
                ]
            }
        )
        self.assertEqual(safe, {"responses": []})

        self.assertEqual(
            sanitize_contract_observation(
                SneakyDict({"responses": ["must-not-traverse"]})
            ),
            {"responses": []},
        )

    def test_concrete_identifier_segments_are_generalized_in_paths(self) -> None:
        """真实作品 ID 不能留在报告路径，也不能被直接冻结为合同。"""

        concrete_id = "7398123456789012345"
        safe = sanitize_contract_observation(
            {
                "responses": [
                    response_shape(
                        url=(
                            "https://creator.douyin.com/work/"
                            f"{concrete_id}/comments?cursor=private"
                        ),
                        keys=("data.comments[].comment_id",),
                        sample=None,
                    )
                ]
            }
        )

        encoded = json.dumps(safe, ensure_ascii=False)
        self.assertNotIn(concrete_id, encoded)
        self.assertEqual(
            safe["responses"][0]["path"],
            "/work/{content_id}/comments",
        )
        with self.assertRaises(CommentInsightFailure):
            contract_module.ContractResponseShape(
                scheme="https",
                host="creator.douyin.com",
                path=f"/work/{concrete_id}/comments",
                method="GET",
                key_paths=("data.comments[].comment_id",),
                field_types=(("data.comments[].comment_id", "str"),),
                pagination_fields=(),
            )

    def test_nested_and_compound_identity_keys_fail_closed(self) -> None:
        """嵌套、蛇形或驼峰身份字段不能绕过字段名脱敏。"""

        keys = (
            "data.comments[].author_detail.sec_user_id",
            "data.comments[].reviewerProfile.avatarURI",
            "data.comments[].owner_metadata.displayName",
            "data.comments[].comment_id",
            "data.comments[].content_id",
            "data.comments[].reply_count",
            "data.metrics.profile_visits",
        )
        safe = sanitize_contract_observation(
            {
                "responses": [
                    response_shape(
                        url="https://creator.douyin.com/comment/list",
                        keys=keys,
                        sample=None,
                    )
                ]
            }
        )

        survived = safe["responses"][0]["keyPaths"]
        self.assertEqual(
            survived,
            [
                "data.comments[].comment_id",
                "data.comments[].content_id",
                "data.comments[].reply_count",
                "data.metrics.profile_visits",
            ],
        )


class ContractManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_missing_or_unverified_manifest_fails_closed(self) -> None:
        """没有真实验证清单时，下游必须保持作品和评论不可用。"""

        missing = Path(self.tempdir.name) / "missing.json"
        with self.assertRaises(CommentInsightFailure) as raised:
            load_verified_contract(missing)
        self.assertEqual(
            raised.exception.error_code, "comment_content_unavailable"
        )

        unverified = Path(self.tempdir.name) / "unverified.json"
        unverified.write_text(
            json.dumps({"schemaVersion": 1, "verified": 1}),
            encoding="utf-8",
        )
        with self.assertRaises(CommentInsightFailure) as raised:
            load_verified_contract(unverified)
        self.assertEqual(
            raised.exception.error_code, "comment_content_unavailable"
        )

    def test_verified_selection_round_trips_exact_explicit_manifest(self) -> None:
        """冻结或加载时漏字段、改字段都会破坏后续解析器的固定合同。"""

        destination = Path(self.tempdir.name) / "contract.json"

        frozen = contract_module.freeze_verified_contract(
            valid_observation(), valid_selection(), destination
        )
        loaded = load_verified_contract(destination)

        self.assertEqual(frozen, loaded)
        self.assertTrue(loaded.verified)
        self.assertEqual(loaded.creator_host, "creator.douyin.com")
        self.assertEqual(loaded.content_response_method, "GET")
        self.assertEqual(loaded.comment_response_method, "GET")
        self.assertEqual(
            loaded.content_metric_fields,
            (
                ("views", "data.items[].metrics.views"),
                ("likes", "data.items[].metrics.likes"),
            ),
        )
        manifest = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest),
            {
                "schemaVersion",
                "verified",
                "creatorHost",
                "contentList",
                "commentList",
            },
        )
        self.assertIs(manifest["verified"], True)
        self.assertNotIn("responses", manifest)

    def test_freeze_rejects_unobserved_field_navigation_or_pagination(self) -> None:
        """清单不能拼接探测中没有同时出现的路径、字段或翻页动作。"""

        invalid_selections = (
            valid_selection(comment_body_field="data.comments[].guessed_body"),
            valid_selection(
                comment_navigation_template="/guessed/{content_id}/comments"
            ),
            valid_selection(comment_pagination_trigger="guessed_click"),
        )
        for index, selection in enumerate(invalid_selections):
            destination = Path(self.tempdir.name) / f"invalid-{index}.json"
            with self.subTest(selection=selection), self.assertRaises(
                CommentInsightFailure
            ) as raised:
                contract_module.freeze_verified_contract(
                    valid_observation(), selection, destination
                )
            self.assertEqual(
                raised.exception.error_code, "comment_content_unavailable"
            )
            self.assertFalse(destination.exists())

    def test_freeze_rejects_ambiguous_http_method_for_selected_path(self) -> None:
        """同一路径同时出现不同方法时不能猜哪一种进入生产合同。"""

        original = valid_observation()
        content = original.responses[0]
        ambiguous = contract_module.ContractObservation(
            verified=True,
            responses=(
                *original.responses,
                contract_module.ContractResponseShape(
                    scheme=content.scheme,
                    host=content.host,
                    path=content.path,
                    method="POST",
                    key_paths=content.key_paths,
                    field_types=content.field_types,
                    pagination_fields=content.pagination_fields,
                ),
            ),
            navigation_templates=original.navigation_templates,
            pagination_triggers=original.pagination_triggers,
            cleanup=original.cleanup,
        )
        destination = Path(self.tempdir.name) / "ambiguous.json"

        with self.assertRaises(CommentInsightFailure) as raised:
            contract_module.freeze_verified_contract(
                ambiguous, valid_selection(), destination
            )

        self.assertEqual(
            raised.exception.error_code, "comment_content_unavailable"
        )
        self.assertFalse(destination.exists())

    def test_freeze_rejects_path_method_seen_with_partial_shape(self) -> None:
        """同一路径的第二种方法即使字段不全，也必须判定为方法歧义。"""

        original = valid_observation()
        content = original.responses[0]
        ambiguous = contract_module.ContractObservation(
            verified=True,
            responses=(
                *original.responses,
                contract_module.ContractResponseShape(
                    scheme=content.scheme,
                    host=content.host,
                    path=content.path,
                    method="POST",
                    key_paths=("data.status",),
                    field_types=(("data.status", "str"),),
                    pagination_fields=(),
                ),
            ),
            navigation_templates=original.navigation_templates,
            pagination_triggers=original.pagination_triggers,
            cleanup=original.cleanup,
        )
        destination = Path(self.tempdir.name) / "partial-method.json"

        with self.assertRaises(CommentInsightFailure) as raised:
            contract_module.freeze_verified_contract(
                ambiguous, valid_selection(), destination
            )

        self.assertEqual(
            raised.exception.error_code, "comment_content_unavailable"
        )
        self.assertFalse(destination.exists())

    def test_freeze_requires_exact_verified_bool_and_complete_cleanup(self) -> None:
        """整数 1 或未关闭资源都不能冒充真实合同已核验。"""

        invalid_observations = (
            valid_observation(verified=1),
            contract_module.ContractObservation(
                verified=True,
                responses=valid_observation().responses,
                navigation_templates=valid_observation().navigation_templates,
                pagination_triggers=valid_observation().pagination_triggers,
                cleanup=CleanupReceipt(
                    closed=False, alive_resource_count=1
                ),
            ),
        )
        for index, observation in enumerate(invalid_observations):
            destination = Path(self.tempdir.name) / f"unsafe-{index}.json"
            with self.assertRaises(CommentInsightFailure) as raised:
                contract_module.freeze_verified_contract(
                    observation, valid_selection(), destination
                )
            self.assertEqual(
                raised.exception.error_code, "comment_content_unavailable"
            )
            self.assertFalse(destination.exists())


class PassiveObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def official_response() -> FakeResponse:
        return FakeResponse(
            (
                "https://creator.douyin.com/verified/comment/list"
                "?cursor=private-cursor"
            ),
            {
                "data": {
                    "comments": [
                        {
                            "comment_id": "private-comment-id",
                            "text": "private comment body",
                            "author_info": {
                                "uid": "private-user-id",
                                "nickname": "private nickname",
                            },
                        }
                    ],
                    "cursor": "private-next-cursor",
                    "has_more": True,
                }
            },
        )

    def test_observer_opens_only_official_root_and_reports_structure(self) -> None:
        """监听晚装、跨域接收或正文出现在报告都会破坏只读探测边界。"""

        page = FakePage(
            (
                FakeResponse(
                    "https://evil.example/private",
                    {"private": "cross-host-secret"},
                ),
                FakeResponse(
                    "https://creator.douyin.com/not-json",
                    {"private": "non-json-secret"},
                    content_type="text/html",
                ),
                self.official_response(),
            )
        )
        harness = ObserverHarness(self.root, page)
        reports: list[dict] = []
        first_patch, second_patch = harness.patches()

        with first_patch, second_patch:
            observation = contract_module.observe_contract_responses(
                harness.account, reports.append
            )

        self.assertFalse(observation.verified)
        self.assertEqual(len(observation.responses), 1)
        response = observation.responses[0]
        self.assertEqual(response.path, "/verified/comment/list")
        self.assertIn("data.comments[].text", response.key_paths)
        self.assertNotIn("data.comments[].author.uid", response.key_paths)
        self.assertFalse(
            any("author_info" in path for path in response.key_paths)
        )
        self.assertEqual(
            response.pagination_fields,
            ("data.cursor", "data.has_more"),
        )
        self.assertEqual(len(page.goto_calls), 1)
        goto_url, wait_until, goto_timeout = page.goto_calls[0]
        self.assertEqual(goto_url, "https://creator.douyin.com")
        self.assertEqual(wait_until, "domcontentloaded")
        self.assertGreater(goto_timeout, 0)
        self.assertLess(goto_timeout, 60000.0)
        self.assertEqual(harness.playwright.chromium.headless_values, [False])
        self.assertEqual(
            harness.browser.storage_states, [str(harness.state)]
        )
        self.assertTrue(observation.cleanup.closed)
        self.assertEqual(observation.cleanup.alive_resource_count, 0)
        self.assertEqual(
            (page.closed, harness.context.closed, harness.browser.closed),
            (1, 1, 1),
        )
        self.assertEqual(harness.playwright.stopped, 1)
        encoded = json.dumps(reports, ensure_ascii=False)
        self.assertIn("data.comments[].text", encoded)
        for forbidden in (
            "private-cursor",
            "private comment body",
            "private-user-id",
            "private nickname",
            "cross-host-secret",
            "non-json-secret",
            "绝不进入报告",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_observer_generalizes_response_and_navigation_identifiers(self) -> None:
        """被动监听得到的响应和导航路径都不能保留真实作品 ID。"""

        concrete_id = "7398123456789012345"
        page = FakePage(
            (
                FakeResponse(
                    (
                        "https://creator.douyin.com/work/"
                        f"{concrete_id}/comments"
                    ),
                    {"data": {"comments": [{"comment_id": "private"}]}},
                ),
            ),
            final_url=(
                "https://creator.douyin.com/work/"
                f"{concrete_id}/comments"
            ),
        )
        harness = ObserverHarness(self.root, page)
        reports: list[dict] = []
        first_patch, second_patch = harness.patches()

        with first_patch, second_patch:
            observation = contract_module.observe_contract_responses(
                harness.account, reports.append
            )

        self.assertEqual(
            observation.responses[0].path,
            "/work/{content_id}/comments",
        )
        self.assertEqual(
            observation.navigation_templates,
            ("/work/{content_id}/comments",),
        )
        self.assertNotIn(
            concrete_id, json.dumps(reports, ensure_ascii=False)
        )

    def test_cross_host_and_non_json_only_fail_closed_after_cleanup(self) -> None:
        """不合格响应不能被当合同，失败也必须先关闭全部资源。"""

        page = FakePage(
            (
                FakeResponse(
                    "https://evil.example/private", {"value": "secret"}
                ),
                FakeResponse(
                    "https://creator.douyin.com/html",
                    {"value": "secret"},
                    content_type="text/html",
                ),
            )
        )
        harness = ObserverHarness(self.root, page)
        reports: list[dict] = []
        first_patch, second_patch = harness.patches()

        with first_patch, second_patch, self.assertRaises(
            CommentInsightFailure
        ) as raised:
            contract_module.observe_contract_responses(
                harness.account, reports.append
            )

        self.assertEqual(
            raised.exception.error_code, "comment_content_unavailable"
        )
        self.assertEqual(
            (page.closed, harness.context.closed, harness.browser.closed),
            (1, 1, 1),
        )
        self.assertEqual(harness.playwright.stopped, 1)
        self.assertEqual(reports[-1]["cleanup"]["aliveResourceCount"], 0)
        self.assertNotIn("secret", json.dumps(reports))

    def test_oversized_json_uses_fixed_error_and_cleans_every_resource(self) -> None:
        """响应超过上限必须固定失败，不能泄露正文或底层异常。"""

        response = self.official_response()
        response._body = b"x" * 65
        page = FakePage((response,))
        harness = ObserverHarness(self.root, page)
        reports: list[dict] = []
        first_patch, second_patch = harness.patches()

        with (
            first_patch,
            second_patch,
            patch.object(contract_module, "_MAX_RESPONSE_BYTES", 64),
            self.assertRaises(CommentInsightFailure) as raised,
        ):
            contract_module.observe_contract_responses(
                harness.account, reports.append
            )

        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        self.assertEqual(
            (page.closed, harness.context.closed, harness.browser.closed),
            (1, 1, 1),
        )
        self.assertEqual(harness.playwright.stopped, 1)
        self.assertNotIn("xxxxx", json.dumps(reports))

    def test_response_count_and_total_bytes_have_independent_caps(self) -> None:
        """只限单响应会允许多个官方响应耗尽内存，数量上限也不能漏掉。"""

        for limit_name, limit_value in (
            ("_MAX_RESPONSES", 1),
            (
                "_MAX_TOTAL_BYTES",
                len(self.official_response()._body) * 2 - 1,
            ),
        ):
            page = FakePage(
                (self.official_response(), self.official_response())
            )
            harness = ObserverHarness(self.root, page)
            first_patch, second_patch = harness.patches()
            with (
                self.subTest(limit=limit_name),
                first_patch,
                second_patch,
                patch.object(contract_module, limit_name, limit_value),
                self.assertRaises(CommentInsightFailure) as raised,
            ):
                contract_module.observe_contract_responses(
                    harness.account, None
                )
            self.assertEqual(
                raised.exception.error_code, "comment_payload_invalid"
            )
            self.assertEqual(
                (page.closed, harness.context.closed, harness.browser.closed),
                (1, 1, 1),
            )
            self.assertEqual(harness.playwright.stopped, 1)

    def test_key_path_count_cap_rejects_wide_json_without_values(self) -> None:
        """超宽 JSON 即使每个值很小，也必须在结构枚举阶段停止。"""

        page = FakePage((self.official_response(),))
        harness = ObserverHarness(self.root, page)
        first_patch, second_patch = harness.patches()

        with (
            first_patch,
            second_patch,
            patch.object(contract_module, "_MAX_KEY_PATHS", 3),
            self.assertRaises(CommentInsightFailure) as raised,
        ):
            contract_module.observe_contract_responses(harness.account, None)

        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        self.assertEqual(
            (page.closed, harness.context.closed, harness.browser.closed),
            (1, 1, 1),
        )
        self.assertEqual(harness.playwright.stopped, 1)

    def test_json_shape_honors_the_remaining_path_budget(self) -> None:
        """字段结构枚举必须按剩余额度停止，不能先越过累计上限。"""

        with self.assertRaises(CommentInsightFailure) as raised:
            contract_module._json_shape(
                {"first": 1, "second": 2}, max_paths=1
            )

        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")

    def test_concurrent_body_reads_are_reserved_before_starting(self) -> None:
        """并发响应读取前必须先占用预算，不能同时越过总字节上限。"""

        state = {"active": 0, "max_active": 0}

        class DelayedResponse(FakeResponse):
            async def body(self) -> bytes:
                state["active"] += 1
                state["max_active"] = max(
                    state["max_active"], state["active"]
                )
                try:
                    await asyncio.sleep(0.02)
                    return await super().body()
                finally:
                    state["active"] -= 1

        first = DelayedResponse(
            "https://creator.douyin.com/first",
            {"data": {"value": "first"}},
        )
        second = DelayedResponse(
            "https://creator.douyin.com/second",
            {"data": {"value": "second"}},
        )
        page = FakePage((first, second))
        harness = ObserverHarness(self.root, page)
        first_patch, second_patch = harness.patches()

        with (
            first_patch,
            second_patch,
            patch.object(
                contract_module,
                "_MAX_TOTAL_BYTES",
                len(first._body),
            ),
            self.assertRaises(CommentInsightFailure) as raised,
        ):
            contract_module.observe_contract_responses(harness.account, None)

        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        self.assertEqual(state["max_active"], 1)

    def test_concurrent_shapes_receive_decreasing_remaining_path_budget(self) -> None:
        """并发响应的结构提取必须共享同一份递减字段路径预算。"""

        original_json_shape = contract_module._json_shape
        seen_budgets: list[int] = []

        def recording_shape(
            payload: object,
            *,
            max_paths: int,
            deadline: float | None = None,
        ):
            seen_budgets.append(max_paths)
            return original_json_shape(
                payload, max_paths=max_paths, deadline=deadline
            )

        page = FakePage(
            (
                FakeResponse(
                    "https://creator.douyin.com/first", {"first": 1}
                ),
                FakeResponse(
                    "https://creator.douyin.com/second", {"second": 2}
                ),
            )
        )
        harness = ObserverHarness(self.root, page)
        first_patch, second_patch = harness.patches()

        with (
            first_patch,
            second_patch,
            patch.object(contract_module, "_MAX_KEY_PATHS", 3),
            patch.object(contract_module, "_json_shape", recording_shape),
        ):
            observation = contract_module.observe_contract_responses(
                harness.account, None
            )

        self.assertEqual(len(observation.responses), 2)
        self.assertEqual(seen_budgets, [3, 2])

    def test_total_timeout_uses_fixed_error_and_cleans_every_resource(self) -> None:
        """总时限到达后不能留下页面或把 asyncio 原始错误带出。"""

        page = FakePage(block_navigation=True)
        harness = ObserverHarness(self.root, page)
        reports: list[dict] = []
        first_patch, second_patch = harness.patches()

        with (
            first_patch,
            second_patch,
            patch.object(contract_module, "_TOTAL_WALL_SECONDS", 0.01),
            self.assertRaises(CommentInsightFailure) as raised,
        ):
            contract_module.observe_contract_responses(
                harness.account, reports.append
            )

        self.assertEqual(raised.exception.error_code, "comment_sync_timeout")
        self.assertEqual(
            (page.closed, harness.context.closed, harness.browser.closed),
            (1, 1, 1),
        )
        self.assertEqual(harness.playwright.stopped, 1)
        self.assertNotIn("TimeoutError", json.dumps(reports))

    def test_hanging_close_obeys_total_deadline_and_exposes_cleanup_receipt(self) -> None:
        """关闭动作卡住时也必须按总时限返回，并给出未关闭资源数。"""

        page = FakePage(
            (self.official_response(),),
            close_delay=0.2,
        )
        harness = ObserverHarness(self.root, page)
        reports: list[dict] = []
        first_patch, second_patch = harness.patches()
        started = time.monotonic()

        with (
            first_patch,
            second_patch,
            patch.object(contract_module, "_TOTAL_WALL_SECONDS", 0.03),
            self.assertRaises(CommentInsightFailure) as raised,
        ):
            contract_module.observe_contract_responses(
                harness.account, reports.append
            )

        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.15)
        self.assertEqual(raised.exception.error_code, "comment_sync_cancelled")
        receipt = raised.exception.cleanup_receipt
        self.assertFalse(receipt.closed)
        self.assertEqual(receipt.alive_resource_count, 1)
        self.assertFalse(reports[-1]["cleanup"]["closed"])
        self.assertEqual(
            reports[-1]["cleanup"]["aliveResourceCount"], 1
        )
        self.assertEqual(
            (harness.context.closed, harness.browser.closed), (1, 1)
        )
        self.assertEqual(harness.playwright.stopped, 1)

    def test_blocking_report_callback_obeys_the_same_total_deadline(self) -> None:
        """同步报告回调卡住时不能阻塞事件循环或突破总时限。"""

        page = FakePage((self.official_response(),))
        harness = ObserverHarness(self.root, page)
        first_patch, second_patch = harness.patches()

        def blocking_report(_payload: dict) -> None:
            time.sleep(0.2)

        started = time.monotonic()
        with (
            first_patch,
            second_patch,
            patch.object(contract_module, "_TOTAL_WALL_SECONDS", 0.03),
            self.assertRaises(CommentInsightFailure) as raised,
        ):
            contract_module.observe_contract_responses(
                harness.account, blocking_report
            )

        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.15)
        self.assertEqual(raised.exception.error_code, "comment_sync_timeout")
        receipt = raised.exception.cleanup_receipt
        self.assertTrue(receipt.closed)
        self.assertEqual(receipt.alive_resource_count, 0)
        self.assertEqual(
            (page.closed, harness.context.closed, harness.browser.closed),
            (1, 1, 1),
        )
        self.assertEqual(harness.playwright.stopped, 1)

    def test_synchronous_body_reader_is_rejected_without_invocation_or_worker(
        self,
    ) -> None:
        """非异步 body() 必须在调用前失败，不能留下永久工作线程。"""

        invoked = 0
        never_returns = threading.Event()

        class BlockingBodyResponse(FakeResponse):
            def body(self) -> bytes:
                nonlocal invoked
                invoked += 1
                never_returns.wait()
                return self._body

        page = FakePage(
            (
                BlockingBodyResponse(
                    "https://creator.douyin.com/blocking-body",
                    {"data": {"value": "private-body-value"}},
                ),
            )
        )
        harness = ObserverHarness(self.root, page)
        reports: list[dict] = []
        first_patch, second_patch = harness.patches()
        worker_names = {
            "douyin-comment-contract-shape",
            "douyin-comment-contract-sync",
        }
        workers_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name in worker_names
        }
        started = time.monotonic()

        with (
            first_patch,
            second_patch,
            patch.object(contract_module, "_TOTAL_WALL_SECONDS", 0.03),
            self.assertRaises(CommentInsightFailure) as raised,
        ):
            contract_module.observe_contract_responses(
                harness.account, reports.append
            )

        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.15)
        workers_after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name in worker_names
        }
        self.assertEqual(invoked, 0)
        self.assertEqual(workers_after, workers_before)
        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        self.assertTrue(raised.exception.cleanup_receipt.closed)
        self.assertEqual(
            raised.exception.cleanup_receipt.alive_resource_count, 0
        )
        self.assertEqual(
            (page.closed, harness.context.closed, harness.browser.closed),
            (1, 1, 1),
        )
        self.assertEqual(harness.playwright.stopped, 1)
        self.assertNotIn(
            "private-body-value", json.dumps(reports, ensure_ascii=False)
        )

    def test_async_body_reader_still_obeys_the_hard_total_deadline(self) -> None:
        """官方异步 body() 卡住时仍须按同一总时限取消并完成清理。"""

        invoked = threading.Event()

        class BlockingAsyncBodyResponse(FakeResponse):
            async def body(self) -> bytes:
                invoked.set()
                await asyncio.Event().wait()
                return self._body

        page = FakePage(
            (
                BlockingAsyncBodyResponse(
                    "https://creator.douyin.com/async-body",
                    {"data": {"value": "private-async-value"}},
                ),
            )
        )
        harness = ObserverHarness(self.root, page)
        first_patch, second_patch = harness.patches()
        started = time.monotonic()

        with (
            first_patch,
            second_patch,
            patch.object(contract_module, "_TOTAL_WALL_SECONDS", 0.03),
            self.assertRaises(CommentInsightFailure) as raised,
        ):
            contract_module.observe_contract_responses(harness.account, None)

        self.assertLess(time.monotonic() - started, 0.15)
        self.assertTrue(invoked.is_set())
        self.assertEqual(raised.exception.error_code, "comment_sync_timeout")
        self.assertTrue(raised.exception.cleanup_receipt.closed)
        self.assertEqual(
            (page.closed, harness.context.closed, harness.browser.closed),
            (1, 1, 1),
        )
        self.assertEqual(harness.playwright.stopped, 1)

    def test_cpu_heavy_json_shape_obeys_the_hard_total_deadline(self) -> None:
        """大量数组元素的 JSON 解析和结构遍历也不能突破总时限。"""

        response = FakeResponse(
            "https://creator.douyin.com/heavy-json",
            {"data": [0] * 500_000},
        )
        page = FakePage((response,))
        harness = ObserverHarness(self.root, page)
        reports: list[dict] = []
        first_patch, second_patch = harness.patches()
        started = time.monotonic()

        with (
            first_patch,
            second_patch,
            patch.object(contract_module, "_TOTAL_WALL_SECONDS", 0.03),
            patch.object(
                contract_module,
                "_MAX_RESPONSE_BYTES",
                len(response._body) + 1,
            ),
            patch.object(
                contract_module,
                "_MAX_TOTAL_BYTES",
                len(response._body) + 1,
            ),
            self.assertRaises(CommentInsightFailure) as raised,
        ):
            contract_module.observe_contract_responses(
                harness.account, reports.append
            )

        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.15)
        self.assertEqual(raised.exception.error_code, "comment_sync_timeout")
        self.assertTrue(raised.exception.cleanup_receipt.closed)
        self.assertEqual(
            (page.closed, harness.context.closed, harness.browser.closed),
            (1, 1, 1),
        )
        self.assertEqual(harness.playwright.stopped, 1)

    def test_repeated_cpu_timeouts_have_one_audited_worker_at_most(self) -> None:
        """重复 CPU 超时不能累计后台线程，存活线程必须进入清理回执。"""

        release = threading.Event()
        entered = threading.Event()
        original_loads = contract_module.json.loads
        worker_names = {
            "douyin-comment-contract-shape",
            "douyin-comment-contract-sync",
        }
        workers_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name in worker_names
        }
        receipts: list[CleanupReceipt] = []
        error_codes: list[str] = []

        def blocking_loads(body: bytes) -> object:
            entered.set()
            release.wait()
            return original_loads(body)

        try:
            with (
                patch.object(contract_module.json, "loads", blocking_loads),
                patch.object(contract_module, "_TOTAL_WALL_SECONDS", 0.03),
            ):
                for index in range(3):
                    response = FakeResponse(
                        f"https://creator.douyin.com/cpu-timeout-{index}",
                        {"data": [index]},
                    )
                    page = FakePage((response,))
                    harness = ObserverHarness(self.root, page)
                    first_patch, second_patch = harness.patches()
                    with (
                        first_patch,
                        second_patch,
                        self.assertRaises(CommentInsightFailure) as raised,
                    ):
                        contract_module.observe_contract_responses(
                            harness.account, None
                        )
                    receipts.append(raised.exception.cleanup_receipt)
                    error_codes.append(raised.exception.error_code)

            self.assertTrue(entered.is_set())
            workers_after = {
                thread.ident
                for thread in threading.enumerate()
                if thread.name in worker_names
            }
            self.assertLessEqual(
                len(workers_after - workers_before),
                1,
            )
            self.assertEqual(
                error_codes,
                ["comment_sync_cancelled"] * 3,
            )
            for receipt in receipts:
                self.assertFalse(receipt.closed)
                self.assertEqual(receipt.alive_resource_count, 1)
        finally:
            release.set()
            for thread in threading.enumerate():
                if thread.name in worker_names:
                    thread.join(0.5)
        remaining_workers = [
            thread
            for thread in threading.enumerate()
            if thread.name in worker_names
        ]
        self.assertEqual(remaining_workers, [])

    def test_cleanup_failure_returns_only_fixed_error_and_receipt(self) -> None:
        """关闭失败的异常原文不能泄漏，且回执必须如实标明一个资源仍存活。"""

        page = FakePage(
            (self.official_response(),),
            close_error=RuntimeError("private cleanup detail"),
        )
        harness = ObserverHarness(self.root, page)
        reports: list[dict] = []
        first_patch, second_patch = harness.patches()

        with first_patch, second_patch, self.assertRaises(
            CommentInsightFailure
        ) as raised:
            contract_module.observe_contract_responses(
                harness.account, reports.append
            )

        self.assertEqual(raised.exception.error_code, "comment_sync_cancelled")
        self.assertEqual(reports[-1]["cleanup"]["aliveResourceCount"], 1)
        self.assertFalse(reports[-1]["cleanup"]["closed"])
        self.assertNotIn("private cleanup detail", json.dumps(reports))
        self.assertEqual(
            (harness.context.closed, harness.browser.closed), (1, 1)
        )
        self.assertEqual(harness.playwright.stopped, 1)

    def test_navigation_failures_map_to_fixed_codes_without_urls(self) -> None:
        """登录、验证和无权限页面只能公开固定码，不能公开跳转网址。"""

        cases = (
            ("https://creator.douyin.com/login?token=private", "comment_login_required"),
            (
                "https://creator.douyin.com/verification?token=private",
                "comment_verification_required",
            ),
            (
                "https://creator.douyin.com/forbidden?token=private",
                "comment_access_denied",
            ),
        )
        for final_url, expected in cases:
            page = FakePage(final_url=final_url)
            harness = ObserverHarness(self.root, page)
            reports: list[dict] = []
            first_patch, second_patch = harness.patches()
            with (
                self.subTest(code=expected),
                first_patch,
                second_patch,
                self.assertRaises(CommentInsightFailure) as raised,
            ):
                contract_module.observe_contract_responses(
                    harness.account, reports.append
                )
            self.assertEqual(raised.exception.error_code, expected)
            self.assertNotIn("private", json.dumps(reports))
            self.assertEqual(
                (page.closed, harness.context.closed, harness.browser.closed),
                (1, 1, 1),
            )

    def test_keyboard_interrupt_and_system_exit_cleanup_before_propagating(self) -> None:
        """强制退出必须传播，但只能在四层资源都尝试关闭之后传播。"""

        for original in (KeyboardInterrupt(), SystemExit(7)):
            page = FakePage(goto_error=original)
            harness = ObserverHarness(self.root, page)
            first_patch, second_patch = harness.patches()
            with (
                self.subTest(error=type(original).__name__),
                first_patch,
                second_patch,
                self.assertRaises(type(original)),
            ):
                contract_module.observe_contract_responses(
                    harness.account, None
                )
            self.assertEqual(
                (page.closed, harness.context.closed, harness.browser.closed),
                (1, 1, 1),
            )
            self.assertEqual(harness.playwright.stopped, 1)

if __name__ == "__main__":
    unittest.main()
