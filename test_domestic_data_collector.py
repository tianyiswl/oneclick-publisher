# -*- coding: utf-8 -*-
"""国内平台共用短会话采集器的安全边界测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from app_core.domestic_data_collector import (
    DomesticBrowserCollector,
    DomesticCollectorConfig,
)
from app_core.platform_data_collection_errors import PlatformDataCollectionError
from app_core.platform_data_models import CollectionBatch, MetricPoint


def config(**overrides) -> DomesticCollectorConfig:
    values = {
        "platform_type": 2,
        "home_url": "https://official.example/home",
        "allowed_hosts": frozenset({"official.example"}),
        "endpoint_by_path": {"/reviewed": "account_base"},
        "phase_by_path": {"/reviewed": "account"},
        "total_timeout_seconds": 0.05,
        "phase_settle_milliseconds": 1,
    }
    values.update(overrides)
    return DomesticCollectorConfig(**values)


def account() -> dict:
    return {"id": 9, "type": 2, "filePath": "state.json"}


def parsed_batch(account_id: int, captures: tuple) -> CollectionBatch:
    if account_id != 9 or not captures:
        raise ValueError("test parser requires one reviewed capture")
    return CollectionBatch(
        platform_type=2,
        source_mode="browser_signed",
        metrics=(
            MetricPoint(
                entity_type="account",
                entity_key="account:9",
                metric_key="views",
                raw_metric_key="views",
                metric_value=1,
                metric_unit="count",
                metric_scope="daily_increment",
                period_start="2026-08-21",
                period_end="2026-08-21",
                observed_at="2026-08-21T00:00:00+00:00",
            ),
        ),
        contents=(),
        account_metrics_available=True,
        content_data_available=False,
        platform_observed_at="2026-08-21T00:00:00+00:00",
        warning_code="content_list_unavailable",
    )


class FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeResponse:
    def __init__(
        self,
        url: str,
        payload: object,
        *,
        content_length: str | None = None,
    ) -> None:
        self.url = url
        self.request = FakeRequest(url)
        self.status = 200
        self.headers = {"content-type": "application/json"}
        if content_length is not None:
            self.headers["content-length"] = content_length
        self._body = json.dumps(payload).encode("utf-8")

    async def body(self) -> bytes:
        return self._body


class FakePage:
    def __init__(self, responses: tuple[FakeResponse, ...], final_url: str) -> None:
        self.responses = responses
        self.url = final_url
        self.listeners: dict[str, object] = {}
        self.closed = False

    def on(self, event: str, callback) -> None:
        self.listeners[event] = callback

    async def goto(self, _url: str, *, wait_until: str, timeout: float) -> None:
        for response in self.responses:
            self.listeners["request"](response.request)
            self.listeners["response"](response)
        await asyncio.sleep(0)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True


class CompletedResponsePage(FakePage):
    async def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
        await super().goto(url, wait_until=wait_until, timeout=timeout)
        await asyncio.sleep(0)


class FakeHrefLocator:
    def __init__(self, hrefs: tuple[str, ...]) -> None:
        self.hrefs = hrefs

    async def evaluate_all(self, _script: str) -> list[str]:
        return list(self.hrefs)


class DynamicNavigationPage(FakePage):
    def __init__(
        self,
        *,
        hrefs: tuple[str, ...],
        responses_by_path: dict[str, tuple[FakeResponse, ...]],
    ) -> None:
        super().__init__((), "https://official.example/home")
        self.hrefs = hrefs
        self.responses_by_path = responses_by_path
        self.goto_urls: list[str] = []

    def locator(self, selector: str) -> FakeHrefLocator:
        if selector != "a[href]":
            raise AssertionError("unexpected selector")
        return FakeHrefLocator(self.hrefs)

    async def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
        self.goto_urls.append(url)
        self.url = url
        for response in self.responses_by_path.get(urlsplit(url).path, ()):
            self.listeners["request"](response.request)
            self.listeners["response"](response)
        await asyncio.sleep(0)


class DelayedDynamicNavigationPage(DynamicNavigationPage):
    async def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
        self.goto_urls.append(url)
        self.url = url
        responses = self.responses_by_path.get(urlsplit(url).path, ())

        async def emit() -> None:
            await asyncio.sleep(0.001)
            for response in responses:
                self.listeners["request"](response.request)
                self.listeners["response"](response)

        asyncio.create_task(emit())
        await asyncio.sleep(0)


class StaggeredResponsePage(DynamicNavigationPage):
    async def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
        self.goto_urls.append(url)
        self.url = url
        responses = self.responses_by_path.get(urlsplit(url).path, ())

        async def emit(response: FakeResponse, delay: float) -> None:
            await asyncio.sleep(delay)
            self.listeners["request"](response.request)
            self.listeners["response"](response)

        for index, response in enumerate(responses, start=1):
            asyncio.create_task(emit(response, index * 0.003))
        await asyncio.sleep(0)

    async def wait_for_timeout(self, milliseconds: int) -> None:
        await asyncio.sleep(milliseconds / 1000)


class CrossPhaseLateResponsePage(DynamicNavigationPage):
    """账号请求先发出，响应在文章阶段到达，文章响应随后才到。"""

    async def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
        self.goto_urls.append(url)
        self.url = url
        path = urlsplit(url).path
        if path == "/account-page":
            first, late = self.responses_by_path[path]
            self.listeners["request"](first.request)
            self.listeners["response"](first)
            self.listeners["request"](late.request)

            async def emit_late_account() -> None:
                await asyncio.sleep(0.003)
                self.listeners["response"](late)

            asyncio.create_task(emit_late_account())
        elif path == "/content-page":
            content = self.responses_by_path[path][0]

            async def emit_content() -> None:
                await asyncio.sleep(0.008)
                self.listeners["request"](content.request)
                self.listeners["response"](content)

            asyncio.create_task(emit_content())
        await asyncio.sleep(0)

    async def wait_for_timeout(self, milliseconds: int) -> None:
        # Compress the collector's poll interval while preserving event order.
        await asyncio.sleep(0.001)


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.closed = False

    async def new_context(self, *, storage_state: str) -> FakeContext:
        return self.context

    async def close(self) -> None:
        self.closed = True

    @property
    def alive_resource_count(self) -> int:
        return int(not (self.closed and self.context.closed and self.context.page.closed))


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser

    async def launch(self, *, headless: bool) -> FakeBrowser:
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeStarter:
    def __init__(self, browser: FakeBrowser) -> None:
        self.playwright = FakePlaywright(browser)

    async def start(self) -> FakePlaywright:
        return self.playwright


def fake_browser(
    *,
    response_url: str = "https://official.example/reviewed",
    payload: object = None,
    response_count: int = 1,
    content_length: str | None = None,
    final_url: str = "https://official.example/home",
) -> FakeStarter:
    responses = tuple(
        FakeResponse(response_url, payload, content_length=content_length)
        for _ in range(response_count)
    )
    return FakeStarter(FakeBrowser(FakeContext(FakePage(responses, final_url))))


class DomesticBrowserCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.cookie_dir = Path(self.tempdir.name)
        (self.cookie_dir / "state.json").write_text("{}", encoding="utf-8")
        self.cookie_patch = patch("app_core.domestic_data_collector.COOKIE_DIR", self.cookie_dir)
        self.cookie_patch.start()

    def tearDown(self) -> None:
        self.cookie_patch.stop()
        self.tempdir.cleanup()

    def test_collector_rejects_unreviewed_host(self) -> None:
        """移除白名单判断会把非审核域的正文交给解析器。"""
        collector = DomesticBrowserCollector(
            config(),
            browser_factory=lambda: fake_browser(
                response_url="https://evil.example/metrics", payload={"data": {}}
            ),
            parse_captures=parsed_batch,
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "metric_payload_empty")

    def test_collector_closes_browser_after_parser_failure(self) -> None:
        """移除 finally 清理会在解析失败时遗留浏览器资源。"""
        starter = fake_browser(payload={"unexpected": True})
        browser = starter.playwright.chromium.browser

        def parser_failure(_account_id: int, _captures: tuple) -> CollectionBatch:
            raise ValueError("parser rejected payload")

        collector = DomesticBrowserCollector(
            config(), browser_factory=lambda: starter, parse_captures=parser_failure
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")
        self.assertEqual(browser.alive_resource_count, 0)

    def test_collector_passes_only_reviewed_capture_to_parser(self) -> None:
        """错误路径或阶段映射会让解析器收到不受控的采集项。"""
        seen: list[tuple[int, tuple]] = []

        def parser(account_id: int, captures: tuple) -> CollectionBatch:
            seen.append((account_id, captures))
            return parsed_batch(account_id, captures)

        collector = DomesticBrowserCollector(
            config(),
            browser_factory=lambda: fake_browser(payload={"data": {}}),
            parse_captures=parser,
        )

        batch = collector.collect(account())

        self.assertEqual(batch.source_mode, "browser_signed")
        self.assertEqual(len(seen), 1)
        captured = seen[0][1][0]
        self.assertEqual((captured.endpoint, captured.phase, captured.path), ("account_base", "account", "/reviewed"))
        self.assertEqual(captured.payload, {"data": {}})

    def test_collector_stops_on_login_redirect(self) -> None:
        """缺少登录跳转检查会把未登录页面误判为采集超时。"""
        collector = DomesticBrowserCollector(
            config(),
            browser_factory=lambda: fake_browser(final_url="https://official.example/login"),
            parse_captures=parsed_batch,
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "login_required")

    def test_collector_stops_on_verification_page(self) -> None:
        """缺少验证页检查会继续读取需人工确认的页面。"""
        collector = DomesticBrowserCollector(
            config(),
            browser_factory=lambda: fake_browser(final_url="https://official.example/verification"),
            parse_captures=parsed_batch,
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "verification_required")

    def test_collector_rejects_response_larger_than_configured_limit(self) -> None:
        """移除长度上限会让大响应进入解析器。"""
        collector = DomesticBrowserCollector(
            config(max_response_bytes=8),
            browser_factory=lambda: fake_browser(payload={"data": {}}, content_length="9"),
            parse_captures=parsed_batch,
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_collector_keeps_completed_response_task_failures(self) -> None:
        """若完成任务被过早丢弃，goto 内的坏响应会被错误报告为超时。"""
        page = CompletedResponsePage(
            (FakeResponse("https://official.example/reviewed", {"data": {}}),),
            "https://official.example/home",
        )
        starter = FakeStarter(FakeBrowser(FakeContext(page)))
        collector = DomesticBrowserCollector(
            config(max_response_bytes=8),
            browser_factory=lambda: starter,
            parse_captures=parsed_batch,
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_collector_rejects_response_bound_to_a_different_navigation_phase(self) -> None:
        """若只按路径贴标签，账号页提前出现的列表响应会串入列表解析。"""
        page = FakePage(
            (FakeResponse("https://official.example/list", {"data": {}}),),
            "https://official.example/home",
        )
        starter = FakeStarter(FakeBrowser(FakeContext(page)))
        collector = DomesticBrowserCollector(
            config(
                endpoint_by_path={
                    "/reviewed": "account_base",
                    "/list": "content_list",
                },
                phase_by_path={"/reviewed": "account", "/list": "list"},
                navigation_by_phase={
                    "account": "https://official.example/home",
                    "list": "https://official.example/list-page",
                },
            ),
            browser_factory=lambda: starter,
            parse_captures=parsed_batch,
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_collector_resolves_one_reviewed_dynamic_navigation_link(self) -> None:
        """若动态链接不能按固定 host/path 唯一解析，公众号登录态页面无法安全进入数据页。"""

        private_link = "https://official.example/metrics-page?action=view&token=private-token"
        page = DynamicNavigationPage(
            hrefs=(private_link,),
            responses_by_path={
                "/home": (
                    FakeResponse("https://official.example/home-data", {"home": {}}),
                ),
                "/metrics-page": (
                    FakeResponse("https://official.example/metrics", {"data": {}}),
                ),
            },
        )
        seen: list[tuple] = []

        def parser(account_id: int, captures: tuple) -> CollectionBatch:
            seen.append(captures)
            return parsed_batch(account_id, captures)

        collector = DomesticBrowserCollector(
            config(
                endpoint_by_path={
                    "/home-data": "account_home",
                    "/metrics": "account_base",
                },
                phase_by_path={"/home-data": "home", "/metrics": "account"},
                navigation_by_phase={
                    "home": "https://official.example/home",
                    "account": "/metrics-page",
                },
            ),
            browser_factory=lambda: FakeStarter(FakeBrowser(FakeContext(page))),
            parse_captures=parser,
        )

        collector.collect(account())

        self.assertEqual(page.goto_urls, ["https://official.example/home", private_link])
        self.assertEqual(
            [(capture.phase, capture.path) for capture in seen[0]],
            [("home", "/home-data"), ("account", "/metrics")],
        )
        self.assertEqual(
            collector._config.navigation_by_phase["account"], "/metrics-page"
        )

    def test_collector_allows_an_uncaptured_bootstrap_navigation_phase(self) -> None:
        """登录首页只负责提供受控链接时，不应强迫平台伪造首页数据端点。"""

        private_link = "https://official.example/metrics-page?token=private-token"
        page = DynamicNavigationPage(
            hrefs=(private_link,),
            responses_by_path={
                "/metrics-page": (
                    FakeResponse("https://official.example/metrics", {"data": {}}),
                ),
            },
        )
        collector = DomesticBrowserCollector(
            config(
                endpoint_by_path={"/metrics": "account_base"},
                phase_by_path={"/metrics": "account"},
                navigation_by_phase={
                    "bootstrap": "https://official.example/home",
                    "account": "/metrics-page",
                },
            ),
            browser_factory=lambda: FakeStarter(FakeBrowser(FakeContext(page))),
            parse_captures=parsed_batch,
        )

        batch = collector.collect(account())

        self.assertEqual(batch.source_mode, "browser_signed")

    def test_collector_waits_for_each_phase_before_following_the_next_link(self) -> None:
        """账号响应稍晚到达时，不能在切到文章阶段后把它判成串位。"""

        page = DelayedDynamicNavigationPage(
            hrefs=(
                "https://official.example/account-page?token=private-token",
                "https://official.example/content-page?token=private-token",
            ),
            responses_by_path={
                "/account-page": (
                    FakeResponse("https://official.example/account", {"account": {}}),
                ),
                "/content-page": (
                    FakeResponse("https://official.example/content", {"content": {}}),
                ),
            },
        )
        collector = DomesticBrowserCollector(
            config(
                endpoint_by_path={
                    "/account": "account_base",
                    "/content": "content_list",
                },
                phase_by_path={"/account": "account", "/content": "content"},
                navigation_by_phase={
                    "bootstrap": "https://official.example/home",
                    "account": "/account-page",
                    "content": "/content-page",
                },
            ),
            browser_factory=lambda: FakeStarter(FakeBrowser(FakeContext(page))),
            parse_captures=parsed_batch,
        )

        batch = collector.collect(account())

        self.assertEqual(batch.source_mode, "browser_signed")

    def test_collector_keeps_responses_arriving_within_phase_settle_window(self) -> None:
        """同一数据页的稍晚响应不能被下一次导航释放后改报读取失败。"""

        page = StaggeredResponsePage(
            hrefs=("https://official.example/account-page?token=private-token",),
            responses_by_path={
                "/account-page": (
                    FakeResponse("https://official.example/account", {"part": 1}),
                    FakeResponse("https://official.example/account", {"part": 2}),
                ),
            },
        )

        def parser(account_id: int, captures: tuple) -> CollectionBatch:
            if len(captures) != 2:
                raise ValueError("both reviewed responses are required")
            return parsed_batch(account_id, captures)

        collector = DomesticBrowserCollector(
            config(
                endpoint_by_path={"/account": "account_base"},
                phase_by_path={"/account": "account"},
                navigation_by_phase={
                    "bootstrap": "https://official.example/home",
                    "account": "/account-page",
                },
                phase_settle_milliseconds=10,
            ),
            browser_factory=lambda: FakeStarter(FakeBrowser(FakeContext(page))),
            parse_captures=parser,
        )

        batch = collector.collect(account())

        self.assertEqual(batch.source_mode, "browser_signed")

    def test_previous_phase_late_response_does_not_release_content_wait(self) -> None:
        """账号迟到响应不能冒充文章阶段已经取得数据。"""

        page = CrossPhaseLateResponsePage(
            hrefs=(
                "https://official.example/account-page?token=private-token",
                "https://official.example/content-page?token=private-token",
            ),
            responses_by_path={
                "/account-page": (
                    FakeResponse("https://official.example/account", {"part": 1}),
                    FakeResponse("https://official.example/account", {"part": 2}),
                ),
                "/content-page": (
                    FakeResponse("https://official.example/content", {"content": {}}),
                ),
            },
        )

        def parser(account_id: int, captures: tuple) -> CollectionBatch:
            phases = [capture.phase for capture in captures]
            if "content" not in phases:
                raise ValueError("content capture is required")
            return parsed_batch(account_id, captures)

        collector = DomesticBrowserCollector(
            config(
                endpoint_by_path={
                    "/account": "account_base",
                    "/content": "content_list",
                },
                phase_by_path={"/account": "account", "/content": "content"},
                navigation_by_phase={
                    "bootstrap": "https://official.example/home",
                    "account": "/account-page",
                    "content": "/content-page",
                },
                phase_settle_milliseconds=1,
            ),
            browser_factory=lambda: FakeStarter(FakeBrowser(FakeContext(page))),
            parse_captures=parser,
        )

        batch = collector.collect(account())

        self.assertEqual(batch.source_mode, "browser_signed")

    def test_dynamic_navigation_rejects_cross_domain_target(self) -> None:
        """同路径的跨域链接不能把已登录会话带离审核主机。"""

        self._assert_dynamic_navigation_failure(
            ("https://evil.example/metrics-page?token=private-token",)
        )

    def test_dynamic_navigation_rejects_path_mismatch(self) -> None:
        """仅 host 相同但 path 不同不能被当成审核数据页。"""

        self._assert_dynamic_navigation_failure(
            ("https://official.example/other?token=private-token",)
        )

    def test_dynamic_navigation_rejects_multiple_matches_without_leaking_query(self) -> None:
        """多个候选必须固定失败，且 token/query 不得进入公共异常。"""

        raised = self._assert_dynamic_navigation_failure(
            (
                "https://official.example/metrics-page?token=private-token-one",
                "https://official.example/metrics-page?token=private-token-two",
            )
        )

        public_error = repr(raised.exception) + repr(raised.exception.failure_diagnostic)
        self.assertNotIn("private-token", public_error)
        self.assertNotIn("?", public_error)

    def test_dynamic_navigation_rejects_missing_match(self) -> None:
        """页面没有目标链接时不能猜 URL 或继续采集。"""

        self._assert_dynamic_navigation_failure(())

    def _assert_dynamic_navigation_failure(
        self, hrefs: tuple[str, ...]
    ):
        page = DynamicNavigationPage(
            hrefs=hrefs,
            responses_by_path={
                "/home": (
                    FakeResponse("https://official.example/home-data", {"home": {}}),
                ),
            },
        )
        collector = DomesticBrowserCollector(
            config(
                endpoint_by_path={
                    "/home-data": "account_home",
                    "/metrics": "account_base",
                },
                phase_by_path={"/home-data": "home", "/metrics": "account"},
                navigation_by_phase={
                    "home": "https://official.example/home",
                    "account": "/metrics-page",
                },
            ),
            browser_factory=lambda: FakeStarter(FakeBrowser(FakeContext(page))),
            parse_captures=parsed_batch,
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")
        self.assertEqual(
            raised.exception.failure_diagnostic,
            {
                "endpoint": "runtime",
                "stage": "navigation",
                "reason": "navigation_link_unavailable",
            },
        )
        return raised

    def test_collector_rejects_request_count_over_limit(self) -> None:
        """删除请求计数会让异常多的已审核请求绕过短会话上限。"""
        collector = DomesticBrowserCollector(
            config(max_requests=1),
            browser_factory=lambda: fake_browser(payload={"data": {}}, response_count=2),
            parse_captures=parsed_batch,
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_collector_total_request_limit_counts_unreviewed_page_noise(self) -> None:
        """图片脚本等非白名单请求也必须受整次短会话总请求上限约束。"""

        responses = (
            FakeResponse("https://official.example/assets/app.js", {}),
            FakeResponse("https://official.example/assets/cover.png", {}),
            FakeResponse("https://static.example/assets/vendor.js", {}),
            FakeResponse("https://official.example/reviewed", {"data": {}}),
        )
        starter = FakeStarter(FakeBrowser(FakeContext(FakePage(
            responses,
            "https://official.example/home",
        ))))
        collector = DomesticBrowserCollector(
            config(max_requests=3),
            browser_factory=lambda: starter,
            parse_captures=parsed_batch,
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")
        self.assertEqual(
            raised.exception.failure_diagnostic,
            {
                "endpoint": "runtime",
                "stage": "request_binding",
                "reason": "request_limit_exceeded",
            },
        )

    def test_collector_stops_when_total_timeout_elapses(self) -> None:
        """没有总时限时，未到达审核响应的页面会无限等待。"""
        collector = DomesticBrowserCollector(
            config(total_timeout_seconds=0.01),
            browser_factory=lambda: fake_browser(response_count=0),
            parse_captures=parsed_batch,
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(account())

        self.assertEqual(raised.exception.error_code, "browser_signature_timeout")


if __name__ == "__main__":
    unittest.main()
