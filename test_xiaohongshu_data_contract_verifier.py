import io
import asyncio
import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import tools.verify_xiaohongshu_data_contract as verifier


class FakeResponse:
    def __init__(self, url, content_type, payload):
        self.url = url
        self.headers = {"content-type": content_type}
        self._payload = payload

    def json(self):
        return self._payload


class FakePage:
    fetch_calls = 0
    click_calls = 0

    def __init__(self, owner):
        self.owner = owner
        self.response_callback = None
        self.goto_url = None

    def on(self, event, callback):
        self.owner.events.append(("on", event))
        self.response_callback = callback

    def goto(self, url, **_kwargs):
        self.owner.events.append(("goto", url))
        self.goto_url = url
        if self.owner.failure is not None:
            raise self.owner.failure
        for response in self.owner.responses:
            self.response_callback(response)

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
    def __init__(self, responses=(), failure=None):
        self.responses = responses
        self.failure = failure
        self.close_order = []
        self.events = []
        self.context_kwargs = None
        self.launch_kwargs = None
        self.page = FakePage(self)
        self.chromium = FakeChromium(self)

    def close(self):
        self.close_order.append("playwright")


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
            self.response_callback(response)

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
            {"data": {"fans": 2}},
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


class XiaohongshuDataContractVerifierTests(unittest.TestCase):
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
        self.assertEqual(
            fake.page.goto_url,
            "https://creator.xiaohongshu.com/creator/home",
        )
        self.assertEqual(fake.events[0], ("on", "response"))
        self.assertEqual(fake.launch_kwargs, {"headless": True})

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

    def test_probe_adapts_async_playwright_and_reads_shapes_before_cleanup(self):
        fake = AsyncFakePlaywright()
        result = verifier._probe_with_browser(
            eligible_account(), playwright_factory=lambda: fake,
            monotonic=FakeClock(), utc_now=fixed_now,
        )
        self.assertEqual(len(result["responses"]), 1)
        self.assertEqual(result["responses"][0]["contentType"], "application/json")
        self.assertEqual(result["responses"][0]["fieldTypes"]["data.fans"], "int")
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
                "url": "https://creator.xiaohongshu.com/" + "a" * 130,
                "status": True,
                "keyPaths": ["a" * 130] * 301,
            }] * 101,
        })
        self.assertEqual(report["mode"], "plan")
        self.assertEqual(report["platformType"], 1)
        self.assertEqual(len(report["responses"]), 100)
        self.assertEqual(report["responses"][0]["status"], 0)
        self.assertEqual(len(report["responses"][0]["keyPaths"]), 300)
        self.assertEqual(len(report["responses"][0]["keyPaths"][0]), 120)

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
