import io
import asyncio
import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import tools.verify_xiaohongshu_data_contract as verifier


class FakeRequest:
    def __init__(self, method="GET"):
        self.method = method


class FakeResponse:
    def __init__(
        self,
        url,
        content_type,
        payload,
        *,
        status=200,
        method="GET",
        content_length="128",
        content_encoding=None,
    ):
        self.url = url
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = content_length
        if content_encoding is not None:
            self.headers["content-encoding"] = content_encoding
        self._payload = payload
        self.status = status
        self.request = FakeRequest(method)

    def json(self):
        return self._payload


class FakePage:
    fetch_calls = 0
    click_calls = 0

    def __init__(self, owner):
        self.owner = owner
        self.response_callback = None
        self.request_callback = None
        self.goto_url = None
        self.goto_urls = []
        self.goto_kwargs = []
        self.passive_waits = []

    def on(self, event, callback):
        self.owner.events.append(("on", event))
        if event == "request":
            self.request_callback = callback
        elif event == "response":
            self.response_callback = callback

    def goto(self, url, **_kwargs):
        self.owner.events.append(("goto", url))
        self.goto_url = url
        self.goto_urls.append(url)
        self.goto_kwargs.append(dict(_kwargs))
        if self.owner.failure is not None:
            raise self.owner.failure
        for response in self.owner.responses:
            if self.request_callback is not None:
                self.request_callback(response.request)
            self.response_callback(response)
        return self.owner.navigation_response

    def wait_for_timeout(self, milliseconds):
        self.passive_waits.append(milliseconds)

    def close(self):
        self.owner.close_order.append("page")


class FakeContext:
    def __init__(self, owner):
        self.owner = owner

    def new_page(self):
        return self.owner.page

    def close(self):
        self.owner.close_order.append("context")


class FakeBrowser:
    def __init__(self, owner):
        self.owner = owner

    def new_context(self, **kwargs):
        self.owner.context_kwargs = kwargs
        return FakeContext(self.owner)

    def close(self):
        self.owner.close_order.append("browser")


class FakeChromium:
    def __init__(self, owner):
        self.owner = owner

    def launch(self, **kwargs):
        self.owner.launch_kwargs = kwargs
        return FakeBrowser(self.owner)


class FakePlaywright:
    def __init__(self, responses=(), failure=None, navigation_response=None):
        self.responses = responses
        self.failure = failure
        self.close_order = []
        self.events = []
        self.context_kwargs = None
        self.launch_kwargs = None
        self.page = FakePage(self)
        self.chromium = FakeChromium(self)
        self.navigation_response = navigation_response or FakeResponse(
            "https://creator.xiaohongshu.com/creator/home",
            "text/html; charset=utf-8",
            None,
        )

    def close(self):
        self.close_order.append("playwright")


class RouteAwarePage(FakePage):
    def goto(self, url, **_kwargs):
        self.owner.events.append(("goto", url))
        self.goto_url = url
        self.goto_urls.append(url)
        self.goto_kwargs.append(dict(_kwargs))
        for response in self.owner.responses_by_url.get(url, ()):
            if self.request_callback is not None:
                self.request_callback(response.request)
            self.response_callback(response)
        return FakeResponse(url, "text/html; charset=utf-8", None)


class RouteAwarePlaywright(FakePlaywright):
    def __init__(self, responses_by_url):
        super().__init__()
        self.responses_by_url = responses_by_url
        self.page = RouteAwarePage(self)


class LateResponsePage(FakePage):
    def goto(self, url, **_kwargs):
        self.owner.events.append(("goto", url))
        self.goto_url = url
        self.goto_urls.append(url)
        self.goto_kwargs.append(dict(_kwargs))
        if url == verifier._CREATOR_HOME:
            if self.request_callback is not None:
                self.request_callback(self.owner.delayed_response.request)
        elif url == verifier._DATA_ANALYSIS_URL:
            if self.request_callback is not None:
                self.request_callback(self.owner.list_response.request)
            self.response_callback(self.owner.list_response)
        elif url.startswith(verifier._NOTE_DETAIL_URL):
            self.response_callback(self.owner.delayed_response)
        return FakeResponse(url, "text/html; charset=utf-8", None)


class LateResponsePlaywright(FakePlaywright):
    def __init__(self, delayed_response, list_response):
        super().__init__()
        self.delayed_response = delayed_response
        self.list_response = list_response
        self.page = LateResponsePage(self)


class LateListResponsePage(FakePage):
    def goto(self, url, **_kwargs):
        self.owner.events.append(("goto", url))
        self.goto_url = url
        self.goto_urls.append(url)
        self.goto_kwargs.append(dict(_kwargs))
        if url == verifier._CREATOR_HOME:
            if self.request_callback is not None:
                self.request_callback(self.owner.delayed_response.request)
        elif url == verifier._DATA_ANALYSIS_URL:
            self.response_callback(self.owner.delayed_response)
            if self.request_callback is not None:
                self.request_callback(self.owner.data_response.request)
            self.response_callback(self.owner.data_response)
        return FakeResponse(url, "text/html; charset=utf-8", None)


class LateListResponsePlaywright(FakePlaywright):
    def __init__(self, delayed_response, data_response):
        super().__init__()
        self.delayed_response = delayed_response
        self.data_response = data_response
        self.page = LateListResponsePage(self)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        current = self.value
        self.value += 0.001
        return current


def fixed_now():
    return datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def eligible_account():
    return {"id": 7, "type": 1, "status": 1, "filePath": __file__}


class AsyncFakeResponse(FakeResponse):
    def __init__(self, owner, url, content_type, payload):
        super().__init__(url, content_type, payload)
        self.owner = owner

    async def json(self):
        if self.owner.close_order:
            raise RuntimeError("response body must be read before cleanup")
        return self._payload


class AsyncFakePage(FakePage):
    async def goto(self, url, **_kwargs):
        self.owner.events.append(("goto", url))
        self.goto_url = url
        for response in self.owner.responses:
            if self.request_callback is not None:
                self.request_callback(response.request)
            self.response_callback(response)
        return self.owner.navigation_response

    async def close(self):
        self.owner.close_order.append("page")


class AsyncFakeContext(FakeContext):
    async def new_page(self):
        return self.owner.page

    async def close(self):
        self.owner.close_order.append("context")


class AsyncFakeBrowser(FakeBrowser):
    async def new_context(self, **kwargs):
        self.owner.context_kwargs = kwargs
        return AsyncFakeContext(self.owner)

    async def close(self):
        self.owner.close_order.append("browser")


class AsyncFakeChromium(FakeChromium):
    async def launch(self, **kwargs):
        self.owner.launch_kwargs = kwargs
        return AsyncFakeBrowser(self.owner)


class AsyncFakePlaywright(FakePlaywright):
    def __init__(self):
        super().__init__()
        self.page = AsyncFakePage(self)
        self.chromium = AsyncFakeChromium(self)
        self.responses = [AsyncFakeResponse(
            self,
            "https://creator.xiaohongshu.com/api/overview",
            "Application/JSON; charset=utf-8",
            {"data": {"trend": {"fans": 2}}},
        )]

    async def stop(self):
        self.close_order.append("playwright")

    close = None


class AsyncHangingPage(AsyncFakePage):
    async def goto(self, url, **_kwargs):
        self.goto_url = url
        await asyncio.sleep(1)


class AsyncHangingPlaywright(AsyncFakePlaywright):
    def __init__(self):
        super().__init__()
        self.page = AsyncHangingPage(self)
        self.responses = []


class AsyncHangingCloseFailurePage(AsyncHangingPage):
    async def close(self):
        self.owner.close_order.append("page")
        raise RuntimeError("secret cleanup failure")


class AsyncHangingCloseFailurePlaywright(AsyncHangingPlaywright):
    def __init__(self):
        super().__init__()
        self.page = AsyncHangingCloseFailurePage(self)


class AsyncHangingCleanupPage(AsyncHangingPage):
    async def close(self):
        self.owner.close_order.append("page")
        await asyncio.sleep(1)


class AsyncHangingCleanupPlaywright(AsyncHangingPlaywright):
    def __init__(self):
        super().__init__()
        self.page = AsyncHangingCleanupPage(self)


class AsyncStartManager:
    def __init__(self, *, failure=None, hangs=False):
        self.failure = failure
        self.hangs = hangs
        self.transport_alive = False
        self.close_order = []

    async def start(self):
        self.transport_alive = True
        if self.hangs:
            await asyncio.sleep(1)
        if self.failure is not None:
            raise self.failure
        raise AssertionError("test manager must not complete start")

    async def __aexit__(self, _exc_type, _exc, _traceback):
        self.close_order.append("playwright_manager")
        self.transport_alive = False


class CloseGetterFailurePage(FakePage):
    @property
    def close(self):
        self.owner.close_order.append("page_getter")
        raise self.owner.close_getter_failure


class CloseGetterFailurePlaywright(FakePlaywright):
    def __init__(self, failure):
        super().__init__()
        self.close_getter_failure = failure
        self.page = CloseGetterFailurePage(self)


class CountingResponse(FakeResponse):
    json_calls = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance_json_calls = 0

    def json(self):
        type(self).json_calls += 1
        self.instance_json_calls += 1
        return self._payload


class ExhaustingClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        current = self.value
        self.value += 1.0
        return current


