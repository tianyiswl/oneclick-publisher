# -*- coding: utf-8 -*-
"""抖音带货平台设置隔离采集器测试。"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any, Mapping
from unittest import mock

from app_core.douyin_commerce_collectors import (
    DouyinCommerceCollectorError,
    DouyinCommerceCollectorManager,
)
from app_core.douyin_commerce_setup_state import CollectorType


class FakeSessionManager:
    """仅替代真实浏览器边界，保留协调器的真实状态与队列行为。"""

    def __init__(self, manager_id: int) -> None:
        self.manager_id = manager_id
        self.session_id = ""
        self.start_payloads: list[dict[str, Any]] = []
        self.start_started = threading.Event()
        self.release_start: threading.Event | None = None
        self.refresh_calls: list[str] = []
        self.location_calls: list[tuple[str, object, object]] = []
        self.location_scopes: list[object] = []
        self.close_calls: list[str | None] = []
        self.close_started = threading.Event()
        self.release_close: threading.Event | None = None
        self.close_during_location = False
        self.refresh_started = threading.Event()
        self.location_started = threading.Event()
        self.release_refresh: threading.Event | None = None
        self.release_location: threading.Event | None = None
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
        self.start_started.set()
        if self.release_start is not None:
            self.release_start.wait(timeout=2)
        self.session_id = f"session-{self.manager_id}"
        return {"sessionId": self.session_id}

    def refresh_favorite_music(self, session_id: str) -> list[dict[str, str]]:
        self.refresh_calls.append(session_id)
        self.refresh_started.set()
        if self.release_refresh is not None:
            self.release_refresh.wait(timeout=2)
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
    ) -> list[dict[str, Any]]:
        self.location_calls.append((session_id, keyword, scope))
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
        if self.release_close is not None:
            self.release_close.wait(timeout=2)
        if self.close_failures_remaining:
            self.close_failures_remaining -= 1
            raise RuntimeError("Cookie=secret DOM=<html>")


class FakeManagerFactory:
    def __init__(self) -> None:
        self.instances: list[FakeSessionManager] = []
        self.block_start_ids: set[int] = set()
        self.errors_by_id: dict[int, Exception] = {}

    def __call__(self) -> FakeSessionManager:
        manager_id = len(self.instances) + 1
        if manager_id in self.errors_by_id:
            raise self.errors_by_id[manager_id]
        instance = FakeSessionManager(manager_id)
        if instance.manager_id in self.block_start_ids:
            instance.release_start = threading.Event()
        self.instances.append(instance)
        return instance


class DouyinCommerceCollectorManagerTests(unittest.TestCase):
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

    def test_domestic_and_local_keywords_never_share_a_session(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        domestic = self.manager.search_locations(
            generation_id, "侨港风情街", "domestic"
        )
        local = self.manager.search_locations(generation_id, "夜南香", "local")

        self.assertEqual(domestic[0]["name"], "侨港风情街")
        self.assertEqual(local[0]["name"], "夜南香")
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
        self.assertEqual(self.events[-1]["errorCode"], "stale_result_discarded")
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
        status = self.manager.status(generation_id)
        self.assertEqual(status["collectors"]["domestic_location"], "active")
        self.assertEqual(
            status["collectorInstanceIds"]["domestic_location"],
            "retry-replacement",
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
        original_wait_running = self.manager._action_queue.wait_running

        try:
            with mock.patch.object(
                self.manager._action_queue,
                "wait_running",
                side_effect=lambda target_id, timeout: original_wait_running(
                    target_id, timeout=0.05
                ),
            ):
                closed = self.manager.close_generation(
                    generation_id, reason="cancelled"
                )

            self.assertFalse(closed["closed"])
            self.assertEqual(closed["aliveCollectorCount"], 1)
            self.assertEqual(
                closed["cleanupResults"]["domestic_location"],
                "cleanup_incomplete",
            )
            self.assertEqual(domestic.close_calls, [])
            self.assertFalse(domestic.close_during_location)
            self.assertEqual(music.close_calls, ["session-2"])
            self.assertEqual(local.close_calls, ["session-3"])
        finally:
            domestic.release_location.set()
            search_thread.join(timeout=1)

        self.assertFalse(search_thread.is_alive())
        self.assertTrue(domestic.close_started.wait(timeout=1))
        self.assertEqual(domestic.close_calls, ["session-1"])
        self.assertFalse(domestic.close_during_location)
        for _ in range(100):
            if self.manager.status(generation_id)["generationState"] == "closed":
                break
            time.sleep(0.01)
        final_status = self.manager.status(generation_id)
        self.assertEqual(final_status["generationState"], "closed")
        self.assertEqual(final_status["aliveCollectorCount"], 0)

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
        original_wait_running = self.manager._action_queue.wait_running
        with mock.patch.object(
            self.manager._action_queue,
            "wait_running",
            side_effect=lambda target_id, timeout: original_wait_running(
                target_id, timeout=0.05
            ),
        ):
            first_close = self.manager.close_generation(
                generation_id, reason="cancelled"
            )
        self.assertFalse(first_close["closed"])
        domestic.release_close = threading.Event()
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


if __name__ == "__main__":
    unittest.main()
