# -*- coding: utf-8 -*-
"""抖音图文矩阵独立本地草稿的离线回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import database, douyin_graphic_matrix_draft_service


class DouyinGraphicMatrixDraftServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "matrix-draft.sqlite3"
        self.database_patch = patch.object(database, "DB_PATH", self.database_path)
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def _payload(title: str = "通用标题") -> dict:
        return {
            "schemaVersion": "oneclick-douyin-graphic-matrix-draft/v1",
            "images": [
                {"mediaId": 8, "path": "/tmp/a.jpg"},
                {"mediaId": 0, "path": "/tmp/manual.png"},
            ],
            "accounts": [
                {"accountId": 3, "filePath": "oneclick_3_account.json"}
            ],
            "common": {
                "title": title,
                "body": "通用正文",
                "tags": ["矩阵发布", "图文"],
            },
            "schedule": {
                "startDate": "2026-08-28",
                "startTime": "18:00",
                "intervalMinutes": 30,
            },
            "targets": [
                {
                    "itemIndex": 1,
                    "accountId": 3,
                    "overrides": {"title": "账号标题"},
                    "scheduleTime": "2026-08-28 18:30",
                    "timezone": "Asia/Shanghai",
                    "scheduleOverridden": True,
                }
            ],
            "currentStep": 1,
        }

    def test_round_trip_is_independent_from_publish_center_draft(self) -> None:
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO publish_drafts (id, payloadJson, updatedAt) VALUES (1, ?, ?)",
                (json.dumps({"content": {"commonText": "发布中心"}}), "2026-08-27 12:00"),
            )

        saved = douyin_graphic_matrix_draft_service.save_draft(self._payload())
        restored = douyin_graphic_matrix_draft_service.load_draft()

        self.assertEqual(restored, saved)
        self.assertEqual(restored["payload"]["common"]["title"], "通用标题")
        with database.connect() as connection:
            publish_center = connection.execute(
                "SELECT payloadJson FROM publish_drafts WHERE id = 1"
            ).fetchone()
        self.assertEqual(json.loads(publish_center["payloadJson"])["content"]["commonText"], "发布中心")

    def test_save_overwrites_only_previous_matrix_snapshot(self) -> None:
        douyin_graphic_matrix_draft_service.save_draft(self._payload("第一版"))
        douyin_graphic_matrix_draft_service.save_draft(self._payload("第二版"))

        restored = douyin_graphic_matrix_draft_service.load_draft()

        self.assertEqual(restored["payload"]["common"]["title"], "第二版")

    def test_corrupted_matrix_snapshot_stops_instead_of_guessing(self) -> None:
        snapshot_path = self.database_path.parent / "douyin_graphic_matrix_draft.json"
        snapshot_path.write_text("not-json", encoding="utf-8")

        with self.assertRaisesRegex(
            douyin_graphic_matrix_draft_service.DouyinGraphicMatrixDraftError,
            "无法读取",
        ):
            douyin_graphic_matrix_draft_service.load_draft()


if __name__ == "__main__":
    unittest.main()
