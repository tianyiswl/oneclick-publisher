# -*- coding: utf-8 -*-
"""抖音带货平台设置隔离采集器测试。"""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Mapping
from unittest import mock

from app_core import douyin_commerce_service, douyin_commerce_session
from app_core.douyin_commerce_collectors import (
    DouyinCommerceCollectorError,
    DouyinCommerceCollectorManager,
)
from app_core.douyin_commerce_probe import build_probe_upload_payload
from app_core.douyin_commerce_setup_state import CollectorType
from tools.verify_douyin_commerce_collectors import (
    _public_close_result,
    _write_report,
    build_verification_sequences,
    main as verification_main,
)


class FakeSessionManager:
    """仅替代真实浏览器边界，保留协调器的真实状态与队列行为。"""

    def __init__(self, manager_id: int) -> None:
        self.manager_id = manager_id
        self.session_id = ""
        self.start_payloads: list[dict[str, Any]] = []
        self.start_started = threading.Event()
        self.start_thread_ident: int | None = None
        self.release_start: threading.Event | None = None
        self.start_session_override: str | None = None
        self.start_result_override: dict[str, str] | None = None
        self.refresh_calls: list[str] = []
        self.location_calls: list[tuple[str, object, object, object]] = []
        self.location_scopes: list[object] = []
        self.close_calls: list[str | None] = []
        self.close_thread_idents: list[int] = []
        self.close_started = threading.Event()
        self.release_close: threading.Event | None = None
        self.close_during_location = False
        self.refresh_started = threading.Event()
        self.location_started = threading.Event()
        self.release_refresh: threading.Event | None = None
        self.release_location: threading.Event | None = None
        self.refresh_error: Exception | None = None
        self.search_error: Exception | None = None
        self.location_result_override: object | None = None
        self.close_failures_remaining = 0

    def start_upload(
        self,
        payload: Mapping[str, Any],
        *,
        on_progress=None,
    ) -> dict[str, str]:
        del on_progress
        self.start_payloads.append(dict(payload))
        self.start_thread_ident = threading.get_ident()
        self.start_started.set()
        if self.release_start is not None:
            self.release_start.wait(timeout=2)
        if self.start_result_override is not None:
            return dict(self.start_result_override)
        self.session_id = self.start_session_override or f"session-{self.manager_id}"
        return {"status": "ready", "sessionId": self.session_id}

    def refresh_favorite_music(self, session_id: str) -> list[dict[str, str]]:
        self.refresh_calls.append(session_id)
        self.refresh_started.set()
        if self.release_refresh is not None:
            self.release_refresh.wait(timeout=2)
        if self.refresh_error is not None:
            raise self.refresh_error
        return [
            {
                "musicId": f"music-{self.manager_id}",
                "title": "收藏音乐",
                "creator": "创作者",
                "duration": "00:30",
            }
        ]

    def search_locations(
        self,
        session_id: str,
        keyword: object,
        scope: object,
        *,
        commission_filter: object = "all",
    ) -> list[dict[str, Any]]:
        self.location_calls.append(
            (session_id, keyword, scope, commission_filter)
        )
        self.location_scopes.append(scope)
        self.location_started.set()
        if self.release_location is not None:
            self.release_location.wait(timeout=2)
        if self.search_error is not None:
            raise self.search_error
        if self.location_result_override is not None:
            return self.location_result_override
        return [
            {
                "poiId": f"poi-{self.manager_id}",
                "name": str(keyword),
                "address": "广西北海",
            }
        ]

    def close(self, session_id: str | None = None) -> None:
        self.close_started.set()
        if (
            self.location_started.is_set()
            and self.release_location is not None
            and not self.release_location.is_set()
        ):
            self.close_during_location = True
        self.close_calls.append(session_id)
        self.close_thread_idents.append(threading.get_ident())
        if self.release_close is not None:
            self.release_close.wait(timeout=2)
        if self.close_failures_remaining:
            self.close_failures_remaining -= 1
            raise RuntimeError("Cookie=secret DOM=<html>")


class FakeManagerFactory:
    def __init__(self) -> None:
        self.instances: list[FakeSessionManager] = []
        self.block_start_ids: set[int] = set()
        self.block_factory_ids: set[int] = set()
        self.factory_started: dict[int, threading.Event] = {}
        self.release_factory: dict[int, threading.Event] = {}
        self.start_session_overrides: dict[int, str] = {}
        self.start_result_overrides: dict[int, dict[str, str]] = {}
        self.errors_by_id: dict[int, Exception] = {}

    def __call__(self) -> FakeSessionManager:
        manager_id = len(self.instances) + 1
        if manager_id in self.block_factory_ids:
            started = self.factory_started.setdefault(manager_id, threading.Event())
            release = self.release_factory.setdefault(manager_id, threading.Event())
            started.set()
            release.wait(timeout=2)
        if manager_id in self.errors_by_id:
            raise self.errors_by_id[manager_id]
        instance = FakeSessionManager(manager_id)
        instance.start_session_override = self.start_session_overrides.get(manager_id)
        instance.start_result_override = self.start_result_overrides.get(manager_id)
        if instance.manager_id in self.block_start_ids:
            instance.release_start = threading.Event()
        self.instances.append(instance)
        return instance


