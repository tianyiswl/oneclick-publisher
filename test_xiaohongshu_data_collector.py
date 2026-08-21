# -*- coding: utf-8 -*-
"""小红书已审核响应的纯 payload 转换测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core.platform_data_collection_errors import PlatformDataCollectionError
from app_core.platform_data_collectors import (
    collector_for_platform,
    registered_platform_types,
)
from app_core.platform_data_models import CollectionFailure, ContentRecord
from app_core import xiaohongshu_data_collector
from app_core.xiaohongshu_data_collector import (
    CREATOR_HOME,
    DATA_ANALYSIS_URL,
    XiaohongshuDataCollector,
)
from app_core.xiaohongshu_data_contract import (
    XhsContentIdentity,
    parse_account_overview,
    parse_content_lifetime,
    parse_content_list,
)


_CONTENT_ID = "6a0ffca800000000080033f8"
_OBSERVED_AT = "2026-08-21T12:00:00+08:00"
_PLATFORM_DAY = "2026-08-21"


class _IntSubclass(int):
    pass


class _StrSubclass(str):
    pass


class XiaohongshuDataContractTests(unittest.TestCase):
    def assert_invalid(self, call) -> None:
        with self.assertRaises(CollectionFailure) as raised:
            call()
        self.assertIn(
            raised.exception.error_code,
            {"metric_payload_invalid", "content_list_truncated"},
        )
        self.assertEqual(str(raised.exception), raised.exception.error_code)
        self.assertIsNone(raised.exception.__cause__)

    def test_parse_account_overview_uses_only_proven_fans_total(self) -> None:
        points = parse_account_overview(
            {"data": {"fans_count": 3199, "unknown": "ignored"}},
            account_id=21,
            observed_at=_OBSERVED_AT,
            platform_day=_PLATFORM_DAY,
        )

        self.assertEqual(
            [(point.metric_key, point.metric_value, point.metric_scope) for point in points],
            [("followers_total", 3199, "lifetime_total")],
        )
        self.assertEqual(points[0].entity_type, "account")
        self.assertEqual(points[0].entity_key, "account:21")
        self.assertEqual(points[0].raw_metric_key, "fans_count")
        self.assertEqual((points[0].period_start, points[0].period_end), (_PLATFORM_DAY, _PLATFORM_DAY))

    def test_account_overview_rejects_unproved_or_non_builtin_values(self) -> None:
        invalid_payloads = (
            None,
            [],
            {"data": []},
            {"data": {}},
            {"data": {"fans_count": True}},
            {"data": {"fans_count": "3199"}},
            {"data": {"fans_count": _IntSubclass(3199)}},
            {"data": {"fans_count": math.nan}},
            {"data": {"fans_count": math.inf}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload_type=type(payload).__name__):
                self.assert_invalid(
                    lambda payload=payload: parse_account_overview(
                        payload,
                        account_id=21,
                        observed_at=_OBSERVED_AT,
                        platform_day=_PLATFORM_DAY,
                    )
                )

    def test_account_overview_requires_a_strict_subject_id(self) -> None:
        """主体 ID 若被硬编码或弱类型接收，会把不同账号指标混写。"""

        payload = {"data": {"fans_count": 3199}}
        for account_id in (True, "21", 0, -1):
            with self.subTest(account_id=account_id):
                self.assert_invalid(
                    lambda account_id=account_id: parse_account_overview(
                        payload,
                        account_id=account_id,
                        observed_at=_OBSERVED_AT,
                        platform_day=_PLATFORM_DAY,
                    )
                )

    def test_content_identity_is_immutable_and_accepts_only_lowercase_hex(self) -> None:
        identity = XhsContentIdentity(_CONTENT_ID)
        self.assertEqual(identity.content_id, _CONTENT_ID)
        with self.assertRaises((AttributeError, TypeError)):
            identity.content_id = "0" * 24

        for invalid_id in ("A" * 24, "0" * 23, _StrSubclass(_CONTENT_ID), True):
            with self.subTest(invalid_id_type=type(invalid_id).__name__):
                self.assert_invalid(lambda invalid_id=invalid_id: XhsContentIdentity(invalid_id))

    def test_parse_content_list_accepts_exact_ids_and_rejects_bool_counts(self) -> None:
        rows = parse_content_list(
            {"data": {"note_infos": [{"id": _CONTENT_ID}], "total": 1}}
        )

        self.assertEqual(rows, (XhsContentIdentity(_CONTENT_ID),))
        self.assert_invalid(
            lambda: parse_content_list(
                {"data": {"note_infos": [{"id": _CONTENT_ID}], "total": True}}
            )
        )

    def test_parse_content_list_accepts_an_empty_proven_list(self) -> None:
        rows = parse_content_list({"data": {"note_infos": [], "total": 0}})

        self.assertEqual(rows, ())

    def test_content_list_rejects_unknown_containers_duplicates_and_truncation(self) -> None:
        invalid_payloads = (
            {"data": {"note_infos": {"id": _CONTENT_ID}, "total": 1}},
            {"data": {"items": [{"id": _CONTENT_ID}], "total": 1}},
            {"data": {"note_infos": [{"id": _CONTENT_ID}], "total": "1"}},
            {"data": {"note_infos": [{"id": _CONTENT_ID}, {"id": _CONTENT_ID}], "total": 2}},
            {"data": {"note_infos": [{"id": _StrSubclass(_CONTENT_ID)}], "total": 1}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=repr(payload)):
                self.assert_invalid(lambda payload=payload: parse_content_list(payload))

        entries = [{"id": f"{index:024x}"} for index in range(51)]
        with self.assertRaises(CollectionFailure) as raised:
            parse_content_list({"data": {"note_infos": entries, "total": 51}})
        self.assertEqual(raised.exception.error_code, "content_list_truncated")
        self.assertIsNone(raised.exception.__cause__)

    def test_parse_lifetime_maps_only_reviewed_metrics_and_marks_unknown_text(self) -> None:
        content, points = parse_content_lifetime(
            {
                "data": {
                    "note_info": {
                        "id": _CONTENT_ID,
                        "view_count": 148,
                        "like_count": 5,
                        "comment_count": 9,
                        "title": "must not be used",
                    },
                    "collect_count": 2,
                    "share_count": 4,
                    "cover": "must not be used",
                }
            },
            XhsContentIdentity(_CONTENT_ID),
            observed_at=_OBSERVED_AT,
            platform_day=_PLATFORM_DAY,
        )

        self.assertEqual(content.content_id, _CONTENT_ID)
        self.assertEqual(content.title, "")
        self.assertEqual(content.cover_url, "")
        self.assertEqual(content.published_at, "")
        self.assertEqual(content.content_status, "unavailable")
        self.assertEqual(content.content_type, "unavailable")
        self.assertEqual({point.metric_key for point in points}, {"views", "likes", "comments", "favorites", "shares"})
        self.assertEqual({point.entity_key for point in points}, {_CONTENT_ID})
        self.assertTrue(all(point.metric_scope == "lifetime_total" for point in points))

    def test_lifetime_rejects_identity_mismatch_and_invalid_metric_values(self) -> None:
        base = {
            "data": {
                "note_info": {
                    "id": _CONTENT_ID,
                    "view_count": 148,
                    "like_count": 5,
                    "comment_count": 9,
                },
                "collect_count": 2,
                "share_count": 4,
            }
        }
        wrong_id = "7b0ffca800000000080033f8"
        self.assert_invalid(
            lambda: parse_content_lifetime(
                {"data": {**base["data"], "note_info": {**base["data"]["note_info"], "id": wrong_id}}},
                XhsContentIdentity(_CONTENT_ID),
                observed_at=_OBSERVED_AT,
                platform_day=_PLATFORM_DAY,
            )
        )

        for key, value in (("view_count", True), ("like_count", "5"), ("comment_count", _IntSubclass(9)), ("share_count", math.nan)):
            payload = {"data": {**base["data"], "note_info": dict(base["data"]["note_info"])}}
            if key in payload["data"]["note_info"]:
                payload["data"]["note_info"][key] = value
            else:
                payload["data"][key] = value
            with self.subTest(key=key, value_type=type(value).__name__):
                self.assert_invalid(
                    lambda payload=payload: parse_content_lifetime(
                        payload,
                        XhsContentIdentity(_CONTENT_ID),
                        observed_at=_OBSERVED_AT,
                        platform_day=_PLATFORM_DAY,
                    )
                )

    def test_lifetime_rejects_empty_or_missing_reviewed_containers(self) -> None:
        identity = XhsContentIdentity(_CONTENT_ID)
        invalid_payloads = (
            {},
            {"data": {}},
            {"data": {"note_info": []}},
            {"data": {"note_info": {"id": _CONTENT_ID}}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=repr(payload)):
                self.assert_invalid(
                    lambda payload=payload: parse_content_lifetime(
                        payload,
                        identity,
                        observed_at=_OBSERVED_AT,
                        platform_day=_PLATFORM_DAY,
                    )
                )

    def test_content_record_allows_only_unavailable_blank_metadata(self) -> None:
        unavailable = ContentRecord(
            content_id=_CONTENT_ID,
            title="",
            cover_url="",
            published_at="",
            content_status="unavailable",
            content_type="unavailable",
        )
        self.assertEqual(unavailable.title, "")
        self.assert_invalid(
            lambda: ContentRecord(
                content_id=_CONTENT_ID,
                title="",
                cover_url="",
                published_at="",
                content_status="published",
                content_type="unavailable",
            )
        )


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeResponse:
    def __init__(
        self,
        url: str,
        payload: object | None = None,
        *,
        body: bytes | None = None,
        status: int = 200,
        content_type: str = "application/json",
        content_length: object | None = None,
        request_url: str | None = None,
    ) -> None:
        self.url = url
        self.request = _FakeRequest(request_url or url)
        self.status = status
        self._body = body if body is not None else json.dumps(payload).encode("utf-8")
        self.headers = {
            "content-type": content_type,
            "content-length": (
                str(len(self._body))
                if content_length is None
                else content_length
            ),
        }
        self.body_calls = 0

    async def body(self) -> bytes:
        self.body_calls += 1
        return self._body


class _FakePage:
    def __init__(self, owner: "_FakeRuntime") -> None:
        self.owner = owner
        self.listeners: dict[str, object] = {}
        self.url = CREATOR_HOME

    def on(self, event: str, callback) -> None:
        self.listeners[event] = callback

    async def goto(self, url: str, **_kwargs) -> object:
        self.owner.goto_urls.append(url)
        self.url = self.owner.final_urls.get(url, url)
        failure = self.owner.goto_failure
        if failure is not None:
            raise failure
        for response in self.owner.responses_by_url.get(url, ()):
            request_callback = self.listeners.get("request")
            response_callback = self.listeners.get("response")
            if request_callback is not None:
                request_callback(response.request)
            if response_callback is not None:
                response_callback(response)
        return _FakeNavigation(self.url)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        self.owner.wait_calls += 1
        if self.owner.wait_hook is not None:
            self.owner.wait_hook(_milliseconds)
        for response in self.owner.delayed_responses.pop(0) if self.owner.delayed_responses else ():
            request_callback = self.listeners.get("request")
            response_callback = self.listeners.get("response")
            if request_callback is not None:
                request_callback(response.request)
            if response_callback is not None:
                response_callback(response)

    async def close(self) -> None:
        self.owner.cleanup.append("page")
        if self.owner.page_close_error is not None:
            raise self.owner.page_close_error


class _FakeNavigation:
    def __init__(self, url: str) -> None:
        self.url = url
        self.status = 200
        self.headers = {"content-type": "text/html"}


class _FakeContext:
    def __init__(self, owner: "_FakeRuntime") -> None:
        self.owner = owner

    async def new_page(self) -> _FakePage:
        return self.owner.page

    async def close(self) -> None:
        self.owner.cleanup.append("context")
        if self.owner.context_close_error is not None:
            raise self.owner.context_close_error


class _FakeBrowser:
    def __init__(self, owner: "_FakeRuntime") -> None:
        self.owner = owner

    async def new_context(self, *, storage_state: str) -> _FakeContext:
        self.owner.storage_states.append(storage_state)
        return _FakeContext(self.owner)

    async def close(self) -> None:
        self.owner.cleanup.append("browser")
        if self.owner.browser_close_error is not None:
            raise self.owner.browser_close_error


class _FakeChromium:
    def __init__(self, owner: "_FakeRuntime") -> None:
        self.owner = owner

    async def launch(self, **_kwargs) -> _FakeBrowser:
        self.owner.launch_options.append(dict(_kwargs))
        return _FakeBrowser(self.owner)


class _FakePlaywright:
    def __init__(self, owner: "_FakeRuntime") -> None:
        self.owner = owner
        self.chromium = _FakeChromium(owner)

    async def stop(self) -> None:
        self.owner.cleanup.append("playwright")
        if self.owner.playwright_close_error is not None:
            raise self.owner.playwright_close_error


class _FakeStarter:
    def __init__(self, owner: "_FakeRuntime") -> None:
        self.owner = owner

    async def start(self) -> _FakePlaywright:
        return _FakePlaywright(self.owner)


class _FakeRuntime:
    def __init__(
        self,
        responses_by_url: dict[str, tuple[_FakeResponse, ...]],
        *,
        final_urls: dict[str, str] | None = None,
        goto_failure: BaseException | None = None,
        page_close_error: BaseException | None = None,
        context_close_error: BaseException | None = None,
        browser_close_error: BaseException | None = None,
        playwright_close_error: BaseException | None = None,
        delayed_responses: tuple[tuple[_FakeResponse, ...], ...] = (),
        wait_hook=None,
    ) -> None:
        self.responses_by_url = responses_by_url
        self.final_urls = final_urls or {}
        self.goto_failure = goto_failure
        self.page_close_error = page_close_error
        self.context_close_error = context_close_error
        self.browser_close_error = browser_close_error
        self.playwright_close_error = playwright_close_error
        self.delayed_responses = list(delayed_responses)
        self.wait_hook = wait_hook
        self.goto_urls: list[str] = []
        self.wait_calls = 0
        self.launch_options: list[dict] = []
        self.cleanup: list[str] = []
        self.storage_states: list[str] = []
        self.page = _FakePage(self)


class _Clock:
    def __init__(self, values: tuple[float, ...] = (0.0,)) -> None:
        self._values = iter(values)
        self._last = values[-1]

    def __call__(self) -> float:
        try:
            self._last = next(self._values)
        except StopIteration:
            pass
        return self._last


class _MutableClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance_milliseconds(self, milliseconds: int) -> None:
        self.now += milliseconds / 1_000


def _account(account_id: int = 21, file_path: str = "oneclick_1_test.json") -> dict:
    return {"id": account_id, "type": 1, "filePath": file_path}


def _detail_url() -> str:
    return (
        "https://creator.xiaohongshu.com/statistics/note-detail?noteId="
        f"{_CONTENT_ID}"
    )


def _reviewed_success_responses() -> dict[str, tuple[_FakeResponse, ...]]:
    return {
        CREATOR_HOME: (
            _FakeResponse(
                "https://creator.xiaohongshu.com/api/galaxy/v2/creator/"
                "datacenter/account/base?private=query",
                {"data": {"fans_count": 3199}},
            ),
            _FakeResponse(
                "https://creator.xiaohongshu.com/api/galaxy/creator/home/"
                "personal_info?private=query",
                {"data": {"fans_count": 3199}},
            ),
        ),
        DATA_ANALYSIS_URL: (
            _FakeResponse(
                "https://creator.xiaohongshu.com/api/galaxy/creator/"
                "datacenter/note/analyze/list?page_num=1",
                {"data": {"note_infos": [{"id": _CONTENT_ID}], "total": 1}},
            ),
        ),
        _detail_url(): (
            _FakeResponse(
                "https://creator.xiaohongshu.com/api/galaxy/creator/"
                "datacenter/note/base?noteId=private",
                {
                    "data": {
                        "note_info": {
                            "id": _CONTENT_ID,
                            "view_count": 148,
                            "like_count": 5,
                            "comment_count": 9,
                        },
                        "collect_count": 2,
                        "share_count": 4,
                    }
                },
                request_url=(
                    "https://creator.xiaohongshu.com/api/galaxy/creator/"
                    f"datacenter/note/base?noteId={_CONTENT_ID}"
                ),
            ),
        ),
    }


class XiaohongshuDataCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.cookie_dir = Path(self.tempdir.name)
        self.state_file = self.cookie_dir / _account()["filePath"]
        self.state_file.write_text("{}", encoding="utf-8")
        self.cookie_patch = patch(
            "app_core.xiaohongshu_data_collector.COOKIE_DIR", self.cookie_dir
        )
        self.cookie_patch.start()

    def tearDown(self) -> None:
        self.cookie_patch.stop()
        self.tempdir.cleanup()

    def _collector_with_responses(
        self,
        responses: dict[str, tuple[_FakeResponse, ...]],
        **runtime_kwargs,
    ) -> tuple[XiaohongshuDataCollector, _FakeRuntime]:
        clock = _MutableClock()
        fake = _FakeRuntime(
            responses,
            wait_hook=clock.advance_milliseconds,
            **runtime_kwargs,
        )
        return (
            XiaohongshuDataCollector(
                browser_factory=lambda: _FakeStarter(fake),
                utc_now=lambda: datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc),
                monotonic=clock,
            ),
            fake,
        )

    def assert_collection_error(self, call, code: str) -> None:
        with self.assertRaises(PlatformDataCollectionError) as caught:
            call()
        self.assertEqual(caught.exception.error_code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertIsNone(caught.exception.__cause__)

    def test_browser_signed_collects_one_standard_batch_and_closes_all_resources(self) -> None:
        """删掉阶段绑定、数据转换或任一关闭动作时，此测试应失败。"""

        collector, fake = self._collector_with_responses(_reviewed_success_responses())

        batch = collector.collect_browser_signed(_account())

        self.assertEqual(batch.platform_type, 1)
        self.assertEqual(batch.source_mode, "browser_signed")
        self.assertTrue(batch.account_metrics_available)
        self.assertTrue(batch.content_data_available)
        self.assertEqual(len(batch.contents), 1)
        self.assertEqual(
            {point.entity_key for point in batch.metrics if point.entity_type == "account"},
            {"account:21"},
        )
        self.assertEqual(
            fake.goto_urls, [CREATOR_HOME, DATA_ANALYSIS_URL, _detail_url()]
        )
        self.assertEqual(fake.cleanup, ["page", "context", "browser", "playwright"])

    def test_browser_signed_diagnostics_exposes_only_strict_successful_cleanup_receipt(self) -> None:
        """删掉受控回执会让成功同步无法证明资源已完全关闭。"""

        collector, _fake = self._collector_with_responses(_reviewed_success_responses())

        outcome = collector.collect_browser_signed_with_diagnostics(_account())

        self.assertEqual(outcome.batch.platform_type, 1)
        self.assertEqual(
            outcome.public_diagnostics(),
            {"cleanup": {"closed": True, "aliveResourceCount": 0}},
        )
        self.assertIs(type(outcome.cleanup.closed), bool)
        self.assertIs(type(outcome.cleanup.alive_resource_count), int)

    def test_validation_mode_uses_headed_browser_and_returns_whitelisted_visible_values(self) -> None:
        """验收模式若仍无头或透传页面文本，就无法做人工比对且会扩大泄露面。"""

        visible = {
            "account": {"followersTotal": 3199},
            "content": {"contentId": _CONTENT_ID, "views": 148},
        }
        fake = _FakeRuntime(_reviewed_success_responses())
        collector = XiaohongshuDataCollector(
            browser_factory=lambda: _FakeStarter(fake),
            utc_now=lambda: datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc),
            monotonic=_Clock(),
            visible_readback=lambda _page, _batch: visible,
        )

        outcome = collector.collect_browser_signed_with_diagnostics(
            _account(), validation_mode=True
        )

        self.assertEqual(fake.launch_options, [{"headless": False}])
        self.assertEqual(
            outcome.public_diagnostics()["validation"],
            {
                "mode": "official_visible_readback",
                "officialVisible": visible,
            },
        )

    def test_production_visible_reader_projects_only_verified_numbers(self) -> None:
        """生产 reader 若回传页面文字或额外字段，就会把可见页以外的数据带出浏览器。"""

        class Page:
            async def evaluate(self, _script, content_id):
                self.content_id = content_id
                return {
                    "account": {"followersTotal": 3199, "private": "drop"},
                    "content": {"contentId": content_id, "views": 148, "html": "drop"},
                }

        batch = XiaohongshuDataCollector(
            browser_factory=lambda: _FakeStarter(_FakeRuntime(_reviewed_success_responses())),
            utc_now=lambda: datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc),
            monotonic=_Clock(),
        ).collect_browser_signed(_account())
        page = Page()
        reader = getattr(xiaohongshu_data_collector, "official_visible_readback", None)

        self.assertTrue(callable(reader))

        result = asyncio.run(
            reader(page, batch)
        )

        self.assertEqual(page.content_id, _CONTENT_ID)
        self.assertEqual(
            result,
            {
                "account": {"followersTotal": 3199},
                "content": {"contentId": _CONTENT_ID, "views": 148},
            },
        )

    def test_account_metrics_are_scoped_to_the_strict_requested_account(self) -> None:
        """固定 account key 会把不同小红书主体的粉丝累计量混在一起。"""

        collector, _fake = self._collector_with_responses(_reviewed_success_responses())
        first = collector.collect_browser_signed(_account(21))
        second_state = self.cookie_dir / "oneclick_1_second.json"
        second_state.write_text("{}", encoding="utf-8")
        collector, _fake = self._collector_with_responses(_reviewed_success_responses())
        second = collector.collect_browser_signed(_account(22, second_state.name))

        self.assertEqual(
            {point.entity_key for point in first.metrics if point.entity_type == "account"},
            {"account:21"},
        )
        self.assertEqual(
            {point.entity_key for point in second.metrics if point.entity_type == "account"},
            {"account:22"},
        )

    def test_proven_empty_content_list_is_a_successful_zero_content_batch(self) -> None:
        """已证实的空列表不能被当成列表不可用而丢掉账号指标。"""

        responses = _reviewed_success_responses()
        responses[DATA_ANALYSIS_URL] = (
            _FakeResponse(
                "https://creator.xiaohongshu.com/api/galaxy/creator/"
                "datacenter/note/analyze/list?page_num=1",
                {"data": {"note_infos": [], "total": 0}},
            ),
        )
        collector, fake = self._collector_with_responses(responses)

        batch = collector.collect_browser_signed(_account())

        self.assertTrue(batch.account_metrics_available)
        self.assertTrue(batch.content_data_available)
        self.assertEqual(batch.contents, ())
        self.assertEqual(batch.warning_code, "")
        self.assertEqual(fake.goto_urls, [CREATOR_HOME, DATA_ANALYSIS_URL])

    def test_missing_content_list_stops_at_deadline_without_partial_snapshot(self) -> None:
        """列表阶段到 deadline 仍无官方响应时，不能把缺失伪装成部分成功。"""

        responses = _reviewed_success_responses()
        responses[DATA_ANALYSIS_URL] = ()
        collector, fake = self._collector_with_responses(responses)

        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()),
            "browser_signature_timeout",
        )
        self.assertEqual(fake.goto_urls, [CREATOR_HOME, DATA_ANALYSIS_URL])

    def test_delayed_reviewed_content_list_is_polled_before_partial_result(self) -> None:
        """把等待退回成一次 sleep(0) 会丢掉稍晚抵达但仍在预算内的合格列表。"""

        responses = _reviewed_success_responses()
        delayed_list = responses[DATA_ANALYSIS_URL]
        responses[DATA_ANALYSIS_URL] = ()
        collector, fake = self._collector_with_responses(
            responses,
            delayed_responses=(delayed_list,),
        )

        batch = collector.collect_browser_signed(_account())

        self.assertTrue(batch.content_data_available)
        self.assertEqual([content.content_id for content in batch.contents], [_CONTENT_ID])
        self.assertEqual(fake.goto_urls, [CREATOR_HOME, DATA_ANALYSIS_URL, _detail_url()])
        self.assertGreaterEqual(fake.wait_calls, 1)

    def test_delayed_reviewed_content_detail_is_polled_before_partial_result(self) -> None:
        """详情响应晚到时若不按当前阶段轮询，会把已验证作品误报为不可用。"""

        responses = _reviewed_success_responses()
        delayed_detail = responses[_detail_url()]
        responses[_detail_url()] = ()
        collector, fake = self._collector_with_responses(
            responses,
            delayed_responses=(delayed_detail,),
        )

        batch = collector.collect_browser_signed(_account())

        self.assertTrue(batch.content_data_available)
        self.assertEqual([content.content_id for content in batch.contents], [_CONTENT_ID])
        self.assertEqual(fake.goto_urls, [CREATOR_HOME, DATA_ANALYSIS_URL, _detail_url()])
        self.assertGreaterEqual(fake.wait_calls, 1)

    def test_phase_polling_accepts_response_after_750ms_before_shared_deadline(self) -> None:
        """把阶段等待写死为三次会漏掉 750ms 后、总时限内才到的详情。"""

        responses = _reviewed_success_responses()
        delayed_detail = responses[_detail_url()]
        responses[_detail_url()] = ()
        clock = _MutableClock()
        fake = _FakeRuntime(
            responses,
            delayed_responses=((), (), (), delayed_detail),
            wait_hook=clock.advance_milliseconds,
        )
        collector = XiaohongshuDataCollector(
            browser_factory=lambda: _FakeStarter(fake),
            utc_now=lambda: datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc),
            monotonic=clock,
        )

        with patch("app_core.xiaohongshu_data_collector._TOTAL_TIMEOUT_SECONDS", 1.5), patch(
            "app_core.xiaohongshu_data_collector._CLEANUP_RESERVE_SECONDS", 0.1
        ):
            batch = collector.collect_browser_signed(_account())

        self.assertTrue(batch.content_data_available)
        self.assertEqual([content.content_id for content in batch.contents], [_CONTENT_ID])
        self.assertEqual(fake.wait_calls, 4)
        self.assertLess(clock.now, 1.4)

    def test_phase_polling_stops_at_shared_deadline(self) -> None:
        """deadline 后还继续轮询会让浏览器会话超出统一资源预算。"""

        responses = _reviewed_success_responses()
        responses[DATA_ANALYSIS_URL] = ()
        clock = _MutableClock()
        fake = _FakeRuntime(
            responses,
            delayed_responses=((), (), (), (), ()),
            wait_hook=clock.advance_milliseconds,
        )
        collector = XiaohongshuDataCollector(
            browser_factory=lambda: _FakeStarter(fake),
            utc_now=lambda: datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc),
            monotonic=clock,
        )

        with patch("app_core.xiaohongshu_data_collector._TOTAL_TIMEOUT_SECONDS", 1.1), patch(
            "app_core.xiaohongshu_data_collector._CLEANUP_RESERVE_SECONDS", 0.1
        ):
            self.assert_collection_error(
                lambda: collector.collect_browser_signed(_account()),
                "browser_signature_timeout",
            )

        self.assertEqual(fake.wait_calls, 4)
        self.assertEqual(fake.cleanup, ["page", "context", "browser", "playwright"])

    def test_context_close_error_does_not_claim_browser_is_alive_after_browser_closes(self) -> None:
        """把下层 close 错误数当存活数，会误报已由 browser 关闭的 context。"""

        collector, fake = self._collector_with_responses(
            _reviewed_success_responses(),
            context_close_error=RuntimeError("private"),
        )

        outcome = collector.collect_browser_signed_with_diagnostics(_account())

        self.assertEqual(
            outcome.public_diagnostics(),
            {"cleanup": {"closed": True, "aliveResourceCount": 0}},
        )
        self.assertIn("browser", fake.cleanup)
        self.assertIn("playwright", fake.cleanup)

    def test_browser_close_failure_counts_only_unproven_top_level_resource(self) -> None:
        """组合 close 异常若把 page/context 错误都累加，会伪造存活资源数。"""

        collector, _fake = self._collector_with_responses(
            _reviewed_success_responses(),
            context_close_error=RuntimeError("private cleanup failure"),
            browser_close_error=RuntimeError("private browser failure"),
        )

        with self.assertRaises(PlatformDataCollectionError) as caught:
            collector.collect_browser_signed_with_diagnostics(_account())

        self.assertEqual(caught.exception.error_code, "browser_cleanup_incomplete")
        self.assertEqual(
            caught.exception.cleanup_receipt.public_payload(),
            {"closed": False, "aliveResourceCount": 1},
        )
        self.assertNotIn("private", repr(caught.exception.cleanup_receipt))

    def test_direct_collection_requests_browser_fallback_without_opening_browser(self) -> None:
        """小红书直连若尝试复制会话请求，就会绕过浏览器签名边界。"""

        collector, fake = self._collector_with_responses(_reviewed_success_responses())

        with self.assertRaises(PlatformDataCollectionError) as caught:
            collector.collect_direct(_account())
        self.assertEqual(caught.exception.error_code, "direct_request_rejected")
        self.assertTrue(caught.exception.fallback_allowed)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(fake.goto_urls, [])
        self.assertEqual(fake.cleanup, [])

    def test_missing_or_linked_state_file_is_rejected_before_browser_start(self) -> None:
        """缺失或链接状态文件若可启动，会把会话边界交给外部路径决定。"""

        self.state_file.unlink()
        collector, fake = self._collector_with_responses(_reviewed_success_responses())
        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()), "session_state_missing"
        )
        self.assertEqual(fake.cleanup, [])

        self.state_file.symlink_to(Path(__file__))
        collector, fake = self._collector_with_responses(_reviewed_success_responses())
        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()), "session_state_missing"
        )
        self.assertEqual(fake.cleanup, [])

    def test_login_or_verification_redirect_stops_collection_and_cleans_up(self) -> None:
        """登录或验证跳转若继续读取响应，可能把匿名页面数据归给账号。"""

        for redirected_url, expected_code in (
            ("https://creator.xiaohongshu.com/login", "login_required"),
            (
                "https://creator.xiaohongshu.com/creator/security/verification",
                "verification_required",
            ),
            ("https://creator.xiaohongshu.com/captcha/challenge", "verification_required"),
        ):
            with self.subTest(redirected_url=redirected_url):
                collector, fake = self._collector_with_responses(
                    {}, final_urls={CREATOR_HOME: redirected_url}
                )
                self.assert_collection_error(
                    lambda: collector.collect_browser_signed(_account()),
                    expected_code,
                )
                self.assertEqual(fake.cleanup, ["page", "context", "browser", "playwright"])

    def test_response_body_and_cumulative_size_limits_fail_closed(self) -> None:
        """放宽任一字节上限会使单会话保留无界响应体。"""

        oversized = _reviewed_success_responses()
        oversized[CREATOR_HOME] = (
            _FakeResponse(
                "https://creator.xiaohongshu.com/api/galaxy/creator/home/personal_info",
                body=b"x" * (1_048_576 + 1),
            ),
        )
        collector, _fake = self._collector_with_responses(oversized)
        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()),
            "metric_payload_invalid",
        )

    def test_reviewed_chunked_or_compressed_json_does_not_require_exact_content_length(self) -> None:
        """官方四个白名单接口可分块传输，压缩长度也不等于解压后的 JSON 长度。"""

        responses = _reviewed_success_responses()
        chunked = responses[CREATOR_HOME][0]
        chunked.headers.pop("content-length")
        compressed = responses[DATA_ANALYSIS_URL][0]
        compressed.headers["content-length"] = "1"

        collector, _fake = self._collector_with_responses(responses)
        batch = collector.collect_browser_signed(_account())

        self.assertGreater(len(batch.metrics), 0)
        self.assertEqual(chunked.body_calls, 1)
        self.assertEqual(compressed.body_calls, 1)

    def test_untrusted_or_over_budget_content_length_rejects_before_body_read(self) -> None:
        """Content-Length 不可信或超预算时，读取 body 本身就已越过资源边界。"""

        for declared_length in ("", "-1", "0", "1.0", True, 1):
            with self.subTest(declared_length=repr(declared_length)):
                responses = _reviewed_success_responses()
                invalid = _FakeResponse(
                    "https://creator.xiaohongshu.com/api/galaxy/creator/home/personal_info",
                    {"data": {"fans_count": 3199}},
                    content_length=declared_length,
                )
                responses[CREATOR_HOME] = (invalid,)
                collector, _fake = self._collector_with_responses(responses)
                self.assert_collection_error(
                    lambda: collector.collect_browser_signed(_account()),
                    "metric_payload_invalid",
                )
                self.assertEqual(invalid.body_calls, 0)

        over_budget = _FakeResponse(
            "https://creator.xiaohongshu.com/api/galaxy/creator/home/personal_info",
            {"data": {"fans_count": 3199}},
            content_length="1048577",
        )
        responses = _reviewed_success_responses()
        responses[CREATOR_HOME] = (over_budget,)
        collector, _fake = self._collector_with_responses(responses)
        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()),
            "metric_payload_invalid",
        )
        self.assertEqual(over_budget.body_calls, 0)

        cumulative = _reviewed_success_responses()
        medium_body = b"{" + b" " * 1_048_574 + b"}"
        cumulative[CREATOR_HOME] = tuple(
            _FakeResponse(
                "https://creator.xiaohongshu.com/api/galaxy/creator/home/personal_info",
                body=medium_body,
            )
            for _ in range(5)
        )
        collector, _fake = self._collector_with_responses(cumulative)
        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()),
            "metric_payload_invalid",
        )

    def test_invalid_json_exposes_only_fixed_endpoint_stage_and_reason(self) -> None:
        """真实失败必须能定位校验点，但不得带出正文、URL 参数或账号信息。"""

        responses = _reviewed_success_responses()
        responses[CREATOR_HOME] = (
            _FakeResponse(
                "https://creator.xiaohongshu.com/api/galaxy/creator/home/personal_info",
                body=b"private-secret-not-json",
            ),
        )
        collector, _fake = self._collector_with_responses(responses)

        with self.assertRaises(PlatformDataCollectionError) as caught:
            collector.collect_browser_signed_with_diagnostics(_account())

        self.assertEqual(caught.exception.error_code, "metric_payload_invalid")
        self.assertEqual(
            caught.exception.failure_diagnostic,
            {
                "endpoint": "account_home",
                "stage": "json_decode",
                "reason": "invalid_json",
            },
        )
        rendered = repr(caught.exception.failure_diagnostic).lower()
        self.assertNotIn("secret", rendered)
        self.assertNotIn("xiaohongshu.com", rendered)

    def test_browser_start_failure_exposes_fixed_runtime_diagnostic(self) -> None:
        """浏览器尚未启动时失败，也必须给出固定阶段且不泄露异常原文。"""

        def fail_start():
            raise RuntimeError("private-secret-browser-error")

        collector = XiaohongshuDataCollector(browser_factory=fail_start)

        with self.assertRaises(PlatformDataCollectionError) as caught:
            collector.collect_browser_signed_with_diagnostics(_account())

        self.assertEqual(
            caught.exception.failure_diagnostic,
            {
                "endpoint": "runtime",
                "stage": "browser_start",
                "reason": "operation_failed",
            },
        )
        self.assertNotIn("secret", repr(caught.exception.failure_diagnostic).lower())

    def test_invalid_navigation_exposes_fixed_runtime_diagnostic(self) -> None:
        collector, _fake = self._collector_with_responses(
            {}, final_urls={CREATOR_HOME: "https://example.invalid/private?token=secret"}
        )

        with self.assertRaises(PlatformDataCollectionError) as caught:
            collector.collect_browser_signed_with_diagnostics(_account())

        self.assertEqual(
            caught.exception.failure_diagnostic,
            {
                "endpoint": "runtime",
                "stage": "navigation",
                "reason": "invalid_navigation",
            },
        )
        self.assertNotIn("example.invalid", repr(caught.exception.failure_diagnostic))

    def test_request_limit_exposes_fixed_capture_diagnostic(self) -> None:
        responses = tuple(
            _FakeResponse(f"https://creator.xiaohongshu.com/unreviewed/{index}", {})
            for index in range(xiaohongshu_data_collector._MAX_REQUESTS + 1)
        )
        collector, _fake = self._collector_with_responses({CREATOR_HOME: responses})

        with self.assertRaises(PlatformDataCollectionError) as caught:
            collector.collect_browser_signed_with_diagnostics(_account())

        self.assertEqual(
            caught.exception.failure_diagnostic,
            {
                "endpoint": "runtime",
                "stage": "response_capture",
                "reason": "request_limit_exceeded",
            },
        )

    def test_late_response_keeps_its_request_phase_and_cannot_drive_note_detail(self) -> None:
        """迟到首页请求若按当前页归类，会把首页响应伪装成作品详情。"""

        delayed = _FakeResponse(
            "https://creator.xiaohongshu.com/api/galaxy/creator/datacenter/note/base",
            {"data": {"note_info": {"id": _CONTENT_ID, "view_count": 1}}},
        )

        class _LatePage(_FakePage):
            async def goto(self, url: str, **_kwargs) -> object:
                self.owner.goto_urls.append(url)
                self.url = url
                request_callback = self.listeners.get("request")
                response_callback = self.listeners.get("response")
                if url == CREATOR_HOME:
                    for response in self.owner.responses_by_url[url]:
                        if request_callback is not None:
                            request_callback(response.request)
                        if response_callback is not None:
                            response_callback(response)
                    if request_callback is not None:
                        request_callback(delayed.request)
                elif url == DATA_ANALYSIS_URL:
                    for response in self.owner.responses_by_url[url]:
                        if request_callback is not None:
                            request_callback(response.request)
                        if response_callback is not None:
                            response_callback(response)
                elif url == _detail_url() and response_callback is not None:
                    response_callback(delayed)
                return _FakeNavigation(url)

        collector, fake = self._collector_with_responses(_reviewed_success_responses())
        fake.page = _LatePage(fake)
        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()),
            "browser_signature_timeout",
        )
        self.assertEqual(fake.goto_urls, [CREATOR_HOME, DATA_ANALYSIS_URL, _detail_url()])

    def test_duplicate_response_is_rejected(self) -> None:
        """同一详情响应重复进入时若静默取一份，会掩盖响应去重故障。"""

        responses = _reviewed_success_responses()
        duplicate = responses[_detail_url()][0]
        responses[_detail_url()] = (duplicate, duplicate)
        collector, _fake = self._collector_with_responses(responses)
        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()),
            "metric_payload_invalid",
        )

    def test_content_payload_failure_returns_account_only_batch(self) -> None:
        """作品详情坏掉时若整个账号指标丢失，会放大局部响应故障。"""

        responses = _reviewed_success_responses()
        responses[_detail_url()] = (
            _FakeResponse(
                "https://creator.xiaohongshu.com/api/galaxy/creator/"
                "datacenter/note/base?noteId=private",
                {"data": {"note_info": {"id": "7b0ffca800000000080033f8"}}},
                request_url=(
                    "https://creator.xiaohongshu.com/api/galaxy/creator/"
                    f"datacenter/note/base?noteId={_CONTENT_ID}"
                ),
            ),
        )
        collector, _fake = self._collector_with_responses(responses)

        batch = collector.collect_browser_signed(_account())

        self.assertTrue(batch.account_metrics_available)
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.contents, ())
        self.assertEqual(batch.warning_code, "content_payload_invalid")

    def test_detail_request_identity_must_match_list_identity_even_when_payload_matches(self) -> None:
        """详情 payload 自报同一 ID 不足以证明浏览器请求没有被串到另一条作品。"""

        responses = _reviewed_success_responses()
        responses[_detail_url()] = (
            _FakeResponse(
                "https://creator.xiaohongshu.com/api/galaxy/creator/"
                "datacenter/note/base?noteId=private",
                {
                    "data": {
                        "note_info": {"id": _CONTENT_ID, "view_count": 148},
                    }
                },
                request_url=(
                    "https://creator.xiaohongshu.com/api/galaxy/creator/"
                    "datacenter/note/base?noteId=7b0ffca800000000080033f8"
                ),
            ),
        )
        collector, _fake = self._collector_with_responses(responses)

        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()),
            "metric_payload_invalid",
        )

    def test_total_timeout_returns_fixed_error_after_cleanup(self) -> None:
        """总时限越界若继续等待，会让短会话失去确定的资源上限。"""

        fake = _FakeRuntime(_reviewed_success_responses())
        collector = XiaohongshuDataCollector(
            browser_factory=lambda: _FakeStarter(fake),
            utc_now=lambda: datetime(2026, 8, 21, tzinfo=timezone.utc),
            monotonic=_Clock((0.0, 0.0, 0.0, 0.0, 0.0, 56.0, 56.0)),
        )
        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()),
            "browser_signature_timeout",
        )
        self.assertEqual(fake.cleanup, ["page", "context", "browser", "playwright"])

    def test_process_control_exceptions_propagate_only_after_cleanup(self) -> None:
        """进程控制异常若跳过 finally，会遗留浏览器和会话上下文。"""

        for error_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(error_type=error_type.__name__):
                collector, fake = self._collector_with_responses(
                    {}, goto_failure=error_type()
                )
                with self.assertRaises(error_type):
                    collector.collect_browser_signed(_account())
                self.assertEqual(fake.cleanup, ["page", "context", "browser", "playwright"])

    def test_registry_exposes_xiaohongshu_only_with_a_real_factory(self) -> None:
        """登记项若是空占位，界面会把不可采集的平台显示为可同步。"""

        self.assertEqual(registered_platform_types(), (1, 3))
        self.assertIsInstance(
            collector_for_platform(1, browser_factory=lambda: _FakeStarter(_FakeRuntime({}))),
            XiaohongshuDataCollector,
        )
