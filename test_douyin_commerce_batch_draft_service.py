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

    def test_batch_draft_keeps_shared_fields_and_item_specific_location_only(self) -> None:
        saved = save_batch_draft(
            {
                "accountId": 7,
                "accountFile": "douyin.json",
                "shared": {"title": "统一标题", "description": "统一文案", "tags": ["北海"]},
                "items": [{"mediaPath": "/tmp/a.mp4", "locationPresetId": "p1", "enableTimer": False}],
                "cookie": "must-not-persist",
            }
        )
        self.assertEqual(saved["payload"]["items"][0]["locationPresetId"], "p1")
        self.assertNotIn("cookie", saved["payload"])
        self.assertEqual(load_batch_draft(), saved)

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
            [{"mediaPath": "/tmp/a.mp4", "locationPresetId": "", "enableTimer": False, "scheduleTimeOverride": ""}],
        )
        self.assertNotIn("token", normalized)

    def test_item_count_must_be_between_one_and_twenty(self) -> None:
        payload = {"accountId": 7, "accountFile": "douyin.json", "shared": {}, "items": []}
        with self.assertRaisesRegex(DouyinCommerceBatchDraftError, "1 至 20"):
            normalize_batch_draft(payload)


if __name__ == "__main__":
    unittest.main()
