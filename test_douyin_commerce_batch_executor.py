# -*- coding: utf-8 -*-
"""抖音带货批量执行器的离线回归测试。

所有会话都是内存替身；测试绝不启动 Playwright、浏览器或平台请求。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import database, task_service
from app_core.douyin_commerce_batch_executor import (
    BatchProgressEvent,
    DouyinCommerceBatchExecutor,
    DouyinCommerceBatchExecutorError,
    _location_search_keywords,
)
from app_core.douyin_commerce_batch_service import apply_interval_schedule, validate_batch_payload
from app_core.douyin_location_service import normalize_location_candidate
from app_core.douyin_sms_cooldown import DouyinSmsCooldownGate
from app_core.douyin_verification import (
    DouyinVerificationBroker,
    DouyinVerificationError,
    VerificationChallenge,
)


class FakeCommerceSessionManager:
    """只记录批量执行器调用顺序的内存会话替身。"""

    def __init__(
        self,
        *,
        fail_item_indexes: set[int] | None = None,
        challenge_on_index: int | None = None,
        login_on_index: int | None = None,
        verification_mode: str = "",
        verification_broker: DouyinVerificationBroker | None = None,
        location_candidates: list[dict] | None = None,
        scheduled_readback_time: str = "",
        baseline_fail_indexes: set[int] | None = None,
    ) -> None:
        self.fail_item_indexes = fail_item_indexes or set()
        self.challenge_on_index = challenge_on_index
        self.login_on_index = login_on_index
        self.verification_mode = verification_mode
        self.verification_broker = verification_broker
        self.location_candidates = location_candidates
        self.location_searches: list[tuple[str, str]] = []
        self.atomic_location_requests: list[
            tuple[str, dict, str, list[str]]
        ] = []
        self.scheduled_readback_time = scheduled_readback_time
        self.baseline_fail_indexes = baseline_fail_indexes or set()
        self.calls: list[str] = []
        self.ordered_calls: list[tuple[str, str]] = []
        self.started_session_ids: list[str] = []
        self.closed_session_ids: list[str] = []
        self.open_sessions = 0
        self.max_open_sessions = 0
        self._index_by_session: dict[str, int] = {}
        self._challenge_seen = False

    def start_upload(self, payload: dict, **_kwargs) -> dict:
        if payload.get("runtimeMode") != "preflight" or payload.get("debugDryRun") is not True:
            raise AssertionError("上传必须保持预检模式")
        index = len([call for call in self.calls if call.startswith("start_upload:")])
        session_id = f"session-{index + 1}"
        self.calls.append(f"start_upload:{index}")
        self.ordered_calls.append(
            ("start_upload", Path(str(payload["fileList"][0])).name)
        )
        if self.login_on_index == index:
            return {
                "status": "needs_login",
                "message": "登录已失效，请到账号管理重新登录",
            }
        self._index_by_session[session_id] = index
        self.started_session_ids.append(session_id)
        self.open_sessions += 1
        self.max_open_sessions = max(self.max_open_sessions, self.open_sessions)
        return {"sessionId": session_id}

    def prepare_publish_settings(self, session_id: str) -> dict:
        index = self._index_by_session[session_id]
        self.ordered_calls.append(("prepare_publish_settings", session_id))
        if index in self.baseline_fail_indexes:
            raise RuntimeError("正式发布页旧浮层未能清理")
        return {
            "status": "clean",
            "sessionId": session_id,
            "openLayerCount": 0,
        }

    def select_cached_favorite_music(self, session_id: str, _music_id: str) -> dict:
        self.calls.append(f"select_music:{self._index_by_session[session_id]}")
        self.ordered_calls.append(("select_cached_favorite_music", session_id))
        return {"musicId": "music-001", "title": "测试音乐", "creator": "测试", "duration": "01:08"}

    def select_content_declaration(self, session_id: str, declaration: str) -> str:
        self.calls.append(f"select_declaration:{self._index_by_session[session_id]}")
        self.ordered_calls.append(("select_content_declaration", session_id))
        return declaration

    def search_locations(self, session_id: str, _keyword: str, _scope: str) -> list[dict]:
        self.calls.append(f"search_locations:{self._index_by_session[session_id]}")
        self.ordered_calls.append(("search_locations", session_id))
        self.location_searches.append((_keyword, _scope))
        if self.location_candidates is not None:
            return self.location_candidates
        return [
            {
                "poiId": "poi-001",
                "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区银滩大道中段",
            }
        ]

    def apply_location(self, session_id: str, location: dict) -> dict:
        self.calls.append(f"apply_location:{self._index_by_session[session_id]}")
        self.ordered_calls.append(("apply_location", session_id))
        # 与真实会话管理器保持一致：地点回读嵌套在 location 字段中。
        return {"location": dict(location)}

    def apply_saved_location(
        self,
        session_id: str,
        preset: dict,
        scope: str,
        keywords: list[str],
    ) -> dict:
        self.calls.append(
            f"apply_saved_location:{self._index_by_session[session_id]}"
        )
        self.ordered_calls.append(("apply_saved_location", session_id))
        self.atomic_location_requests.append(
            (session_id, dict(preset), scope, list(keywords))
        )
        location = (
            dict(self.location_candidates[0])
            if self.location_candidates
            else dict(preset)
        )
        return {"location": location, "matchedKeyword": keywords[0]}

    def sync_schedule(self, session_id: str, _payload: dict) -> dict:
        self.calls.append(f"sync_schedule:{self._index_by_session[session_id]}")
        self.ordered_calls.append(("sync_schedule", session_id))
        return {"scheduled": False}

    def preflight(self, session_id: str, _payload: dict) -> dict:
        if _payload.get("runtimeMode") != "preflight" or _payload.get("debugDryRun") is not True:
            raise AssertionError("预检必须保持 dry-run")
        self.calls.append(f"preflight:{self._index_by_session[session_id]}")
        self.ordered_calls.append(("preflight", session_id))
        return {"ok": True}

    def submit(
        self,
        session_id: str,
        _payload: dict,
        task_id: int | None = None,
        **kwargs,
    ) -> dict:
        if _payload.get("runtimeMode") != "publish" or _payload.get("debugDryRun") is not False:
            raise AssertionError("最终提交必须明确发布模式")
        index = self._index_by_session[session_id]
        self.calls.append(f"submit:{index}")
        self.ordered_calls.append(("submit", session_id))
        if index in self.fail_item_indexes:
            raise RuntimeError(f"第 {index} 条平台回读失败")
        if self.challenge_on_index == index and not self._challenge_seen:
            self._challenge_seen = True
            callback = kwargs.get("on_verification")
            if self.verification_broker is not None and callback is not None:
                request_id = self.verification_broker.create_sms(
                    task_id=int(task_id or 0),
                    message="请在一键发客户端输入短信验证码",
                )
                callback(VerificationChallenge(kind="sms", message="需要短信验证"))
                if self.verification_mode == "active_success":
                    self.assert_active_request(request_id)
                    self.verification_broker.submit_code(request_id, "123456")
                    if self.verification_broker.claim_code(request_id) != "123456":
                        raise AssertionError("验证码必须在同一 active 请求中被领取")
                    self.verification_broker.succeed(request_id)
                    return {
                        "ok": True,
                        "scheduled": False,
                        "message": f"第 {index} 条作品已由平台管理页回读",
                        "platformReceipt": {
                            "platformPostId": f"post-{index}",
                            "publishedAt": "2026-08-06 10:00",
                            "timezone": "Asia/Shanghai",
                        },
                    }
                elif self.verification_mode == "cancelled":
                    if not self.verification_broker.cancel(request_id):
                        raise AssertionError("active 验证必须允许被取消")
                    raise DouyinVerificationError("用户已取消抖音验证，发布已安全停止")
                elif self.verification_mode == "failed":
                    self.verification_broker.fail(request_id)
                    raise DouyinVerificationError("抖音短信验证未通过，发布已安全停止")
                elif self.verification_mode == "timed_out":
                    self.verification_broker.wait(request_id, timeout_seconds=0.000001)
                    raise DouyinVerificationError("等待抖音验证超时，发布已安全停止")
                elif self.verification_mode == "active_pending":
                    raise DouyinVerificationError("抖音需要短信验证")
                else:
                    raise AssertionError("测试替身缺少验证状态模式")
            raise DouyinVerificationError("抖音需要短信验证")
        if self.scheduled_readback_time:
            return {
                "ok": True,
                "scheduled": True,
                "message": f"第 {index} 条定时作品已由平台管理页回读",
                "scheduledReadback": {
                    "scheduledAt": self.scheduled_readback_time,
                    "timezone": "Asia/Shanghai",
                },
            }
        return {
            "ok": True,
            "scheduled": False,
            "message": f"第 {index} 条作品已由平台管理页回读",
            "platformReceipt": {
                "platformPostId": f"post-{index}",
                "publishedAt": "2026-08-06 10:00",
                "timezone": "Asia/Shanghai",
            },
        }

    def close(self, session_id: str | None = None) -> None:
        if session_id is not None and session_id in self._index_by_session:
            self.calls.append(f"close:{self._index_by_session[session_id]}")
            self.ordered_calls.append(("close", session_id))
            self.closed_session_ids.append(session_id)
            self.open_sessions -= 1

    def status(self) -> dict[str, str]:
        return {"active": "true" if self.open_sessions else "false", "stage": ""}

    def assert_active_request(self, request_id: str) -> None:
        if self.verification_broker is None:
            raise AssertionError("测试替身缺少验证代理")
        snapshot = self.verification_broker.snapshot(request_id)
        if snapshot.get("state") != "waiting":
            raise AssertionError("验证请求必须在用户输入前保持 active")


class DouyinCommerceBatchExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "database.db"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        database.ensure_schema()
        self.media_paths = []
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            path = Path(self.directory.name) / name
            path.write_bytes(b"offline-video")
            self.media_paths.append(str(path))
        self.batch = apply_interval_schedule(
            validate_batch_payload(
                {
                    "type": 3,
                    "workflow": "douyin-commerce-batch",
                    "commerceMode": "local-group-buy",
                    "contentType": "video",
                    "accountList": ["oneclick_3_offline.json"],
                    "shared": {
                        "title": "批量测试",
                        "description": "每条视频使用同一份作品内容。",
                        "tags": ["测试"],
                        "selectedMusic": {
                            "musicId": "music-001",
                            "title": "测试音乐",
                            "creator": "测试",
                            "duration": "01:08",
                        },
                        "contentDeclaration": "无需添加自主声明",
                    },
                    "publishMode": "immediate",
                    "schedule": {},
                    "items": [
                        {
                            "mediaPath": path,
                            "locationPreset": {
                                "poiId": "poi-001",
                                "name": "北海银滩景区",
                                "address": "广西壮族自治区北海市银海区银滩大道中段",
                                "scope": "domestic",
                            },
                        }
                        for path in self.media_paths
                    ],
                }
            ),
            now=__import__("datetime").datetime(2026, 8, 6, 10, 0),
        )
        self.task = task_service.create_douyin_batch_task(self.batch)

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.directory.cleanup()

    def test_executor_processes_items_serially_and_continues_after_item_failure(self) -> None:
        manager = FakeCommerceSessionManager(fail_item_indexes={1})
        events: list[BatchProgressEvent] = []

        result = DouyinCommerceBatchExecutor(manager).run_publish(
            self.batch, task_id=self.task["id"], confirmed=True, progress=events.append
        )

        self.assertEqual([row["status"] for row in result], ["published", "failed", "published"])
        self.assertEqual(result[1]["diagnostic"], "第 1 条平台回读失败")
        self.assertEqual(manager.max_open_sessions, 1)
        self.assertIn("submit:0", manager.calls)
        self.assertIn("submit:1", manager.calls)
        self.assertIn("submit:2", manager.calls)
        self.assertEqual(
            [call for call in manager.calls if call.startswith("start_upload:")],
            ["start_upload:0", "start_upload:1", "start_upload:2"],
        )
        self.assertEqual(
            [
                (keywords[0], scope)
                for _session_id, _preset, scope, keywords
                in manager.atomic_location_requests
            ],
            [
                (item["locationPreset"]["address"], item["locationPreset"]["scope"])
                for item in self.batch["items"]
            ],
        )
        self.assertEqual([item["status"] for item in task_service.get_task(self.task["id"])["items"]], ["success", "failed", "success"])
        self.assertTrue(
            any("第 1 条平台回读失败" in event.message for event in events)
        )
        self.assertTrue(any(event.phase == "uploading" for event in events))

    def test_each_video_uses_fresh_session_and_clean_baseline_before_settings(self) -> None:
        """每条视频上传后必须先清理正式页，再按计划顺序写入设置。"""

        manager = FakeCommerceSessionManager()

        result = DouyinCommerceBatchExecutor(manager).run_publish(
            self.batch, task_id=self.task["id"], confirmed=True
        )

        self.assertEqual([row["status"] for row in result], ["published"] * 3)
        self.assertEqual(
            manager.ordered_calls[:5],
            [
                ("start_upload", "a.mp4"),
                ("prepare_publish_settings", "session-1"),
                ("select_cached_favorite_music", "session-1"),
                ("select_content_declaration", "session-1"),
                ("apply_saved_location", "session-1"),
            ],
        )
        self.assertEqual(
            manager.started_session_ids,
            ["session-1", "session-2", "session-3"],
        )
        self.assertEqual(
            manager.closed_session_ids,
            ["session-1", "session-2", "session-3"],
        )

    def test_executor_uses_one_atomic_location_action_before_schedule(self) -> None:
        """正式执行器不得再把搜索与点击拆成两次面板动作。"""

        manager = FakeCommerceSessionManager()

        result = DouyinCommerceBatchExecutor(manager).run_preflight(
            self.batch,
            task_id=self.task["id"],
        )

        self.assertEqual([row["status"] for row in result], ["preflighted"] * 3)
        first_session_calls = [
            name
            for name, session_id in manager.ordered_calls
            if session_id == "session-1"
        ]
        self.assertEqual(
            first_session_calls,
            [
                "prepare_publish_settings",
                "select_cached_favorite_music",
                "select_content_declaration",
                "apply_saved_location",
                "sync_schedule",
                "preflight",
                "close",
            ],
        )
        self.assertNotIn("search_locations:0", manager.calls)
        self.assertNotIn("apply_location:0", manager.calls)
        self.assertEqual(
            manager.atomic_location_requests[0][3],
            _location_search_keywords(self.batch["items"][0]["locationPreset"]),
        )

    def test_atomic_location_failure_blocks_schedule_preflight_and_submit(self) -> None:
        """地点原子应用失败后本条只能关闭会话，不得继续排期或提交。"""

        class FailingAtomicLocationManager(FakeCommerceSessionManager):
            def apply_saved_location(
                self,
                session_id: str,
                preset: dict,
                scope: str,
                keywords: list[str],
            ) -> dict:
                del preset, scope, keywords
                self.ordered_calls.append(("apply_saved_location", session_id))
                raise RuntimeError("publish_location_click_failed")

        batch = {**self.batch, "items": [dict(self.batch["items"][0])]}
        task = task_service.create_douyin_batch_task(batch)
        manager = FailingAtomicLocationManager()

        result = DouyinCommerceBatchExecutor(manager).run_publish(
            batch,
            task_id=task["id"],
            confirmed=True,
        )

        self.assertEqual(result[0]["status"], "failed")
        self.assertEqual(result[0]["diagnostic"], "publish_location_click_failed")
        self.assertEqual(
            [
                name
                for name, session_id in manager.ordered_calls
                if session_id == "session-1"
            ],
            [
                "prepare_publish_settings",
                "select_cached_favorite_music",
                "select_content_declaration",
                "apply_saved_location",
                "close",
            ],
        )

    def test_baseline_cleanup_failure_closes_item_without_applying_settings(self) -> None:
        """基线清理异常只能将本条记为失败，关闭会话后按既有策略继续。"""

        manager = FakeCommerceSessionManager(baseline_fail_indexes={0})

        result = DouyinCommerceBatchExecutor(manager).run_publish(
            self.batch, task_id=self.task["id"], confirmed=True
        )

        self.assertEqual(
            [row["status"] for row in result],
            ["failed", "published", "published"],
        )
        self.assertEqual(result[0]["diagnostic"], "正式发布页旧浮层未能清理")
        self.assertEqual(
            [
                call
                for call in manager.ordered_calls
                if call[1] == "session-1"
            ],
            [
                ("prepare_publish_settings", "session-1"),
                ("close", "session-1"),
            ],
        )
        self.assertEqual(manager.started_session_ids[:2], ["session-1", "session-2"])
        self.assertEqual(manager.closed_session_ids[:2], ["session-1", "session-2"])

    def test_clean_status_with_open_layer_is_rejected_as_dirty_baseline(self) -> None:
        """status=clean 不足以放行，仍有浮层时不得写入或记 published。"""

        class OpenLayerBaselineManager(FakeCommerceSessionManager):
            def prepare_publish_settings(self, session_id: str) -> dict:
                result = super().prepare_publish_settings(session_id)
                if session_id == "session-1":
                    result["openLayerCount"] = 1
                return result

        manager = OpenLayerBaselineManager()

        result = DouyinCommerceBatchExecutor(manager).run_publish(
            self.batch, task_id=self.task["id"], confirmed=True
        )

        self.assertEqual(result[0]["status"], "failed")
        self.assertEqual(
            result[0]["diagnostic"], "抖音正式发布页未取得干净设置基线"
        )
        self.assertNotIn(
            ("select_cached_favorite_music", "session-1"), manager.ordered_calls
        )
        self.assertIn(("close", "session-1"), manager.ordered_calls)
        self.assertNotIn(("submit", "session-1"), manager.ordered_calls)

    def test_ambiguous_post_submit_readback_pauses_before_next_video(self) -> None:
        """点击最终提交后无法回读时，不能当成普通失败继续提交后续视频。"""

        class AmbiguousReceiptManager(FakeCommerceSessionManager):
            def submit(
                self,
                session_id: str,
                _payload: dict,
                task_id: int | None = None,
                **kwargs,
            ) -> dict:
                del task_id, kwargs
                index = self._index_by_session[session_id]
                self.calls.append(f"submit:{index}")
                if index == 1:
                    raise RuntimeError(
                        "抖音带货最终提交未能获得平台回执："
                        "抖音定时提交后未能从作品管理页回读标题和指定时间"
                    )
                return super().submit(session_id, _payload)

        manager = AmbiguousReceiptManager()
        events: list[BatchProgressEvent] = []

        result = DouyinCommerceBatchExecutor(manager).run_publish(
            self.batch,
            task_id=self.task["id"],
            confirmed=True,
            progress=events.append,
        )

        self.assertEqual(
            [row["status"] for row in result],
            ["published", "receipt_ambiguous", "pending"],
        )
        self.assertNotIn("start_upload:2", manager.calls)
        self.assertTrue(any(event.phase == "receipt_ambiguous" for event in events))
        saved = task_service.get_task(self.task["id"])
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(
            saved["pauseReasonCode"],
            task_service.PAUSE_REASON_RECEIPT_AMBIGUOUS,
        )
        self.assertEqual(
            [item["status"] for item in saved["items"]],
            ["success", "failed", "pending"],
        )
        self.assertEqual(saved["events"][-1]["eventType"], "batch_paused")

    def test_five_consecutive_failures_pause_batch_and_leave_later_items_unstarted(self) -> None:
        """同一根因连续失败五条后，绝不能继续上传第六条。"""

        extra_paths: list[str] = []
        for name in ("d.mp4", "e.mp4", "f.mp4"):
            path = Path(self.directory.name) / name
            path.write_bytes(b"offline-video")
            extra_paths.append(str(path))
        batch = dict(self.batch)
        batch["items"] = [
            *[dict(item) for item in self.batch["items"]],
            *[
                {
                    "mediaPath": path,
                    "locationPreset": {
                        "poiId": "poi-001",
                        "name": "北海银滩景区",
                        "address": "广西壮族自治区北海市银海区银滩大道中段",
                        "scope": "domestic",
                    },
                }
                for path in extra_paths
            ],
        ]
        task = task_service.create_douyin_batch_task(batch)
        events: list[BatchProgressEvent] = []

        result = DouyinCommerceBatchExecutor(
            FakeCommerceSessionManager(fail_item_indexes={0, 1, 2, 3, 4})
        ).run_publish(batch, task_id=task["id"], confirmed=True, progress=events.append)

        self.assertEqual(
            [row["status"] for row in result],
            ["failed", "failed", "failed", "failed", "failed", "paused"],
        )
        self.assertEqual(
            [event.phase for event in events if event.phase == "uploading"],
            ["uploading"] * 5,
        )
        self.assertTrue(any(event.phase == "auto_paused" for event in events))
        saved = task_service.get_task(task["id"])
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(
            saved["pauseReasonCode"],
            task_service.PAUSE_REASON_AUTO_FAILURE,
        )
        self.assertEqual(
            [item["status"] for item in saved["items"]],
            ["failed", "failed", "failed", "failed", "failed", "pending"],
        )

    def test_manage_page_navigation_receipt_is_a_valid_immediate_publish_evidence(self) -> None:
        """即时发表不暴露作品时间时，管理页最终跳转可作为可审计回执。"""

        class NavigationReceiptManager(FakeCommerceSessionManager):
            def submit(self, session_id: str, _payload: dict, task_id: int | None = None, **kwargs) -> dict:
                del task_id, kwargs
                index = self._index_by_session[session_id]
                self.calls.append(f"submit:{index}")
                return {
                    "ok": True,
                    "scheduled": False,
                    "message": "抖音已进入作品管理页",
                    "platformReceipt": {
                        "status": "published",
                        "url": "https://creator.douyin.com/creator-micro/content/manage",
                    },
                }

        manager = NavigationReceiptManager()
        result = DouyinCommerceBatchExecutor(manager).run_publish(
            self.batch, task_id=self.task["id"], confirmed=True
        )

        self.assertEqual([row["status"] for row in result], ["published"] * 3)
        events = task_service.get_task(self.task["id"])["events"]
        receipts = [event for event in events if event["eventType"] == "platform_publish_receipt"]
        self.assertEqual(len(receipts), 3)
        self.assertIn("content/manage", receipts[0]["detailJson"])
        self.assertIn("Asia/Shanghai", receipts[0]["detailJson"])

    def test_scheduled_item_only_succeeds_when_final_readback_exactly_matches_its_schedule(self) -> None:
        from datetime import datetime

        target = "2026-08-07 10:00"
        scheduled_batch = apply_interval_schedule(
            validate_batch_payload(
                {
                    **self.batch,
                    "publishMode": "interval-schedule",
                    "schedule": {
                        "timezone": "Asia/Shanghai",
                        "startTime": target,
                        "intervalMinutes": 30,
                    },
                },
                now=datetime(2026, 8, 6, 10, 0),
            ),
            now=datetime(2026, 8, 6, 10, 0),
        )
        mismatch_task = task_service.create_douyin_batch_task(
            scheduled_batch,
            schedule_now=datetime(2026, 8, 6, 10, 0),
        )
        mismatch = DouyinCommerceBatchExecutor(
            FakeCommerceSessionManager(scheduled_readback_time="2026-08-07 10:01"),
            now=lambda: datetime(2026, 8, 6, 10, 0),
        ).run_publish(scheduled_batch, task_id=mismatch_task["id"], confirmed=True)

        self.assertEqual(mismatch[0]["status"], "failed")
        self.assertEqual(task_service.get_task(mismatch_task["id"])["items"][0]["status"], "failed")

        matching_task = task_service.create_douyin_batch_task(
            scheduled_batch,
            schedule_now=datetime(2026, 8, 6, 10, 0),
        )
        matching = DouyinCommerceBatchExecutor(
            FakeCommerceSessionManager(scheduled_readback_time=target),
            now=lambda: datetime(2026, 8, 6, 10, 0),
        ).run_publish(scheduled_batch, task_id=matching_task["id"], confirmed=True)

        self.assertEqual(matching[0]["status"], "published")
        self.assertEqual(task_service.get_task(matching_task["id"])["items"][0]["status"], "success")

    def test_login_or_verification_pauses_batch_without_submitting_later_items(self) -> None:
        broker = DouyinVerificationBroker()
        manager = FakeCommerceSessionManager(
            challenge_on_index=1,
            verification_mode="active_pending",
            verification_broker=broker,
        )

        result = DouyinCommerceBatchExecutor(manager, verification_broker=broker).run_publish(
            self.batch, task_id=self.task["id"], confirmed=True
        )

        self.assertEqual([row["status"] for row in result], ["published", "waiting_verification", "pending"])
        self.assertNotIn("submit:2", manager.calls)
        self.assertNotIn("close:1", manager.calls)
        self.assertEqual(manager.open_sessions, 1)
        saved = task_service.get_task(self.task["id"])
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(
            saved["pauseReasonCode"],
            task_service.PAUSE_REASON_WAITING_VERIFICATION,
        )
        self.assertEqual(saved["items"][1]["status"], "running")

    def test_needs_login_controlled_status_pauses_whole_batch_without_marking_item_failed(self) -> None:
        manager = FakeCommerceSessionManager(login_on_index=1)

        result = DouyinCommerceBatchExecutor(manager).run_publish(
            self.batch, task_id=self.task["id"], confirmed=True
        )

        self.assertEqual(
            [row["status"] for row in result],
            ["published", "waiting_login", "pending"],
        )
        self.assertNotIn("submit:1", manager.calls)
        self.assertNotIn("start_upload:2", manager.calls)
        saved = task_service.get_task(self.task["id"])
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(
            saved["pauseReasonCode"],
            task_service.PAUSE_REASON_WAITING_LOGIN,
        )
        self.assertEqual(
            [item["status"] for item in saved["items"]],
            ["success", "running", "pending"],
        )

    def test_active_sms_verification_keeps_current_session_and_continues_same_item_once(self) -> None:
        now = [100.0]
        broker = DouyinVerificationBroker()
        manager = FakeCommerceSessionManager(
            challenge_on_index=1,
            verification_mode="active_success",
            verification_broker=broker,
        )
        events: list[BatchProgressEvent] = []

        result = DouyinCommerceBatchExecutor(
            manager,
            verification_broker=broker,
            cooldown_gate=DouyinSmsCooldownGate(
                clock=lambda: now[0],
                waiter=lambda seconds: now.__setitem__(0, now[0] + seconds),
            ),
            utc_now=lambda: datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
        ).run_publish(
            self.batch,
            task_id=self.task["id"],
            confirmed=True,
            progress=events.append,
        )

        self.assertEqual([row["status"] for row in result], ["published"] * 3)
        self.assertEqual(manager.calls.count("submit:1"), 1)
        self.assertEqual(manager.calls.count("start_upload:1"), 1)
        self.assertEqual(manager.max_open_sessions, 1)
        self.assertIn("close:1", manager.calls)
        self.assertTrue(any(event.phase == "waiting_verification" for event in events))
        self.assertTrue(any(event.phase == "published" and event.index == 1 for event in events))

    def test_sms_cooldown_waits_after_next_preflight_then_auto_submits_once(self) -> None:
        now = [100.0]
        broker = DouyinVerificationBroker()
        manager = FakeCommerceSessionManager(
            challenge_on_index=0,
            verification_mode="active_success",
            verification_broker=broker,
        )
        gate = DouyinSmsCooldownGate(
            clock=lambda: now[0],
            waiter=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        events: list[BatchProgressEvent] = []
        executor = DouyinCommerceBatchExecutor(
            manager,
            verification_broker=broker,
            cooldown_gate=gate,
            utc_now=lambda: datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
        )

        result = executor.run_publish(
            self.batch,
            task_id=self.task["id"],
            confirmed=True,
            progress=events.append,
        )

        self.assertEqual([row["status"] for row in result], ["published"] * 3)
        self.assertEqual(manager.calls.count("submit:0"), 1)
        self.assertEqual(manager.calls.count("submit:1"), 1)
        self.assertLess(
            manager.ordered_calls.index(("preflight", "session-2")),
            manager.ordered_calls.index(("submit", "session-2")),
        )
        cooldown = [row for row in events if row.phase == "verification_cooldown"]
        self.assertEqual(
            (cooldown[0].remaining_seconds, cooldown[-1].remaining_seconds),
            (60, 1),
        )
        self.assertEqual(cooldown[0].to_public_dict()["remainingSeconds"], 60)
        self.assertNotIn(
            "remainingSeconds",
            BatchProgressEvent(0, 3, "uploading", "正在上传").to_public_dict(),
        )
        stored_event_types = [
            event["eventType"]
            for event in task_service.get_task(self.task["id"])["events"]
        ]
        self.assertEqual(stored_event_types.count("douyin_sms_cooldown_started"), 1)
        self.assertEqual(
            stored_event_types.count("verification_cooldown_wait_started"),
            1,
        )
        self.assertEqual(
            stored_event_types.count("verification_cooldown_wait_finished"),
            1,
        )

    def test_shutdown_interface_accepts_source_positionally_and_generations_increase(self) -> None:
        executor = DouyinCommerceBatchExecutor(FakeCommerceSessionManager())

        first_generation = executor.reset_shutdown()
        second_generation = executor.reset_shutdown()

        self.assertIs(type(first_generation), int)
        self.assertEqual(second_generation, first_generation + 1)
        self.assertFalse(executor.request_shutdown("unexpected"))
        self.assertTrue(executor.request_shutdown("client_shutdown"))
        self.assertFalse(executor.request_shutdown("client_shutdown"))

    def test_qr_or_inactive_sms_callback_never_records_cooldown(self) -> None:
        now = [100.0]
        gate = DouyinSmsCooldownGate(clock=lambda: now[0], waiter=lambda _seconds: None)

        class ActiveBroker:
            @staticmethod
            def request_for_task(_task_id: int) -> str:
                return "active-request"

            @staticmethod
            def snapshot(_request_id: str) -> dict[str, str]:
                return {"state": "waiting"}

        executor = DouyinCommerceBatchExecutor(
            FakeCommerceSessionManager(),
            verification_broker=ActiveBroker(),
            cooldown_gate=gate,
            utc_now=lambda: datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
        )
        item_id = task_service.get_task(self.task["id"])["items"][0]["id"]
        callback = executor._verification_progress_callback(
            task_id=self.task["id"],
            item_id=item_id,
            index=0,
            total=3,
            label="a.mp4",
            account_key=self.batch["accountFile"],
            progress=None,
        )

        callback(VerificationChallenge(kind="qr", message="需要扫码"))

        self.assertEqual(gate.remaining_seconds(self.batch["accountFile"]), 0)
        event_types = [
            event["eventType"]
            for event in task_service.get_task(self.task["id"])["events"]
        ]
        self.assertNotIn("douyin_sms_cooldown_started", event_types)

        inactive_executor = DouyinCommerceBatchExecutor(
            FakeCommerceSessionManager(),
            verification_broker=DouyinVerificationBroker(),
            cooldown_gate=gate,
            utc_now=lambda: datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
        )
        inactive_callback = inactive_executor._verification_progress_callback(
            task_id=self.task["id"],
            item_id=item_id,
            index=0,
            total=3,
            label="a.mp4",
            account_key=self.batch["accountFile"],
            progress=None,
        )
        inactive_callback(VerificationChallenge(kind="sms", message="需要短信验证"))
        self.assertEqual(gate.remaining_seconds(self.batch["accountFile"]), 0)

    def test_persisted_sms_cooldown_is_restored_before_first_submit(self) -> None:
        monotonic_now = [100.0]
        triggered_at = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
        task_service.record_douyin_sms_cooldown(
            self.task["id"],
            task_service.get_task(self.task["id"])["items"][0]["id"],
            triggered_at_utc=triggered_at,
        )
        events: list[BatchProgressEvent] = []
        manager = FakeCommerceSessionManager()
        executor = DouyinCommerceBatchExecutor(
            manager,
            cooldown_gate=DouyinSmsCooldownGate(
                clock=lambda: monotonic_now[0],
                waiter=lambda seconds: monotonic_now.__setitem__(
                    0, monotonic_now[0] + seconds
                ),
            ),
            utc_now=lambda: datetime(
                2026, 8, 10, 1, 0, 30, tzinfo=timezone.utc
            ),
        )

        result = executor.run_publish(
            self.batch,
            task_id=self.task["id"],
            confirmed=True,
            progress=events.append,
        )

        self.assertEqual([row["status"] for row in result], ["published"] * 3)
        cooldown = [event for event in events if event.phase == "verification_cooldown"]
        self.assertEqual(
            (cooldown[0].remaining_seconds, cooldown[-1].remaining_seconds),
            (30, 1),
        )
        self.assertLess(
            manager.ordered_calls.index(("preflight", "session-1")),
            manager.ordered_calls.index(("submit", "session-1")),
        )

    def test_invalid_persisted_cooldown_uses_fixed_error_without_cause(self) -> None:
        with patch.object(
            task_service,
            "load_douyin_sms_cooldown_remaining",
            side_effect=ValueError("不应泄露的底层细节"),
        ):
            with self.assertRaises(DouyinCommerceBatchExecutorError) as caught:
                DouyinCommerceBatchExecutor(
                    FakeCommerceSessionManager()
                ).run_publish(
                    self.batch,
                    task_id=self.task["id"],
                    confirmed=True,
                )

        self.assertEqual(str(caught.exception), "verification_cooldown_state_invalid")
        self.assertIsNone(caught.exception.__cause__)

    def test_gate_failure_uses_fixed_error_without_leaking_waiter_exception(self) -> None:
        now = [100.0]

        def broken_waiter(_seconds: float) -> None:
            raise RuntimeError("不应泄露的等待器细节")

        gate = DouyinSmsCooldownGate(clock=lambda: now[0], waiter=broken_waiter)
        gate.record_trigger(self.batch["accountFile"])
        executor = DouyinCommerceBatchExecutor(
            FakeCommerceSessionManager(),
            cooldown_gate=gate,
        )
        item_id = task_service.get_task(self.task["id"])["items"][0]["id"]

        with self.assertRaises(DouyinCommerceBatchExecutorError) as caught:
            executor._wait_for_sms_cooldown(
                self.batch["accountFile"],
                task_id=self.task["id"],
                item_id=item_id,
                index=0,
                total=3,
                progress=None,
                run_generation=0,
            )

        self.assertEqual(str(caught.exception), "verification_cooldown_failed")
        self.assertIsNone(caught.exception.__cause__)

    def test_user_pause_during_cooldown_finishes_current_item_then_stops_next(self) -> None:
        now = [100.0]
        broker = DouyinVerificationBroker()
        manager = FakeCommerceSessionManager(
            challenge_on_index=0,
            verification_mode="active_success",
            verification_broker=broker,
        )
        executor: DouyinCommerceBatchExecutor
        first_wait = [True]

        def waiter(seconds: float) -> None:
            if first_wait[0]:
                first_wait[0] = False
                executor.request_pause(source="user_confirmed")
            now[0] += seconds

        executor = DouyinCommerceBatchExecutor(
            manager,
            verification_broker=broker,
            cooldown_gate=DouyinSmsCooldownGate(
                clock=lambda: now[0],
                waiter=waiter,
            ),
            utc_now=lambda: datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
        )

        result = executor.run_publish(
            self.batch,
            task_id=self.task["id"],
            confirmed=True,
        )

        self.assertEqual(
            [row["status"] for row in result],
            ["published", "published", "paused"],
        )
        self.assertEqual(manager.calls.count("submit:1"), 1)
        self.assertNotIn("start_upload:2", manager.calls)

    def test_client_shutdown_during_cooldown_never_submits_current_item(self) -> None:
        now = [100.0]
        broker = DouyinVerificationBroker()
        manager = FakeCommerceSessionManager(
            challenge_on_index=0,
            verification_mode="active_success",
            verification_broker=broker,
        )
        executor: DouyinCommerceBatchExecutor
        first_wait = [True]

        def waiter(seconds: float) -> None:
            if first_wait[0]:
                first_wait[0] = False
                executor.request_shutdown(source="client_shutdown")
            now[0] += seconds

        executor = DouyinCommerceBatchExecutor(
            manager,
            verification_broker=broker,
            cooldown_gate=DouyinSmsCooldownGate(
                clock=lambda: now[0],
                waiter=waiter,
            ),
            utc_now=lambda: datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
        )

        result = executor.run_publish(
            self.batch,
            task_id=self.task["id"],
            confirmed=True,
        )

        self.assertEqual(
            [row["status"] for row in result],
            ["published", "client_shutdown", "pending"],
        )
        self.assertNotIn("submit:1", manager.calls)
        self.assertIn("close:1", manager.calls)
        saved = task_service.get_task(self.task["id"])
        self.assertEqual(
            (saved["status"], saved["pauseReasonCode"]),
            ("paused", "client_shutdown"),
        )
        self.assertEqual(saved["items"][1]["status"], "pending")

    def test_replaced_run_generation_drops_old_cooldown_callback(self) -> None:
        now = [100.0]
        broker = DouyinVerificationBroker()
        manager = FakeCommerceSessionManager(
            challenge_on_index=0,
            verification_mode="active_success",
            verification_broker=broker,
        )
        executor: DouyinCommerceBatchExecutor
        events: list[BatchProgressEvent] = []
        first_wait = [True]
        ticks_at_reset = [0]

        def waiter(seconds: float) -> None:
            if first_wait[0]:
                first_wait[0] = False
                executor.reset_shutdown()
                ticks_at_reset[0] = len(
                    [event for event in events if event.phase == "verification_cooldown"]
                )
            now[0] += seconds

        executor = DouyinCommerceBatchExecutor(
            manager,
            verification_broker=broker,
            cooldown_gate=DouyinSmsCooldownGate(
                clock=lambda: now[0],
                waiter=waiter,
            ),
            utc_now=lambda: datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
        )

        result = executor.run_publish(
            self.batch,
            task_id=self.task["id"],
            confirmed=True,
            progress=events.append,
        )

        self.assertEqual(
            [row["status"] for row in result],
            ["published", "client_shutdown", "pending"],
        )
        self.assertNotIn("submit:1", manager.calls)
        self.assertIn("close:1", manager.calls)
        self.assertEqual(
            len([event for event in events if event.phase == "verification_cooldown"]),
            ticks_at_reset[0],
        )

    def test_cancelled_sms_verification_stops_batch_and_never_claims_waiting_resume(self) -> None:
        broker = DouyinVerificationBroker()
        manager = FakeCommerceSessionManager(
            challenge_on_index=1,
            verification_mode="cancelled",
            verification_broker=broker,
        )

        result = DouyinCommerceBatchExecutor(
            manager,
            verification_broker=broker,
        ).run_publish(self.batch, task_id=self.task["id"], confirmed=True)

        self.assertEqual(
            [row["status"] for row in result],
            ["published", "verification_failed", "pending"],
        )
        self.assertNotIn("submit:2", manager.calls)
        self.assertIn("close:1", manager.calls)
        self.assertIsNone(broker.request_for_task(self.task["id"]))
        self.assertEqual(
            [item["status"] for item in task_service.get_task(self.task["id"])["items"]],
            ["success", "failed", "pending"],
        )

    def test_failed_or_timed_out_sms_verification_stops_batch_without_waiting_resume(self) -> None:
        for mode in ("failed", "timed_out"):
            with self.subTest(mode=mode):
                broker = DouyinVerificationBroker()
                task = task_service.create_douyin_batch_task(self.batch)
                manager = FakeCommerceSessionManager(
                    challenge_on_index=1,
                    verification_mode=mode,
                    verification_broker=broker,
                )

                result = DouyinCommerceBatchExecutor(
                    manager,
                    verification_broker=broker,
                ).run_publish(self.batch, task_id=task["id"], confirmed=True)

                self.assertEqual(
                    [row["status"] for row in result],
                    ["published", "verification_failed", "pending"],
                )
                self.assertIsNone(broker.request_for_task(task["id"]))
                saved = task_service.get_task(task["id"])
                self.assertEqual(saved["status"], "paused")
                self.assertEqual(
                    saved["pauseReasonCode"],
                    task_service.PAUSE_REASON_WAITING_VERIFICATION,
                )
                events = saved["events"]
                event_types = [event["eventType"] for event in events]
                self.assertEqual(event_types[-2:], ["verification_failed", "batch_paused"])
                self.assertIn(result[1]["diagnostic"], events[-2]["message"])

    def test_user_requested_pause_records_manual_reason_after_current_video(self) -> None:
        """若用户暂停被误分类或后续视频已启动，该测试必须失败。"""

        executor: DouyinCommerceBatchExecutor

        class PauseAfterFirstSubmitManager(FakeCommerceSessionManager):
            def submit(self, session_id: str, payload: dict, **kwargs) -> dict:
                index = self._index_by_session[session_id]
                result = super().submit(session_id, payload, **kwargs)
                if index == 0:
                    executor.request_pause(source="user_confirmed")
                return result

        manager = PauseAfterFirstSubmitManager()
        executor = DouyinCommerceBatchExecutor(manager)
        result = executor.run_publish(self.batch, task_id=self.task["id"], confirmed=True)

        self.assertEqual([row["status"] for row in result], ["published", "paused", "paused"])
        self.assertNotIn("start_upload:1", manager.calls)
        saved = task_service.get_task(self.task["id"])
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(
            saved["pauseReasonCode"],
            task_service.PAUSE_REASON_USER_REQUEST,
        )

    def test_pause_request_requires_explicit_user_confirmation_source(self) -> None:
        """验证回调或无来源调用不得伪装成用户手动暂停。"""

        executor = DouyinCommerceBatchExecutor(FakeCommerceSessionManager())

        self.assertFalse(executor.request_pause())
        self.assertTrue(executor.request_pause(source="user_confirmed"))

    def test_location_preset_must_exactly_match_current_editor_candidates(self) -> None:
        different_address = normalize_location_candidate(
            {
                "poiId": "poi-other",
                "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区不同道路 1 号",
            }
        )
        manager = FakeCommerceSessionManager(location_candidates=[different_address])

        result = DouyinCommerceBatchExecutor(manager).run_preflight(
            self.batch, task_id=self.task["id"]
        )

        self.assertEqual(result[0]["status"], "failed")
        self.assertNotIn("apply_location:0", manager.calls)
        self.assertNotIn("submit:0", manager.calls)

    def test_domestic_location_passes_bounded_address_city_and_name_keywords(self) -> None:
        """执行器一次传入地址、城市加店名和店名，由同面板原子动作内部回退。"""

        location = {
            "poiId": "visible-poi:shanghai-store",
            "name": "夜南香北京烤鸭",
            "address": "上海市静安区青云路与东宝兴路交叉口西100米",
            "scope": "domestic",
        }
        batch = {**self.batch, "items": [{**self.batch["items"][0], "locationPreset": location}]}
        task = task_service.create_douyin_batch_task(batch)

        manager = FakeCommerceSessionManager()
        result = DouyinCommerceBatchExecutor(manager).run_preflight(batch, task_id=task["id"])

        self.assertEqual(result[0]["status"], "preflighted")
        self.assertEqual(
            manager.atomic_location_requests[0][3],
            [
                location["address"],
                "上海 夜南香北京烤鸭",
                "夜南香北京烤鸭",
            ],
        )

    def test_location_search_keywords_use_address_then_city_and_name(self) -> None:
        self.assertEqual(
            _location_search_keywords(
                {
                    "name": "夜南香北京烤鸭",
                    "address": "广西壮族自治区北海市银海区银滩大道万泉城二区北门36栋0112号",
                }
            ),
            [
                "广西壮族自治区北海市银海区银滩大道万泉城二区北门36栋0112号",
                "北海 夜南香北京烤鸭",
                "夜南香北京烤鸭",
            ],
        )

    def test_preflight_never_submits_and_publish_requires_explicit_total_confirmation(self) -> None:
        manager = FakeCommerceSessionManager()
        executor = DouyinCommerceBatchExecutor(manager)

        result = executor.run_preflight(self.batch, task_id=self.task["id"])
        self.assertEqual([row["status"] for row in result], ["preflighted"] * 3)
        self.assertFalse(any(call.startswith("submit:") for call in manager.calls))
        with self.assertRaisesRegex(DouyinCommerceBatchExecutorError, "总确认"):
            executor.run_publish(self.batch, task_id=self.task["id"])

    def test_second_preflight_rebuilds_all_editor_sessions_after_previous_sessions_close(self) -> None:
        """前一轮预检收束会话后，下一轮必须重新为每条视频创建上传会话。"""

        manager = FakeCommerceSessionManager()
        executor = DouyinCommerceBatchExecutor(manager)
        executor.run_preflight(self.batch, task_id=self.task["id"])
        second_task = task_service.create_douyin_batch_task(self.batch)

        result = executor.run_preflight(self.batch, task_id=second_task["id"])

        self.assertEqual([row["status"] for row in result], ["preflighted"] * 3)
        self.assertEqual(
            [call for call in manager.calls if call.startswith("start_upload:")],
            [
                "start_upload:0", "start_upload:1", "start_upload:2",
                "start_upload:3", "start_upload:4", "start_upload:5",
            ],
        )
        self.assertEqual(manager.open_sessions, 0)

    def test_public_module_has_no_dict_to_success_bridge(self) -> None:
        import app_core.douyin_commerce_batch_executor as executor_module

        self.assertFalse(hasattr(executor_module, "write_verified_platform_result"))

    def test_verification_challenge_keeps_batch_item_context_only_in_memory(self) -> None:
        challenge = VerificationChallenge(
            kind="sms",
            message="需要短信验证",
            item_index=2,
            item_label="b.mp4",
        )

        self.assertEqual(challenge.item_index, 2)
        self.assertEqual(challenge.item_label, "b.mp4")


if __name__ == "__main__":
    unittest.main()
