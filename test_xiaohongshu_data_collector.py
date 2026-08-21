# -*- coding: utf-8 -*-
"""小红书已审核响应的纯 payload 转换测试。"""

from __future__ import annotations

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
            observed_at=_OBSERVED_AT,
            platform_day=_PLATFORM_DAY,
        )

        self.assertEqual(
            [(point.metric_key, point.metric_value, point.metric_scope) for point in points],
            [("followers_total", 3199, "lifetime_total")],
        )
        self.assertEqual(points[0].entity_type, "account")
        self.assertEqual(points[0].entity_key, "account")
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
    pass


class _FakeResponse:
    def __init__(
        self,
        url: str,
        payload: object | None = None,
        *,
        body: bytes | None = None,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.url = url
        self.request = _FakeRequest()
        self.status = status
        self._body = body if body is not None else json.dumps(payload).encode("utf-8")
        self.headers = {
            "content-type": content_type,
            "content-length": str(len(self._body)),
        }

    async def body(self) -> bytes:
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
        return None

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
    ) -> None:
        self.responses_by_url = responses_by_url
        self.final_urls = final_urls or {}
        self.goto_failure = goto_failure
        self.page_close_error = page_close_error
        self.context_close_error = context_close_error
        self.browser_close_error = browser_close_error
        self.playwright_close_error = playwright_close_error
        self.goto_urls: list[str] = []
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


def _account() -> dict:
    return {"id": 21, "type": 1, "filePath": "oneclick_1_test.json"}


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
                "datacenter/note/base?note_id=private",
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
        fake = _FakeRuntime(responses, **runtime_kwargs)
        return (
            XiaohongshuDataCollector(
                browser_factory=lambda: _FakeStarter(fake),
                utc_now=lambda: datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc),
                monotonic=_Clock(),
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
            fake.goto_urls, [CREATOR_HOME, DATA_ANALYSIS_URL, _detail_url()]
        )
        self.assertEqual(fake.cleanup, ["page", "context", "browser", "playwright"])

    def test_cleanup_failure_never_returns_success(self) -> None:
        """资源关闭报错若被吞掉，会把不完整会话误写成采集成功。"""

        collector, fake = self._collector_with_responses(
            _reviewed_success_responses(),
            context_close_error=RuntimeError("private"),
        )

        self.assert_collection_error(
            lambda: collector.collect_browser_signed(_account()),
            "browser_cleanup_incomplete",
        )
        self.assertIn("browser", fake.cleanup)
        self.assertIn("playwright", fake.cleanup)

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

        for redirected_url in (
            "https://creator.xiaohongshu.com/login",
            "https://creator.xiaohongshu.com/creator/security/verification",
        ):
            with self.subTest(redirected_url=redirected_url):
                collector, fake = self._collector_with_responses(
                    {}, final_urls={CREATOR_HOME: redirected_url}
                )
                self.assert_collection_error(
                    lambda: collector.collect_browser_signed(_account()),
                    "login_required",
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
        batch = collector.collect_browser_signed(_account())
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.warning_code, "content_payload_invalid")
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
                "https://creator.xiaohongshu.com/api/galaxy/creator/datacenter/note/base",
                {"data": {"note_info": {"id": "7b0ffca800000000080033f8"}}},
            ),
        )
        collector, _fake = self._collector_with_responses(responses)

        batch = collector.collect_browser_signed(_account())

        self.assertTrue(batch.account_metrics_available)
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.contents, ())
        self.assertEqual(batch.warning_code, "content_payload_invalid")

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