class XiaohongshuDataContractVerifierTests(unittest.TestCase):
    def setUp(self):
        self.report_path = (
            Path(verifier.__file__).resolve().parents[1]
            / ".superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery"
            / "probe-report-test.json"
        )

    def tearDown(self):
        self.report_path.unlink(missing_ok=True)

    def test_probe_closes_playwright_manager_when_start_fails_midway(self):
        manager = AsyncStartManager(failure=RuntimeError("secret start failure"))
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: manager,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        self.assertEqual(result["errorCode"], "xiaohongshu_probe_failed")
        self.assertFalse(manager.transport_alive)
        self.assertEqual(manager.close_order, ["playwright_manager"])
        self.assertNotIn("secret", json.dumps(result))

    def test_probe_closes_playwright_manager_when_start_times_out_midway(self):
        manager = AsyncStartManager(hangs=True)
        with patch.object(verifier, "_TOTAL_TIMEOUT_SECONDS", 0.02):
            result = verifier._probe_with_browser(
                eligible_account(), playwright_factory=lambda: manager,
                monotonic=time.monotonic, utc_now=fixed_now,
            )
        self.assertEqual(result["errorCode"], "xiaohongshu_probe_timeout")
        self.assertFalse(manager.transport_alive)
        self.assertEqual(manager.close_order, ["playwright_manager"])

    def test_probe_continues_cleanup_when_close_getter_raises_exception(self):
        fake = CloseGetterFailurePlaywright(RuntimeError("secret getter failure"))
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        self.assertEqual(
            result["errorCode"], "xiaohongshu_probe_cleanup_incomplete"
        )
        self.assertEqual(result["cleanup"]["aliveResourceCount"], 1)
        self.assertEqual(
            fake.close_order,
            ["page_getter", "context", "browser", "playwright"],
        )
        self.assertNotIn("secret", json.dumps(result))

    def test_probe_propagates_close_getter_process_control_after_all_cleanup(self):
        for failure in (KeyboardInterrupt(), SystemExit()):
            fake = CloseGetterFailurePlaywright(failure)
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(type(failure)) as raised:
                    verifier._probe_with_browser(
                        eligible_account(), playwright_factory=lambda: fake,
                        monotonic=FakeClock(), utc_now=fixed_now,
                    )
                self.assertIs(raised.exception, failure)
                self.assertEqual(
                    fake.close_order,
                    ["page_getter", "context", "browser", "playwright"],
                )

    def test_probe_iteratively_bounds_deep_builtin_payload(self):
        payload = {"leaf": "private-deep-value"}
        for _ in range(2_000):
            payload = {"next": payload}
        fake = FakePlaywright(responses=[FakeResponse(
            "https://creator.xiaohongshu.com/api/deep",
            "application/json",
            payload,
        )])
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        self.assertEqual(len(result["responses"]), 1)
        self.assertLessEqual(len(result["responses"][0]["keyPaths"]), 300)
        self.assertNotIn("private-deep-value", json.dumps(result))

    def test_probe_bounds_wide_dict_and_samples_builtin_list(self):
        payload = {
            "wide": {f"key_{index}": f"private-{index}" for index in range(1_000)},
            "items": [{f"item_{index}": index} for index in range(1_000)],
        }
        fake = FakePlaywright(responses=[FakeResponse(
            "https://creator.xiaohongshu.com/api/wide",
            "application/json",
            payload,
        )])
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        response = result["responses"][0]
        self.assertLessEqual(len(response["fieldTypes"]), 300)
        self.assertIn("items[].:key", response["fieldTypes"])
        self.assertNotIn("item_0", json.dumps(response))
        self.assertNotIn("item_999", json.dumps(response))
        self.assertNotIn("private-", json.dumps(result))

    def test_probe_detects_cycle_in_builtin_payload(self):
        payload = {"value": "private-cycle-value"}
        payload["self"] = payload
        fake = FakePlaywright(responses=[FakeResponse(
            "https://creator.xiaohongshu.com/api/cycle",
            "application/json",
            payload,
        )])
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        self.assertEqual(len(result["responses"]), 1)
        self.assertIn(":key", result["responses"][0]["keyPaths"])
        self.assertNotIn("self", json.dumps(result["responses"][0]))
        self.assertNotIn("private-cycle-value", json.dumps(result))

    def test_probe_structural_walk_honors_total_deadline(self):
        payload = {f"key_{index}": index for index in range(1_000)}
        fake = FakePlaywright(responses=[FakeResponse(
            "https://creator.xiaohongshu.com/api/deadline",
            "application/json",
            payload,
        )])
        with patch.object(verifier, "_TOTAL_TIMEOUT_SECONDS", 20.0):
            result = verifier._probe_with_browser(
                eligible_account(), playwright_factory=lambda: fake,
                monotonic=ExhaustingClock(), utc_now=fixed_now,
            )
        self.assertEqual(result["errorCode"], "xiaohongshu_probe_timeout")
        self.assertEqual(
            fake.close_order,
            ["page", "context", "browser", "playwright"],
        )

    def test_probe_listener_caps_retained_responses_before_body_reads(self):
        CountingResponse.json_calls = 0
        fake = FakePlaywright(responses=[
            CountingResponse(
                f"https://creator.xiaohongshu.com/api/item/{index}",
                "application/json",
                {"index": index},
            )
            for index in range(150)
        ])
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        self.assertEqual(len(result["responses"]), 100)
        self.assertEqual(CountingResponse.json_calls, 100)

    def test_response_byte_budgets_reject_before_json_decode(self):
        direct_cases = (
            (
                "single_response_over_limit",
                {"content_length": "65"},
            ),
            (
                "content_length_missing",
                {"content_length": None},
            ),
            (
                "content_length_malformed",
                {"content_length": "12.5"},
            ),
            (
                "compressed_body",
                {"content_length": "8", "content_encoding": "gzip"},
            ),
        )
        with patch.object(
            verifier, "_MAX_RESPONSE_BODY_BYTES", 64, create=True
        ):
            for label, response_options in direct_cases:
                response = CountingResponse(
                    "https://creator.xiaohongshu.com/api/data",
                    "application/json",
                    {"data": {"trend": {"fans": 2}}},
                    **response_options,
                )
                with self.subTest(label=label):
                    self.assertIsNone(verifier._response_shape(response))
                    self.assertEqual(response.instance_json_calls, 0)

        first = CountingResponse(
            "https://creator.xiaohongshu.com/api/data/first",
            "application/json",
            {"data": {"trend": {"fans": 2}}},
            content_length="40",
        )
        second = CountingResponse(
            "https://creator.xiaohongshu.com/api/data/second",
            "application/json",
            {"data": {"items": [{"note_id": "private-id"}]}},
            content_length="40",
        )
        fake = FakePlaywright(responses=[first, second])
        with patch.object(
            verifier, "_MAX_RESPONSE_BODY_BYTES", 64, create=True
        ), patch.object(
            verifier, "_MAX_TOTAL_RESPONSE_BODY_BYTES", 64, create=True
        ):
            result = verifier._probe_with_browser(
                eligible_account(), playwright_factory=lambda: fake,
                monotonic=FakeClock(), utc_now=fixed_now,
            )
        self.assertEqual(first.instance_json_calls, 0)
        self.assertEqual(second.instance_json_calls, 0)
        self.assertEqual(result["responses"], [])
        self.assertEqual(result["phases"], [])
        self.assertEqual(result["status"], "failed")

    def test_probe_listener_limit_counts_only_eligible_responses(self):
        responses = [
            FakeResponse(
                f"https://evil.example/static/{index}",
                "application/json",
                {"private": index},
            )
            for index in range(100)
        ]
        responses.append(FakeResponse(
            "https://creator.xiaohongshu.com/api/eligible",
            "application/json",
            {"data": {"fans": 1}},
        ))
        fake = FakePlaywright(responses=responses)
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        self.assertEqual(len(result["responses"]), 1)
        self.assertEqual(result["responses"][0]["path"], "/api/eligible")

    def test_unknown_official_json_path_is_redacted_retained_and_unclassified(self):
        response = FakeResponse(
            "https://creator.xiaohongshu.com/api/galaxy/creator/analytics"
            "?note_id=private-query",
            "application/json",
            {
                "data": {
                    "overview": {"fans": 2},
                    "items": [{"note_id": "private-id", "view_count": 3}],
                },
            },
        )

        shape = verifier._response_shape(response)
        self.assertIsNotNone(shape)
        self.assertEqual(shape["path"], "/api/:segment/creator/:segment")
        self.assertEqual(verifier._classify_shape(shape), "unclassified")
        self.assertEqual(
            verifier._contract_outcome([shape]),
            (
                [],
                ["account_overview", "content_list", "content_lifetime"],
                "failed",
                "xiaohongshu_contracts_unobserved",
            ),
        )

        fake = FakePlaywright(responses=[response])
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        self.assertEqual(len(result["responses"]), 1)
        self.assertEqual(
            result["responses"][0]["path"],
            "/api/:segment/creator/:segment",
        )
        self.assertEqual(result["phases"], [])
        self.assertEqual(result["status"], "failed")
        encoded = json.dumps(result, ensure_ascii=False)
        for forbidden in ("galaxy", "analytics", "note_id=", "private-"):
            self.assertNotIn(forbidden, encoded)

    def test_probe_only_accepts_creator_https_json_and_never_fetches(self):
        fake = FakePlaywright(responses=[
            FakeResponse("https://evil.example/data", "application/json", {"x": 1}),
            FakeResponse("http://creator.xiaohongshu.com/api", "application/json", {"x": 1}),
            FakeResponse(
                "https://creator.xiaohongshu.com/api/data?q=secret",
                "text/html",
                "<html>",
            ),
            FakeResponse(
                "https://creator.xiaohongshu.com/api/data?q=secret",
                "application/json",
                {
                    "data": {"items": [{"note_id": "private", "view_count": 3}]},
                    "cursor": "private-cursor",
                },
            ),
        ])
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        self.assertEqual(len(result["responses"]), 1)
        self.assertEqual(result["responses"][0]["path"], "/api/data")
        self.assertIn("data.items[].view_count", result["responses"][0]["keyPaths"])
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(fake.page.fetch_calls, 0)
        self.assertEqual(fake.page.click_calls, 0)
        self.assertEqual(fake.page.goto_url, verifier._DATA_ANALYSIS_URL)
        self.assertEqual(
            fake.page.goto_urls,
            [verifier._CREATOR_HOME, verifier._DATA_ANALYSIS_URL],
        )
        self.assertEqual(fake.events[0], ("on", "response"))
        self.assertEqual(fake.launch_kwargs, {"headless": True})

    def test_probe_passively_navigates_data_analysis_and_one_note_detail(self):
        list_response = FakeResponse(
            "https://creator.xiaohongshu.com/api/galaxy/creator/datacenter/note/analyze/list?page_num=1",
            "application/json",
            {"data": {"note_infos": [{"id": "6a0ffca800000000080033f8"}]}},
        )
        detail_response = FakeResponse(
            "https://creator.xiaohongshu.com/api/galaxy/creator/datacenter/note/base?note_id=private",
            "application/json",
            {"data": {"note_id": "private", "read_count": 3}},
        )
        data_url = "https://creator.xiaohongshu.com/statistics/data-analysis?source=official"
        detail_url = (
            "https://creator.xiaohongshu.com/statistics/note-detail"
            "?noteId=6a0ffca800000000080033f8"
        )
        fake = RouteAwarePlaywright({
            verifier._CREATOR_HOME: (),
            data_url: (list_response,),
            detail_url: (detail_response,),
        })

        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )

        self.assertEqual(
            fake.page.goto_urls,
            [verifier._CREATOR_HOME, data_url, detail_url],
        )
        self.assertEqual(
            [item["wait_until"] for item in fake.page.goto_kwargs],
            ["domcontentloaded", "domcontentloaded", "domcontentloaded"],
        )
        self.assertEqual(fake.page.passive_waits, [2000, 2000, 2000])
        self.assertEqual(len(result["responses"]), 2)
        self.assertEqual(
            [item["observationPhase"] for item in result["responses"]],
            ["data_analysis", "content_lifetime"],
        )
        self.assertEqual(result["phases"], ["content_list"])
        self.assertEqual(result["status"], "partial_success")
        self.assertNotIn("6a0ffca800000000080033f8", json.dumps(result))
        self.assertEqual(fake.page.click_calls, 0)
        self.assertEqual(fake.page.fetch_calls, 0)

    def test_probe_binds_late_response_to_request_start_phase(self):
        delayed_lifetime = FakeResponse(
            "https://creator.xiaohongshu.com/api/private/detail",
            "application/json",
            {"data": {
                "note_id": "private-id",
                "lifetime": {"views": 3},
            }},
        )
        list_response = FakeResponse(
            "https://creator.xiaohongshu.com/api/private/list",
            "application/json",
            {"data": {"note_infos": [{
                "id": "6a0ffca800000000080033f8",
            }]}},
        )
        fake = LateResponsePlaywright(delayed_lifetime, list_response)

        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )

        self.assertEqual(
            [item["observationPhase"] for item in result["responses"]],
            ["data_analysis", "account_home"],
        )
        self.assertEqual(result["phases"], ["content_list"])
        self.assertEqual(
            result["missingPhases"],
            ["account_overview", "content_lifetime"],
        )

    def test_probe_never_uses_late_account_response_as_detail_source(self):
        delayed_account_list = FakeResponse(
            "https://creator.xiaohongshu.com/api/private/account-list",
            "application/json",
            {"data": {"note_infos": [{
                "id": "6a0ffca800000000080033f8",
            }]}},
        )
        empty_data_list = FakeResponse(
            "https://creator.xiaohongshu.com/api/private/data-list",
            "application/json",
            {"data": {"note_infos": [], "total": 0}},
        )
        fake = LateListResponsePlaywright(
            delayed_account_list, empty_data_list
        )

        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )

        self.assertEqual(
            fake.page.goto_urls,
            [verifier._CREATOR_HOME, verifier._DATA_ANALYSIS_URL],
        )
        self.assertEqual(
            [item["observationPhase"] for item in result["responses"]],
            ["account_home", "data_analysis"],
        )
        self.assertEqual(result["phases"], ["content_list"])

    def test_probe_never_reports_success_until_all_required_contracts_observed(self):
        reviewed_paths = {
            "/api/overview": "account_overview",
            "/api/data/list": "content_list",
            "/api/note/:id/metrics": "content_lifetime",
        }
        contracts = [
            FakeResponse(
                "https://creator.xiaohongshu.com/api/overview",
                "application/json",
                {"data": {"trend": {"fans": 2}}},
            ),
            FakeResponse(
                "https://creator.xiaohongshu.com/api/data/list",
                "application/json",
                {"data": {"items": [{"note_id": "private-id"}]}},
            ),
            FakeResponse(
                "https://creator.xiaohongshu.com/api/note/123/metrics",
                "application/json",
                {
                    "data": {
                        "note_id": "private-id",
                        "lifetime": {"view_count": 3},
                    },
                },
            ),
        ]
        cases = (
            (0, "failed", "xiaohongshu_contracts_unobserved", [], [
                "account_overview", "content_list", "content_lifetime",
            ]),
            (1, "partial_success", "xiaohongshu_contracts_incomplete", [
                "account_overview",
            ], ["content_list", "content_lifetime"]),
            (2, "partial_success", "xiaohongshu_contracts_incomplete", [
                "account_overview", "content_list",
            ], ["content_lifetime"]),
            (3, "success", "", [
                "account_overview", "content_list", "content_lifetime",
            ], []),
        )

        for count, status, error_code, phases, missing in cases:
            with self.subTest(count=count), patch.object(
                verifier,
                "_REVIEWED_CONTRACT_PATH_TEMPLATES",
                reviewed_paths,
            ):
                result = verifier._probe_with_browser(
                    eligible_account(),
                    playwright_factory=lambda count=count: FakePlaywright(
                        responses=contracts[:count]
                    ),
                    monotonic=FakeClock(),
                    utc_now=fixed_now,
                )
                self.assertEqual(result["status"], status)
                self.assertEqual(result["errorCode"], error_code)
                self.assertEqual(result["phases"], phases)
                self.assertEqual(result["missingPhases"], missing)

    def test_contract_classifier_rejects_non_content_identifiers(self):
        reviewed_paths = {
            "/api/overview": "account_overview",
            "/api/data/list": "content_list",
            "/api/note/:id/metrics": "content_lifetime",
        }
        collision_shapes = [
            verifier._response_shape(FakeResponse(
                "https://creator.xiaohongshu.com/api/overview",
                "application/json",
                {
                    "data": {
                        "trend": {
                            "fans": 2,
                            "request_id": "private-request",
                        },
                    },
                },
            )),
            verifier._response_shape(FakeResponse(
                "https://creator.xiaohongshu.com/api/data/list",
                "application/json",
                {"data": {"items": [{"trace_id": "private-trace"}]}},
            )),
            verifier._response_shape(FakeResponse(
                "https://creator.xiaohongshu.com/api/note/123/metrics",
                "application/json",
                {
                    "data": {
                        "session_id": "private-session",
                        "lifetime": {"view_count": 3},
                    },
                },
            )),
        ]
        self.assertNotIn(None, collision_shapes)
        with patch.object(
            verifier,
            "_REVIEWED_CONTRACT_PATH_TEMPLATES",
            reviewed_paths,
        ):
            phases, missing, status, error_code = verifier._contract_outcome(
                collision_shapes
            )
        self.assertEqual(phases, ["account_overview"])
        self.assertEqual(missing, ["content_list", "content_lifetime"])
        self.assertEqual(status, "partial_success")
        self.assertEqual(error_code, "xiaohongshu_contracts_incomplete")
        encoded_collisions = json.dumps(collision_shapes, ensure_ascii=False)
        for forbidden in ("request_id", "trace_id", "session_id", "private-"):
            self.assertNotIn(forbidden, encoded_collisions)

        for identity_key in ("note_id", "item_id", "content_id"):
            with self.subTest(identity_key=identity_key):
                list_shape = verifier._response_shape(FakeResponse(
                    "https://creator.xiaohongshu.com/api/data/list",
                    "application/json",
                    {"data": {"items": [{identity_key: "private-content"}]}},
                ))
                lifetime_shape = verifier._response_shape(FakeResponse(
                    "https://creator.xiaohongshu.com/api/note/123/metrics",
                    "application/json",
                    {
                        "data": {
                            identity_key: "private-content",
                            "lifetime": {"view_count": 3},
                        },
                    },
                ))
                with patch.object(
                    verifier,
                    "_REVIEWED_CONTRACT_PATH_TEMPLATES",
                    reviewed_paths,
                ):
                    self.assertEqual(
                        verifier._classify_shape(list_shape), "content_list"
                    )
                    self.assertEqual(
                        verifier._classify_shape(lifetime_shape),
                        "content_lifetime",
                    )
                encoded = json.dumps(
                    [list_shape, lifetime_shape], ensure_ascii=False
                )
                path_segments = {
                    segment.removesuffix("[]")
                    for shape in (list_shape, lifetime_shape)
                    for path in shape["keyPaths"]
                    for segment in path.split(".")
                }
                self.assertIn(identity_key, path_segments)
                self.assertNotIn("private-content", encoded)
                self.assertNotIn(":content_identifier", encoded)

    def test_contract_classifier_uses_fixed_observation_phase_with_structure(self):
        cases = (
            (
                "account_home",
                "https://creator.xiaohongshu.com/api/private/overview",
                {"data": {"trend": {"fans": 2}}},
                "account_overview",
            ),
            (
                "data_analysis",
                "https://creator.xiaohongshu.com/api/private/list",
                {"data": {"items": [{"note_id": "private-id"}]}},
                "content_list",
            ),
            (
                "content_lifetime",
                "https://creator.xiaohongshu.com/api/private/detail",
                {"data": {"note_id": "private-id", "lifetime": {"views": 3}}},
                "content_lifetime",
            ),
        )
        for observation_phase, url, payload, expected in cases:
            with self.subTest(observation_phase=observation_phase):
                shape = verifier._response_shape(FakeResponse(
                    url, "application/json", payload,
                ))
                shape["observationPhase"] = observation_phase
                self.assertEqual(verifier._classify_shape(shape), expected)

        ambiguous = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/private/mixed",
            "application/json",
            {"data": {
                "trend": {"fans": 2},
                "items": [{"note_id": "private-id"}],
            }},
        ))
        ambiguous["observationPhase"] = "data_analysis"
        self.assertEqual(verifier._classify_shape(ambiguous), "unclassified")

    def test_data_analysis_empty_paginated_list_is_content_list_contract(self):
        shape = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/private/creator/list",
            "application/json",
            {"data": {"note_infos": [], "total": 0}},
        ))
        shape["observationPhase"] = "data_analysis"

        self.assertEqual(shape["listLengths"], {"data.note_infos": 0})
        self.assertEqual(shape["paginationKeys"], ["data.total"])
        self.assertEqual(verifier._classify_shape(shape), "content_list")

        wrong_phase = dict(shape, observationPhase="account_home")
        self.assertEqual(verifier._classify_shape(wrong_phase), "unclassified")

        ambiguous_lists = dict(shape, listLengths={
            "data.note_infos": 0,
            "data.items": 0,
        })
        self.assertEqual(
            verifier._classify_shape(ambiguous_lists), "unclassified"
        )

    def test_data_analysis_generic_empty_paginated_list_is_not_content_list(self):
        """通用空结果若被当成作品列表，会把有作品的账号误报为 0 条。"""

        shape = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/private/creator/summary",
            "application/json",
            {"data": {"result": [], "total": 0}},
        ))
        shape["observationPhase"] = "data_analysis"

        self.assertEqual(shape["listLengths"], {"data.result": 0})
        self.assertEqual(shape["paginationKeys"], ["data.total"])
        self.assertEqual(verifier._classify_shape(shape), "unclassified")

    def test_classifier_requires_reviewed_path_and_common_ancestor_semantics(self):
        reviewed_paths = {
            "/api/overview": "account_overview",
            "/api/data/list": "content_list",
            "/api/note/:id/metrics": "content_lifetime",
        }

        profile_only = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/overview",
            "application/json",
            {"data": {"profile": {}}},
        ))
        unrelated_list = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/data/list",
            "application/json",
            {
                "data": {
                    "note_id": "private-id",
                    "items": [{"message": "private-value"}],
                },
            },
        ))
        window_metric = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/note/123/metrics",
            "application/json",
            {
                "data": {
                    "note_id": "private-id",
                    "window": {"view_count": 3},
                },
            },
        ))
        placeholder_identity = {
            "method": "GET",
            "path": "/api/data/list",
            "status": 200,
            "contentType": "application/json",
            "keyPaths": ["data.items[]", "data.items[].:content_identifier"],
            "fieldTypes": {
                "data.items[]": "dict",
                "data.items[].:content_identifier": "str",
            },
        }

        valid_account = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/overview",
            "application/json",
            {"data": {"trend": {"fans": 2}}},
        ))
        valid_list = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/data/list",
            "application/json",
            {"data": {"items": [{"note_id": "private-id"}]}},
        ))
        valid_lifetime = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/note/123/metrics",
            "application/json",
            {
                "data": {
                    "note_id": "private-id",
                    "lifetime": {"view_count": 3},
                },
            },
        ))
        all_shapes = (
            profile_only, unrelated_list, window_metric,
            valid_account, valid_list, valid_lifetime,
        )
        self.assertNotIn(None, all_shapes)

        with patch.object(
            verifier,
            "_REVIEWED_CONTRACT_PATH_TEMPLATES",
            reviewed_paths,
        ):
            for label, shape in (
                ("profile_only", profile_only),
                ("unrelated_list", unrelated_list),
                ("window_metric", window_metric),
                ("placeholder_identity", placeholder_identity),
            ):
                with self.subTest(label=label):
                    self.assertEqual(
                        verifier._classify_shape(shape), "unclassified"
                    )

            self.assertEqual(
                verifier._contract_outcome(
                    [profile_only, unrelated_list, window_metric]
                ),
                (
                    [],
                    ["account_overview", "content_list", "content_lifetime"],
                    "failed",
                    "xiaohongshu_contracts_unobserved",
                ),
            )
            self.assertEqual(
                [
                    verifier._classify_shape(shape)
                    for shape in (valid_account, valid_list, valid_lifetime)
                ],
                ["account_overview", "content_list", "content_lifetime"],
            )
            self.assertEqual(
                verifier._contract_outcome(
                    [valid_account, valid_list, valid_lifetime]
                ),
                (
                    ["account_overview", "content_list", "content_lifetime"],
                    [],
                    "success",
                    "",
                ),
            )

        self.assertEqual(
            verifier._contract_outcome(
                [valid_account, valid_list, valid_lifetime]
            )[0],
            [],
        )

    def test_probe_classifies_login_and_verification_without_claiming_contracts(self):
        cases = (
            (
                "login",
                FakeResponse(
                    "https://creator.xiaohongshu.com/login",
                    "text/html; charset=utf-8",
                    None,
                ),
                "login_required",
            ),
            (
                "verification",
                FakeResponse(
                    "https://creator.xiaohongshu.com/creator/security/verification",
                    "text/html; charset=utf-8",
                    None,
                    status=403,
                ),
                "verification_required",
            ),
            (
                "normal_home_without_contracts",
                FakeResponse(
                    "https://creator.xiaohongshu.com/creator/home",
                    "text/html; charset=utf-8",
                    None,
                ),
                "xiaohongshu_contracts_unobserved",
            ),
        )

        for label, navigation, error_code in cases:
            late_response = CountingResponse(
                "https://creator.xiaohongshu.com/api/overview",
                "application/json",
                {"data": {"fans": 2}},
            )
            CountingResponse.json_calls = 0
            responses = [] if label == "normal_home_without_contracts" else [
                late_response
            ]
            fake = FakePlaywright(
                responses=responses,
                navigation_response=navigation,
            )
            with self.subTest(label=label):
                result = verifier._probe_with_browser(
                    eligible_account(),
                    playwright_factory=lambda fake=fake: fake,
                    monotonic=FakeClock(),
                    utc_now=fixed_now,
                )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["errorCode"], error_code)
                self.assertEqual(result["responses"], [])
                self.assertEqual(result["phases"], [])
                self.assertEqual(
                    result["missingPhases"],
                    ["account_overview", "content_list", "content_lifetime"],
                )
                self.assertEqual(CountingResponse.json_calls, 0)
                self.assertEqual(
                    fake.close_order,
                    ["page", "context", "browser", "playwright"],
                )

    def test_probe_closes_all_resources_on_timeout_and_base_exception(self):
        for failure in (TimeoutError("secret"), KeyboardInterrupt(), SystemExit()):
            fake = FakePlaywright(failure=failure)
            with self.subTest(failure=type(failure).__name__):
                if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                    with self.assertRaises(type(failure)):
                        verifier._probe_with_browser(
                            eligible_account(), playwright_factory=lambda: fake,
                            monotonic=FakeClock(), utc_now=fixed_now,
                        )
                else:
                    result = verifier._probe_with_browser(
                        eligible_account(), playwright_factory=lambda: fake,
                        monotonic=FakeClock(), utc_now=fixed_now,
                    )
                    self.assertEqual(
                        result["errorCode"], "xiaohongshu_probe_timeout"
                    )
                self.assertEqual(
                    fake.close_order,
                    ["page", "context", "browser", "playwright"],
                )

    def test_review_probe_reports_navigation_and_closes_every_resource(self):
        response = FakeResponse(
            "https://creator.xiaohongshu.com/api/galaxy/creator/datacenter/overview",
            "application/json", {"data": {"views": 12}},
        )
        fake = FakePlaywright(responses=(response,))
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        review = verifier._review_schema(result)
        self.assertEqual(review["navigation"], [
            {"phase": "account_home", "path": "/creator/home"},
            {"phase": "data_analysis", "path": "/statistics/data-analysis"},
        ])
        self.assertTrue(result["cleanup"]["closed"])
        self.assertIs(type(result["cleanup"]["aliveResourceCount"]), int)
        self.assertEqual(result["cleanup"]["aliveResourceCount"], 0)
        self.assertEqual(fake.page.fetch_calls, 0)
        self.assertEqual(fake.page.click_calls, 0)
        self.assertNotIn("navigation", verifier.sanitize_probe_report(result))

    def test_review_mode_keeps_response_identity_and_byte_budgets(self):
        response = CountingResponse(
            "https://creator.xiaohongshu.com/api/galaxy/creator/datacenter/overview",
            "application/json", {"data": {"views": 1}},
        )
        fake = FakePlaywright(responses=(response, response))
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        review = verifier._review_schema(result)
        self.assertEqual(len(review["responses"]), 1)
        self.assertEqual(response.instance_json_calls, 1)

    def test_probe_adapts_async_playwright_and_reads_shapes_before_cleanup(self):
        fake = AsyncFakePlaywright()
        with patch.object(
            verifier,
            "_REVIEWED_CONTRACT_PATH_TEMPLATES",
            {"/api/overview": "account_overview"},
        ):
            result = verifier._probe_with_browser(
                eligible_account(), playwright_factory=lambda: fake,
                monotonic=FakeClock(), utc_now=fixed_now,
            )
        self.assertEqual(len(result["responses"]), 1)
        self.assertEqual(result["responses"][0]["contentType"], "application/json")
        self.assertEqual(
            result["responses"][0]["fieldTypes"]["data.trend.fans"], "int"
        )
        self.assertEqual(result["phases"], ["account_overview"])
        self.assertEqual(
            fake.close_order,
            ["page", "context", "browser", "playwright"],
        )

    def test_probe_total_timeout_is_fixed_and_still_closes_async_resources(self):
        fake = AsyncHangingPlaywright()
        with patch.object(verifier, "_TOTAL_TIMEOUT_SECONDS", 0.02):
            result = verifier._probe_with_browser(
                eligible_account(), playwright_factory=lambda: fake,
                monotonic=time.monotonic, utc_now=fixed_now,
            )
        self.assertEqual(result["errorCode"], "xiaohongshu_probe_timeout")
        self.assertEqual(
            fake.close_order,
            ["page", "context", "browser", "playwright"],
        )
        self.assertEqual(
            result["cleanup"], {"closed": True, "aliveResourceCount": 0}
        )

    def test_probe_total_timeout_reports_exact_cleanup_failure_count(self):
        fake = AsyncHangingCloseFailurePlaywright()
        with patch.object(verifier, "_TOTAL_TIMEOUT_SECONDS", 0.02):
            result = verifier._probe_with_browser(
                eligible_account(), playwright_factory=lambda: fake,
                monotonic=time.monotonic, utc_now=fixed_now,
            )
        self.assertEqual(result["errorCode"], "xiaohongshu_probe_cleanup_incomplete")
        self.assertEqual(
            result["cleanup"], {"closed": False, "aliveResourceCount": 1}
        )
        self.assertIs(type(result["cleanup"]["aliveResourceCount"]), int)
        self.assertEqual(
            fake.close_order,
            ["page", "context", "browser", "playwright"],
        )
        self.assertNotIn("secret", json.dumps(result))

    def test_probe_total_budget_also_bounds_hanging_cleanup(self):
        fake = AsyncHangingCleanupPlaywright()
        started_at = time.monotonic()
        with patch.object(verifier, "_TOTAL_TIMEOUT_SECONDS", 0.02):
            result = verifier._probe_with_browser(
                eligible_account(), playwright_factory=lambda: fake,
                monotonic=time.monotonic, utc_now=fixed_now,
            )
        elapsed = time.monotonic() - started_at
        self.assertLess(elapsed, 0.2)
        self.assertEqual(result["errorCode"], "xiaohongshu_probe_cleanup_incomplete")
        self.assertEqual(result["cleanup"]["aliveResourceCount"], 1)
        self.assertEqual(
            fake.close_order,
            ["page", "context", "browser", "playwright"],
        )

    def test_probe_rejects_missing_storage_state_without_path_disclosure(self):
        private_path = "/private/account-secret-storage-state.json"
        result = verifier._probe_with_browser(
            {**eligible_account(), "filePath": private_path},
            playwright_factory=lambda: FakePlaywright(),
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["errorCode"], "xiaohongshu_session_state_invalid"
        )
        self.assertEqual(
            result["cleanup"], {"closed": True, "aliveResourceCount": 0}
        )
        self.assertNotIn(private_path, json.dumps(result))

    def test_execute_requires_exactly_one_normal_xiaohongshu_account(self):
        cases = (
            [],
            [{"id": 1, "type": 1, "status": 0}],
            [{"id": 1, "type": 1, "status": 1},
             {"id": 2, "type": 1, "status": 1}],
            [{"id": 1, "type": True, "status": 1}],
        )
        for accounts in cases:
            with self.subTest(accounts=accounts), patch(
                "app_core.account_service.list_accounts", return_value=accounts
            ):
                with self.assertRaisesRegex(
                    verifier.ProbeFailure,
                    "^xiaohongshu_account_selection_required$",
                ):
                    verifier._select_single_eligible_account()

    def test_execute_accepts_only_platform_type_one_and_builtin_ids(self):
        account = {"id": 7, "type": 1, "status": 1, "filePath": "state.json"}
        with patch("app_core.account_service.list_accounts", return_value=[account]):
            selected = verifier._select_single_eligible_account()
        self.assertEqual(selected, account)

    def test_default_mode_is_zero_action_plan(self):
        with patch("tools.verify_xiaohongshu_data_contract._execute") as execute:
            output = io.StringIO()
            code = verifier.main([], stdout=output)
        self.assertEqual(code, 0)
        execute.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "plan")
        self.assertEqual(payload["phases"], [
            "account_overview", "content_list", "content_lifetime"
        ])

    def test_report_sanitizer_drops_values_and_sensitive_keys(self):
        report = verifier.sanitize_probe_report({
            "responses": [{
                "url": "https://creator.xiaohongshu.com/api/data?token=secret",
                "headers": {"Cookie": "secret"},
                "keys": ["data", "note_id", "title"],
                "sample": {"title": "private work"},
            }]
        })
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private work", encoded)
        self.assertEqual(report["responses"][0]["path"], "/api/data")

    def test_report_sanitizer_uses_safe_default_for_malformed_builtin_url(self):
        report = verifier.sanitize_probe_report({
            "responses": [{"url": "https://["}],
        })
        self.assertEqual(report["responses"][0]["path"], "")

    def test_report_sanitizer_caps_structural_text_and_rejects_subclasses(self):
        class UntrustedText(str):
            def __str__(self):
                raise AssertionError("sanitizer must not stringify untrusted data")

        report = verifier.sanitize_probe_report({
            "mode": UntrustedText("execute"),
            "platformType": True,
            "responses": [{
                "url": "https://creator.xiaohongshu.com/api/data",
                "status": True,
                "keyPaths": ["a" * 130] * 301,
            }] * 101,
        })
        self.assertEqual(report["mode"], "plan")
        self.assertEqual(report["platformType"], 1)
        self.assertEqual(len(report["responses"]), 100)
        self.assertEqual(report["responses"][0]["status"], 0)
        self.assertEqual(len(report["responses"][0]["keyPaths"]), 300)
        self.assertEqual(report["responses"][0]["keyPaths"][0], ":key")

    def test_report_sanitizer_rebuilds_builtin_containers_with_safe_defaults(self):
        report = verifier.sanitize_probe_report({
            "responses": "not-a-list",
            "cleanup": {"closed": "yes", "aliveResourceCount": True},
        })
        self.assertEqual(report["responses"], [])
        self.assertEqual(report["cleanup"], {"closed": True, "aliveResourceCount": 0})
        self.assertIs(type(report), dict)
        self.assertIs(type(report["responses"]), list)
        self.assertIs(type(report["cleanup"]), dict)

    def test_schema_review_retains_static_names_and_templates_ids(self):
        source = {"responses": [{
            "url": ("https://creator.xiaohongshu.com/api/galaxy/creator/"
                    "datacenter/note/67b196bf000000001d0368f8?token=secret"),
            "method": "GET", "status": 200,
            "contentType": "application/json",
            "keyPaths": ["data.note_list", "data.note_list[].note_id"],
            "fieldTypes": {"data.note_list": "list",
                           "data.note_list[].note_id": "str"},
            "listLengths": {"data.note_list": 1},
            "paginationKeys": ["data.total"],
            "sample": {"title": "private title", "cookie": "secret"},
        }]}
        review = verifier._review_schema(source)
        encoded = json.dumps(review, ensure_ascii=False)
        self.assertEqual(review["responses"][0]["path"],
                         "/api/galaxy/creator/datacenter/note/:id")
        self.assertIn("data.note_list[].note_id", encoded)
        for forbidden in ("67b196bf000000001d0368f8", "private title", "secret"):
            self.assertNotIn(forbidden, encoded)

    def test_schema_review_rejects_sensitive_keys_and_non_builtin_containers(self):
        class UntrustedDict(dict):
            pass
        source = {"responses": [{
            "path": "/api/data",
            "keyPaths": ["data.cookie", "data.authorization", "data.safe_key"],
            "fieldTypes": UntrustedDict({"data.safe_key": "int"}),
        }]}
        encoded = json.dumps(verifier._review_schema(source), ensure_ascii=False)
        self.assertNotIn("cookie", encoded.lower())
        self.assertNotIn("authorization", encoded.lower())
        self.assertNotIn("safe_key", encoded)

    def test_schema_review_does_not_evaluate_untrusted_url_truthiness(self):
        class UntrustedValue:
            def __bool__(self):
                raise AssertionError("untrusted truthiness evaluated")

        review = verifier._review_schema({"responses": [{
            "url": UntrustedValue(),
            "path": "/api/data",
        }]})

        self.assertEqual(review["responses"], [{"path": "/api/data"}])

    def test_schema_review_retains_only_builtin_observation_phase(self):
        class UntrustedPhase(str):
            pass

        review = verifier._review_schema({"responses": [
            {"path": "/api/data", "observationPhase": "data_analysis"},
            {"path": "/api/data", "observationPhase": UntrustedPhase(
                "content_lifetime"
            )},
        ]})

        self.assertEqual(
            review["responses"][0]["observationPhase"], "data_analysis"
        )
        self.assertNotIn("observationPhase", review["responses"][1])

    def test_review_cli_is_execute_only_and_persisted_report_stays_sanitized(self):
        payload = verifier.build_plan()
        payload.update({"mode": "execute", "status": "failed",
                        "errorCode": "xiaohongshu_contracts_unobserved",
                        "navigation": [{"phase": "account_home",
                                        "path": "/creator/home"}],
                        "responses": [{"path": "/api/galaxy/creator/datacenter/list",
                                       "keyPaths": ["data.note_list"],
                                       "fieldTypes": {"data.note_list": "list"}}]})
        output = io.StringIO()
        with patch.object(verifier, "_execute", return_value=payload), \
             patch.object(verifier, "_write_report") as writer:
            code = verifier.main(["--execute", "--review-schema"], stdout=output)
        self.assertEqual(code, 1)
        reviewed_payload = json.loads(output.getvalue())
        self.assertEqual(reviewed_payload["schemaReview"]["navigation"], [
            {"phase": "account_home", "path": "/creator/home"},
        ])
        self.assertNotIn("navigation", reviewed_payload)
        writer.assert_not_called()
        invalid = io.StringIO()
        self.assertEqual(verifier.main(["--review-schema"], stdout=invalid), 2)

    def test_review_cli_report_keeps_review_only_in_stdout(self):
        payload = verifier.build_plan()
        payload.update({
            "mode": "execute",
            "status": "failed",
            "errorCode": "xiaohongshu_contracts_unobserved",
            "navigation": [{"phase": "account_home", "path": "/creator/home"}],
            "responses": [{"path": "/api/data"}],
        })
        output = io.StringIO()
        with patch.object(verifier, "_execute", return_value=payload):
            code = verifier.main([
                "--execute", "--review-schema", "--report", str(self.report_path),
            ], stdout=output)

        self.assertEqual(code, 1)
        stdout_payload = json.loads(output.getvalue())
        persisted = json.loads(self.report_path.read_text("utf-8"))
        self.assertIn("schemaReview", stdout_payload)
        self.assertEqual(stdout_payload["schemaReview"]["navigation"], [
            {"phase": "account_home", "path": "/creator/home"},
        ])
        self.assertNotIn("schemaReview", persisted)
        self.assertNotIn("navigation", persisted)

    def test_persisted_report_is_finally_sanitized(self):
        injected = {
            "status": "success",
            "account": {"nickname": "private", "phone": "13800000000"},
            "responses": [{
                "url": "https://creator.xiaohongshu.com/api?cookie=secret",
                "keyPaths": ["data.items[].note_id"],
                "raw": {"note_id": "private-id", "title": "private-title"},
            }],
            "cleanup": {"closed": True, "aliveResourceCount": 0, "unknown": "secret"},
        }
        verifier._write_report(self.report_path, injected)
        text = self.report_path.read_text("utf-8")
        for forbidden in ("13800000000", "private-id", "private-title", "cookie=", "secret"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(json.loads(text)["responses"][0]["path"], "/api")

    def test_persisted_report_redacts_dynamic_object_keys_and_allowed_slot_injections(self):
        uuid_key = "123e4567-e89b-12d3-a456-426614174000"
        phone_key = "13800138000"
        entropy_key = "AbCdEf0123456789GhIjKlMn"
        sensitive_keys = (
            uuid_key, phone_key, entropy_key, "token", "cookie", "title",
            "nickname",
        )
        payload = {
            "data": {
                "view_count": 1,
                uuid_key: {"like_count": 2},
                phone_key: [{"comment_count": 3}],
                entropy_key: {"share_count": 4},
                "token": "private-token-value",
                "cookie": "private-cookie-value",
                "title": "private-title-value",
                "nickname": "private-nickname-value",
                "note_id": "private-note-id-value",
            }
        }
        shape = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/data",
            "application/json",
            payload,
        ))
        self.assertIsNotNone(shape)
        collected = json.dumps(shape, ensure_ascii=False)
        for forbidden in (*sensitive_keys, "private-"):
            self.assertNotIn(forbidden, collected)
        self.assertIn("data.view_count", shape["keyPaths"])
        self.assertIn("data.:key.like_count", shape["keyPaths"])
        self.assertIn("data.:key[].comment_count", shape["keyPaths"])
        self.assertIn("data.note_id", shape["keyPaths"])

        verifier._write_report(self.report_path, {
            "schemaVersion": entropy_key,
            "platformType": 1,
            "mode": entropy_key,
            "phases": ["account_overview", uuid_key, "note_id"],
            "missingPhases": [phone_key, "content_list"],
            "status": phone_key,
            "errorCode": "token",
            "observedAt": uuid_key,
            "responses": [{
                "method": entropy_key,
                "path": "/api/data",
                "status": 200,
                "contentType": "cookie",
                "keyPaths": [
                    "data.view_count",
                    f"data.{uuid_key}.like_count",
                    f"data.{phone_key}[].comment_count",
                    "data.note_id",
                    "data.title",
                ],
                "fieldTypes": {
                    "data.view_count": "int",
                    f"data.{entropy_key}.share_count": "nickname",
                },
                "listLengths": {f"data.{phone_key}": 1},
                "paginationKeys": [f"data.{uuid_key}.next_cursor"],
            }],
            "cleanup": {"closed": True, "aliveResourceCount": 0},
        })
        text = self.report_path.read_text("utf-8")
        persisted = json.loads(text)
        for forbidden in (*sensitive_keys, "private-"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(
            persisted["schemaVersion"],
            "xiaohongshu-data-contract-probe/v1",
        )
        self.assertEqual(persisted["mode"], "plan")
        self.assertEqual(persisted["status"], "planned")
        self.assertEqual(persisted["errorCode"], "")
        response = persisted["responses"][0]
        self.assertEqual(response["method"], "")
        self.assertEqual(response["contentType"], "")
        self.assertIn("data.view_count", response["keyPaths"])
        self.assertIn("data.:key.like_count", response["keyPaths"])
        self.assertIn("data.note_id", response["keyPaths"])
        self.assertEqual(response["fieldTypes"]["data.view_count"], "int")

    def test_unknown_short_structural_keys_are_redacted_at_both_boundaries(self):
        shape = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/data",
            "application/json",
            {
                "data": {
                    "andy": {"view_count": 1},
                    "items": [{"mars": {"like_count": 2}}],
                },
            },
        ))
        self.assertIsNotNone(shape)
        self.assertIn("data.:key.view_count", shape["keyPaths"])
        self.assertIn("data.items[].:key.like_count", shape["keyPaths"])
        collected = json.dumps(shape, ensure_ascii=False)
        self.assertNotIn("andy", collected)
        self.assertNotIn("mars", collected)

        verifier._write_report(self.report_path, {
            "mode": "execute",
            "responses": [{
                "method": "GET",
                "path": "/api/data",
                "status": 200,
                "contentType": "application/json",
                "keyPaths": [
                    "data.andy.view_count",
                    "data.items[].mars.like_count",
                ],
                "fieldTypes": {
                    "data.andy.view_count": "int",
                    "data.items[].mars.like_count": "int",
                },
            }],
            "cleanup": {"closed": True, "aliveResourceCount": 0},
        })
        persisted_text = self.report_path.read_text("utf-8")
        persisted = json.loads(persisted_text)
        self.assertEqual(
            persisted["responses"][0]["keyPaths"],
            ["data.:key.view_count", "data.items[].:key.like_count"],
        )
        self.assertEqual(
            persisted["responses"][0]["fieldTypes"],
            {
                "data.:key.view_count": "int",
                "data.items[].:key.like_count": "int",
            },
        )
        self.assertNotIn("andy", persisted_text)
        self.assertNotIn("mars", persisted_text)

    def test_report_writer_rejects_escape_and_untrusted_path_types(self):
        class UntrustedPath(str):
            def __str__(self):
                raise AssertionError("writer must not stringify untrusted paths")

        paths = (
            self.report_path.parent / "nested" / ".." / "probe-report.json",
            self.report_path.parent.parent / "private-report.json",
            UntrustedPath("private-path-secret"),
        )
        for path in paths:
            with self.subTest(path=type(path).__name__):
                with self.assertRaises(verifier.ProbeFailure) as raised:
                    verifier._write_report(path, {"status": "success"})
                self.assertEqual(
                    str(raised.exception), "xiaohongshu_report_path_invalid"
                )
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn("secret", str(raised.exception))

    def test_report_writer_creates_missing_sdd_tree_from_repository_root_fd(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory) / "clean-repository"
            repository_root.mkdir()
            output_directory = (
                repository_root
                / ".superpowers/sdd/2026-08-20-xiaohongshu-data-contract-discovery"
            )
            destination = output_directory / "probe-report.json"

            with patch.object(
                verifier, "_REPOSITORY_ROOT", repository_root, create=True
            ), patch.object(
                verifier, "_REPORT_OUTPUT_DIRECTORY", output_directory
            ):
                try:
                    verifier._write_report(destination, verifier.build_plan())
                except verifier.ProbeFailure:
                    self.fail("writer did not create the missing clean-checkout tree")

            self.assertTrue(destination.is_file())
            self.assertEqual(
                json.loads(destination.read_text("utf-8")),
                verifier.build_plan(),
            )
            self.assertTrue((repository_root / ".superpowers").is_dir())
            self.assertTrue((repository_root / ".superpowers/sdd").is_dir())

    def test_report_writer_removes_same_directory_temporary_file_on_replace_failure(self):
        with patch.object(verifier.os, "replace", side_effect=OSError("private failure")):
            with self.assertRaisesRegex(
                verifier.ProbeFailure, "^xiaohongshu_report_write_failed$"
            ) as raised:
                verifier._write_report(self.report_path, {"status": "success"})
        self.assertIsNone(raised.exception.__cause__)
        self.assertFalse(self.report_path.exists())
        self.assertEqual(
            list(self.report_path.parent.glob(f".{self.report_path.name}.*.tmp")),
            [],
        )

    def test_report_writer_rejects_symlink_output_escape(self):
        escape_link = self.report_path.parent / "probe-report-test-escape"
        escape_link.symlink_to(self.report_path.parent.parent, target_is_directory=True)
        try:
            with self.assertRaisesRegex(
                verifier.ProbeFailure, "^xiaohongshu_report_path_invalid$"
            ) as raised:
                verifier._write_report(
                    escape_link / "private-report.json", {"status": "success"}
                )
            self.assertIsNone(raised.exception.__cause__)
        finally:
            escape_link.unlink(missing_ok=True)

    def test_persisted_report_templates_dynamic_endpoint_path_segments(self):
        dynamic_segments = (
            "123e4567-e89b-12d3-a456-426614174000",
            "12345678901234567890",
            "67b196bf000000001d0368f8",
        )
        verifier._write_report(self.report_path, {
            "responses": [
                {"url": f"https://creator.xiaohongshu.com/api/note/{segment}?token=secret"}
                for segment in dynamic_segments
            ] + [{
                "url": "https://creator.xiaohongshu.com/api/data?token=secret",
            }, {
                "url": "https://creator.xiaohongshu.com/api/note/private-work-id",
            }],
        })
        text = self.report_path.read_text("utf-8")
        report = json.loads(text)
        self.assertEqual(
            [response["path"] for response in report["responses"]],
            [
                "/api/note/:id", "/api/note/:id", "/api/note/:id",
                "/api/data", "/api/note/:id",
            ],
        )
        for segment in (*dynamic_segments, "private-work-id", "token=secret"):
            self.assertNotIn(segment, text)

    def test_collector_templates_dynamic_endpoint_path_segments(self):
        metadata = verifier._response_metadata(FakeResponse(
            "https://creator.xiaohongshu.com/api/note/67b196bf000000001d0368f8?token=secret",
            "application/json",
            {},
        ))
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata[0], "/api/note/:id")

    def test_persisted_report_templates_static_named_dynamic_ids(self):
        verifier._write_report(self.report_path, {
            "responses": [{
                "url": "https://creator.xiaohongshu.com/api/note/data",
            }, {
                "url": "https://creator.xiaohongshu.com/api/content/overview",
            }, {
                "url": "https://creator.xiaohongshu.com/api/data",
            }],
        })
        text = self.report_path.read_text("utf-8")
        self.assertEqual(
            [response["path"] for response in json.loads(text)["responses"]],
            ["/api/note/:id", "/api/content/:id", "/api/data"],
        )
        self.assertNotIn("/api/note/data", text)
        self.assertNotIn("/api/content/overview", text)

    def test_report_writer_does_not_follow_parent_swapped_after_revalidation(self):
        root = self.report_path.parent
        parent = root / "probe-report-race-parent"
        moved_parent = root / "probe-report-race-moved"
        outside_parent = root.parent / "probe-report-race-outside"
        destination = parent / "probe-report.json"
        outside_report = outside_parent / "probe-report.json"
        for directory in (parent, moved_parent, outside_parent):
            if directory.is_symlink():
                directory.unlink()
            elif directory.exists():
                shutil.rmtree(directory)
        parent.mkdir()
        outside_parent.mkdir()
        original_destination = verifier._report_destination
        calls = 0

        def swap_after_revalidation(path):
            nonlocal calls
            result = original_destination(path)
            calls += 1
            if calls == 2:
                parent.rename(moved_parent)
                parent.symlink_to(outside_parent, target_is_directory=True)
            return result

        try:
            with patch.object(
                verifier, "_report_destination", side_effect=swap_after_revalidation
            ):
                with self.assertRaisesRegex(
                    verifier.ProbeFailure, "^xiaohongshu_report_write_failed$"
                ) as raised:
                    verifier._write_report(destination, {"status": "success"})
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(calls, 2)
            self.assertFalse(outside_report.exists())
        finally:
            if parent.is_symlink():
                parent.unlink()
            for directory in (moved_parent, outside_parent):
                if directory.exists():
                    shutil.rmtree(directory)

    def test_main_execute_with_report_runs_probe_and_persists_final_report(self):
        observed = verifier.build_plan()
        observed.update({
            "mode": "execute",
            "phases": [],
            "missingPhases": [
                "account_overview", "content_list", "content_lifetime",
            ],
            "status": "failed",
            "errorCode": "xiaohongshu_probe_failed",
            "navigation": [{"phase": "account_home",
                            "path": "/creator/home"}],
        })
        stdout = io.StringIO()
        writer_inputs = []
        original_writer = verifier._write_report

        def record_writer_input(destination, payload):
            writer_inputs.append(payload)
            return original_writer(destination, payload)

        with patch.object(verifier, "_execute", return_value=observed) as execute, \
             patch.object(verifier, "_write_report", side_effect=record_writer_input):
            exit_code = verifier.main(
                ["--execute", "--report", str(self.report_path)],
                stdout=stdout,
            )

        self.assertEqual(exit_code, 1)
        execute.assert_called_once()
        self.assertTrue(self.report_path.is_file())
        expected = verifier.sanitize_probe_report(observed)
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        self.assertEqual(json.loads(self.report_path.read_text("utf-8")), expected)
        self.assertEqual(len(writer_inputs), 1)
        self.assertNotIn("navigation", writer_inputs[0])

    def test_main_exit_code_matches_terminal_status_matrix(self):
        reviewed_paths = {
            "/api/overview": "account_overview",
            "/api/data/list": "content_list",
            "/api/note/:id/metrics": "content_lifetime",
        }
        shapes = [
            verifier._response_shape(FakeResponse(
                "https://creator.xiaohongshu.com/api/overview",
                "application/json",
                {"data": {"trend": {"fans": 2}}},
            )),
            verifier._response_shape(FakeResponse(
                "https://creator.xiaohongshu.com/api/data/list",
                "application/json",
                {"data": {"items": [{"note_id": "private-id"}]}},
            )),
            verifier._response_shape(FakeResponse(
                "https://creator.xiaohongshu.com/api/note/123/metrics",
                "application/json",
                {
                    "data": {
                        "note_id": "private-id",
                        "lifetime": {"view_count": 3},
                    },
                },
            )),
        ]
        self.assertNotIn(None, shapes)

        def execution_report(responses):
            return verifier.sanitize_probe_report({
                "schemaVersion": "xiaohongshu-data-contract-probe/v1",
                "platformType": 1,
                "mode": "execute",
                "observedAt": "2026-08-20T12:00:00+00:00",
                "responses": responses,
                "cleanup": {"closed": True, "aliveResourceCount": 0},
            })

        with patch.object(
            verifier,
            "_REVIEWED_CONTRACT_PATH_TEMPLATES",
            reviewed_paths,
        ):
            reports = (
                ("success", execution_report(shapes), 0),
                ("partial_success", execution_report(shapes[:1]), 1),
                ("failed", execution_report([]), 1),
            )
            for label, observed, expected_exit_code in reports:
                with self.subTest(status=label), patch.object(
                    verifier, "_execute", return_value=observed
                ):
                    stdout = io.StringIO()
                    exit_code = verifier.main(
                        ["--execute", "--report", str(self.report_path)],
                        stdout=stdout,
                    )
                persisted = json.loads(self.report_path.read_text("utf-8"))
                emitted = json.loads(stdout.getvalue())
                self.assertEqual(exit_code, expected_exit_code)
                self.assertEqual(emitted["status"], label)
                self.assertEqual(persisted, emitted)

        plan_stdout = io.StringIO()
        self.assertEqual(verifier.main([], stdout=plan_stdout), 0)
        self.assertEqual(json.loads(plan_stdout.getvalue())["status"], "planned")

        arguments_stdout = io.StringIO()
        self.assertEqual(
            verifier.main(["--execute", "--unknown"], stdout=arguments_stdout),
            2,
        )
        self.assertEqual(
            json.loads(arguments_stdout.getvalue())["status"], "failed"
        )

        path_stdout = io.StringIO()
        outside_path = self.report_path.parent.parent / "private-report.json"
        with patch.object(verifier, "_execute") as execute:
            self.assertEqual(
                verifier.main(
                    ["--execute", "--report", str(outside_path)],
                    stdout=path_stdout,
                ),
                1,
            )
        execute.assert_not_called()
        self.assertEqual(json.loads(path_stdout.getvalue())["status"], "failed")

        writer_stdout = io.StringIO()
        with patch.object(
            verifier, "_execute", return_value=execution_report([])
        ), patch.object(
            verifier,
            "_write_report",
            side_effect=verifier.ProbeFailure(
                "xiaohongshu_report_write_failed"
            ),
        ):
            self.assertEqual(
                verifier.main(
                    ["--execute", "--report", str(self.report_path)],
                    stdout=writer_stdout,
                ),
                1,
            )
        self.assertEqual(json.loads(writer_stdout.getvalue())["status"], "failed")

        for failure in (KeyboardInterrupt(), SystemExit()):
            with self.subTest(process_control=type(failure).__name__), patch.object(
                verifier, "_execute", side_effect=failure
            ):
                with self.assertRaises(type(failure)) as raised:
                    verifier.main(["--execute"], stdout=io.StringIO())
                self.assertIs(raised.exception, failure)

    def test_main_execute_with_report_persists_fixed_account_gate_failure(self):
        stdout = io.StringIO()

        with patch.object(
            verifier,
            "_execute",
            side_effect=verifier.ProbeFailure(
                "xiaohongshu_account_selection_required"
            ),
        ):
            exit_code = verifier.main(
                ["--execute", "--report", str(self.report_path)],
                stdout=stdout,
            )

        self.assertEqual(exit_code, 1)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["mode"], "execute")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(
            report["errorCode"], "xiaohongshu_account_selection_required"
        )
        self.assertEqual(report["responses"], [])
        self.assertEqual(
            report["cleanup"], {"closed": True, "aliveResourceCount": 0}
        )
        self.assertEqual(
            json.loads(self.report_path.read_text("utf-8")), report
        )

    def test_preprobe_failures_report_all_contracts_missing(self):
        required = ["account_overview", "content_list", "content_lifetime"]

        def assert_all_missing(report):
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["responses"], [])
            self.assertEqual(report["phases"], [])
            self.assertEqual(report["missingPhases"], required)

        for error_code in (
            "xiaohongshu_account_selection_required",
            "xiaohongshu_probe_failed",
        ):
            with self.subTest(error_code=error_code), patch.object(
                verifier,
                "_execute",
                side_effect=verifier.ProbeFailure(error_code),
            ):
                stdout = io.StringIO()
                exit_code = verifier.main(["--execute"], stdout=stdout)
                self.assertEqual(exit_code, 1)
                report = json.loads(stdout.getvalue())
                self.assertEqual(report["errorCode"], error_code)
                assert_all_missing(report)

        stdout = io.StringIO()
        exit_code = verifier.main(["--execute", "--unknown"], stdout=stdout)
        self.assertNotEqual(exit_code, 0)
        arguments_report = json.loads(stdout.getvalue())
        self.assertEqual(
            arguments_report["errorCode"], "xiaohongshu_arguments_invalid"
        )
        assert_all_missing(arguments_report)

        stdout = io.StringIO()
        outside_path = self.report_path.parent.parent / "private-report.json"
        with patch.object(verifier, "_execute") as execute:
            exit_code = verifier.main(
                ["--execute", "--report", str(outside_path)], stdout=stdout
            )
        self.assertNotEqual(exit_code, 0)
        execute.assert_not_called()
        path_report = json.loads(stdout.getvalue())
        self.assertEqual(
            path_report["errorCode"], "xiaohongshu_report_path_invalid"
        )
        assert_all_missing(path_report)

        account_shape = verifier._response_shape(FakeResponse(
            "https://creator.xiaohongshu.com/api/overview",
            "application/json",
            {"data": {"trend": {"fans": 2}}},
        ))
        with patch.object(
            verifier,
            "_REVIEWED_CONTRACT_PATH_TEMPLATES",
            {"/api/overview": "account_overview"},
        ):
            partial_source = verifier.sanitize_probe_report({
                "schemaVersion": "xiaohongshu-data-contract-probe/v1",
                "platformType": 1,
                "mode": "execute",
                "status": "partial_success",
                "errorCode": "xiaohongshu_contracts_incomplete",
                "observedAt": "2026-08-20T12:00:00+00:00",
                "responses": [account_shape],
                "cleanup": {"closed": True, "aliveResourceCount": 0},
            })
            writer_failure = verifier._failed_execution_report(
                "xiaohongshu_report_write_failed", partial_source
            )
        self.assertEqual(writer_failure["status"], "failed")
        self.assertEqual(
            writer_failure["errorCode"], "xiaohongshu_report_write_failed"
        )
        self.assertEqual(writer_failure["phases"], ["account_overview"])
        self.assertEqual(
            writer_failure["missingPhases"],
            ["content_list", "content_lifetime"],
        )

    def test_main_prevalidates_report_destination_before_execute(self):
        outside_path = self.report_path.parent.parent / "private-report.json"
        stdout = io.StringIO()

        with patch.object(verifier, "_execute") as execute:
            try:
                exit_code = verifier.main(
                    ["--execute", "--report", str(outside_path)],
                    stdout=stdout,
                )
            except verifier.ProbeFailure:
                self.fail("main leaked report destination validation failure")

        self.assertNotEqual(exit_code, 0)
        execute.assert_not_called()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["mode"], "execute")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(
            report["errorCode"], "xiaohongshu_report_path_invalid"
        )
        self.assertEqual(report["responses"], [])
        self.assertIs(type(report["cleanup"]["closed"]), bool)
        self.assertIs(type(report["cleanup"]["aliveResourceCount"]), int)
        self.assertNotIn(str(outside_path), stdout.getvalue())

    def test_main_prevalidation_redacts_untrusted_filesystem_failure(self):
        stdout = io.StringIO()

        with patch.object(
            verifier,
            "_report_destination",
            side_effect=OSError("private path failure"),
        ), patch.object(verifier, "_execute") as execute:
            try:
                exit_code = verifier.main(
                    ["--execute", "--report", str(self.report_path)],
                    stdout=stdout,
                )
            except OSError:
                self.fail("main leaked report destination filesystem failure")

        self.assertNotEqual(exit_code, 0)
        execute.assert_not_called()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(
            report["errorCode"], "xiaohongshu_report_path_invalid"
        )
        self.assertNotIn("private", stdout.getvalue())

    def test_main_preserves_process_control_from_report_validation_and_writer(self):
        for failure in (KeyboardInterrupt(), SystemExit()):
            with self.subTest(stage="validation", failure=type(failure).__name__), \
                    patch.object(
                        verifier, "_report_destination", side_effect=failure
                    ), patch.object(verifier, "_execute") as execute:
                with self.assertRaises(type(failure)) as raised:
                    verifier.main(
                        ["--execute", "--report", str(self.report_path)],
                        stdout=io.StringIO(),
                    )
                self.assertIs(raised.exception, failure)
                execute.assert_not_called()

            observed = verifier.build_plan()
            observed.update({"mode": "execute", "status": "failed"})
            with self.subTest(stage="writer", failure=type(failure).__name__), \
                    patch.object(
                        verifier, "_execute", return_value=observed
                    ), patch.object(
                        verifier, "_write_report", side_effect=failure
                    ):
                with self.assertRaises(type(failure)) as raised:
                    verifier.main(
                        ["--execute", "--report", str(self.report_path)],
                        stdout=io.StringIO(),
                    )
                self.assertIs(raised.exception, failure)

    def test_main_writer_failure_outputs_fixed_sanitized_nonzero_terminal(self):
        observed = verifier.build_plan()
        observed.update({
            "mode": "execute",
            "status": "success",
            "cleanup": {"closed": False, "aliveResourceCount": 2},
        })
        stdout = io.StringIO()

        with patch.object(verifier, "_execute", return_value=observed), patch.object(
            verifier,
            "_write_report",
            side_effect=verifier.ProbeFailure(
                "xiaohongshu_report_write_failed"
            ),
        ):
            try:
                exit_code = verifier.main(
                    ["--execute", "--report", str(self.report_path)],
                    stdout=stdout,
                )
            except verifier.ProbeFailure:
                self.fail("main leaked report writer failure")

        self.assertNotEqual(exit_code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["mode"], "execute")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(
            report["errorCode"], "xiaohongshu_report_write_failed"
        )
        self.assertEqual(
            report["cleanup"], {"closed": False, "aliveResourceCount": 2}
        )
        self.assertIs(type(report["cleanup"]["closed"]), bool)
        self.assertIs(type(report["cleanup"]["aliveResourceCount"]), int)
        self.assertNotIn(str(self.report_path), stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())

    def test_main_rejects_malformed_execute_arguments_before_import_or_action(self):
        malformed_arguments = (
            ["--execute", "--report"],
            ["--execute", "--unknown"],
            ["--execute", "extra"],
            ["--execute", "--report", str(self.report_path), "extra"],
        )
        original_import = __import__

        def reject_runtime_import(name, *args, **kwargs):
            if name == "app_core" or name.startswith("app_core."):
                raise AssertionError("invalid arguments imported account runtime")
            if name == "playwright" or name.startswith("playwright."):
                raise AssertionError("invalid arguments imported browser runtime")
            return original_import(name, *args, **kwargs)

        for arguments in malformed_arguments:
            with self.subTest(arguments=arguments), patch.object(
                verifier, "_execute"
            ) as execute, patch(
                "builtins.__import__", side_effect=reject_runtime_import
            ):
                stdout = io.StringIO()
                exit_code = verifier.main(arguments, stdout=stdout)

            self.assertNotEqual(exit_code, 0)
            execute.assert_not_called()
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["mode"], "execute")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(
                report["errorCode"], "xiaohongshu_arguments_invalid"
            )
            self.assertEqual(report["responses"], [])
            self.assertIs(type(report["cleanup"]["closed"]), bool)
            self.assertIs(type(report["cleanup"]["aliveResourceCount"]), int)
            self.assertFalse(self.report_path.exists())
            for argument in arguments:
                self.assertNotIn(argument, stdout.getvalue())

    def test_main_bare_execute_remains_legal(self):
        observed = verifier.build_plan()
        observed.update({"mode": "execute", "status": "failed"})
        stdout = io.StringIO()

        with patch.object(verifier, "_execute", return_value=observed) as execute:
            exit_code = verifier.main(["--execute"], stdout=stdout)

        self.assertEqual(exit_code, 1)
        execute.assert_called_once()
        self.assertEqual(
            json.loads(stdout.getvalue()), verifier.sanitize_probe_report(observed)
        )

    def test_execute_stdout_omits_navigation_without_schema_review(self):
        observed = verifier.build_plan()
        observed.update({
            "mode": "execute",
            "status": "failed",
            "errorCode": "xiaohongshu_contracts_unobserved",
            "navigation": [{"phase": "account_home",
                            "path": "/creator/home"}],
        })
        stdout = io.StringIO()

        with patch.object(verifier, "_execute", return_value=observed):
            exit_code = verifier.main(["--execute"], stdout=stdout)

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertNotIn("navigation", payload)
        self.assertNotIn("schemaReview", payload)
