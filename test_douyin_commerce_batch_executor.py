# -*- coding: utf-8 -*-
"""抖音带货批量执行器的离线回归测试。

所有会话都是内存替身；测试绝不启动 Playwright、浏览器或平台请求。
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import database, task_service
from app_core.douyin_commerce_batch_executor import (
    BatchProgressEvent,
    DouyinCommerceBatchExecutor,
    DouyinCommerceBatchExecutorError,
)
from app_core.douyin_commerce_batch_service import apply_interval_schedule, validate_batch_payload
from app_core.douyin_location_service import normalize_location_candidate
from app_core.douyin_verification import DouyinVerificationError, VerificationChallenge


class FakeCommerceSessionManager:
    """只记录批量执行器调用顺序的内存会话替身。"""

    def __init__(
        self,
        *,
        fail_item_indexes: set[int] | None = None,
        challenge_on_index: int | None = None,
        location_candidates: list[dict] | None = None,
    ) -> None:
        self.fail_item_indexes = fail_item_indexes or set()
        self.challenge_on_index = challenge_on_index
        self.location_candidates = location_candidates
        self.calls: list[str] = []
        self.open_sessions = 0
        self.max_open_sessions = 0
        self._index_by_session: dict[str, int] = {}
        self._challenge_seen = False

    def start_upload(self, payload: dict, **_kwargs) -> dict:
        if payload.get("runtimeMode") != "preflight" or payload.get("debugDryRun") is not True:
            raise AssertionError("上传必须保持预检模式")
        index = len([call for call in self.calls if call.startswith("start_upload:")])
        session_id = f"session-{index}"
        self.calls.append(f"start_upload:{index}")
        self._index_by_session[session_id] = index
        self.open_sessions += 1
        self.max_open_sessions = max(self.max_open_sessions, self.open_sessions)
        return {"sessionId": session_id}

    def synchronize_content(self, session_id: str, _payload: dict, **_kwargs) -> dict:
        self.calls.append(f"synchronize_content:{self._index_by_session[session_id]}")
        return {}

    def select_cached_favorite_music(self, session_id: str, _music_id: str) -> dict:
        self.calls.append(f"select_music:{self._index_by_session[session_id]}")
        return {"musicId": "music-001", "title": "测试音乐", "creator": "测试", "duration": "01:08"}

    def select_content_declaration(self, session_id: str, declaration: str) -> str:
        self.calls.append(f"select_declaration:{self._index_by_session[session_id]}")
        return declaration

    def search_locations(self, session_id: str, _keyword: str, _scope: str) -> list[dict]:
        self.calls.append(f"search_locations:{self._index_by_session[session_id]}")
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
        return dict(location)

    def sync_schedule(self, session_id: str, _payload: dict) -> dict:
        self.calls.append(f"sync_schedule:{self._index_by_session[session_id]}")
        return {"scheduled": False}

    def preflight(self, session_id: str, _payload: dict) -> dict:
        if _payload.get("runtimeMode") != "preflight" or _payload.get("debugDryRun") is not True:
            raise AssertionError("预检必须保持 dry-run")
        self.calls.append(f"preflight:{self._index_by_session[session_id]}")
        return {"ok": True}

    def submit(
        self,
        session_id: str,
        _payload: dict,
        task_id: int | None = None,
        **_kwargs,
    ) -> dict:
        del task_id
        if _payload.get("runtimeMode") != "publish" or _payload.get("debugDryRun") is not False:
            raise AssertionError("最终提交必须明确发布模式")
        index = self._index_by_session[session_id]
        self.calls.append(f"submit:{index}")
        if index in self.fail_item_indexes:
            raise RuntimeError(f"第 {index} 条平台回读失败")
        if self.challenge_on_index == index and not self._challenge_seen:
            self._challenge_seen = True
            raise DouyinVerificationError("抖音需要短信验证")
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
            self.open_sessions -= 1

    def status(self) -> dict[str, str]:
        return {"active": "true" if self.open_sessions else "false", "stage": ""}


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
        self.assertEqual(manager.max_open_sessions, 1)
        self.assertIn("submit:0", manager.calls)
        self.assertIn("submit:1", manager.calls)
        self.assertIn("submit:2", manager.calls)
        self.assertEqual([item["status"] for item in task_service.get_task(self.task["id"])["items"]], ["success", "failed", "success"])
        self.assertTrue(any(event.phase == "uploading" for event in events))

    def test_login_or_verification_pauses_batch_without_submitting_later_items(self) -> None:
        manager = FakeCommerceSessionManager(challenge_on_index=1)

        result = DouyinCommerceBatchExecutor(manager).run_publish(
            self.batch, task_id=self.task["id"], confirmed=True
        )

        self.assertEqual([row["status"] for row in result], ["published", "waiting_verification", "pending"])
        self.assertNotIn("submit:2", manager.calls)
        self.assertEqual(task_service.get_task(self.task["id"])["items"][1]["status"], "running")

    def test_location_preset_must_exactly_match_current_editor_candidates(self) -> None:
        different_address = normalize_location_candidate(
            {
                "poiId": "poi-001",
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

    def test_preflight_never_submits_and_publish_requires_explicit_total_confirmation(self) -> None:
        manager = FakeCommerceSessionManager()
        executor = DouyinCommerceBatchExecutor(manager)

        result = executor.run_preflight(self.batch, task_id=self.task["id"])
        self.assertEqual([row["status"] for row in result], ["preflighted"] * 3)
        self.assertFalse(any(call.startswith("submit:") for call in manager.calls))
        with self.assertRaisesRegex(DouyinCommerceBatchExecutorError, "总确认"):
            executor.run_publish(self.batch, task_id=self.task["id"])

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
