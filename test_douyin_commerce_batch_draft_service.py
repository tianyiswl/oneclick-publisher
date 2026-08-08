# -*- coding: utf-8 -*-
"""抖音带货批次草稿的离线回归测试。"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core.douyin_commerce_batch_draft_service import (
    DouyinCommerceBatchDraftError,
    load_batch_draft,
    normalize_batch_draft,
    save_batch_draft,
)


class DouyinCommerceBatchDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_patch = patch(
            "app_core.database.DB_PATH",
            Path(self.temporary_directory.name) / "drafts.sqlite3",
        )
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.temporary_directory.cleanup()

    def test_batch_draft_v3_keeps_stable_platform_intent_without_session_cache(self) -> None:
        saved = save_batch_draft(
            {
                "accountId": 7,
                "accountFile": "douyin.json",
                "shared": {
                    "title": "统一标题",
                    "description": "统一文案",
                    "tags": ["北海"],
                    "selectedMusic": {
                        "musicId": "music-1",
                        "title": "收藏音乐",
                        "creator": "作者",
                        "duration": "00:30",
                        "sessionMarker": "must-not-persist",
                    },
                    "contentDeclaration": "无需添加自主声明",
                },
                "lastLocationSearch": {
                    "scope": "domestic",
                    "keyword": "夜南香北京烤鸭",
                    "candidates": [{"poiId": "must-not-persist"}],
                },
                "items": [
                    {
                        "mediaPath": "/tmp/a.mp4",
                        "locationPresetId": "p1",
                        "locationPreset": {
                            "id": "p1",
                            "accountId": 7,
                            "poiId": "poi-1",
                            "name": "夜南香北京烤鸭",
                            "address": "陕西省安康市汉滨区江北办富民街2号",
                            "scope": "domestic",
                            "verifiedAt": "2026-08-08 15:00",
                            "domMarker": "must-not-persist",
                        },
                        "enableTimer": False,
                    }
                ],
                "cookie": "must-not-persist",
            }
        )
        self.assertEqual(saved["payload"]["schemaVersion"], 3)
        self.assertEqual(saved["payload"]["shared"]["selectedMusic"]["musicId"], "music-1")
        self.assertNotIn("sessionMarker", saved["payload"]["shared"]["selectedMusic"])
        self.assertEqual(
            saved["payload"]["shared"]["contentDeclaration"],
            "无需添加自主声明",
        )
        self.assertEqual(
            saved["payload"]["lastLocationSearch"],
            {"scope": "domestic", "keyword": "夜南香北京烤鸭"},
        )
        self.assertEqual(saved["payload"]["items"][0]["locationPresetId"], "p1")
        self.assertEqual(
            saved["payload"]["items"][0]["locationPreset"]["address"],
            "陕西省安康市汉滨区江北办富民街2号",
        )
        self.assertNotIn("domMarker", saved["payload"]["items"][0]["locationPreset"])
        self.assertNotIn("cookie", saved["payload"])
        self.assertEqual(load_batch_draft(), saved)

    def test_schema_v2_draft_upgrades_with_empty_optional_platform_intent(self) -> None:
        normalized = normalize_batch_draft(
            {
                "schemaVersion": 2,
                "accountId": 7,
                "accountFile": "douyin.json",
                "shared": {"title": "标题", "description": "文案", "tags": []},
                "items": [
                    {
                        "mediaPath": "/tmp/a.mp4",
                        "locationPresetId": "p1",
                    }
                ],
            }
        )

        self.assertEqual(normalized["schemaVersion"], 3)
        self.assertEqual(normalized["shared"]["selectedMusic"], {})
        self.assertEqual(normalized["shared"]["contentDeclaration"], "")
        self.assertEqual(normalized["lastLocationSearch"], {"scope": "domestic", "keyword": ""})
        self.assertEqual(normalized["items"][0]["locationPreset"], {})

    def test_unknown_and_sensitive_item_fields_are_not_persisted(self) -> None:
        normalized = normalize_batch_draft(
            {
                "accountId": 7,
                "accountFile": "douyin.json",
                "shared": {"title": "标题", "description": "文案", "tags": []},
                "items": [{"mediaPath": "/tmp/a.mp4", "sessionId": "no", "cookie": "no"}],
                "token": "no",
            }
        )
        self.assertEqual(
            normalized["items"],
            [
                {
                    "mediaPath": "/tmp/a.mp4",
                    "locationPresetId": "",
                    "locationPreset": {},
                    "enableTimer": False,
                    "scheduleTimeOverride": "",
                }
            ],
        )
        self.assertNotIn("token", normalized)

    def test_roundtrip_keeps_batch_mode_shanghai_schedule_and_per_item_override(self) -> None:
        """真实 SQLite 回读必须完整保留排期控件，而非只在内存规范化。"""

        saved = save_batch_draft(
            {
                "accountId": 7,
                "accountFile": "douyin.json",
                "shared": {"title": "标题", "description": "文案", "tags": ["北海"]},
                "publishMode": "interval-schedule",
                "schedule": {
                    "timezone": "Asia/Shanghai",
                    "startTime": "2026-08-10 09:00",
                    "intervalMinutes": 30,
                },
                "items": [
                    {"mediaPath": "/tmp/a.mp4", "locationPresetId": "p1"},
                    {
                        "mediaPath": "/tmp/b.mp4",
                        "locationPresetId": "p2",
                        "scheduleTimeOverride": "2026-08-10 11:00",
                    },
                ],
            }
        )

        restored = load_batch_draft()
        self.assertEqual(restored, saved)
        self.assertEqual(restored["payload"]["publishMode"], "interval-schedule")
        self.assertEqual(restored["payload"]["schedule"]["timezone"], "Asia/Shanghai")
        self.assertEqual(restored["payload"]["schedule"]["startTime"], "2026-08-10 09:00")
        self.assertEqual(restored["payload"]["schedule"]["intervalMinutes"], 30)
        self.assertEqual(
            restored["payload"]["items"][1]["scheduleTimeOverride"],
            "2026-08-10 11:00",
        )

    def test_item_count_must_be_between_one_and_twenty(self) -> None:
        payload = {"accountId": 7, "accountFile": "douyin.json", "shared": {}, "items": []}
        with self.assertRaisesRegex(DouyinCommerceBatchDraftError, "1 至 20"):
            normalize_batch_draft(payload)


if __name__ == "__main__":
    unittest.main()
