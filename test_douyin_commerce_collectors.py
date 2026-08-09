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
        self.refresh_calls: list[str] = []
        self.location_calls: list[tuple[str, object, object]] = []
        self.location_scopes: list[object] = []
        self.close_calls: list[str | None] = []
        self.refresh_started = threading.Event()
        self.location_started = threading.Event()
        self.release_refresh: threading.Event | None = None
        self.release_location: threading.Event | None = None
        self.search_error: Exception | None = None
        self.close_failures_remaining = 0

    def start_upload(
        self,
        payload: Mapping[str, Any],
        *,
        on_progress=None,
    ) -> dict[str, str]:
        del on_progress
        self.start_payloads.append(dict(payload))
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
        return [
            {
                "poiId": f"poi-{self.manager_id}",
                "name": str(keyword),
                "address": "广西北海",
            }
        ]

    def close(self, session_id: str | None = None) -> None:
        self.close_calls.append(session_id)
        if self.close_failures_remaining:
            self.close_failures_remaining -= 1
            raise RuntimeError("Cookie=secret DOM=<html>")


class FakeManagerFactory:
    def __init__(self) -> None:
        self.instances: list[FakeSessionManager] = []

    def __call__(self) -> FakeSessionManager:
        instance = FakeSessionManager(len(self.instances) + 1)
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

    def test_running_action_timeout_is_reported_as_cleanup_incomplete(self):
        generation_id = self.manager.begin_generation(self.upload_payload)[
            "setupGenerationId"
        ]

        with mock.patch.object(
            self.manager._action_queue,
            "running_collector_type",
            return_value=CollectorType.DOMESTIC_LOCATION,
        ), mock.patch.object(
            self.manager._action_queue,
            "wait_running",
            return_value=False,
        ):
            result = self.manager.close_generation(
                generation_id, reason="cancelled"
            )

        self.assertFalse(result["closed"])
        self.assertEqual(result["aliveCollectorCount"], 1)
        self.assertEqual(
            result["cleanupResults"]["domestic_location"],
            "cleanup_incomplete",
        )


if __name__ == "__main__":
    unittest.main()