class FakeVerificationManager:
    """仅实现验证器允许使用的协调器公开边界。"""

    def __init__(self) -> None:
        self.begin_payloads: list[dict[str, object]] = []
        self.actions: list[tuple[str, str, str]] = []
        self.close_calls: list[tuple[str | None, str]] = []
        self.current_generation_id = ""
        self.instance_ids: dict[str, str] = {}
        self.action_error_codes: dict[tuple[int, str], str] = {}

    def begin_generation(self, payload: Mapping[str, object]) -> dict[str, object]:
        self.begin_payloads.append(dict(payload))
        generation_number = len(self.begin_payloads)
        self.current_generation_id = f"generation-full-secret-{generation_number}"
        self.instance_ids = {
            "domestic_location": f"domestic-full-secret-{generation_number}",
            "favorite_music": f"music-full-secret-{generation_number}",
            "local_location": f"local-full-secret-{generation_number}",
        }
        return self.status(self.current_generation_id)

    def refresh_favorite_music(self, generation_id: str) -> dict[str, object]:
        self._raise_configured("music")
        self.actions.append((generation_id, "music", ""))
        return {"ok": True, "candidates": [{"title": "收藏音乐"}]}

    def search_locations(
        self,
        generation_id: str,
        keyword: object,
        scope: object,
    ) -> dict[str, object]:
        normalized_scope = str(scope)
        self._raise_configured(normalized_scope)
        self.actions.append((generation_id, normalized_scope, str(keyword)))
        return {
            "ok": True,
            "candidates": [
                {"poiId": f"poi-{normalized_scope}", "name": "夜南香", "address": "北海"}
            ],
        }

    def close_generation(
        self,
        generation_id: str | None = None,
        *,
        reason: str,
    ) -> dict[str, object]:
        self.close_calls.append((generation_id, reason))
        return {
            "closed": True,
            "setupGenerationId": generation_id or "",
            "aliveCollectorCount": 0,
            "cleanupResults": {
                "domestic_location": "closed",
                "favorite_music": "closed",
                "local_location": "closed",
            },
        }

    def status(self, generation_id: str | None = None) -> dict[str, object]:
        return {
            "setupGenerationId": generation_id or self.current_generation_id,
            "generationState": "collecting",
            "collectorInstanceIds": dict(self.instance_ids),
            "aliveCollectorCount": 3,
        }

    def recent_diagnostics(
        self,
        generation_id: str,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        del limit
        was_closed = any(
            closed_generation_id == generation_id
            for closed_generation_id, _reason in self.close_calls
        )
        return [
            {
                "timestamp": "2026-08-09T12:00:00+08:00",
                "setupGenerationId": generation_id,
                "collectorType": "domestic_location",
                "action": "close_generation" if was_closed else "search_locations",
                "candidateCount": 1,
                "outcome": "success",
                "errorCode": "",
                "cleanupResult": "closed" if was_closed else "",
            }
        ]

    def _raise_configured(self, action: str) -> None:
        generation_number = len(self.begin_payloads)
        code = self.action_error_codes.get((generation_number, action))
        if code:
            error = RuntimeError(code)
            error.code = code
            raise error


class ExitGateLock:
    """在指定线程第一次退出锁后制造确定性调度间隙。"""

    def __init__(self, lock: object, target_ident: int) -> None:
        self._lock = lock
        self._target_ident = target_ident
        self._gated = False
        self.exited = threading.Event()
        self.release = threading.Event()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        del exc_type, exc_value, traceback
        self._lock.release()
        if threading.get_ident() == self._target_ident and not self._gated:
            self._gated = True
            self.exited.set()
            self.release.wait(timeout=2)


class DouyinCommerceCollectorManagerTests(unittest.TestCase):
    _DIAGNOSTIC_FIELDS = {
        "timestamp",
        "requestId",
        "setupGenerationId",
        "collectorType",
        "collectorInstanceId",
        "accountMaskedId",
        "phase",
        "action",
        "scope",
        "keyword",
        "attempt",
        "candidateCount",
        "durationMs",
        "outcome",
        "errorCode",
        "cleanupResult",
    }

    def setUp(self) -> None:
        self.factory = FakeManagerFactory()
        self.events: list[dict[str, object]] = []
        self.manager = DouyinCommerceCollectorManager(
            manager_factory=self.factory,
            probe_payload_builder=lambda payload: {
                **payload,
                "fileList": ["probe.mp4"],
                "runtimeMode": "preflight",
                "debugDryRun": True,
            },
            event_sink=self.events.append,
        )
        self.upload_payload = {
            "type": 3,
            "workflow": "douyin-commerce",
            "commerceMode": "local-group-buy",
            "contentType": "video",
            "accountId": 31,
            "accountList": ["account.json"],
            "fileList": ["user-video.mp4"],
        }

    def tearDown(self) -> None:
        try:
            self.manager.close_generation(reason="test_cleanup")
        except Exception:
            pass

    def assert_complete_diagnostic(self, event: Mapping[str, object]) -> None:
        self.assertEqual(set(event), self._DIAGNOSTIC_FIELDS)
        self.assertTrue(event["timestamp"])
        self.assertTrue(event["requestId"])
        self.assertTrue(event["setupGenerationId"])
        self.assertTrue(event["collectorInstanceId"])
        self.assertEqual(event["accountMaskedId"], "account-31")
        self.assertIs(type(event["attempt"]), int)
        self.assertIs(type(event["candidateCount"]), int)
        self.assertIs(type(event["durationMs"]), int)

    def test_recent_diagnostics_records_successful_domestic_search(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        self.manager.search_locations(generation_id, "侨港风情街", "domestic")

        event = self.manager.recent_diagnostics(generation_id)[-1]
        self.assert_complete_diagnostic(event)
        self.assertEqual(event["collectorType"], "domestic_location")
        self.assertEqual(event["action"], "search_locations")
        self.assertEqual(event["scope"], "domestic")
        self.assertEqual(event["keyword"], "侨港风情街")
        self.assertEqual(event["attempt"], 1)
        self.assertEqual(event["candidateCount"], 1)
        self.assertEqual(event["outcome"], "success")
        self.assertEqual(event["errorCode"], "collector_unknown")

    def test_location_search_normalizes_and_passes_commission_filter_to_session_manager(self):
        """协调器必须在 POI 去重前把返佣筛选传给真实会话搜索边界。"""

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        try:
            self.manager.search_locations(
                generation_id,
                "侨港风情街",
                "domestic",
                commission_filter="commission",
            )
        except TypeError as exc:
            self.fail(f"地点搜索边界未接收返佣筛选：{exc}")

        self.assertEqual(
            self.factory.instances[0].location_calls,
            [
                (
                    self.factory.instances[0].session_id,
                    "侨港风情街",
                    "domestic",
                    "commission",
                )
            ],
        )

    def test_metadata_search_crosses_real_collector_session_and_service_when_filter_is_empty(self):
        """过滤空也必须经真实三层链路作为成功结果返回。"""

        class SearchInput:
            def __init__(self) -> None:
                self.value = ""
                self.scroll_into_view_if_needed = mock.AsyncMock()
                self.click = mock.AsyncMock()

            async def fill(self, value: str, **_kwargs) -> None:
                self.value = value

            async def evaluate(self, _script: str) -> str:
                return self.value

        class Page:
            def __init__(self) -> None:
                self.is_closed = mock.MagicMock(return_value=False)
                self.wait_for_timeout = mock.AsyncMock()

        page = Page()
        created: list[douyin_commerce_session.DouyinCommerceSessionManager] = []

        class OfflineSessionManager(
            douyin_commerce_session.DouyinCommerceSessionManager
        ):
            def start_upload(self, payload, *, on_progress=None):
                del on_progress
                session_id = f"offline-session-{len(created)}"
                self._session = douyin_commerce_session._CommerceEditorSession(
                    session_id=session_id,
                    upload_payload=dict(payload),
                    account_name="测试账号",
                    browser=None,
                    context=None,
                    page=page,
                    playwright=None,
                    uploader=None,
                )
                return {"status": "ready", "sessionId": session_id}

            def close_strict(self, session_id=None):
                del session_id
                self._session = None
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                if self._thread is not None:
                    self._thread.join(timeout=1)

            close = close_strict

        def build_session_manager():
            manager = OfflineSessionManager()
            created.append(manager)
            return manager

        manager = DouyinCommerceCollectorManager(
            manager_factory=build_session_manager,
            probe_payload_builder=lambda payload: {
                **payload,
                "fileList": ["probe.mp4"],
                "runtimeMode": "preflight",
                "debugDryRun": True,
            },
        )
        rows = [
            {
                "name": f"北海无佣地点 {index}",
                "address": f"广西北海市测试路 {index} 号",
                "commerceInfo": "3件商品 · 0件返佣",
                "unknown": "must-not-return",
            }
            for index in range(3)
        ]
        try:
            with mock.patch.object(
                douyin_commerce_service,
                "_ensure_position_tag",
                new_callable=mock.AsyncMock,
            ), mock.patch.object(
                douyin_commerce_service,
                "_ensure_local_group_buy_mode",
                new_callable=mock.AsyncMock,
                return_value=object(),
            ), mock.patch.object(
                douyin_commerce_service,
                "_open_commerce_search_input",
                new_callable=mock.AsyncMock,
                return_value=SearchInput(),
            ), mock.patch.object(
                douyin_commerce_service,
                "set_commerce_location_scope",
                new_callable=mock.AsyncMock,
                return_value="国内",
            ), mock.patch.object(
                douyin_commerce_service,
                "_visible_commerce_location_result_snapshot",
                new_callable=mock.AsyncMock,
                return_value=(object(), rows, "fresh-three-no-commission"),
            ), mock.patch.object(
                douyin_commerce_service,
                "close_commerce_store_selector",
                new_callable=mock.AsyncMock,
            ):
                generation_id = manager.begin_generation(self.upload_payload)[
                    "setupGenerationId"
                ]
                result = manager.search_locations(
                    generation_id,
                    "北海",
                    "domestic",
                    commission_filter="commission",
                    include_metadata=True,
                )
        finally:
            manager.close_generation(reason="test_cleanup")

        self.assertTrue(result["ok"])
        self.assertEqual(result["platformResultCount"], 3)
        self.assertEqual(result["candidates"], [])
        self.assertNotIn("rawCandidates", result)

    def test_real_probe_builder_preserves_account_id_for_runtime_and_diagnostics(self):
        """真实探针白名单必须把 UI 的整数账号 ID 交给协调器。"""

        factory = FakeManagerFactory()
        manager = DouyinCommerceCollectorManager(
            manager_factory=factory,
            probe_payload_builder=build_probe_upload_payload,
        )
        try:
            begun = manager.begin_generation(self.upload_payload)
            generation_id = begun["setupGenerationId"]
            with manager._state_lock:
                self.assertEqual(manager._runtime.generation.account_id, 31)

            manager.search_locations(generation_id, "侨港风情街", "domestic")

            event = manager.recent_diagnostics(generation_id)[-1]
            self.assertEqual(event["accountMaskedId"], "account-31")
            self.assertNotIn("account.json", str(event))
        finally:
            manager.close_generation(reason="test_cleanup")

    def test_untrusted_exception_string_methods_are_never_called_by_diagnostics(self):
        class SideEffectStringError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("candidate panel missing")
                self.calls = 0

            def __str__(self) -> str:
                self.calls += 1
                return "candidate panel missing"

        class RaisingStringError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("candidate panel missing")
                self.calls = 0

            def __str__(self) -> str:
                self.calls += 1
                raise RuntimeError("diagnostic string conversion must not run")

        class BlockingStringError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("candidate panel missing")
                self.calls = 0
                self.entered = threading.Event()
                self.release = threading.Event()

            def __str__(self) -> str:
                self.calls += 1
                self.entered.set()
                self.release.wait(timeout=0.2)
                return "candidate panel missing"

        for error_type in (
            SideEffectStringError,
            RaisingStringError,
            BlockingStringError,
        ):
            with self.subTest(error_type=error_type.__name__):
                factory = FakeManagerFactory()
                manager = DouyinCommerceCollectorManager(
                    manager_factory=factory,
                    probe_payload_builder=lambda payload: {
                        **payload,
                        "fileList": ["probe.mp4"],
                        "runtimeMode": "preflight",
                        "debugDryRun": True,
                    },
                )
                try:
                    generation_id = manager.begin_generation(self.upload_payload)[
                        "setupGenerationId"
                    ]
                    manager.refresh_favorite_music(generation_id)
                    untrusted_error = error_type()
                    factory.instances[1].refresh_error = untrusted_error

                    with self.assertLogs(
                        "app_core.douyin_commerce_collectors", level="WARNING"
                    ), self.assertRaises(DouyinCommerceCollectorError) as raised:
                        manager.refresh_favorite_music(generation_id)

                    self.assertEqual(raised.exception.code, "collector_unknown")
                    self.assertEqual(untrusted_error.calls, 0)
                finally:
                    manager.close_generation(reason="test-cleanup")

    def test_empty_location_candidates_use_fixed_error_and_diagnostic(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.factory.instances[0].location_result_override = []

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.search_locations(generation_id, "夜南香", "domestic")

        self.assertEqual(str(raised.exception), "candidate_empty")
        event = self.manager.recent_diagnostics(generation_id)[-1]
        self.assert_complete_diagnostic(event)
        self.assertEqual(event["candidateCount"], 0)
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["errorCode"], "candidate_empty")

    def test_music_failure_is_classified_and_only_safe_detail_reaches_log(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.manager.refresh_favorite_music(generation_id)
        self.factory.instances[1].refresh_error = RuntimeError(
            "candidate panel missing cookiesFile/account.json "
            "验证码123456 Cookie=secret DOM=<html>" + "private" * 80
        )

        with self.assertLogs(
            "app_core.douyin_commerce_collectors", level="WARNING"
        ) as captured, self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.refresh_favorite_music(generation_id)

        self.assertEqual(str(raised.exception), "candidate_panel_missing")
        self.assertIsNone(raised.exception.__cause__)
        event = self.manager.recent_diagnostics(generation_id)[-1]
        self.assert_complete_diagnostic(event)
        self.assertEqual(event["collectorType"], "favorite_music")
        self.assertEqual(event["action"], "refresh_favorite_music")
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["errorCode"], "candidate_panel_missing")
        combined = "\n".join(captured.output) + repr(
            self.manager.recent_diagnostics(generation_id)
        )
        for secret in (
            "cookiesFile/account.json",
            "123456",
            "secret",
            "<html>",
            "privateprivate",
        ):
            self.assertNotIn(secret, combined)
        lowered = combined.casefold()
        for marker in ("cookie=", "token=", "dom=", "<html"):
            self.assertNotIn(marker, lowered)
        self.assertIn("candidate_panel_missing", combined)
        safe_detail = self.manager._safe_diagnostic_detail(
            "/Users/andy/private/cookiesFile/account.json 验证码123456 "
            "Token=private DOM=<html> SessionId=session-secret " + "x" * 400
        )
        self.assertLessEqual(len(safe_detail), 180)
        self.assertNotIn("/Users/andy/private", safe_detail)
        self.assertNotIn("123456", safe_detail)
        self.assertNotIn("验证码", safe_detail)
        self.assertNotIn("token=", safe_detail.casefold())
        self.assertNotIn("dom=", safe_detail.casefold())
        self.assertNotIn("session", safe_detail.casefold())

    def test_reviewer_payloads_are_absent_from_safe_log_and_public_events(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        payloads = (
            (
                "/Users/andy/private/account.json",
                ("/users", "andy", "private", "account.json"),
            ),
            (
                "/tmp/browser-profile/state.json",
                ("/tmp", "browser-profile", "state.json"),
            ),
            (
                'selector=<div data-secret="abc">private</div>',
                ("selector", "<div", "data-secret", "private"),
            ),
            (
                "access_token=top-secret",
                ("access_token", "top-secret"),
            ),
            (
                "cookie_value=top-secret",
                ("cookie_value", "top-secret"),
            ),
        )

        for payload, forbidden_parts in payloads:
            with self.subTest(payload=payload):
                current_manager = self.factory.instances[-1]
                current_manager.search_error = RuntimeError(payload)
                with self.assertLogs(
                    "app_core.douyin_commerce_collectors", level="WARNING"
                ) as captured, self.assertRaises(DouyinCommerceCollectorError):
                    self.manager.search_locations(
                        generation_id, "审查关键词", "domestic"
                    )

                event = self.manager.recent_diagnostics(generation_id)[-1]
                safe_detail = self.manager._safe_diagnostic_detail(
                    RuntimeError(payload)
                )
                combined = (
                    safe_detail
                    + "\n"
                    + "\n".join(captured.output)
                    + "\n"
                    + repr(event)
                ).casefold()
                current_manager.search_error = None
                self.manager.retry_collector(
                    generation_id, "domestic_location"
                )

                self.assertLessEqual(len(safe_detail), 180)
                for forbidden in forbidden_parts:
                    self.assertNotIn(forbidden, combined)
                self.assertIn("collector_unknown", combined)

    def test_sensitive_search_keywords_are_redacted_at_both_public_event_exits(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        payloads = (
            (
                "/Users/andy/private/account.json",
                ("/users", "andy", "private", "account.json"),
            ),
            (
                "/tmp/browser-profile/state.json",
                ("/tmp", "browser-profile", "state.json"),
            ),
            (
                'selector=<div data-secret="abc">private</div>',
                ("selector", "<div", "data-secret", "private"),
            ),
            (
                "access_token=top-secret",
                ("access_token", "top-secret"),
            ),
            (
                "cookie_value=top-secret",
                ("cookie_value", "top-secret"),
            ),
        )

        for payload, forbidden_parts in payloads:
            self.manager.search_locations(generation_id, payload, "domestic")
            public_events = (
                (
                    "recent_diagnostics",
                    self.manager.recent_diagnostics(generation_id)[-1],
                ),
                ("event_sink", self.events[-1]),
            )
            for outlet, event in public_events:
                with self.subTest(payload=payload, outlet=outlet):
                    public_keyword = event["keyword"].casefold()
                    self.assertNotEqual(public_keyword, payload.casefold())
                    for forbidden in forbidden_parts:
                        self.assertNotIn(forbidden, public_keyword)

        self.manager.search_locations(generation_id, "夜南香", "domestic")
        self.assertEqual(
            self.manager.recent_diagnostics(generation_id)[-1]["keyword"],
            "夜南香",
        )
        self.assertEqual(self.events[-1]["keyword"], "夜南香")

    def test_local_retry_records_attempt_and_cleanup_result(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.manager.search_locations(generation_id, "夜南香", "local")

        self.manager.retry_collector(generation_id, "local_location")

        event = self.manager.recent_diagnostics(generation_id)[-1]
        self.assert_complete_diagnostic(event)
        self.assertEqual(event["collectorType"], "local_location")
        self.assertEqual(event["action"], "retry_collector")
        self.assertEqual(event["scope"], "local")
        self.assertEqual(event["attempt"], 2)
        self.assertEqual(event["outcome"], "success")
        self.assertEqual(event["cleanupResult"], "closed")

    def test_completed_old_action_event_keeps_dispatched_instance_after_retry(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        old_instance_id = self.manager.status(generation_id)[
            "collectorInstanceIds"
        ]["domestic_location"]
        old_wait_returned = threading.Event()
        release_old_wait = threading.Event()
        outcome: dict[str, object] = {}
        original_wait = self.manager._action_queue.wait
        search_thread: threading.Thread | None = None

        def gate_old_wait(action):
            result = original_wait(action)
            if threading.current_thread() is search_thread:
                old_wait_returned.set()
                release_old_wait.wait(timeout=1)
            return result

        with mock.patch.object(
            self.manager._action_queue,
            "wait",
            side_effect=gate_old_wait,
        ):
            search_thread = threading.Thread(
                target=lambda: self._capture_call(
                    outcome,
                    "search",
                    lambda: self.manager.search_locations(
                        generation_id, "侨港风情街", "domestic"
                    ),
                )
            )
            search_thread.start()
            self.assertTrue(old_wait_returned.wait(timeout=1))

            retried = self.manager.retry_collector(
                generation_id, "domestic_location"
            )
            new_instance_id = retried["collectorInstanceId"]
            release_old_wait.set()
            search_thread.join(timeout=1)

        self.assertFalse(search_thread.is_alive())
        self.assertNotEqual(new_instance_id, old_instance_id)
        self.assertEqual(
            outcome["search"]["collectorInstanceId"], old_instance_id
        )
        search_event = [
            event
            for event in self.manager.recent_diagnostics(
                generation_id, limit=200
            )
            if event["action"] == "search_locations"
            and event["keyword"] == "侨港风情街"
            and event["outcome"] == "success"
        ][-1]
        self.assertEqual(search_event["collectorInstanceId"], old_instance_id)

    def test_cancelled_unstarted_local_action_event_keeps_reserved_instance(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        local_action_reserved = threading.Event()
        outcome: dict[str, object] = {}
        original_start = self.manager._action_queue.start

        def hold_local_action(action):
            if (
                action.collector_type is CollectorType.LOCAL_LOCATION
                and not action.is_cleanup
            ):
                local_action_reserved.set()
                return None
            return original_start(action)

        with mock.patch.object(
            self.manager._action_queue,
            "start",
            side_effect=hold_local_action,
        ):
            search_thread = threading.Thread(
                target=lambda: self._capture_call(
                    outcome,
                    "search",
                    lambda: self.manager.search_locations(
                        generation_id, "夜南香", "local"
                    ),
                )
            )
            search_thread.start()
            self.assertTrue(local_action_reserved.wait(timeout=1))
            outcome["close"] = self.manager.close_generation(
                generation_id, reason="cancelled"
            )
            search_thread.join(timeout=1)

        self.assertFalse(search_thread.is_alive())
        error_result = outcome["search_error"].to_public_action_result()
        self.assertEqual(error_result["errorCode"], "stale_result_discarded")
        self.assertTrue(error_result["collectorInstanceId"])
        cancelled_event = [
            event
            for event in self.manager.recent_diagnostics(
                generation_id, limit=200
            )
            if event["action"] == "search_locations"
            and event["keyword"] == "夜南香"
        ][-1]
        self.assertEqual(cancelled_event["outcome"], "discarded")
        self.assertEqual(
            cancelled_event["collectorInstanceId"],
            error_result["collectorInstanceId"],
        )

    def test_stale_result_and_incomplete_close_are_diagnosed(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.manager.refresh_favorite_music(generation_id)
        domestic = self.factory.instances[0]
        domestic.location_result_override = []
        domestic.release_location = threading.Event()
        outcome: dict[str, object] = {}
        search_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "search",
                lambda: self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                ),
            )
        )
        search_thread.start()
        self.assertTrue(domestic.location_started.wait(timeout=1))
        with self.manager._state_lock:
            self.manager._runtime.generation.collectors[
                CollectorType.DOMESTIC_LOCATION
            ].instance_id = "replacement-instance"
        domestic.release_location.set()
        search_thread.join(timeout=1)

        stale = self.manager.recent_diagnostics(generation_id)[-1]
        self.assert_complete_diagnostic(stale)
        self.assertEqual(stale["outcome"], "discarded")
        self.assertEqual(stale["errorCode"], "stale_result_discarded")

        self.factory.instances[1].close_failures_remaining = 1
        closed = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertFalse(closed["closed"])
        cleanup = [
            event
            for event in self.manager.recent_diagnostics(generation_id, limit=200)
            if event["action"] == "close_generation"
            and event["collectorType"] == "favorite_music"
        ][-1]
        self.assert_complete_diagnostic(cleanup)
        self.assertEqual(cleanup["phase"], "cleanup")
        self.assertEqual(cleanup["outcome"], "failed")
        self.assertEqual(cleanup["errorCode"], "cleanup_incomplete")
        self.assertEqual(cleanup["cleanupResult"], "cleanup_incomplete")

    def test_recent_diagnostics_keeps_only_current_generation_last_200(self):
        first_generation = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        for index in range(205):
            self.manager.search_locations(
                first_generation, f"国内地点{index}", "domestic"
            )

        retained = self.manager.recent_diagnostics(first_generation, limit=999)
        self.assertEqual(len(retained), 200)
        self.assertEqual(retained[0]["keyword"], "国内地点5")

        second_generation = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        self.assertEqual(self.manager.recent_diagnostics(first_generation), [])
        self.assertEqual(self.manager.recent_diagnostics(second_generation), [])

    def test_error_classification_uses_fixed_priority_and_codes(self):
        cases = (
            ("login required; multiple targets", "login_required"),
            ("scope not confirmed; multiple targets", "scope_not_confirmed"),
            (
                "抖音位置搜索范围本地未能确认，已安全停止",
                "scope_not_confirmed",
            ),
            ("candidate panel missing", "candidate_panel_missing"),
            ("multiple candidate panels", "candidate_ambiguous"),
            ("抖音可带货地点列表未能唯一显示", "candidate_ambiguous"),
            ("no candidates returned", "candidate_empty"),
            (
                "抖音未返回与夜南香相符的最新完整发布定位",
                "candidate_empty",
            ),
            ("HTTP 429 rate limited", "rate_limited_or_degraded"),
            ("browser close failed", "cleanup_incomplete"),
            ("unexpected automation fault", "collector_unknown"),
        )

        for detail, expected in cases:
            with self.subTest(detail=detail):
                self.assertEqual(
                    self.manager._classify_diagnostic_error(RuntimeError(detail)),
                    expected,
                )

    def test_begin_starts_only_domestic_then_music_and_local_start_lazily(self):
        begun = self.manager.begin_generation(self.upload_payload)
        generation_id = begun["setupGenerationId"]

        self.assertEqual(len(self.factory.instances), 1)
        self.assertEqual(begun["collectors"]["domestic_location"], "active")
        self.assertEqual(begun["collectors"]["favorite_music"], "not_started")
        self.assertEqual(begun["collectors"]["local_location"], "not_started")
        self.assertEqual(
            self.factory.instances[0].start_payloads[0]["fileList"],
            ["probe.mp4"],
        )

        self.manager.refresh_favorite_music(generation_id)
        self.manager.search_locations(generation_id, "夜南香", "local")

        self.assertEqual(len(self.factory.instances), 3)
        self.assertEqual(
            len({item.session_id for item in self.factory.instances}),
            3,
        )
        self.assertEqual(self.factory.instances[0].location_scopes, [])
        self.assertEqual(self.factory.instances[2].location_scopes, ["local"])

    def test_public_actions_return_controlled_instance_envelopes(self):
        """真实 manager 的三类动作都必须携带本次实例门禁信息。"""

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        music = self.manager.refresh_favorite_music(generation_id)
        domestic = self.manager.search_locations(
            generation_id, "侨港风情街", "domestic"
        )
        retried = self.manager.retry_collector(generation_id, "favorite_music")

        self.assertEqual(music["setupGenerationId"], generation_id)
        self.assertEqual(music["collectorType"], "favorite_music")
        self.assertTrue(music["collectorInstanceId"])
        self.assertEqual(music["candidates"][0]["musicId"], "music-2")
        self.assertEqual(domestic["setupGenerationId"], generation_id)
        self.assertEqual(domestic["collectorType"], "domestic_location")
        self.assertTrue(domestic["collectorInstanceId"])
        self.assertEqual(domestic["candidates"][0]["name"], "侨港风情街")
        self.assertEqual(retried["setupGenerationId"], generation_id)
        self.assertEqual(retried["collectorType"], "favorite_music")
        self.assertTrue(retried["collectorInstanceId"])
        self.assertNotEqual(
            retried["collectorInstanceId"], music["collectorInstanceId"]
        )

    def test_needs_login_start_is_preserved_as_fixed_login_required_error(self):
        """底层登录失效结果不得降级为普通启动失败或泄露原始字段。"""

        self.factory.start_result_overrides[1] = {
            "status": "needs_login",
            "message": "登录已失效，请到账号管理重新登录",
        }

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.begin_generation(self.upload_payload)

        self.assertEqual(str(raised.exception), "login_required")
        self.assertNotIn("登录已失效", str(raised.exception))
        self.assertNotIn("session", str(raised.exception).casefold())

    def test_public_action_error_carries_fixed_instance_envelope(self):
        """真实 manager 失败也必须携带门禁，且不暴露浏览器异常原文。"""

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        expected_instance = self.manager.status(generation_id)[
            "collectorInstanceIds"
        ]["domestic_location"]
        self.factory.instances[0].search_error = RuntimeError(
            "Cookie=secret DOM=<html>private</html>"
        )

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.search_locations(
                generation_id, "侨港风情街", "domestic"
            )

        result = raised.exception.to_public_action_result()
        self.assertEqual(
            result,
            {
                "ok": False,
                "errorCode": "collector_unknown",
                "setupGenerationId": generation_id,
                "collectorType": "domestic_location",
                "collectorInstanceId": expected_instance,
            },
        )
        self.assertNotIn("secret", str(result))
        self.assertNotIn("<html>", str(result))

    def test_begin_never_reads_original_mapping_after_builder_snapshot(self):
        class GetRaisesMapping(Mapping[str, object]):
            def __init__(self, values: dict[str, object]) -> None:
                self._values = values

            def __getitem__(self, key: str) -> object:
                return self._values[key]

            def __iter__(self):
                return iter(self._values)

            def __len__(self) -> int:
                return len(self._values)

            def get(self, key: str, default=None):
                del key, default
                raise RuntimeError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

        self.manager._probe_payload_builder = lambda payload: {
            **dict(payload),
            "fileList": ["probe.mp4"],
        }
        begun = self.manager.begin_generation(
            GetRaisesMapping(dict(self.upload_payload))
        )

        self.assertEqual(begun["collectors"]["domestic_location"], "active")
        self.assertEqual(self.factory.instances[0].close_calls, [])

    def test_close_cancels_blocked_builder_before_runtime_or_upload_and_next_begin_starts(self):
        builder_started = threading.Event()
        release_builder = threading.Event()
        outcome: dict[str, object] = {}

        def blocked_builder(payload: Mapping[str, Any]) -> dict[str, Any]:
            builder_started.set()
            release_builder.wait(timeout=2)
            return {
                **dict(payload),
                "fileList": ["probe.mp4"],
                "runtimeMode": "preflight",
                "debugDryRun": True,
            }

        self.manager._probe_payload_builder = blocked_builder
        begin_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "begin",
                lambda: self.manager.begin_generation(self.upload_payload),
            )
        )
        begin_thread.start()
        self.assertTrue(builder_started.wait(timeout=1))

        next_thread: threading.Thread | None = None
        try:
            closed = self.manager.close_generation(reason="cancelled")
            self.assertTrue(closed["closed"])
            self.assertEqual(closed["aliveCollectorCount"], 0)
            self.assertIsNone(self.manager._runtime)

            self.manager._probe_payload_builder = lambda payload: {
                **dict(payload),
                "fileList": ["probe.mp4"],
                "runtimeMode": "preflight",
                "debugDryRun": True,
            }
            next_thread = threading.Thread(
                target=lambda: self._capture_call(
                    outcome,
                    "next_begin",
                    lambda: self.manager.begin_generation(self.upload_payload),
                )
            )
            next_thread.start()
            next_thread.join(timeout=0.3)
            self.assertFalse(next_thread.is_alive())
        finally:
            release_builder.set()
            begin_thread.join(timeout=1)
            if next_thread is not None:
                next_thread.join(timeout=1)

        self.assertFalse(begin_thread.is_alive())
        self.assertNotIn("begin", outcome)
        self.assertEqual(str(outcome["begin_error"]), "stale_result_discarded")
        self.assertIsNone(outcome["begin_error"].__cause__)
        with self.manager._action_queue._lock:
            self.assertEqual(self.manager._action_queue._actions, [])

        self.assertNotIn("next_begin_error", outcome)
        next_generation = outcome["next_begin"]

        self.assertEqual(next_generation["generationState"], "collecting")
        self.assertEqual(
            next_generation["collectors"]["domestic_location"], "active"
        )
        self.assertEqual(len(self.factory.instances), 1)
        self.assertEqual(len(self.factory.instances[0].start_payloads), 1)
        self.assertEqual(
            self.manager.status()["setupGenerationId"],
            next_generation["setupGenerationId"],
        )

    def test_begin_deep_snapshots_nested_accounts_for_lazy_collectors(self):
        accounts = [{"id": 31, "path": "account-a.json"}]
        payload = {
            **self.upload_payload,
            "accountId": 0,
            "accountList": accounts,
        }

        generation_id = self.manager.begin_generation(payload)[
            "setupGenerationId"
        ]
        accounts[0]["id"] = 99
        accounts[0]["path"] = "account-mutated.json"
        accounts.append({"id": 100, "path": "account-b.json"})

        self.manager.refresh_favorite_music(generation_id)
        self.manager.search_locations(generation_id, "夜南香", "local")

        expected_accounts = [{"id": 31, "path": "account-a.json"}]
        self.assertEqual(
            self.factory.instances[1].start_payloads[0]["accountList"],
            expected_accounts,
        )
        self.assertEqual(
            self.factory.instances[2].start_payloads[0]["accountList"],
            expected_accounts,
        )
        self.assertIsNot(
            self.factory.instances[1].start_payloads[0]["accountList"],
            accounts,
        )

    def test_begin_snapshot_conversion_failure_is_fixed_and_sanitized(self):
        class SensitiveUncopyableAccount:
            def __deepcopy__(self, memo):
                del memo
                raise DouyinCommerceCollectorError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

        self.manager._probe_payload_builder = lambda payload: {
            **dict(payload),
            "accountList": [SensitiveUncopyableAccount()],
            "fileList": ["probe.mp4"],
        }

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.begin_generation(self.upload_payload)

        self.assertEqual(str(raised.exception), "collector_start_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(self.factory.instances, [])

    def test_begin_snapshot_rejects_self_aliasing_custom_container(self):
        class SelfAliasingAccountList(list):
            def __deepcopy__(self, memo):
                del memo
                return self

        unsafe_accounts = SelfAliasingAccountList(
            [{"id": 31, "path": "account-a.json"}]
        )
        self.manager._probe_payload_builder = lambda payload: {
            **dict(payload),
            "accountId": 0,
            "accountList": unsafe_accounts,
            "fileList": ["probe.mp4"],
        }

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.begin_generation(self.upload_payload)

        self.assertEqual(str(raised.exception), "collector_start_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.factory.instances, [])

    def test_begin_snapshot_rejects_self_aliasing_custom_outer_mapping(self):
        class SelfAliasingPayload(dict):
            def __deepcopy__(self, memo):
                del memo
                return self

        self.manager._probe_payload_builder = lambda payload: SelfAliasingPayload(
            {
                **dict(payload),
                "fileList": ["probe.mp4"],
            }
        )

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.begin_generation(self.upload_payload)

        self.assertEqual(str(raised.exception), "collector_start_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.factory.instances, [])

    def test_builder_account_conversion_failure_is_sanitized(self):
        class SensitiveAccountId:
            def __bool__(self):
                raise RuntimeError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

            def __int__(self):
                raise RuntimeError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

        self.manager._probe_payload_builder = lambda payload: {
            **dict(payload),
            "accountId": SensitiveAccountId(),
            "fileList": ["probe.mp4"],
        }

        with self.assertRaises(Exception) as raised:
            self.manager.begin_generation(self.upload_payload)

        self.assertIsInstance(raised.exception, DouyinCommerceCollectorError)
        self.assertEqual(str(raised.exception), "collector_start_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("secret", str(raised.exception))

    def test_builtin_account_conversion_failure_uses_start_error(self):
        self.manager._probe_payload_builder = lambda payload: {
            **dict(payload),
            "accountId": [],
            "fileList": ["probe.mp4"],
        }

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.begin_generation(self.upload_payload)

        self.assertEqual(str(raised.exception), "collector_start_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.factory.instances, [])

    def test_domestic_and_local_keywords_never_share_a_session(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        domestic = self.manager.search_locations(
            generation_id, "侨港风情街", "domestic"
        )
        local = self.manager.search_locations(generation_id, "夜南香", "local")

        self.assertEqual(domestic["candidates"][0]["name"], "侨港风情街")
        self.assertEqual(local["candidates"][0]["name"], "夜南香")
        self.assertEqual(self.factory.instances[0].location_scopes, ["domestic"])
        self.assertEqual(self.factory.instances[1].location_scopes, ["local"])
        self.assertNotEqual(
            self.factory.instances[0].session_id,
            self.factory.instances[1].session_id,
        )

    def test_fixed_scope_guard_rejects_a_mismatched_collector_before_browser_call(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        runtime = self.manager._runtime
        self.assertIsNotNone(runtime)
        domestic = runtime.collectors[CollectorType.DOMESTIC_LOCATION]
        runtime.collectors[CollectorType.LOCAL_LOCATION] = domestic
        local_slot = runtime.generation.collectors[CollectorType.LOCAL_LOCATION]
        local_slot.instance_id = domestic.instance_id
        local_slot.session_id = domestic.session_id
        local_slot.state = runtime.generation.collectors[
            CollectorType.DOMESTIC_LOCATION
        ].state

        with self.assertRaisesRegex(
            DouyinCommerceCollectorError, "collector_scope_mismatch"
        ):
            self.manager.search_locations(generation_id, "夜南香", "local")

        self.assertEqual(self.factory.instances[0].location_calls, [])

    def test_all_platform_actions_use_one_serial_queue(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_location = threading.Event()
        results: dict[str, object] = {}

        search_thread = threading.Thread(
            target=lambda: results.setdefault(
                "locations",
                self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                ),
            )
        )
        search_thread.start()
        self.assertTrue(domestic.location_started.wait(timeout=1))

        music_thread = threading.Thread(
            target=lambda: results.setdefault(
                "music", self.manager.refresh_favorite_music(generation_id)
            )
        )
        music_thread.start()
        time.sleep(0.05)

        self.assertEqual(len(self.factory.instances), 1)
        domestic.release_location.set()
        search_thread.join(timeout=1)
        music_thread.join(timeout=1)

        self.assertFalse(search_thread.is_alive())
        self.assertFalse(music_thread.is_alive())
        self.assertEqual(len(self.factory.instances), 2)
        self.assertTrue(self.factory.instances[1].refresh_started.is_set())

    def test_executor_submission_never_occurs_while_state_lock_is_held(self):
        original_start = self.manager._action_queue.start

        def assert_lock_free_start(action):
            acquired = threading.Event()

            def probe_lock() -> None:
                with self.manager._state_lock:
                    acquired.set()

            probe = threading.Thread(target=probe_lock)
            probe.start()
            probe.join(timeout=0.2)
            self.assertTrue(acquired.is_set())
            return original_start(action)

        with mock.patch.object(
            self.manager._action_queue,
            "start",
            side_effect=assert_lock_free_start,
        ):
            generation_id = self.manager.begin_generation(self.upload_payload)[
                "setupGenerationId"
            ]
            self.manager.refresh_favorite_music(generation_id)
            self.manager.search_locations(generation_id, "夜南香", "local")
            self.manager.retry_collector(generation_id, "favorite_music")

    def test_close_cancels_reserved_action_before_executor_submission(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        outcome: dict[str, object] = {}
        start_refresh = threading.Event()
        refresh_thread = threading.Thread(
            target=lambda: (
                start_refresh.wait(timeout=1),
                self._capture_call(
                    outcome,
                    "refresh",
                    lambda: self.manager.refresh_favorite_music(generation_id),
                ),
            )
        )
        refresh_thread.start()
        gate = ExitGateLock(self.manager._state_lock, refresh_thread.ident)
        self.manager._state_lock = gate
        executor_submit = self.manager._action_queue._executor.submit
        original_cancel_pending = self.manager._action_queue.cancel_pending
        cancel_entered = threading.Event()
        release_cancel = threading.Event()
        noncleanup_after_closing: list[str] = []

        def track_executor_submit(callback, action):
            generation_state = self.manager.status(generation_id)[
                "generationState"
            ]
            if generation_state != "collecting" and not action.is_cleanup:
                noncleanup_after_closing.append(action.request_id)
            return executor_submit(callback, action)

        def block_cancel_pending(target_id):
            cancel_entered.set()
            release_cancel.wait(timeout=1)
            return original_cancel_pending(target_id)

        with (
            mock.patch.object(
                self.manager._action_queue._executor,
                "submit",
                side_effect=track_executor_submit,
            ),
            mock.patch.object(
                self.manager._action_queue,
                "cancel_pending",
                side_effect=block_cancel_pending,
            ),
        ):
            start_refresh.set()
            self.assertTrue(gate.exited.wait(timeout=1))
            close_thread = threading.Thread(
                target=lambda: outcome.setdefault(
                    "closed",
                    self.manager.close_generation(
                        generation_id, reason="cancelled"
                    ),
                )
            )
            close_thread.start()
            self.assertTrue(cancel_entered.wait(timeout=1))
            gate.release.set()
            refresh_thread.join(timeout=1)
            release_cancel.set()
            close_thread.join(timeout=1)

        self.assertFalse(refresh_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertTrue(outcome["closed"]["closed"])
        self.assertEqual(str(outcome["refresh_error"]), "stale_result_discarded")
        self.assertEqual(noncleanup_after_closing, [])
        self.assertEqual(len(self.factory.instances), 1)

    def test_closing_generation_discards_a_late_result(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_location = threading.Event()
        outcome: dict[str, object] = {}

        def run_search() -> None:
            try:
                self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                )
            except Exception as error:
                outcome["error"] = error

        search_thread = threading.Thread(target=run_search)
        search_thread.start()
        self.assertTrue(domestic.location_started.wait(timeout=1))

        close_thread = threading.Thread(
            target=lambda: outcome.setdefault(
                "closed",
                self.manager.close_generation(generation_id, reason="cancelled"),
            )
        )
        close_thread.start()
        for _ in range(100):
            if self.manager.status(generation_id)["generationState"] == "cancelling":
                break
            time.sleep(0.01)
        self.assertEqual(
            self.manager.status(generation_id)["generationState"], "cancelling"
        )

        domestic.release_location.set()
        search_thread.join(timeout=1)
        close_thread.join(timeout=1)

        self.assertIsInstance(outcome.get("error"), DouyinCommerceCollectorError)
        self.assertEqual(str(outcome["error"]), "stale_result_discarded")
        self.assertTrue(outcome["closed"]["closed"])
        stale_events = [
            event
            for event in self.events
            if event["errorCode"] == "stale_result_discarded"
        ]
        self.assertEqual(stale_events[-1]["errorCode"], "stale_result_discarded")
        self.assertEqual(
            sum(
                event["errorCode"] == "stale_result_discarded"
                for event in self.events
            ),
            1,
        )

        next_generation = self.manager.begin_generation(self.upload_payload)
        self.assertNotEqual(next_generation["setupGenerationId"], generation_id)
        self.assertEqual(next_generation["generationState"], "collecting")
        self.assertEqual(
            next_generation["collectors"],
            {
                "domestic_location": "active",
                "favorite_music": "not_started",
                "local_location": "not_started",
            },
        )

    def test_close_cancels_pending_actions_and_leaves_the_queue_empty(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_location = threading.Event()
        outcome: dict[str, object] = {}

        search_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "search",
                lambda: self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                ),
            )
        )
        search_thread.start()
        self.assertTrue(domestic.location_started.wait(timeout=1))
        music_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "music",
                lambda: self.manager.refresh_favorite_music(generation_id),
            )
        )
        music_thread.start()
        time.sleep(0.05)

        close_thread = threading.Thread(
            target=lambda: outcome.setdefault(
                "closed",
                self.manager.close_generation(generation_id, reason="cancelled"),
            )
        )
        close_thread.start()
        for _ in range(100):
            if self.manager.status(generation_id)["generationState"] == "cancelling":
                break
            time.sleep(0.01)
        domestic.release_location.set()
        search_thread.join(timeout=1)
        music_thread.join(timeout=1)
        close_thread.join(timeout=1)

        self.assertEqual(str(outcome["music_error"]), "stale_result_discarded")
        self.assertTrue(outcome["closed"]["closed"])
        self.assertEqual(self.manager._action_queue._actions, [])

    def test_publish_barrier_uses_closing_collectors_state(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_location = threading.Event()
        outcome: dict[str, object] = {}
        search_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "search",
                lambda: self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                ),
            )
        )
        search_thread.start()
        self.assertTrue(domestic.location_started.wait(timeout=1))
        close_thread = threading.Thread(
            target=lambda: outcome.setdefault(
                "closed",
                self.manager.close_generation(generation_id, reason="publish"),
            )
        )
        close_thread.start()
        for _ in range(100):
            if self.manager.status(generation_id)["generationState"] != "collecting":
                break
            time.sleep(0.01)

        self.assertEqual(
            self.manager.status(generation_id)["generationState"],
            "closing_collectors",
        )
        domestic.release_location.set()
        search_thread.join(timeout=1)
        close_thread.join(timeout=1)
        self.assertTrue(outcome["closed"]["closed"])

    def test_music_login_error_arriving_after_cancel_keeps_same_action_instance(self):
        """音乐懒启动阻塞后取消代际，迟到登录错误仍携带原动作实例。"""

        self._assert_late_login_after_generation_close(
            collector_type="favorite_music",
            close_reason="abandoned",
            expected_generation_state="cancelling",
        )

    def test_local_login_error_arriving_after_publish_close_keeps_same_action_instance(self):
        """本地点懒启动阻塞后发布关闭，迟到登录错误仍携带原动作实例。"""

        self._assert_late_login_after_generation_close(
            collector_type="local_location",
            close_reason="publish_completed",
            expected_generation_state="closing_collectors",
        )

    def _assert_late_login_after_generation_close(
        self,
        *,
        collector_type: str,
        close_reason: str,
        expected_generation_state: str,
    ) -> None:
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.factory.block_start_ids.add(2)
        self.factory.start_result_overrides[2] = {
            "status": "needs_login",
            "message": "登录已失效，请到账号管理重新登录",
        }
        outcome: dict[str, object] = {}
        action = (
            (lambda: self.manager.refresh_favorite_music(generation_id))
            if collector_type == "favorite_music"
            else (
                lambda: self.manager.search_locations(
                    generation_id, "夜南香", "local"
                )
            )
        )
        action_thread = threading.Thread(
            target=lambda: self._capture_call(outcome, "action", action)
        )
        action_thread.start()
        for _ in range(100):
            if len(self.factory.instances) >= 2:
                break
            time.sleep(0.01)
        blocked_manager = self.factory.instances[1]
        self.assertTrue(blocked_manager.start_started.wait(timeout=1))
        expected_instance_id = self.manager.status(generation_id)[
            "collectorInstanceIds"
        ][collector_type]

        close_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "close",
                lambda: self.manager.close_generation(
                    generation_id, reason=close_reason
                ),
            )
        )
        close_thread.start()
        for _ in range(100):
            status = self.manager.status(generation_id)
            if status["generationState"] == expected_generation_state:
                break
            time.sleep(0.01)
        self.assertEqual(status["generationState"], expected_generation_state)
        self.assertEqual(
            status["collectorInstanceIds"][collector_type],
            expected_instance_id,
        )

        blocked_manager.release_start.set()
        action_thread.join(timeout=1)
        close_thread.join(timeout=1)

        self.assertFalse(action_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        error = outcome["action_error"]
        self.assertEqual(str(error), "login_required")
        self.assertEqual(
            error.to_public_action_result()["collectorInstanceId"],
            expected_instance_id,
        )
        self.assertTrue(outcome["close"]["closed"])

    @staticmethod
    def _capture_call(
        outcome: dict[str, object],
        key: str,
        callback,
    ) -> None:
        try:
            outcome[key] = callback()
        except Exception as error:
            outcome[f"{key}_error"] = error

    def test_replaced_instance_discards_the_old_instances_late_result(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_location = threading.Event()
        outcome: dict[str, object] = {}

        def run_search() -> None:
            try:
                self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                )
            except Exception as error:
                outcome["error"] = error

        thread = threading.Thread(target=run_search)
        thread.start()
        self.assertTrue(domestic.location_started.wait(timeout=1))

        with self.manager._state_lock:
            runtime = self.manager._runtime
            runtime.generation.collectors[
                CollectorType.DOMESTIC_LOCATION
            ].instance_id = "replacement-instance"
        domestic.release_location.set()
        thread.join(timeout=1)

        self.assertEqual(str(outcome["error"]), "stale_result_discarded")
        self.assertEqual(
            self.manager.status(generation_id)["collectorInstanceIds"][
                "domestic_location"
            ],
            "replacement-instance",
        )

    def test_late_failure_from_retry_replaced_instance_does_not_fail_new_slot(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        old_manager = self.factory.instances[0]
        old_manager.release_location = threading.Event()
        old_manager.search_error = RuntimeError("Cookie=old DOM=<html>old</html>")
        outcome: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "search",
                lambda: self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                ),
            )
        )
        thread.start()
        self.assertTrue(old_manager.location_started.wait(timeout=1))

        with self.manager._state_lock:
            runtime = self.manager._runtime
            old_runtime = runtime.collectors[CollectorType.DOMESTIC_LOCATION]
            old_instance_id = old_runtime.instance_id
            replacement_manager = self.factory()
            replacement_manager.start_upload(self.upload_payload)
            replacement = type(old_runtime)(
                collector_type=CollectorType.DOMESTIC_LOCATION,
                manager=replacement_manager,
                instance_id="retry-replacement",
                session_id=replacement_manager.session_id,
                fixed_scope="domestic",
            )
            runtime.collectors[CollectorType.DOMESTIC_LOCATION] = replacement
            runtime.generation.activate_collector(
                CollectorType.DOMESTIC_LOCATION,
                instance_id=replacement.instance_id,
                session_id=replacement.session_id,
            )

        old_manager.release_location.set()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(str(outcome["search_error"]), "stale_result_discarded")
        self.assertEqual(
            outcome["search_error"].to_public_action_result()[
                "collectorInstanceId"
            ],
            old_instance_id,
        )
        status = self.manager.status(generation_id)
        self.assertEqual(status["collectors"]["domestic_location"], "active")
        self.assertEqual(
            status["collectorInstanceIds"]["domestic_location"],
            "retry-replacement",
        )

    def test_replaced_lazy_music_login_error_keeps_old_action_instance(self):
        """懒启动音乐实例被替换后，登录错误仍绑定旧动作且保留固定码。"""

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.factory.block_start_ids.add(2)
        self.factory.start_result_overrides[2] = {
            "status": "needs_login",
            "message": "登录已失效，请到账号管理重新登录",
        }
        outcome: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "music",
                lambda: self.manager.refresh_favorite_music(generation_id),
            )
        )
        thread.start()
        for _ in range(100):
            if len(self.factory.instances) >= 2:
                break
            time.sleep(0.01)
        old_manager = self.factory.instances[1]
        self.assertTrue(old_manager.start_started.wait(timeout=1))

        with self.manager._state_lock:
            runtime = self.manager._runtime
            old_runtime = runtime.collectors[CollectorType.FAVORITE_MUSIC]
            old_instance_id = old_runtime.instance_id
            replacement_manager = self.factory()
            replacement_manager.start_upload(self.upload_payload)
            replacement = type(old_runtime)(
                collector_type=CollectorType.FAVORITE_MUSIC,
                manager=replacement_manager,
                instance_id="music-replacement",
                session_id=replacement_manager.session_id,
                fixed_scope="",
            )
            runtime.collectors[CollectorType.FAVORITE_MUSIC] = replacement
            runtime.generation.activate_collector(
                CollectorType.FAVORITE_MUSIC,
                instance_id=replacement.instance_id,
                session_id=replacement.session_id,
            )

        old_manager.release_start.set()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        error = outcome["music_error"]
        self.assertEqual(str(error), "login_required")
        self.assertEqual(
            error.to_public_action_result()["collectorInstanceId"],
            old_instance_id,
        )
        status = self.manager.status(generation_id)
        self.assertEqual(status["collectors"]["favorite_music"], "active")
        self.assertEqual(
            status["collectorInstanceIds"]["favorite_music"],
            "music-replacement",
        )

    def test_replaced_instance_candidate_normalization_error_keeps_old_instance(self):
        """候选规范化发生在替换后时，失败 envelope 仍绑定动作旧实例。"""

        class InvalidCandidate:
            def __iter__(self):
                raise RuntimeError("Cookie=secret DOM=<html>private</html>")

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        old_manager = self.factory.instances[0]
        old_manager.release_location = threading.Event()
        old_manager.location_result_override = [InvalidCandidate()]
        outcome: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "search",
                lambda: self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                ),
            )
        )
        thread.start()
        self.assertTrue(old_manager.location_started.wait(timeout=1))

        with self.manager._state_lock:
            runtime = self.manager._runtime
            old_runtime = runtime.collectors[CollectorType.DOMESTIC_LOCATION]
            old_instance_id = old_runtime.instance_id
            replacement_manager = self.factory()
            replacement_manager.start_upload(self.upload_payload)
            replacement = type(old_runtime)(
                collector_type=CollectorType.DOMESTIC_LOCATION,
                manager=replacement_manager,
                instance_id="normalization-replacement",
                session_id=replacement_manager.session_id,
                fixed_scope="domestic",
            )
            runtime.collectors[CollectorType.DOMESTIC_LOCATION] = replacement
            runtime.generation.activate_collector(
                CollectorType.DOMESTIC_LOCATION,
                instance_id=replacement.instance_id,
                session_id=replacement.session_id,
            )

        old_manager.release_location.set()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        error = outcome["search_error"]
        self.assertEqual(str(error), "stale_result_discarded")
        self.assertEqual(
            error.to_public_action_result()["collectorInstanceId"],
            old_instance_id,
        )
        self.assertNotIn("secret", str(error.to_public_action_result()))
        status = self.manager.status(generation_id)
        self.assertEqual(status["collectors"]["domestic_location"], "active")
        self.assertEqual(
            status["collectorInstanceIds"]["domestic_location"],
            "normalization-replacement",
        )

    def test_start_activation_instance_mismatch_discards_and_closes_own_session(self):
        self.factory.block_start_ids.add(1)
        outcome: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "begin",
                lambda: self.manager.begin_generation(self.upload_payload),
            )
        )
        thread.start()
        for _ in range(100):
            if self.factory.instances:
                break
            time.sleep(0.01)
        created = self.factory.instances[0]
        self.assertTrue(created.start_started.wait(timeout=1))
        with self.manager._state_lock:
            runtime = self.manager._runtime
            slot = runtime.generation.collectors[
                CollectorType.DOMESTIC_LOCATION
            ]
            slot.instance_id = "replacement-before-activation"

        created.release_start.set()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("begin", outcome)
        self.assertIn("begin_error", outcome)
        self.assertEqual(str(outcome["begin_error"]), "stale_result_discarded")
        self.assertEqual(created.close_calls, ["session-1"])
        with self.manager._state_lock:
            runtime = self.manager._runtime
            slot = runtime.generation.collectors[
                CollectorType.DOMESTIC_LOCATION
            ]
            self.assertEqual(slot.instance_id, "replacement-before-activation")
            self.assertNotIn(
                CollectorType.DOMESTIC_LOCATION,
                runtime.collectors,
            )

    def test_close_cannot_interleave_between_activation_check_and_write(self):
        self.factory.block_start_ids.add(1)
        outcome: dict[str, object] = {}
        activation_states: list[str] = []
        close_finished = threading.Event()
        begin_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "begin",
                lambda: self.manager.begin_generation(self.upload_payload),
            )
        )
        begin_thread.start()
        for _ in range(100):
            if self.factory.instances:
                break
            time.sleep(0.01)
        domestic = self.factory.instances[0]
        self.assertTrue(domestic.start_started.wait(timeout=1))
        generation_id = self.manager._runtime.generation.generation_id
        runtime = self.manager._runtime
        original_activate = runtime.generation.activate_collector

        def tracked_activate(*args, **kwargs):
            activation_states.append(runtime.generation.state.value)
            return original_activate(*args, **kwargs)

        runtime.generation.activate_collector = tracked_activate
        gate = ExitGateLock(self.manager._state_lock, domestic.start_thread_ident)
        self.manager._state_lock = gate
        domestic.release_start.set()
        self.assertTrue(gate.exited.wait(timeout=1))
        def run_close() -> None:
            try:
                outcome["close"] = self.manager.close_generation(
                    generation_id, reason="cancelled"
                )
            finally:
                close_finished.set()

        self.manager._close_wait_seconds = 0.05
        close_thread = threading.Thread(target=run_close)
        close_thread.start()
        self.assertTrue(close_finished.wait(timeout=0.3))
        gate.release.set()
        begin_thread.join(timeout=1)
        close_thread.join(timeout=1)
        self.assertFalse(begin_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(len(activation_states), 1)
        self.assertNotIn("cancelling", activation_states)
        self.assertNotIn("closing_collectors", activation_states)
        self.assertNotIn("closed", activation_states)

    def test_retry_replaces_only_requested_collector(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.manager.refresh_favorite_music(generation_id)
        before = self.manager.status(generation_id)["collectorInstanceIds"]

        retried = self.manager.retry_collector(generation_id, "favorite_music")

        after = retried["collectorInstanceIds"]
        self.assertEqual(after["domestic_location"], before["domestic_location"])
        self.assertNotEqual(after["favorite_music"], before["favorite_music"])
        self.assertEqual(after["local_location"], before["local_location"])
        self.assertEqual(self.factory.instances[1].close_calls, ["session-2"])
        self.assertEqual(len(self.factory.instances), 3)

    def test_retry_old_cleanup_failure_envelope_uses_expected_old_instance(self):
        """retry 旧实例清理失败必须匹配仍存活的旧槽，不能绑定预留新实例。"""

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        before = self.manager.status(generation_id)
        old_instance_id = before["collectorInstanceIds"]["domestic_location"]
        self.factory.instances[0].close_failures_remaining = 1

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.retry_collector(generation_id, "domestic_location")

        result = raised.exception.to_public_action_result()
        self.assertEqual(result["errorCode"], "cleanup_incomplete")
        self.assertEqual(result["collectorInstanceId"], old_instance_id)
        status = self.manager.status(generation_id)
        self.assertEqual(
            status["collectorInstanceIds"]["domestic_location"],
            old_instance_id,
        )
        self.assertEqual(status["collectors"]["domestic_location"], "failed")

    def test_retry_new_start_failure_envelope_uses_reserved_new_instance(self):
        """旧实例全关后，新启动失败必须绑定已进入启动阶段的预留新实例。"""

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        old_instance_id = self.manager.status(generation_id)[
            "collectorInstanceIds"
        ]["domestic_location"]
        self.factory.errors_by_id[2] = RuntimeError(
            "Cookie=secret DOM=<html>private</html>"
        )

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.retry_collector(generation_id, "domestic_location")

        result = raised.exception.to_public_action_result()
        self.assertEqual(result["errorCode"], "collector_start_failed")
        self.assertTrue(result["collectorInstanceId"])
        self.assertNotEqual(result["collectorInstanceId"], old_instance_id)
        status = self.manager.status(generation_id)
        self.assertEqual(
            status["collectorInstanceIds"]["domestic_location"],
            result["collectorInstanceId"],
        )
        self.assertEqual(status["collectors"]["domestic_location"], "failed")

    def test_retry_keyboard_interrupt_completes_owner_and_allows_close_retry(self):
        self._assert_retry_process_control_interruption_completes_owner(
            KeyboardInterrupt
        )

    def test_retry_system_exit_completes_owner_and_allows_close_retry(self):
        self._assert_retry_process_control_interruption_completes_owner(
            SystemExit
        )

    def _assert_retry_process_control_interruption_completes_owner(
        self,
        interruption_type: type[BaseException],
    ) -> None:
        class InterruptedRetrySessionManager(FakeSessionManager):
            def __init__(self) -> None:
                super().__init__(1)
                self.strict_close_calls: list[str | None] = []
                self.interruptions_remaining = 1

            def close_strict(self, session_id: str | None = None) -> None:
                self.strict_close_calls.append(session_id)
                if self.interruptions_remaining:
                    self.interruptions_remaining -= 1
                    raise interruption_type(
                        "Cookie=secret 验证码123456 DOM=<html>private</html>"
                    )

        strict_manager = InterruptedRetrySessionManager()
        self.manager._manager_factory = lambda: strict_manager
        self.manager._close_wait_seconds = 0.2
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        with self.assertRaises(interruption_type):
            self.manager.retry_collector(generation_id, "domestic_location")

        with self.manager._state_lock:
            runtime = self.manager._runtime
            self.assertIsNotNone(runtime)
            collector = runtime.collectors[CollectorType.DOMESTIC_LOCATION]
            first_owner = collector.close_owner
            self.assertIsNotNone(first_owner)
            self.assertTrue(first_owner.done.is_set())
            self.assertEqual(first_owner.result, "cleanup_interrupted")
            self.assertEqual(
                runtime.cleanup_results[CollectorType.DOMESTIC_LOCATION],
                "cleanup_interrupted",
            )
            self.assertEqual(
                runtime.generation.collectors[
                    CollectorType.DOMESTIC_LOCATION
                ].state.value,
                "failed",
            )
            self.assertNotIn(
                id(strict_manager), self.manager._manager_close_owners
            )

        continued = self.manager._action_queue.submit(
            generation_id,
            "queue-after-retry-interruption",
            CollectorType.DOMESTIC_LOCATION,
            lambda: "queue_continues",
            is_cleanup=True,
        )
        self.assertEqual(
            self.manager._action_queue.wait(continued),
            "queue_continues",
        )

        closed = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertTrue(closed["closed"])
        self.assertEqual(closed["aliveCollectorCount"], 0)
        self.assertEqual(
            closed["cleanupResults"]["domestic_location"], "closed"
        )
        self.assertEqual(
            strict_manager.strict_close_calls,
            ["session-1", "session-1"],
        )
        self.assertIsNot(collector.close_owner, first_owner)
        self.assertTrue(collector.close_owner.done.is_set())
        self.assertEqual(strict_manager.close_calls, [])

    def test_generation_close_reuses_retry_owned_blocking_close(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_close = threading.Event()
        outcome: dict[str, object] = {}
        retry_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "retry",
                lambda: self.manager.retry_collector(
                    generation_id, "domestic_location"
                ),
            )
        )
        retry_thread.start()
        self.assertTrue(domestic.close_started.wait(timeout=1))
        self.assertEqual(domestic.close_calls, ["session-1"])
        self.manager._close_wait_seconds = 0.05
        closed = self.manager.close_generation(generation_id, reason="cancelled")
        self.assertFalse(closed["closed"])
        self.assertEqual(
            closed["cleanupResults"]["domestic_location"],
            "cleanup_incomplete",
        )
        self.assertEqual(domestic.close_calls, ["session-1"])

        domestic.release_close.set()
        retry_thread.join(timeout=1)
        self.assertFalse(retry_thread.is_alive())
        self.assertEqual(str(outcome["retry_error"]), "stale_result_discarded")
        for _ in range(100):
            if self.manager.status(generation_id)["generationState"] == "closed":
                break
            time.sleep(0.01)
        self.assertEqual(domestic.close_calls, ["session-1"])
        self.assertEqual(
            self.manager.status(generation_id)["generationState"], "closed"
        )

    def test_late_retry_close_failure_does_not_overwrite_closing_state(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_close = threading.Event()
        domestic.close_failures_remaining = 1
        outcome: dict[str, object] = {}
        retry_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "retry",
                lambda: self.manager.retry_collector(
                    generation_id, "domestic_location"
                ),
            )
        )
        retry_thread.start()
        self.assertTrue(domestic.close_started.wait(timeout=1))
        self.manager._close_wait_seconds = 0.05
        first_close = self.manager.close_generation(
            generation_id, reason="cancelled"
        )
        self.assertFalse(first_close["closed"])
        domestic.release_close.set()
        retry_thread.join(timeout=1)

        self.assertFalse(retry_thread.is_alive())
        self.assertEqual(str(outcome["retry_error"]), "stale_result_discarded")
        status = self.manager.status(generation_id)
        self.assertNotEqual(
            status["collectors"]["domestic_location"], "failed"
        )
        self.assertNotEqual(
            status["collectors"]["domestic_location"], "closed"
        )

        second_close = self.manager.close_generation(
            generation_id, reason="cancelled"
        )
        self.assertTrue(second_close["closed"])

    def test_browser_exception_is_normalized_without_preserving_raw_details(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.factory.instances[0].search_error = RuntimeError(
            "Cookie=secret 验证码123456 DOM=<html>private</html>"
        )

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.search_locations(
                generation_id, "侨港风情街", "domestic"
            )

        self.assertEqual(str(raised.exception), "collector_unknown")
        self.assertIsNone(raised.exception.__cause__)
        public_text = str(self.events)
        self.assertNotIn("secret", public_text)
        self.assertNotIn("123456", public_text)
        self.assertNotIn("<html>", public_text)

    def test_lazy_manager_factory_exception_is_replaced_by_fixed_public_error(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.factory.errors_by_id[2] = RuntimeError(
            "Cookie=secret 验证码123456 DOM=<html>private</html>"
        )

        with self.assertRaises(Exception) as raised:
            self.manager.refresh_favorite_music(generation_id)

        self.assertIsInstance(raised.exception, DouyinCommerceCollectorError)
        self.assertEqual(str(raised.exception), "collector_start_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("123456", str(raised.exception))
        self.assertNotIn("<html>", str(raised.exception))

    def test_all_untrusted_start_result_failures_use_fixed_start_error(self):
        sensitive = "Cookie=secret 验证码123456 DOM=<html>private</html>"

        class BoolFailureMapping(Mapping[str, object]):
            def __getitem__(self, key: str) -> object:
                if key == "sessionId":
                    return "session-safe"
                raise KeyError(key)

            def __iter__(self):
                return iter(("sessionId",))

            def __len__(self) -> int:
                return 1

            def __bool__(self) -> bool:
                raise DouyinCommerceCollectorError(sensitive)

        class GetFailureMapping(Mapping[str, object]):
            def __getitem__(self, key: str) -> object:
                raise KeyError(key)

            def __iter__(self):
                return iter(("sessionId",))

            def __len__(self) -> int:
                return 1

            def get(self, key: str, default=None):
                del key, default
                raise DouyinCommerceCollectorError(sensitive)

        class SessionIdBoolFailure:
            def __bool__(self) -> bool:
                raise DouyinCommerceCollectorError(sensitive)

            def __str__(self) -> str:
                return "session-safe"

        class SessionIdStringFailure:
            def __bool__(self) -> bool:
                return True

            def __str__(self) -> str:
                raise DouyinCommerceCollectorError(sensitive)

        cases = {
            "manager_start": DouyinCommerceCollectorError(sensitive),
            "result_truthiness": BoolFailureMapping(),
            "result_get": GetFailureMapping(),
            "session_truthiness": {"sessionId": SessionIdBoolFailure()},
            "session_string": {"sessionId": SessionIdStringFailure()},
        }
        for name, unsafe_result in cases.items():
            with self.subTest(boundary=name):
                unsafe_manager = FakeSessionManager(1)

                def unsafe_start(payload, *, on_progress=None, result=unsafe_result):
                    del payload, on_progress
                    if isinstance(result, Exception):
                        raise result
                    return result

                unsafe_manager.start_upload = unsafe_start
                manager = DouyinCommerceCollectorManager(
                    manager_factory=lambda item=unsafe_manager: item,
                    probe_payload_builder=lambda payload: {
                        **payload,
                        "fileList": ["probe.mp4"],
                    },
                )
                try:
                    with self.assertRaises(DouyinCommerceCollectorError) as raised:
                        manager.begin_generation(self.upload_payload)
                    self.assertEqual(
                        str(raised.exception), "collector_start_failed"
                    )
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertNotIn("secret", str(raised.exception))
                    self.assertNotIn("123456", str(raised.exception))
                    self.assertNotIn("<html>", str(raised.exception))
                finally:
                    manager.close_generation(reason="test_cleanup")

    def test_shared_manager_collision_removes_failed_slot_without_closing_owner(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        self.manager._manager_factory = lambda: domestic

        with self.assertRaisesRegex(
            DouyinCommerceCollectorError, "collector_start_failed"
        ):
            self.manager.refresh_favorite_music(generation_id)

        status = self.manager.status(generation_id)
        self.assertEqual(status["aliveCollectorCount"], 1)
        self.assertEqual(status["collectorInstanceIds"]["favorite_music"], "")
        self.assertEqual(domestic.close_calls, [])
        closed = self.manager.close_generation(generation_id, reason="cancelled")
        self.assertTrue(closed["closed"])
        self.assertEqual(domestic.close_calls, ["session-1"])

    def test_shared_manager_collision_cannot_gain_a_second_close_owner(self):
        """共用 manager 在碰撞检出后不得进入第二运行时或被关闭两次。"""

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        factory_entered = threading.Event()
        release_factory = threading.Event()
        outcome: dict[str, object] = {}

        def return_shared_manager() -> FakeSessionManager:
            factory_entered.set()
            release_factory.wait(timeout=2)
            return domestic

        self.manager._manager_factory = return_shared_manager
        refresh_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "refresh",
                lambda: self.manager.refresh_favorite_music(generation_id),
            )
        )
        refresh_thread.start()
        self.assertTrue(factory_entered.wait(timeout=1))

        gate = ExitGateLock(self.manager._state_lock, domestic.start_thread_ident)
        self.manager._state_lock = gate
        release_factory.set()
        self.assertTrue(gate.exited.wait(timeout=1))
        with self.manager._state_lock:
            collision = self.manager._runtime.collectors[
                CollectorType.FAVORITE_MUSIC
            ]
            shared_manager_entered_second_runtime = collision.manager is domestic

        close_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "close",
                lambda: self.manager.close_generation(
                    generation_id, reason="cancelled"
                ),
            )
        )
        close_thread.start()
        try:
            for _ in range(100):
                with self.manager._state_lock:
                    owner = self.manager._runtime.collectors[
                        CollectorType.DOMESTIC_LOCATION
                    ].close_owner
                if owner is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(owner)
        finally:
            gate.release.set()
            refresh_thread.join(timeout=1)
            close_thread.join(timeout=1)

        self.assertFalse(refresh_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(str(outcome["refresh_error"]), "collector_start_failed")
        self.assertNotIn("close_error", outcome)
        self.assertTrue(outcome["close"]["closed"])
        self.assertFalse(shared_manager_entered_second_runtime)
        self.assertEqual(domestic.close_calls, ["session-1"])

    def test_shared_session_collision_closes_only_rejected_distinct_manager(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        self.factory.start_session_overrides[2] = "session-1"

        with self.assertRaisesRegex(
            DouyinCommerceCollectorError, "collector_start_failed"
        ):
            self.manager.refresh_favorite_music(generation_id)

        rejected = self.factory.instances[1]
        status = self.manager.status(generation_id)
        self.assertEqual(status["aliveCollectorCount"], 1)
        self.assertEqual(status["collectorInstanceIds"]["favorite_music"], "")
        self.assertEqual(domestic.close_calls, [])
        self.assertEqual(rejected.close_calls, [None])
        closed = self.manager.close_generation(generation_id, reason="cancelled")
        self.assertTrue(closed["closed"])
        self.assertEqual(domestic.close_calls, ["session-1"])
        self.assertEqual(rejected.close_calls, [None])

    def test_shared_session_collision_does_not_complete_generation_owner_early(self):
        """碰撞分支必须等待 generation owner 真实关闭，失败后仍可重试。"""

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        self.factory.start_session_overrides[2] = "session-1"
        self.factory.block_start_ids.add(2)
        outcome: dict[str, object] = {}
        refresh_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "refresh",
                lambda: self.manager.refresh_favorite_music(generation_id),
            )
        )
        refresh_thread.start()
        for _ in range(100):
            if len(self.factory.instances) >= 2:
                break
            time.sleep(0.01)
        rejected = self.factory.instances[1]
        self.assertTrue(rejected.start_started.wait(timeout=1))
        rejected.close_failures_remaining = 1
        rejected.release_close = threading.Event()

        gate = ExitGateLock(self.manager._state_lock, rejected.start_thread_ident)
        self.manager._state_lock = gate
        rejected.release_start.set()
        self.assertTrue(gate.exited.wait(timeout=1))

        close_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "first_close",
                lambda: self.manager.close_generation(
                    generation_id, reason="cancelled"
                ),
            )
        )
        close_thread.start()
        try:
            for _ in range(100):
                with self.manager._state_lock:
                    owner = self.manager._runtime.collectors[
                        CollectorType.FAVORITE_MUSIC
                    ].close_owner
                if owner is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(owner)
        finally:
            gate.release.set()

        self.assertTrue(rejected.close_started.wait(timeout=1))
        rejected.release_close.set()
        refresh_thread.join(timeout=1)
        close_thread.join(timeout=1)
        for _ in range(100):
            with self.manager._action_queue._lock:
                queue_empty = not self.manager._action_queue._actions
            if queue_empty:
                break
            time.sleep(0.01)

        self.assertFalse(refresh_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(str(outcome["refresh_error"]), "collector_start_failed")
        self.assertNotIn("first_close_error", outcome)
        self.assertFalse(outcome["first_close"]["closed"])
        self.assertEqual(outcome["first_close"]["aliveCollectorCount"], 1)
        self.assertEqual(rejected.close_calls, ["session-1"])

        second = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertTrue(second["closed"])
        self.assertEqual(second["aliveCollectorCount"], 0)
        self.assertEqual(rejected.close_calls, ["session-1", "session-1"])

    def test_late_factory_failure_after_close_is_reported_as_stale(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.factory.block_factory_ids.add(2)
        self.factory.errors_by_id[2] = RuntimeError(
            "Cookie=secret 验证码123456 DOM=<html>private</html>"
        )
        outcome: dict[str, object] = {}
        refresh_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "refresh",
                lambda: self.manager.refresh_favorite_music(generation_id),
            )
        )
        refresh_thread.start()
        self.assertTrue(
            self.factory.factory_started.setdefault(2, threading.Event()).wait(
                timeout=1
            )
        )
        self.manager._close_wait_seconds = 0.05
        closed = self.manager.close_generation(generation_id, reason="cancelled")
        self.assertFalse(closed["closed"])
        self.factory.release_factory.setdefault(2, threading.Event()).set()
        refresh_thread.join(timeout=1)

        self.assertFalse(refresh_thread.is_alive())
        self.assertEqual(
            str(outcome["refresh_error"]), "stale_result_discarded"
        )
        self.assertIsNone(outcome["refresh_error"].__cause__)

    def test_malformed_collector_result_is_replaced_by_fixed_public_error(self):
        class SensitiveMalformedResult:
            def __iter__(self):
                raise RuntimeError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.factory.instances[0].location_result_override = [
            SensitiveMalformedResult()
        ]

        with self.assertRaises(Exception) as raised:
            self.manager.search_locations(
                generation_id, "侨港风情街", "domestic"
            )

        self.assertIsInstance(raised.exception, DouyinCommerceCollectorError)
        self.assertEqual(str(raised.exception), "collector_unknown")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("123456", str(raised.exception))
        self.assertNotIn("<html>", str(raised.exception))

    def test_scope_bool_and_string_failures_are_replaced_by_fixed_error(self):
        class SensitiveBoolScope:
            def __bool__(self):
                raise RuntimeError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

        class SensitiveStringScope:
            def __bool__(self):
                return True

            def __str__(self):
                raise RuntimeError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        for unsafe_scope in (SensitiveBoolScope(), SensitiveStringScope()):
            with self.subTest(scope_type=type(unsafe_scope).__name__):
                with self.assertRaises(Exception) as raised:
                    self.manager.search_locations(
                        generation_id, "侨港风情街", unsafe_scope
                    )
                self.assertIsInstance(
                    raised.exception, DouyinCommerceCollectorError
                )
                self.assertEqual(
                    str(raised.exception), "collector_scope_mismatch"
                )
                self.assertIsNone(raised.exception.__cause__)

    def test_collector_type_conversion_failures_are_replaced_by_fixed_error(self):
        class SensitiveBoolType:
            def __bool__(self):
                raise RuntimeError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

        class SensitiveStringType:
            def __bool__(self):
                return True

            def __str__(self):
                raise RuntimeError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        for unsafe_type in (SensitiveBoolType(), SensitiveStringType()):
            with self.subTest(type_name=type(unsafe_type).__name__):
                with self.assertRaises(Exception) as raised:
                    self.manager.retry_collector(generation_id, unsafe_type)
                self.assertIsInstance(
                    raised.exception, DouyinCommerceCollectorError
                )
                self.assertEqual(str(raised.exception), "collector_unknown")
                self.assertIsNone(raised.exception.__cause__)

    def test_generation_id_and_close_reason_conversion_failures_are_sanitized(self):
        class SensitiveText:
            def __bool__(self):
                raise RuntimeError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

            def __str__(self):
                raise RuntimeError(
                    "Cookie=secret 验证码123456 DOM=<html>private</html>"
                )

        self.manager.begin_generation(self.upload_payload)
        with self.assertRaises(Exception) as generation_error:
            self.manager.refresh_favorite_music(SensitiveText())
        self.assertIsInstance(
            generation_error.exception, DouyinCommerceCollectorError
        )
        self.assertEqual(
            str(generation_error.exception), "stale_result_discarded"
        )
        self.assertIsNone(generation_error.exception.__cause__)

        with self.assertRaises(Exception) as reason_error:
            self.manager.close_generation(reason=SensitiveText())
        self.assertIsInstance(reason_error.exception, DouyinCommerceCollectorError)
        self.assertEqual(str(reason_error.exception), "collector_unknown")
        self.assertIsNone(reason_error.exception.__cause__)

    def test_empty_generation_id_cannot_close_the_current_generation(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.close_generation("", reason="cancelled")

        self.assertEqual(str(raised.exception), "stale_result_discarded")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.factory.instances[0].close_calls, [])
        self.assertEqual(
            self.manager.status(generation_id)["generationState"], "collecting"
        )

    def test_empty_generation_id_without_runtime_is_stale_for_close(self):
        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.close_generation("", reason="cancelled")

        self.assertEqual(str(raised.exception), "stale_result_discarded")
        self.assertIsNone(raised.exception.__cause__)

    def test_empty_generation_id_cannot_read_the_current_generation_status(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.status("")

        self.assertEqual(str(raised.exception), "stale_result_discarded")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(
            self.manager.status(generation_id)["generationState"], "collecting"
        )

    def test_empty_generation_id_without_runtime_is_stale_for_status(self):
        with self.assertRaises(DouyinCommerceCollectorError) as raised:
            self.manager.status("")

        self.assertEqual(str(raised.exception), "stale_result_discarded")
        self.assertIsNone(raised.exception.__cause__)

    def test_late_diagnostic_reuses_normalized_keyword_without_second_conversion(self):
        class ConvertOnceKeyword:
            def __init__(self) -> None:
                self.string_calls = 0

            def __bool__(self):
                return True

            def __str__(self):
                self.string_calls += 1
                if self.string_calls > 1:
                    raise RuntimeError(
                        "Cookie=secret 验证码123456 DOM=<html>private</html>"
                    )
                return "侨港风情街"

        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_location = threading.Event()
        keyword = ConvertOnceKeyword()
        outcome: dict[str, object] = {}
        search_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "search",
                lambda: self.manager.search_locations(
                    generation_id, keyword, "domestic"
                ),
            )
        )
        search_thread.start()
        self.assertTrue(domestic.location_started.wait(timeout=1))
        with self.manager._state_lock:
            self.manager._runtime.generation.collectors[
                CollectorType.DOMESTIC_LOCATION
            ].instance_id = "replacement-instance"
        domestic.release_location.set()
        search_thread.join(timeout=1)

        self.assertFalse(search_thread.is_alive())
        self.assertEqual(str(outcome["search_error"]), "stale_result_discarded")
        self.assertIsNone(outcome["search_error"].__cause__)
        self.assertEqual(keyword.string_calls, 1)
        self.assertNotIn("secret", str(self.events))

    def test_close_continues_after_one_failure_and_retry_is_idempotent(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.manager.refresh_favorite_music(generation_id)
        self.manager.search_locations(generation_id, "夜南香", "local")
        self.factory.instances[1].close_failures_remaining = 1

        first = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertFalse(first["closed"])
        self.assertEqual(first["aliveCollectorCount"], 1)
        self.assertEqual(
            first["cleanupResults"],
            {
                "domestic_location": "closed",
                "favorite_music": "cleanup_incomplete",
                "local_location": "closed",
            },
        )
        self.assertEqual(
            [item.close_calls for item in self.factory.instances],
            [["session-1"], ["session-2"], ["session-3"]],
        )

        second = self.manager.close_generation(generation_id, reason="cancelled")
        calls_after_second = [list(item.close_calls) for item in self.factory.instances]
        third = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertTrue(second["closed"])
        self.assertEqual(second["aliveCollectorCount"], 0)
        self.assertEqual(second["cleanupResults"]["favorite_music"], "closed")
        self.assertEqual(
            calls_after_second,
            [["session-1"], ["session-2", "session-2"], ["session-3"]],
        )
        self.assertEqual(
            [item.close_calls for item in self.factory.instances], calls_after_second
        )
        self.assertEqual(third, second)

    def test_failed_generation_close_keeps_slot_alive_and_not_closed(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.close_failures_remaining = 1

        first = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertFalse(first["closed"])
        self.assertEqual(first["aliveCollectorCount"], 1)
        self.assertEqual(
            first["cleanupResults"]["domestic_location"],
            "cleanup_incomplete",
        )
        first_status = self.manager.status(generation_id)
        self.assertNotEqual(
            first_status["collectors"]["domestic_location"], "closed"
        )
        self.assertEqual(first_status["aliveCollectorCount"], 1)

        second = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertTrue(second["closed"])
        self.assertEqual(second["aliveCollectorCount"], 0)
        self.assertEqual(
            self.manager.status(generation_id)["collectors"][
                "domestic_location"
            ],
            "closed",
        )
        self.assertEqual(domestic.close_calls, ["session-1", "session-1"])

    def test_generation_close_prefers_strict_close_and_retries_its_failure(self):
        class StrictSessionManager(FakeSessionManager):
            def __init__(self) -> None:
                super().__init__(1)
                self.strict_close_calls: list[str | None] = []
                self.strict_failures_remaining = 1

            def close_strict(self, session_id: str | None = None) -> None:
                self.strict_close_calls.append(session_id)
                if self.strict_failures_remaining:
                    self.strict_failures_remaining -= 1
                    raise RuntimeError(
                        "Cookie=secret 验证码123456 DOM=<html>private</html>"
                    )

        strict_manager = StrictSessionManager()
        self.manager._manager_factory = lambda: strict_manager
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        first = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertFalse(first["closed"])
        self.assertEqual(first["aliveCollectorCount"], 1)
        self.assertEqual(
            first["cleanupResults"]["domestic_location"],
            "cleanup_incomplete",
        )
        self.assertEqual(strict_manager.strict_close_calls, ["session-1"])
        self.assertEqual(strict_manager.close_calls, [])
        self.assertNotIn("secret", str(first))
        self.assertNotIn("<html>", str(first))

        second = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertTrue(second["closed"])
        self.assertEqual(second["aliveCollectorCount"], 0)
        self.assertEqual(
            strict_manager.strict_close_calls,
            ["session-1", "session-1"],
        )
        self.assertEqual(strict_manager.close_calls, [])

    def test_keyboard_interrupt_close_completes_owner_and_retries(self):
        self._assert_process_control_close_completes_owner_and_retries(
            KeyboardInterrupt
        )

    def test_system_exit_close_completes_owner_and_retries(self):
        self._assert_process_control_close_completes_owner_and_retries(
            SystemExit
        )

    def _assert_process_control_close_completes_owner_and_retries(
        self,
        interruption_type: type[BaseException],
    ) -> None:
        class InterruptedStrictSessionManager(FakeSessionManager):
            def __init__(self) -> None:
                super().__init__(1)
                self.strict_close_calls: list[str | None] = []
                self.interruptions_remaining = 1

            def close_strict(self, session_id: str | None = None) -> None:
                self.strict_close_calls.append(session_id)
                if self.interruptions_remaining:
                    self.interruptions_remaining -= 1
                    raise interruption_type(
                        "Cookie=secret 验证码123456 DOM=<html>private</html>"
                    )

        strict_manager = InterruptedStrictSessionManager()
        self.manager._manager_factory = lambda: strict_manager
        self.manager._close_wait_seconds = 0.2
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        first = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertFalse(first["closed"])
        self.assertEqual(first["aliveCollectorCount"], 1)
        self.assertEqual(
            first["cleanupResults"]["domestic_location"],
            "cleanup_interrupted",
        )
        self.assertNotIn("secret", str(first))
        self.assertNotIn("123456", str(first))
        self.assertNotIn("<html>", str(first))
        with self.manager._state_lock:
            runtime = self.manager._runtime
            self.assertIsNotNone(runtime)
            collector = runtime.collectors[CollectorType.DOMESTIC_LOCATION]
            first_owner = collector.close_owner
            self.assertIsNotNone(first_owner)
            self.assertTrue(first_owner.done.is_set())
            self.assertEqual(first_owner.result, "cleanup_interrupted")
            self.assertNotIn(
                id(strict_manager), self.manager._manager_close_owners
            )

        continued = self.manager._action_queue.submit(
            generation_id,
            "queue-after-interruption",
            CollectorType.DOMESTIC_LOCATION,
            lambda: "queue_continues",
            is_cleanup=True,
        )
        self.assertEqual(
            self.manager._action_queue.wait(continued),
            "queue_continues",
        )

        second = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertTrue(second["closed"])
        self.assertEqual(second["aliveCollectorCount"], 0)
        self.assertEqual(
            second["cleanupResults"]["domestic_location"],
            "closed",
        )
        self.assertEqual(
            strict_manager.strict_close_calls,
            ["session-1", "session-1"],
        )
        self.assertIsNot(collector.close_owner, first_owner)
        self.assertTrue(collector.close_owner.done.is_set())
        self.assertEqual(strict_manager.close_calls, [])

    def test_close_reports_unstarted_collectors_without_constructing_them(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        closed = self.manager.close_generation(generation_id, reason="cancelled")

        self.assertTrue(closed["closed"])
        self.assertEqual(closed["aliveCollectorCount"], 0)
        self.assertEqual(
            closed["cleanupResults"],
            {
                "domestic_location": "closed",
                "favorite_music": "not_started",
                "local_location": "not_started",
            },
        )
        self.assertEqual(len(self.factory.instances), 1)

    def test_timeout_defers_running_slot_close_to_the_serial_queue(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.manager.refresh_favorite_music(generation_id)
        self.manager.search_locations(generation_id, "夜南香", "local")
        domestic, music, local = self.factory.instances
        domestic.release_location = threading.Event()
        outcome: dict[str, object] = {}
        search_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "search",
                lambda: self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                ),
            )
        )
        search_thread.start()
        self.assertTrue(domestic.location_started.wait(timeout=1))
        try:
            self.manager._close_wait_seconds = 0.05
            closed = self.manager.close_generation(
                generation_id, reason="cancelled"
            )

            self.assertFalse(closed["closed"])
            self.assertEqual(closed["aliveCollectorCount"], 3)
            self.assertEqual(
                closed["cleanupResults"]["domestic_location"],
                "cleanup_incomplete",
            )
            self.assertEqual(
                closed["cleanupResults"]["favorite_music"],
                "cleanup_incomplete",
            )
            self.assertEqual(
                closed["cleanupResults"]["local_location"],
                "cleanup_incomplete",
            )
            self.assertEqual(domestic.close_calls, [])
            self.assertFalse(domestic.close_during_location)
            self.assertEqual(music.close_calls, [])
            self.assertEqual(local.close_calls, [])
        finally:
            domestic.release_location.set()
            search_thread.join(timeout=1)

        self.assertFalse(search_thread.is_alive())
        self.assertTrue(domestic.close_started.wait(timeout=1))
        self.assertEqual(domestic.close_calls, ["session-1"])
        self.assertFalse(domestic.close_during_location)
        self.assertTrue(music.close_started.wait(timeout=1))
        self.assertTrue(local.close_started.wait(timeout=1))
        self.assertEqual(music.close_calls, ["session-2"])
        self.assertEqual(local.close_calls, ["session-3"])
        for _ in range(100):
            if self.manager.status(generation_id)["generationState"] == "closed":
                break
            time.sleep(0.01)
        final_status = self.manager.status(generation_id)
        self.assertEqual(final_status["generationState"], "closed")
        self.assertEqual(final_status["aliveCollectorCount"], 0)

    def test_generation_close_queues_all_sibling_closes_and_returns_on_timeout(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.manager.refresh_favorite_music(generation_id)
        self.manager.search_locations(generation_id, "夜南香", "local")
        domestic, music, local = self.factory.instances
        domestic.release_close = threading.Event()
        outcome: dict[str, object] = {}
        close_finished = threading.Event()
        caller_ident: dict[str, int] = {}

        def run_close() -> None:
            caller_ident["value"] = threading.get_ident()
            try:
                outcome["close"] = self.manager.close_generation(
                    generation_id, reason="cancelled"
                )
            finally:
                close_finished.set()

        self.manager._close_wait_seconds = 0.05
        close_thread = threading.Thread(target=run_close)
        close_thread.start()
        self.assertTrue(domestic.close_started.wait(timeout=1))
        try:
            self.assertTrue(close_finished.wait(timeout=0.3))
            self.assertFalse(outcome["close"]["closed"])
            self.assertEqual(domestic.close_calls, ["session-1"])
            self.assertEqual(music.close_calls, [])
            self.assertEqual(local.close_calls, [])
        finally:
            domestic.release_close.set()
            close_thread.join(timeout=1)

        self.assertFalse(close_thread.is_alive())
        self.assertTrue(music.close_started.wait(timeout=1))
        self.assertTrue(local.close_started.wait(timeout=1))
        all_close_threads = {
            *domestic.close_thread_idents,
            *music.close_thread_idents,
            *local.close_thread_idents,
        }
        self.assertEqual(len(all_close_threads), 1)
        self.assertNotIn(caller_ident["value"], all_close_threads)
        for _ in range(100):
            if self.manager.status(generation_id)["generationState"] == "closed":
                break
            time.sleep(0.01)
        final_status = self.manager.status(generation_id)
        self.assertEqual(final_status["generationState"], "closed")
        self.assertEqual(final_status["aliveCollectorCount"], 0)

    def test_close_can_timeout_while_begin_start_upload_is_blocked(self):
        self.factory.block_start_ids.add(1)
        outcome: dict[str, object] = {}
        begin_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "begin",
                lambda: self.manager.begin_generation(self.upload_payload),
            )
        )
        begin_thread.start()
        for _ in range(100):
            if self.factory.instances:
                break
            time.sleep(0.01)
        domestic = self.factory.instances[0]
        self.assertTrue(domestic.start_started.wait(timeout=1))
        generation_id = self.manager._runtime.generation.generation_id
        close_finished = threading.Event()

        def run_close() -> None:
            try:
                outcome["close"] = self.manager.close_generation(
                    generation_id, reason="cancelled"
                )
            except Exception as error:
                outcome["close_error"] = error
            finally:
                close_finished.set()

        self.manager._close_wait_seconds = 0.05
        close_thread = threading.Thread(target=run_close)
        close_thread.start()
        try:
            self.assertTrue(close_finished.wait(timeout=0.3))
            self.assertNotIn("close_error", outcome)
            self.assertFalse(outcome["close"]["closed"])
            self.assertEqual(
                outcome["close"]["cleanupResults"]["domestic_location"],
                "cleanup_incomplete",
            )
            self.assertEqual(domestic.close_calls, [])
        finally:
            domestic.release_start.set()
            begin_thread.join(timeout=1)
            close_thread.join(timeout=1)

        self.assertFalse(begin_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertTrue(domestic.close_started.wait(timeout=1))
        self.assertEqual(domestic.close_calls, ["session-1"])

    def test_close_can_timeout_while_lazy_manager_factory_is_blocked(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        self.factory.block_factory_ids.add(2)
        outcome: dict[str, object] = {}
        refresh_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "refresh",
                lambda: self.manager.refresh_favorite_music(generation_id),
            )
        )
        refresh_thread.start()
        self.assertTrue(
            self.factory.factory_started.setdefault(2, threading.Event()).wait(
                timeout=1
            )
        )
        close_finished = threading.Event()

        def run_close() -> None:
            try:
                outcome["close"] = self.manager.close_generation(
                    generation_id, reason="cancelled"
                )
            except Exception as error:
                outcome["close_error"] = error
            finally:
                close_finished.set()

        self.manager._close_wait_seconds = 0.05
        close_thread = threading.Thread(target=run_close)
        close_thread.start()
        try:
            self.assertTrue(close_finished.wait(timeout=0.3))
            self.assertNotIn("close_error", outcome)
            self.assertFalse(outcome["close"]["closed"])
            self.assertEqual(
                outcome["close"]["cleanupResults"]["favorite_music"],
                "cleanup_incomplete",
            )
            self.assertEqual(domestic.close_calls, [])
        finally:
            self.factory.release_factory.setdefault(2, threading.Event()).set()
            refresh_thread.join(timeout=1)
            close_thread.join(timeout=1)

        self.assertFalse(refresh_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        lazy = self.factory.instances[1]
        self.assertEqual(lazy.start_payloads, [])
        self.assertTrue(domestic.close_started.wait(timeout=1))
        self.assertEqual(domestic.close_calls, ["session-1"])
        self.assertTrue(lazy.close_started.wait(timeout=1))
        self.assertEqual(lazy.close_calls, [None])

    def test_second_close_reuses_pending_deferred_cleanup_without_duplicate_close(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_location = threading.Event()
        outcome: dict[str, object] = {}
        search_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "search",
                lambda: self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                ),
            )
        )
        search_thread.start()
        self.assertTrue(domestic.location_started.wait(timeout=1))
        self.manager._close_wait_seconds = 0.05
        first_close = self.manager.close_generation(
            generation_id, reason="cancelled"
        )
        self.assertFalse(first_close["closed"])
        domestic.release_close = threading.Event()
        self.manager._close_wait_seconds = 1
        second_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "second_close",
                lambda: self.manager.close_generation(
                    generation_id, reason="cancelled"
                ),
            )
        )
        second_thread.start()

        domestic.release_location.set()
        search_thread.join(timeout=1)
        self.assertTrue(domestic.close_started.wait(timeout=1))
        time.sleep(0.05)
        try:
            self.assertEqual(domestic.close_calls, ["session-1"])
        finally:
            domestic.release_close.set()
            second_thread.join(timeout=1)

        self.assertFalse(second_thread.is_alive())
        self.assertNotIn("second_close_error", outcome)
        self.assertTrue(outcome["second_close"]["closed"])
        self.assertEqual(domestic.close_calls, ["session-1"])

    def test_close_result_recomputes_latest_state_after_deferred_cleanup_finishes(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_location = threading.Event()
        outcome: dict[str, object] = {}
        search_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "search",
                lambda: self.manager.search_locations(
                    generation_id, "侨港风情街", "domestic"
                ),
            )
        )
        search_thread.start()
        self.assertTrue(domestic.location_started.wait(timeout=1))
        self.manager._close_wait_seconds = 1
        close_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "close",
                lambda: self.manager.close_generation(
                    generation_id, reason="cancelled"
                ),
            )
        )
        close_thread.start()
        for _ in range(100):
            runtime = self.manager._runtime
            owner = runtime.collectors[
                CollectorType.DOMESTIC_LOCATION
            ].close_owner
            if owner is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(owner)
        domestic.release_location.set()
        search_thread.join(timeout=1)
        self.assertTrue(domestic.close_started.wait(timeout=1))
        for _ in range(100):
            if self.manager.status(generation_id)["generationState"] == "closed":
                break
            time.sleep(0.01)
        self.assertEqual(
            self.manager.status(generation_id)["generationState"], "closed"
        )
        close_thread.join(timeout=1)

        self.assertFalse(close_thread.is_alive())
        self.assertTrue(outcome["close"]["closed"])
        self.assertEqual(outcome["close"]["aliveCollectorCount"], 0)

    def test_concurrent_begin_calls_are_single_flight_without_leaking_first_session(self):
        self.factory.block_start_ids.add(1)
        outcome: dict[str, object] = {}
        first_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "first_begin",
                lambda: self.manager.begin_generation(self.upload_payload),
            )
        )
        first_thread.start()
        for _ in range(100):
            if self.factory.instances:
                break
            time.sleep(0.01)
        first = self.factory.instances[0]
        self.assertTrue(first.start_started.wait(timeout=1))

        second_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "second_begin",
                lambda: self.manager.begin_generation(self.upload_payload),
            )
        )
        second_thread.start()
        time.sleep(0.05)
        self.assertEqual(len(self.factory.instances), 1)

        first.release_start.set()
        first_thread.join(timeout=1)
        second_thread.join(timeout=1)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertNotIn("first_begin_error", outcome)
        self.assertNotIn("second_begin_error", outcome)
        first_generation = outcome["first_begin"]["setupGenerationId"]
        second_generation = outcome["second_begin"]["setupGenerationId"]
        self.assertNotEqual(first_generation, second_generation)
        self.assertEqual(first.close_calls, ["session-1"])
        self.assertEqual(len(self.factory.instances), 2)
        self.assertEqual(
            self.manager.status()["setupGenerationId"], second_generation
        )

    def test_concurrent_close_calls_wait_for_one_idempotent_cleanup(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        self.manager.refresh_favorite_music(generation_id)
        self.manager.search_locations(generation_id, "夜南香", "local")
        domestic = self.factory.instances[0]
        domestic.release_close = threading.Event()
        outcome: dict[str, object] = {}
        first_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "first_close",
                lambda: self.manager.close_generation(
                    generation_id, reason="cancelled"
                ),
            )
        )
        first_thread.start()
        self.assertTrue(domestic.close_started.wait(timeout=1))
        second_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "second_close",
                lambda: self.manager.close_generation(
                    generation_id, reason="cancelled"
                ),
            )
        )
        second_thread.start()
        time.sleep(0.05)

        domestic.release_close.set()
        first_thread.join(timeout=1)
        second_thread.join(timeout=1)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertNotIn("first_close_error", outcome)
        self.assertNotIn("second_close_error", outcome)
        self.assertEqual(outcome["first_close"], outcome["second_close"])
        self.assertTrue(outcome["first_close"]["closed"])
        self.assertEqual(
            [item.close_calls for item in self.factory.instances],
            [["session-1"], ["session-2"], ["session-3"]],
        )

    def test_stale_close_owner_cannot_block_valid_generation_close(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        outcome: dict[str, object] = {}
        start_stale = threading.Event()
        stale_thread = threading.Thread(
            target=lambda: (
                start_stale.wait(timeout=1),
                self._capture_call(
                    outcome,
                    "stale_close",
                    lambda: self.manager.close_generation(
                        "stale-generation", reason="cancelled"
                    ),
                ),
            )
        )
        stale_thread.start()
        gate = ExitGateLock(self.manager._state_lock, stale_thread.ident)
        self.manager._state_lock = gate
        start_stale.set()
        self.assertTrue(gate.exited.wait(timeout=1))
        valid_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "valid_close",
                lambda: self.manager.close_generation(
                    generation_id, reason="cancelled"
                ),
            )
        )
        valid_thread.start()
        time.sleep(0.05)
        gate.release.set()
        stale_thread.join(timeout=1)
        valid_thread.join(timeout=1)

        self.assertFalse(stale_thread.is_alive())
        self.assertFalse(valid_thread.is_alive())
        self.assertEqual(
            str(outcome["stale_close_error"]), "stale_result_discarded"
        )
        self.assertNotIn("valid_close_error", outcome)
        self.assertTrue(outcome["valid_close"]["closed"])
        self.assertEqual(self.factory.instances[0].close_calls, ["session-1"])

    def test_stale_close_waiter_cannot_reuse_valid_generation_result(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]
        domestic = self.factory.instances[0]
        domestic.release_close = threading.Event()
        outcome: dict[str, object] = {}
        valid_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "valid_close",
                lambda: self.manager.close_generation(
                    generation_id, reason="cancelled"
                ),
            )
        )
        valid_thread.start()
        self.assertTrue(domestic.close_started.wait(timeout=1))
        stale_thread = threading.Thread(
            target=lambda: self._capture_call(
                outcome,
                "stale_close",
                lambda: self.manager.close_generation(
                    "stale-generation", reason="cancelled"
                ),
            )
        )
        stale_thread.start()
        time.sleep(0.05)
        domestic.release_close.set()
        valid_thread.join(timeout=1)
        stale_thread.join(timeout=1)

        self.assertFalse(valid_thread.is_alive())
        self.assertFalse(stale_thread.is_alive())
        self.assertNotIn("valid_close_error", outcome)
        self.assertTrue(outcome["valid_close"]["closed"])
        self.assertEqual(
            str(outcome["stale_close_error"]), "stale_result_discarded"
        )
        self.assertNotIn("stale_close", outcome)


