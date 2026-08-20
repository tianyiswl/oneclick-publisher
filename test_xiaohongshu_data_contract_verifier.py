import io
import asyncio
import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
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

    def json(self):
        type(self).json_calls += 1
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
        self.assertIn("items[].item_0", response["fieldTypes"])
        self.assertNotIn("items[].item_999", response["fieldTypes"])
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
        self.assertIn("self", result["responses"][0]["keyPaths"])
        self.assertNotIn("self.self", result["responses"][0]["keyPaths"])
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

    def test_report_writer_removes_same_directory_temporary_file_on_replace_failure(self):
        with patch.object(Path, "replace", side_effect=OSError("private failure")):
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
