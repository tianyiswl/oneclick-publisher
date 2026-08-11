# -*- coding: utf-8 -*-

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from app_core import database, publish_config_service
from app_core.publish_config_service import parse_tags


class PublishConfigServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "database.db"
        self.database_patch = patch.object(database, "DB_PATH", self.database_path)
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.tempdir.cleanup()

    def test_parses_hash_and_line_separated_tags_without_duplicates(self):
        self.assertEqual(
            parse_tags("#AI工具\n#AI工作流 #AI工具\n独立开发"),
            ["AI工具", "AI工作流", "独立开发"],
        )

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(parse_tags(""), [])

    def test_publish_draft_round_trip_has_no_expiration(self):
        payload = {
            "schemaVersion": 1,
            "content": {
                "commonTitle": "测试标题",
                "commonText": "测试文案",
                "tags": "#测试 #恢复",
            },
            "selectedAccountIds": [2],
            "selectedMediaIds": [5],
        }

        saved = publish_config_service.save_publish_draft(payload)
        loaded = publish_config_service.load_publish_draft()

        self.assertEqual(loaded, saved)
        self.assertEqual(loaded["payload"], payload)
        self.assertRegex(loaded["updatedAt"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_publish_draft_overwrites_previous_content(self):
        publish_config_service.save_publish_draft({"content": {"commonText": "第一版"}})
        publish_config_service.save_publish_draft({"content": {"commonText": "第二版"}})

        loaded = publish_config_service.load_publish_draft()
        self.assertEqual(loaded["payload"]["content"]["commonText"], "第二版")
        with database.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM publish_drafts").fetchone()[0]
        self.assertEqual(count, 1)

    def test_corrupted_publish_draft_raises_actionable_error(self):
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO publish_drafts (id, payloadJson, updatedAt) VALUES (1, ?, ?)",
                ("not-json", "2026-08-11 10:00"),
            )

        with self.assertRaisesRegex(
            publish_config_service.PublishConfigError,
            "无法读取",
        ):
            publish_config_service.load_publish_draft()


if __name__ == "__main__":
    unittest.main()
