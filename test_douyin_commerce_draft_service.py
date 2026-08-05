# -*- coding: utf-8 -*-
"""抖音带货内容准备本地保存的离线回归测试。"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import douyin_commerce_draft_service


class DouyinCommerceContentDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "drafts.sqlite3"
        self.database_patch = patch("app_core.database.DB_PATH", self.database_path)
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.temporary_directory.cleanup()

    def test_save_and_restore_keeps_only_content_preparation_fields(self) -> None:
        saved = douyin_commerce_draft_service.save_content_draft(
            {
                "accountId": 7,
                "accountFile": "oneclick_3_demo.json",
                "mediaId": 9,
                "mediaPath": "/tmp/demo.mp4",
                "title": "本地团购测试",
                "description": "只验证本机内容保存。",
                "tags": ["北海", "本地团购", "北海"],
                "selectedMusic": {"musicId": "不应保存"},
                "locationPoi": {"poiId": "不应保存"},
                "commerceStore": {"storeId": "不应保存"},
                "sessionId": "不应保存",
                "cookie": "不应保存",
            }
        )
        restored = douyin_commerce_draft_service.load_content_draft()

        self.assertEqual(saved["payload"]["tags"], ["北海", "本地团购"])
        self.assertEqual(restored["payload"]["accountFile"], "oneclick_3_demo.json")
        self.assertEqual(restored["payload"]["mediaPath"], "/tmp/demo.mp4")
        self.assertNotIn("selectedMusic", restored["payload"])
        self.assertNotIn("locationPoi", restored["payload"])
        self.assertNotIn("commerceStore", restored["payload"])
        self.assertNotIn("sessionId", restored["payload"])
        self.assertNotIn("cookie", restored["payload"])

    def test_malformed_saved_payload_stops_instead_of_guessing_restore(self) -> None:
        from app_core import database

        with database.connect() as connection:
            connection.execute(
                "INSERT INTO douyin_commerce_content_drafts (id, payloadJson, updatedAt) VALUES (1, ?, ?)",
                ("not-json", "2026-08-04 18:00"),
            )
        with self.assertRaisesRegex(
            douyin_commerce_draft_service.DouyinCommerceContentDraftError,
            "无法读取",
        ):
            douyin_commerce_draft_service.load_content_draft()

    def test_tag_history_only_remembers_plain_local_tags(self) -> None:
        first = douyin_commerce_draft_service.remember_tag_history(
            ["北海", "本地团购", "北海", "#探店"]
        )
        second = douyin_commerce_draft_service.remember_tag_history(["本地团购", "短视频"])

        self.assertIn("北海", first)
        self.assertIn("探店", first)
        self.assertIn("短视频", second)
        self.assertEqual(len(second), len(set(second)))
        self.assertNotIn("cookie", " ".join(second).lower())


if __name__ == "__main__":
    unittest.main()