class DouyinCommerceCollectorVerifierTests(unittest.TestCase):
    """验证器只允许采集公开候选并关闭临时代际。"""

    _ACCOUNT = {
        "id": 31,
        "type": 3,
        "status": 1,
        "filePath": "private-account-state.json",
    }

    def _persist_diagnostic_report(
        self,
        diagnostic: dict[str, object],
        *,
        generation_id: str = "generation-safe",
        instance_id: str = "instance-safe",
        close_result: dict[str, object] | None = None,
    ) -> tuple[str, dict[str, object]]:
        report = {
            "accountMaskedId": "account-31",
            "actionIntervalMs": 800,
            "generations": [
                {
                    "generation": "A",
                    "order": "music-domestic-local",
                    "ending": "normal",
                    "outcome": "completed",
                    "stopReason": "",
                    "generationIdHash": generation_id,
                    "collectorInstanceHashes": {
                        "domestic_location": instance_id,
                    },
                    "candidateCounts": {},
                    "diagnostics": [diagnostic],
                    "closeResult": close_result
                    or {
                        "closed": True,
                        "aliveCollectorCount": 0,
                        "cleanupResults": {},
                    },
                }
            ],
            "stopReason": "",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "diagnostic-report.json"
            _write_report(report_path, report)
            report_text = report_path.read_text(encoding="utf-8")
        return report_text, json.loads(report_text)

    def test_cleanup_interrupted_survives_public_close_and_final_json(self):
        raw_close = {
            "closed": False,
            "aliveCollectorCount": 1,
            "cleanupResults": {
                "domestic_location": "cleanup_interrupted",
            },
        }

        public_close = _public_close_result(raw_close)

        self.assertEqual(
            public_close["cleanupResults"]["domestic_location"],
            "cleanup_interrupted",
        )
        _report_text, report = self._persist_diagnostic_report(
            {
                "phase": "cleanup",
                "action": "close_generation",
                "outcome": "failed",
                "cleanupResult": "cleanup_interrupted",
            },
            close_result=raw_close,
        )
        generation = report["generations"][0]
        self.assertEqual(
            generation["diagnostics"][0]["cleanupResult"],
            "cleanup_interrupted",
        )
        self.assertEqual(
            generation["closeResult"]["cleanupResults"][
                "domestic_location"
            ],
            "cleanup_interrupted",
        )

    def test_report_writer_rejects_uuid_in_structural_phase_field(self):
        secret_uuid = "123e4567-e89b-12d3-a456-426614174000"

        report_text, report = self._persist_diagnostic_report(
            {
                "phase": secret_uuid,
                "action": "search_locations",
                "outcome": "private-outcome-secret",
            }
        )

        diagnostic = report["generations"][0]["diagnostics"][0]
        self.assertNotIn(secret_uuid, report_text)
        self.assertNotIn("private-outcome-secret", report_text)
        self.assertEqual(diagnostic["phase"], "unknown")
        self.assertEqual(diagnostic["outcome"], "unknown")

    def test_report_writer_redacts_unlabelled_cookie_key_values(self):
        cookie_secret = "ttwid=private-ttwid; passport_csrf=private-csrf"

        report_text, report = self._persist_diagnostic_report(
            {
                "phase": "result",
                "action": "search_locations",
                "keyword": cookie_secret,
                "outcome": "success",
            }
        )

        diagnostic = report["generations"][0]["diagnostics"][0]
        self.assertNotIn("private-ttwid", report_text)
        self.assertNotIn("private-csrf", report_text)
        self.assertEqual(diagnostic["keyword"], "<redacted>")

    def test_report_writer_redacts_uuid_in_readable_keyword(self):
        secret_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        report_text, report = self._persist_diagnostic_report(
            {
                "phase": "result",
                "action": "search_locations",
                "keyword": secret_uuid,
                "outcome": "success",
            }
        )

        diagnostic = report["generations"][0]["diagnostics"][0]
        self.assertNotIn(secret_uuid, report_text)
        self.assertEqual(diagnostic["keyword"], "<redacted>")

    def test_report_writer_rejects_html_tag_in_structural_action_field(self):
        html_secret = '<input value="private-node-secret">'

        report_text, report = self._persist_diagnostic_report(
            {
                "phase": "result",
                "action": html_secret,
                "outcome": "success",
            }
        )

        diagnostic = report["generations"][0]["diagnostics"][0]
        self.assertNotIn("private-node-secret", report_text)
        self.assertEqual(diagnostic["action"], "unknown")

    def test_report_writer_rehashes_twelve_hex_character_ids(self):
        report_text, report = self._persist_diagnostic_report(
            {
                "requestIdHash": "111111111111",
                "setupGenerationIdHash": "222222222222",
                "collectorInstanceIdHash": "333333333333",
                "phase": "result",
                "action": "search_locations",
                "outcome": "success",
            },
            generation_id="0123456789ab",
            instance_id="abcdef123456",
        )

        for raw_id in (
            "0123456789ab",
            "abcdef123456",
            "111111111111",
            "222222222222",
            "333333333333",
        ):
            self.assertNotIn(raw_id, report_text)
        generation = report["generations"][0]
        diagnostic = generation["diagnostics"][0]
        self.assertEqual(generation["generationIdHash"], "d407ad901895")
        self.assertEqual(
            generation["collectorInstanceHashes"]["domestic_location"],
            "da4ec3358a10",
        )
        self.assertEqual(diagnostic["requestIdHash"], "a18ac4e6fbd3")
        self.assertEqual(diagnostic["setupGenerationIdHash"], "76eb17a8fb17")
        self.assertEqual(
            diagnostic["collectorInstanceIdHash"], "5080b6ee794e"
        )

    def test_execute_rejects_duplicate_three_field_accounts_before_file_path_validation(self):
        manager = FakeVerificationManager()
        duplicate_accounts = [
            {**self._ACCOUNT, "filePath": ""},
            {**self._ACCOUNT, "filePath": "valid-account-state.json"},
        ]
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "must-not-exist.json"
            with mock.patch(
                "tools.verify_douyin_commerce_collectors.commerce_collector_manager",
                manager,
            ), mock.patch(
                "tools.verify_douyin_commerce_collectors.account_service.list_accounts",
                return_value=duplicate_accounts,
            ), mock.patch(
                "tools.verify_douyin_commerce_collectors.time.sleep"
            ), redirect_stdout(output):
                result = verification_main(
                    [
                        "--account-id",
                        "31",
                        "--keyword",
                        "夜南香",
                        "--execute",
                        "--output",
                        str(report_path),
                    ]
                )

            self.assertEqual(result, 2)
            self.assertFalse(report_path.exists())
        self.assertEqual(manager.begin_payloads, [])
        self.assertEqual(
            json.loads(output.getvalue())["errorCode"],
            "account_not_available",
        )

    def test_report_writer_independently_redacts_ids_text_and_cleanup_values(self):
        malicious_report = {
            "schemaVersion": "douyin-commerce-collector-verification/v1",
            "accountMaskedId": "account-31",
            "actionIntervalMs": 800,
            "generations": [
                {
                    "generation": "A",
                    "order": "music-domestic-local",
                    "ending": "normal",
                    "outcome": "completed",
                    "stopReason": "",
                    "generationIdHash": "raw-generation-secret",
                    "collectorInstanceHashes": {
                        "domestic_location": "raw-instance-secret",
                    },
                    "candidateCounts": {
                        "favoriteMusic": [1],
                        "domestic": [1],
                        "local": [1],
                    },
                    "diagnostics": [
                        {
                            "timestamp": "2026-08-09T12:00:00+08:00",
                            "requestId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                            "setupGenerationId": "raw-generation-secret",
                            "collectorInstanceId": "raw-instance-secret",
                            "collectorType": "domestic_location",
                            "accountMaskedId": "account-31",
                            "phase": "result",
                            "action": "search_locations",
                            "scope": "domestic",
                            "keyword": (
                                "/Users/andy/private/account.json "
                                "Cookie=top-secret-cookie "
                                "HTML=<html>private</html> DOM=private-dom"
                            ),
                            "attempt": 1,
                            "candidateCount": 1,
                            "durationMs": 12,
                            "outcome": "success",
                            "errorCode": "",
                            "cleanupResult": "DOM=private-dom",
                        }
                    ],
                    "closeResult": {
                        "closed": True,
                        "aliveCollectorCount": 0,
                        "cleanupResults": {
                            "domestic_location": "Cookie=top-secret-cookie",
                            "favorite_music": "closed",
                            "local_location": "not_started",
                            "unknown-cleanup-key": "HTML=<html>private</html>",
                        },
                    },
                }
            ],
            "stopReason": "",
            "finalSubmitCount": 99,
            "draftSaveCount": 98,
            "publicPublishCount": 97,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "security-boundary.json"
            _write_report(report_path, malicious_report)
            report_text = report_path.read_text(encoding="utf-8")
            report = json.loads(report_text)

        for secret in (
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "raw-generation-secret",
            "raw-instance-secret",
            "/Users/andy/private/account.json",
            "top-secret-cookie",
            "<html>private</html>",
            "private-dom",
            "unknown-cleanup-key",
        ):
            self.assertNotIn(secret, report_text)
        generation = report["generations"][0]
        diagnostic = generation["diagnostics"][0]
        self.assertEqual(diagnostic["requestIdHash"], "9af645a8fef3")
        self.assertEqual(diagnostic["cleanupResult"], "cleanup_unknown")
        self.assertEqual(
            generation["closeResult"]["cleanupResults"],
            {
                "domestic_location": "cleanup_unknown",
                "favorite_music": "closed",
                "local_location": "not_started",
            },
        )
        self.assertEqual(report["finalSubmitCount"], 0)
        self.assertEqual(report["draftSaveCount"], 0)
        self.assertEqual(report["publicPublishCount"], 0)

    def test_default_mode_independent_process_never_imports_collector_module(self):
        verifier = Path(__file__).resolve().parent / "tools" / "verify_douyin_commerce_collectors.py"
        guard_script = """
import builtins
import runpy
import sys

verifier = sys.argv[1]
original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "app_core.douyin_commerce_collectors":
        raise RuntimeError("collector module imported in default mode")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
sys.argv = [verifier, "--account-id", "31", "--keyword", "夜南香"]
try:
    runpy.run_path(verifier, run_name="__main__")
except SystemExit as error:
    if error.code not in (None, 0):
        raise
if "app_core.douyin_commerce_collectors" in sys.modules:
    raise RuntimeError("collector module retained in default mode")
print("DEFAULT_MODE_NO_COLLECTOR_IMPORT")
"""

        completed = subprocess.run(
            [sys.executable, "-c", guard_script, str(verifier)],
            cwd=Path(__file__).resolve().parent,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("DEFAULT_MODE_NO_COLLECTOR_IMPORT", completed.stdout)
        self.assertNotIn("collector module imported", completed.stderr)

    def test_verifier_default_mode_only_prints_plan(self):
        output = io.StringIO()
        with mock.patch(
            "tools.verify_douyin_commerce_collectors.commerce_collector_manager"
        ) as manager, mock.patch(
            "tools.verify_douyin_commerce_collectors.account_service.list_accounts"
        ) as list_accounts, redirect_stdout(output):
            result = verification_main(
                ["--account-id", "31", "--keyword", "夜南香"]
            )

        self.assertEqual(result, 0)
        manager.begin_generation.assert_not_called()
        list_accounts.assert_not_called()
        printed = json.loads(output.getvalue())
        self.assertEqual(printed[0]["generation"], "A")
        self.assertEqual(len(printed), 3)

    def test_verification_sequences_cover_three_generations_and_three_orders(self):
        sequences = build_verification_sequences("夜南香", domestic_count=20)

        self.assertEqual([row["generation"] for row in sequences], ["A", "B", "C"])
        self.assertEqual(
            [row["order"] for row in sequences],
            [
                "music-domestic-local",
                "domestic-music-local",
                "local-domestic-music",
            ],
        )
        self.assertEqual(
            [row["domesticSearchCount"] for row in sequences],
            [20, 20, 1],
        )
        self.assertEqual(
            [row["localSearchCount"] for row in sequences],
            [2, 1, 1],
        )
        self.assertEqual(
            [row["ending"] for row in sequences],
            ["normal", "abandon", "normal"],
        )
        self.assertEqual(
            [action["type"] for action in sequences[0]["actions"][:2]],
            ["music", "domestic"],
        )
        self.assertEqual(
            [action["type"] for action in sequences[1]["actions"][-2:]],
            ["music", "local"],
        )
        self.assertEqual(
            [action["type"] for action in sequences[2]["actions"]],
            ["local", "domestic", "music"],
        )

    def test_fake_execute_closes_every_generation_and_writes_redacted_zero_submit_report(self):
        manager = FakeVerificationManager()
        sleep_intervals: list[float] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "collector-verification.json"
            with mock.patch(
                "tools.verify_douyin_commerce_collectors.commerce_collector_manager",
                manager,
            ), mock.patch(
                "tools.verify_douyin_commerce_collectors.account_service.list_accounts",
                return_value=[dict(self._ACCOUNT)],
            ), mock.patch(
                "tools.verify_douyin_commerce_collectors.time.sleep",
                side_effect=sleep_intervals.append,
            ):
                result = verification_main(
                    [
                        "--account-id",
                        "31",
                        "--keyword",
                        "夜南香",
                        "--execute",
                        "--output",
                        str(report_path),
                    ]
                )
            report_text = report_path.read_text(encoding="utf-8")
            report = json.loads(report_text)

        self.assertEqual(result, 0)
        self.assertEqual(len(manager.begin_payloads), 3)
        self.assertEqual(
            [payload["accountList"] for payload in manager.begin_payloads],
            [["private-account-state.json"]] * 3,
        )
        self.assertEqual(
            manager.close_calls,
            [
                ("generation-full-secret-1", "verification_normal"),
                ("generation-full-secret-2", "verification_abandon"),
                ("generation-full-secret-3", "verification_normal"),
            ],
        )
        self.assertTrue(sleep_intervals)
        self.assertTrue(all(interval >= 0.8 for interval in sleep_intervals))
        self.assertEqual(report["finalSubmitCount"], 0)
        self.assertEqual(report["draftSaveCount"], 0)
        self.assertEqual(report["publicPublishCount"], 0)
        self.assertEqual([item["generation"] for item in report["generations"]], ["A", "B", "C"])
        self.assertTrue(
            all(
                item["diagnostics"][-1]["action"] == "close_generation"
                for item in report["generations"]
            )
        )
        self.assertNotIn("private-account-state.json", report_text)
        self.assertNotIn("generation-full-secret", report_text)
        self.assertNotIn("domestic-full-secret", report_text)
        self.assertNotIn("music-full-secret", report_text)
        self.assertNotIn("local-full-secret", report_text)

    def test_execute_stops_immediately_and_closes_current_generation_on_safety_signal(self):
        for error_code in (
            "login_required",
            "rate_limited_or_degraded",
            "account_verification_required",
        ):
            with self.subTest(error_code=error_code):
                manager = FakeVerificationManager()
                manager.action_error_codes[(1, "music")] = error_code
                with tempfile.TemporaryDirectory() as temp_dir:
                    report_path = Path(temp_dir) / "stopped.json"
                    with mock.patch(
                        "tools.verify_douyin_commerce_collectors.commerce_collector_manager",
                        manager,
                    ), mock.patch(
                        "tools.verify_douyin_commerce_collectors.account_service.list_accounts",
                        return_value=[dict(self._ACCOUNT)],
                    ), mock.patch(
                        "tools.verify_douyin_commerce_collectors.time.sleep"
                    ):
                        result = verification_main(
                            [
                                "--account-id",
                                "31",
                                "--keyword",
                                "夜南香",
                                "--execute",
                                "--output",
                                str(report_path),
                            ]
                        )
                    report = json.loads(report_path.read_text(encoding="utf-8"))

                self.assertEqual(result, 3)
                self.assertEqual(len(manager.begin_payloads), 1)
                self.assertEqual(
                    manager.close_calls,
                    [("generation-full-secret-1", "verification_stopped")],
                )
                self.assertEqual(report["stopReason"], error_code)
                self.assertEqual(report["finalSubmitCount"], 0)
                self.assertEqual(report["draftSaveCount"], 0)
                self.assertEqual(report["publicPublishCount"], 0)


if __name__ == "__main__":
    unittest.main()
