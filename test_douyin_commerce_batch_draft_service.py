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

    def test_batch_draft_v4_keeps_commission_intent_without_session_cache(self) -> None:
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
                    "commissionFilter": "no_commission",
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
                            "commissionFilter": "commission",
                            "observedCommissionType": "commission",
                            "productCount": 15,
                            "commissionProductCount": 15,
                            "commerceInfo": "must-not-persist",
                            "domMarker": "must-not-persist",
                        },
                        "enableTimer": False,
                    }
                ],
                "cookie": "must-not-persist",
            }
        )
        self.assertEqual(saved["payload"]["schemaVersion"], 4)
        self.assertEqual(saved["payload"]["shared"]["selectedMusic"]["musicId"], "music-1")
        self.assertNotIn("sessionMarker", saved["payload"]["shared"]["selectedMusic"])
        self.assertEqual(
            saved["payload"]["shared"]["contentDeclaration"],
            "无需添加自主声明",
        )
        self.assertEqual(
            saved["payload"]["lastLocationSearch"],
            {
                "scope": "domestic",
                "keyword": "夜南香北京烤鸭",
                "commissionFilter": "no_commission",
            },
        )
        self.assertEqual(saved["payload"]["items"][0]["locationPresetId"], "p1")
        self.assertEqual(
            saved["payload"]["items"][0]["locationPreset"]["address"],
            "陕西省安康市汉滨区江北办富民街2号",
        )
        location = saved["payload"]["items"][0]["locationPreset"]
        self.assertEqual(location["commissionFilter"], "commission")
        self.assertEqual(location["observedCommissionType"], "commission")
        self.assertEqual(location["productCount"], 15)
        self.assertEqual(location["commissionProductCount"], 15)
        self.assertNotIn("commerceInfo", location)
        self.assertNotIn("domMarker", location)
        self.assertNotIn("cookie", saved["payload"])
        self.assertEqual(load_batch_draft(), saved)

    def test_schema_v3_draft_upgrades_with_legacy_commission_defaults(self) -> None:
        normalized = normalize_batch_draft(
            {
                "schemaVersion": 3,
                "accountId": 7,
                "accountFile": "douyin.json",
                "shared": {"title": "标题", "description": "文案", "tags": []},
                "items": [
                    {
                        "mediaPath": "/tmp/a.mp4",
                        "locationPresetId": "p1",
                        "locationPreset": {
                            "id": "p1",
                            "poiId": "poi-1",
                            "name": "旧地点",
                            "address": "广西壮族自治区北海市旧址1号",
                            "scope": "domestic",
                            "productCount": True,
                            "commissionProductCount": -1,
                        },
                    }
                ],
            }
        )

        self.assertEqual(normalized["schemaVersion"], 4)
        self.assertEqual(normalized["shared"]["selectedMusic"], {})
        self.assertEqual(normalized["shared"]["contentDeclaration"], "")
        self.assertEqual(
            normalized["lastLocationSearch"],
            {"scope": "domestic", "keyword": "", "commissionFilter": "commission"},
        )
        location = normalized["items"][0]["locationPreset"]
        self.assertEqual(location["commissionFilter"], "all")
        self.assertEqual(location["observedCommissionType"], "unknown")
        self.assertIsNone(location["productCount"])
        self.assertIsNone(location["commissionProductCount"])

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
                    "mediaId": None,
                    "mediaPath": "/tmp/a.mp4",
                    "locationPresetId": "",
                    "locationPreset": {},
                    "locationAssignmentSource": "manual",
                    "enableTimer": False,
                    "scheduleTimeOverride": "",
                }
            ],
        )
        self.assertNotIn("token", normalized)

    def test_draft_roundtrip_preserves_location_assignment_provenance(self) -> None:
        """自动地点必须带根词来源，手动地点不应被误标。"""

        saved = save_batch_draft(
            {
                "accountId": 7,
                "accountFile": "douyin.json",
                "shared": {"title": "标题", "description": "文案", "tags": []},
                "items": [
                    {
                        "mediaPath": "/tmp/auto.mp4",
                        "locationAssignmentSource": "auto: 广东 joymark ",
                    },
                    {
                        "mediaPath": "/tmp/manual.mp4",
                        "locationAssignmentSource": "manual",
                    },
                ],
            }
        )

        self.assertEqual(
            saved["payload"]["items"][0]["locationAssignmentSource"],
            "auto:广东joymark",
        )
        self.assertEqual(
            load_batch_draft()["payload"]["items"][1]["locationAssignmentSource"],
            "manual",
        )

    def test_legacy_draft_without_location_assignment_provenance_is_manual(self) -> None:
        """旧草稿未记录来源时按手动处理，不能在换词时删掉用户地点。"""

        normalized = normalize_batch_draft(
            {
                "schemaVersion": 4,
                "accountId": 7,
                "accountFile": "douyin.json",
                "shared": {"title": "标题", "description": "文案", "tags": []},
                "items": [{"mediaPath": "/tmp/legacy.mp4"}],
            }
        )

        self.assertEqual(
            normalized["items"][0]["locationAssignmentSource"], "manual"
        )

    def test_draft_roundtrip_preserves_only_positive_builtin_media_identity(self) -> None:
        raw = {
            "accountId": 7,
            "accountFile": "douyin.json",
            "shared": {"title": "标题", "description": "文案", "tags": []},
            "items": [{"mediaPath": "/tmp/a.mp4", "mediaId": 71}],
        }

        normalized = normalize_batch_draft(raw)
        saved = save_batch_draft(raw)
        restored = load_batch_draft()

        self.assertEqual(normalized["items"][0]["mediaId"], 71)
        self.assertEqual(saved["payload"]["items"][0]["mediaId"], 71)
        self.assertEqual(restored["payload"]["items"][0]["mediaId"], 71)

        for invalid in (True, 0, -1, "71"):
            with self.subTest(invalid=invalid):
                normalized = normalize_batch_draft(
                    {**raw, "items": [{"mediaPath": "/tmp/a.mp4", "mediaId": invalid}]}
                )
                self.assertIsNone(normalized["items"][0]["mediaId"])

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

    def test_media_path_rejects_whitespace_only_value(self) -> None:
        """路径必须保留合法内容，但全空白仍必须拒绝。"""

        payload = {
            "accountId": 7,
            "accountFile": "douyin.json",
            "shared": {},
            "items": [{"mediaPath": " \t\n "}],
        }
        with self.assertRaisesRegex(DouyinCommerceBatchDraftError, "缺少本地媒体路径"):
            normalize_batch_draft(payload)


if __name__ == "__main__":
    unittest.main()
