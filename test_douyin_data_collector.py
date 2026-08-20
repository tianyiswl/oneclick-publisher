# -*- coding: utf-8 -*-
"""抖音登录会话只读数据采集器测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core.douyin_data_collector import (
    DOUYIN_DASHBOARD_URL,
    DOUYIN_DATA_PAGE_URL,
    DouyinDataCollectionError,
    DouyinDataCollector,
)
from app_core.platform_data_collectors import collector_for_platform
from app_core.platform_data_models import CollectionFailure


class FakeResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class FakeCookieJar:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str, str]] = []

    def set(self, name: str, value: str, *, domain: str, path: str) -> None:
        self.entries.append((name, value, domain, path))


class FakeSession:
    def __init__(
        self,
        payload: object,
        *,
        status_code: int = 200,
        error: BaseException | None = None,
    ) -> None:
        self.cookies = FakeCookieJar()
        self.headers: dict[str, str] = {}
        self.payload = payload
        self.status_code = status_code
        self.error = error
        self.calls: list[tuple[str, dict, float]] = []
        self.closed = 0

    def post(self, url: str, *, json: dict, timeout: float) -> FakeResponse:
        self.calls.append((url, dict(json), timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload, status_code=self.status_code)

    def close(self) -> None:
        self.closed += 1


class DouyinDirectCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.cookie_dir = Path(self.tempdir.name)
        self.state_file = self.cookie_dir / "oneclick_3_test.json"
        self.state_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "sessionid",
                            "value": "private-session",
                            "domain": ".creator.douyin.com",
                            "path": "/",
                        },
                        {
                            "name": "shared",
                            "value": "allowed",
                            "domain": ".douyin.com",
                            "path": "/",
                        },
                        {
                            "name": "foreign",
                            "value": "must-not-leak",
                            "domain": ".example.com",
                            "path": "/",
                        },
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )
        self.account = {
            "id": 12,
            "type": 3,
            "filePath": self.state_file.name,
            "userName": "不进入公开载荷",
        }
        self.cookie_patch = patch(
            "app_core.douyin_data_collector.COOKIE_DIR",
            self.cookie_dir,
        )
        self.cookie_patch.start()

    def tearDown(self) -> None:
        self.cookie_patch.stop()
        self.tempdir.cleanup()

    @staticmethod
    def _valid_payload() -> dict:
        return {
            "status_code": 0,
            "status_msg": "",
            "metrics": [
                {
                    "english_metric_name": "play_cnt",
                    "trends": [{"date_time": "20260820", "value": 125}],
                },
                {
                    "english_metric_name": "digg_cnt",
                    "trends": [{"date_time": "20260820", "value": 9}],
                },
                {
                    "english_metric_name": "private_cookie_value",
                    "trends": [{"date_time": "20260820", "value": 999}],
                },
            ],
        }

    def test_valid_direct_payload_is_normalized_and_cookie_domain_is_isolated(self) -> None:
        """放宽 Cookie 域或指标白名单会泄露外域会话或未知字段。"""

        session = FakeSession(self._valid_payload())
        collector = DouyinDataCollector(session_factory=lambda: session)

        batch = collector.collect_direct(self.account)

        self.assertEqual(batch.platform_type, 3)
        self.assertEqual(batch.source_mode, "direct_session")
        self.assertEqual(
            [
                (
                    item.metric_key,
                    item.metric_value,
                    item.metric_scope,
                    item.period_start,
                    item.period_end,
                )
                for item in batch.metrics
            ],
            [
                ("views", 125, "daily_increment", "2026-08-20", "2026-08-20"),
                ("likes", 9, "daily_increment", "2026-08-20", "2026-08-20"),
            ],
        )
        self.assertTrue(batch.account_metrics_available)
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.contents, ())
        self.assertEqual(
            [(name, domain) for name, _value, domain, _path in session.cookies.entries],
            [
                ("sessionid", ".creator.douyin.com"),
                ("shared", ".douyin.com"),
            ],
        )
        self.assertEqual(
            session.calls,
            [(DOUYIN_DASHBOARD_URL, {"recent_days": 30}, 20.0)],
        )
        self.assertEqual(session.closed, 1)

    def test_account_batch_marks_content_list_unavailable_without_speculative_request(self) -> None:
        """尚未验证作品接口时，只能保留账户数据，不能猜测补采请求。"""

        session = FakeSession(self._valid_payload())
        batch = DouyinDataCollector(
            session_factory=lambda: session
        ).collect_direct(self.account)

        self.assertTrue(batch.account_metrics_available)
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.contents, ())
        self.assertEqual(batch.warning_code, "content_list_unavailable")
        self.assertEqual(
            session.calls,
            [(DOUYIN_DASHBOARD_URL, {"recent_days": 30}, 20.0)],
        )

    def test_all_daily_trend_points_are_preserved(self) -> None:
        """只读取最后一天会丢失历史日趋势，本测试必须失败。"""

        raw_dates = [
            "20260722",
            "20260723",
            "20260724",
            "20260725",
            "20260726",
            "20260727",
            "20260728",
            "20260729",
            "20260730",
            "20260731",
            "20260801",
            "20260802",
            "20260803",
            "20260804",
            "20260805",
            "20260806",
            "20260807",
            "20260808",
            "20260809",
            "20260810",
            "20260811",
            "20260812",
            "20260813",
            "20260814",
            "20260815",
            "20260816",
            "20260817",
            "20260818",
            "20260819",
            "20260820",
        ]
        expected_days = [
            "2026-07-22",
            "2026-07-23",
            "2026-07-24",
            "2026-07-25",
            "2026-07-26",
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
            "2026-08-01",
            "2026-08-02",
            "2026-08-03",
            "2026-08-04",
            "2026-08-05",
            "2026-08-06",
            "2026-08-07",
            "2026-08-08",
            "2026-08-09",
            "2026-08-10",
            "2026-08-11",
            "2026-08-12",
            "2026-08-13",
            "2026-08-14",
            "2026-08-15",
            "2026-08-16",
            "2026-08-17",
            "2026-08-18",
            "2026-08-19",
            "2026-08-20",
        ]
        payload = {
            "status_code": 0,
            "metrics": [
                {
                    "english_metric_name": "play_cnt",
                    "trends": [
                        {"date_time": raw_date, "value": index}
                        for index, raw_date in enumerate(raw_dates, start=1)
                    ],
                }
            ],
        }
        batch = DouyinDataCollector(
            session_factory=lambda: FakeSession(payload)
        ).collect_direct(self.account)

        points = [
            point for point in batch.metrics if point.metric_key == "views"
        ]
        self.assertEqual(len(points), 30)
        self.assertEqual(
            [point.period_start for point in points], expected_days
        )
        self.assertTrue(
            all(point.period_end == point.period_start for point in points)
        )
        self.assertTrue(
            all(point.metric_scope == "daily_increment" for point in points)
        )
        self.assertEqual(
            len(
                {
                    (point.metric_key, point.metric_scope, point.period_start)
                    for point in points
                }
            ),
            30,
        )

    def test_daily_trend_rejects_invalid_or_duplicate_dates(self) -> None:
        """伪日期、重复日和弱类型数值都不能伪装成日趋势。"""

        invalid_cases = {
            "missing_date": [
                {"value": 1},
            ],
            "invalid_date": [
                {"date_time": "20260230", "value": 1},
            ],
            "duplicate_date": [
                {"date_time": "20260820", "value": 1},
                {"date_time": "20260820", "value": 2},
            ],
            "boolean_value": [
                {"date_time": "20260820", "value": True},
            ],
            "numeric_string": [
                {"date_time": "20260820", "value": "1"},
            ],
        }

        for name, trends in invalid_cases.items():
            with self.subTest(name=name):
                payload = {
                    "status_code": 0,
                    "metrics": [
                        {
                            "english_metric_name": "play_cnt",
                            "trends": trends,
                        }
                    ],
                }
                collector = DouyinDataCollector(
                    session_factory=lambda current=payload: FakeSession(current)
                )

                with self.assertRaises(DouyinDataCollectionError) as raised:
                    collector.collect_direct(self.account)

                self.assertEqual(
                    raised.exception.error_code, "metric_payload_invalid"
                )
                self.assertFalse(raised.exception.fallback_allowed)
                self.assertIsNone(raised.exception.__cause__)

    def test_http_200_with_nested_login_rejection_is_not_success(self) -> None:
        """真实探测中的内层 status_code=8 必须进入登录失败而非空指标。"""

        session = FakeSession(
            {
                "status_code": 0,
                "data": {
                    "play": {
                        "status_code": 8,
                        "status_message": "用户未登录",
                    }
                },
            }
        )
        collector = DouyinDataCollector(session_factory=lambda: session)

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_direct(self.account)

        self.assertEqual(raised.exception.error_code, "login_required")
        self.assertTrue(raised.exception.fallback_allowed)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(session.closed, 1)

    def test_empty_success_envelope_is_not_a_zero_metric_sync(self) -> None:
        """空 metrics 若被当成功，UI 会把无数据误解为业务零值。"""

        collector = DouyinDataCollector(
            session_factory=lambda: FakeSession(
                {"status_code": 0, "status_msg": "", "metrics": [{}]}
            )
        )

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_direct(self.account)

        self.assertEqual(raised.exception.error_code, "metric_payload_empty")
        self.assertTrue(raised.exception.fallback_allowed)

    def test_invalid_metric_values_fail_closed(self) -> None:
        """布尔值和字符串数字不能穿过平台响应边界。"""

        for value in (True, "125"):
            with self.subTest(value=value):
                payload = self._valid_payload()
                payload["metrics"][0]["trends"][0]["value"] = value
                collector = DouyinDataCollector(
                    session_factory=lambda current=payload: FakeSession(current)
                )
                with self.assertRaises(DouyinDataCollectionError) as raised:
                    collector.collect_direct(self.account)
                self.assertEqual(
                    raised.exception.error_code,
                    "metric_payload_invalid",
                )
                self.assertFalse(raised.exception.fallback_allowed)

    def test_untrusted_transport_error_is_fixed_and_redacted(self) -> None:
        """网络异常原文不得携带 Cookie、URL 或路径离开采集器。"""

        session = FakeSession(
            {},
            error=RuntimeError(
                "Cookie=sessionid=secret "
                "https://creator.douyin.com/?token=secret "
                f"{self.state_file}"
            ),
        )
        collector = DouyinDataCollector(session_factory=lambda: session)

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_direct(self.account)

        self.assertEqual(
            raised.exception.error_code,
            "direct_request_rejected",
        )
        self.assertFalse(raised.exception.fallback_allowed)
        self.assertEqual(str(raised.exception), "direct_request_rejected")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(session.closed, 1)

    def test_registry_rejects_non_douyin_and_non_builtin_platform_ids(self) -> None:
        """注册表不能为未知平台伪造空采集器。"""

        for platform_type in (1, True, "3"):
            with self.subTest(platform_type=platform_type):
                with self.assertRaises(CollectionFailure) as raised:
                    collector_for_platform(platform_type)
                self.assertEqual(
                    raised.exception.error_code,
                    "collector_not_available",
                )


class FakeBrowserResponse:
    def __init__(self, url: str, payload: object) -> None:
        self.url = url
        self._payload = payload

    async def json(self) -> object:
        return self._payload


class FakeBrowserPage:
    def __init__(
        self,
        response: FakeBrowserResponse | None,
        *,
        final_url: str = "https://creator.douyin.com/creator-micro/home",
    ) -> None:
        self.response = response
        self.url = final_url
        self.listeners: dict[str, object] = {}
        self.goto_calls: list[tuple[str, str, float]] = []
        self.closed = 0

    def on(self, event: str, callback) -> None:
        self.listeners[event] = callback

    async def goto(
        self,
        url: str,
        *,
        wait_until: str,
        timeout: float,
    ) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        if self.response is not None:
            self.listeners["response"](self.response)
            await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed += 1


class FakeBrowserContext:
    def __init__(
        self,
        page: FakeBrowserPage,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self.page = page
        self.close_error = close_error
        self.storage_states: list[str] = []
        self.closed = 0

    async def new_page(self) -> FakeBrowserPage:
        return self.page

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class FakeBrowser:
    def __init__(self, context: FakeBrowserContext) -> None:
        self.context = context
        self.launch_context_calls: list[str] = []
        self.closed = 0

    async def new_context(self, *, storage_state: str) -> FakeBrowserContext:
        self.launch_context_calls.append(storage_state)
        return self.context

    async def close(self) -> None:
        self.closed += 1


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.launch_calls: list[dict] = []

    async def launch(self, **options) -> FakeBrowser:
        self.launch_calls.append(dict(options))
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stopped = 0

    async def stop(self) -> None:
        self.stopped += 1


class FakePlaywrightStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> FakePlaywright:
        return self.playwright


class DouyinBrowserSignedCollectorTests(DouyinDirectCollectorTests):
    @staticmethod
    def _current_overview_payload() -> dict:
        return {
            "status_code": 0,
            "status_message": "",
            "data": {
                "play": {
                    "status_code": 0,
                    "current_count": 230,
                    "option_list": [
                        {"date": "20260819", "count": 100},
                        {"date": "20260820", "count": 230},
                    ],
                },
                "digg": {
                    "status_code": 0,
                    "current_count": 18,
                    "option_list": [{"date": "20260820", "count": 18}],
                },
                "private_metric": {
                    "status_code": 0,
                    "current_count": 999,
                },
            },
        }

    def _collector_with_browser(
        self,
        response: FakeBrowserResponse | None,
        *,
        final_url: str = "https://creator.douyin.com/creator-micro/home",
        close_error: BaseException | None = None,
        timeout: float = 0.05,
    ):
        page = FakeBrowserPage(response, final_url=final_url)
        context = FakeBrowserContext(page, close_error=close_error)
        browser = FakeBrowser(context)
        playwright = FakePlaywright(browser)
        collector = DouyinDataCollector(
            session_factory=lambda: FakeSession({}),
            playwright_factory=lambda: FakePlaywrightStarter(playwright),
            browser_timeout_seconds=timeout,
        )
        return collector, page, context, browser, playwright

    def test_browser_option_list_preserves_daily_scope_and_proven_total(self) -> None:
        """浏览器趋势必须逐日保留，只有带日期的 fans 当前数才是总粉丝。"""

        payload = {
            "status_code": 0,
            "data": {
                "play": {
                    "status_code": 0,
                    "current_count": 999,
                    "option_list": [
                        {"date": "2026-08-19", "count": 100},
                        {"date": "2026-08-20", "count": 230},
                    ],
                },
                "fans": {
                    "status_code": 0,
                    "current_count": 431,
                    "option_list": [
                        {"date": "2026-08-19", "count": 11},
                        {"date": "2026-08-20", "count": 12},
                    ],
                },
                "digg": {
                    "status_code": 0,
                    "current_count": 18,
                },
            },
        }
        response = FakeBrowserResponse(
            "https://creator.douyin.com/aweme/janus/creator/data/overview/all/",
            payload,
        )
        collector, _page, _context, _browser, _playwright = (
            self._collector_with_browser(response)
        )

        batch = collector.collect_browser_signed(self.account)

        self.assertEqual(
            [
                (
                    point.metric_key,
                    point.metric_value,
                    point.metric_scope,
                    point.period_start,
                    point.period_end,
                )
                for point in batch.metrics
            ],
            [
                ("views", 100, "daily_increment", "2026-08-19", "2026-08-19"),
                ("views", 230, "daily_increment", "2026-08-20", "2026-08-20"),
                (
                    "followers_net",
                    11,
                    "daily_increment",
                    "2026-08-19",
                    "2026-08-19",
                ),
                (
                    "followers_net",
                    12,
                    "daily_increment",
                    "2026-08-20",
                    "2026-08-20",
                ),
                (
                    "followers_total",
                    431,
                    "lifetime_total",
                    "2026-08-20",
                    "2026-08-20",
                ),
            ],
        )
        self.assertTrue(batch.account_metrics_available)
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.contents, ())
        self.assertEqual(
            len(
                {
                    (point.metric_key, point.metric_scope, point.period_start)
                    for point in batch.metrics
                }
            ),
            len(batch.metrics),
        )

    def test_browser_signed_accepts_only_allowlisted_official_response_and_closes(self) -> None:
        """官方响应路径或严格关闭缺失时，本测试必须失败。"""

        response = FakeBrowserResponse(
            "https://creator.douyin.com/aweme/janus/creator/data/overview/all/"
            "?msToken=private",
            self._current_overview_payload(),
        )
        collector, page, context, browser, playwright = (
            self._collector_with_browser(response)
        )

        batch = collector.collect_browser_signed(self.account)

        self.assertEqual(batch.source_mode, "browser_signed")
        self.assertEqual(batch.warning_code, "content_list_unavailable")
        self.assertEqual(
            [
                (
                    point.metric_key,
                    point.metric_value,
                    point.metric_scope,
                    point.period_start,
                    point.period_end,
                )
                for point in batch.metrics
            ],
            [
                ("views", 100, "daily_increment", "2026-08-19", "2026-08-19"),
                ("views", 230, "daily_increment", "2026-08-20", "2026-08-20"),
                ("likes", 18, "daily_increment", "2026-08-20", "2026-08-20"),
            ],
        )
        self.assertEqual(page.closed, 1)
        self.assertEqual(context.closed, 1)
        self.assertEqual(browser.closed, 1)
        self.assertEqual(playwright.stopped, 1)
        self.assertEqual(playwright.chromium.launch_calls, [{"headless": True}])
        self.assertEqual(
            page.goto_calls,
            [(DOUYIN_DATA_PAGE_URL, "domcontentloaded", 50.0)],
        )

    def test_non_allowlisted_response_times_out_without_being_parsed(self) -> None:
        """同域未知接口不能被当作数据指标来源。"""

        response = FakeBrowserResponse(
            "https://creator.douyin.com/web/api/media/aweme/create/",
            self._current_overview_payload(),
        )
        collector, _page, _context, _browser, _playwright = (
            self._collector_with_browser(response, timeout=0.01)
        )

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_browser_signed(self.account)

        self.assertEqual(
            raised.exception.error_code,
            "browser_signature_timeout",
        )
        self.assertIsNone(raised.exception.__cause__)

    def test_verification_page_returns_login_required(self) -> None:
        """验证页必须停止，不能等待超时或尝试页面内请求。"""

        collector, page, context, browser, playwright = (
            self._collector_with_browser(
                None,
                final_url="https://creator.douyin.com/verification",
            )
        )

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_browser_signed(self.account)

        self.assertEqual(raised.exception.error_code, "login_required")
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)

    def test_cleanup_failure_overrides_apparent_metric_success(self) -> None:
        """资源未严格归零时不得写入刚捕获的指标。"""

        response = FakeBrowserResponse(
            "https://creator.douyin.com/aweme/janus/creator/data/overview/all/",
            self._current_overview_payload(),
        )
        collector, page, context, browser, playwright = (
            self._collector_with_browser(
                response,
                close_error=RuntimeError("sensitive browser path"),
            )
        )

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_browser_signed(self.account)

        self.assertEqual(
            raised.exception.error_code,
            "browser_cleanup_incomplete",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)


if __name__ == "__main__":
    unittest.main()
