# -*- coding: utf-8 -*-
"""抖音带货批量任务记录的回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import database, task_service


class DouyinCommerceBatchTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "database.db"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        database.ensure_schema()
        self.batch = {
            "accountFile": "oneclick_3_offline.json",
            "shared": {
                "title": "北海团购",
                "description": "三条本地团购视频",
                "tags": ["北海", "团购"],
                "selectedMusic": {"musicId": "music-001", "title": "收藏音乐"},
                "contentDeclaration": "内容由AI生成",
            },
            "items": [
                {
                    "mediaPath": f"/tmp/batch-{index}.mp4",
                    "locationPreset": {
                        "poiId": f"poi-{index}",
                        "name": "北海银滩景区",
                        "address": "广西壮族自治区北海市银海区银滩大道中段",
                        "scope": "domestic",
                    },
                    "enableTimer": False,
                }
                for index in range(1, 4)
            ],
        }

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tempdir.cleanup()

    def test_batch_task_creates_one_publish_item_per_video_and_keeps_location_time(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        detail = task_service.get_task(task["id"])

        self.assertEqual(detail["workflowLabel"], "抖音带货批量")
        self.assertEqual(len(detail["items"]), 3)
        self.assertIn("北海银滩景区", detail["commerceSummary"])
        self.assertIn("立即发布", detail["commerceSummary"])

    def test_public_platform_receipt_keeps_a_batch_item_running(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        task_service.mark_batch_item_result(
            task_id, item_id, ok=True, message="编辑页填写完成", event_type="editor_written", readback={}
        )
        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=True,
            message="平台已定时",
            event_type="platform_scheduled_receipt",
            readback={"scheduleTime": "2026-08-07 09:00"},
        )
        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

    def test_final_receipt_without_required_readback_keeps_item_running(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=True,
            message="平台已发布",
            event_type="platform_publish_receipt",
            readback={"publishedAt": "2026-08-06 10:00"},
        )

        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

    def test_public_batch_result_cannot_mark_item_success(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=True,
            message="平台已发布",
            event_type="platform_publish_receipt",
            readback={"platformPostId": "post-001", "publishedAt": "2026-08-06 10:00"},
        )

        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

    def test_controlled_executor_receipt_marks_item_success_with_shanghai_timezone(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]
        receipt = task_service._build_controlled_batch_receipt(
            "platform_scheduled_receipt",
            {"scheduleTime": "2026-08-07 09:00"},
            timezone="Asia/Shanghai",
        )

        task_service._mark_controlled_batch_receipt(task_id, item_id, receipt, "平台已定时")

        detail = task_service.get_task(task_id)
        self.assertEqual(detail["items"][0]["status"], "success")
        self.assertEqual(json.loads(detail["events"][-1]["detailJson"])["timezone"], "Asia/Shanghai")

    def test_scheduled_receipt_requires_controlled_source_and_beijing_time(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        for readback in (
            {"scheduleTime": "2026-08-07"},
            {"scheduleTime": True},
            {"scheduleTime": "2026-08-07 09:00"},
        ):
            task_service.mark_batch_item_result(
                task_id,
                item_id,
                ok=True,
                message="平台已定时",
                event_type="platform_scheduled_receipt",
                readback=readback,
            )
            self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

    def test_publish_receipt_rejects_boolean_and_non_string_identity(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        for readback in ({"platformPostId": True}, {"platformPostId": 1}, {"postUrl": False}):
            task_service.mark_batch_item_result(
                task_id,
                item_id,
                ok=True,
                message="平台已发布",
                event_type="platform_publish_receipt",
                readback=readback,
            )
            self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

    def test_readback_is_whitelisted_before_persistence(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=True,
            message="平台已定时",
            event_type="platform_scheduled_receipt",
            readback={
                "scheduleTime": "2026-08-07 09:00",
                "platformPostId": "post-001",
                "cookie": "must-not-store",
                "accessToken": "must-not-store",
                "session": {"id": "must-not-store"},
                "account": "must-not-store",
            },
        )

        event = task_service.get_task(task_id)["events"][-1]
        self.assertEqual(
            json.loads(event["detailJson"]),
            {"scheduleTime": "2026-08-07 09:00", "platformPostId": "post-001"},
        )

    def test_normal_event_after_success_does_not_roll_back_item_status(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]
        task_service._mark_controlled_batch_receipt(
            task_id,
            item_id,
            task_service._build_controlled_batch_receipt(
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-08-07 09:00"},
                timezone="Asia/Shanghai",
            ),
            "平台已定时",
        )

        task_service.mark_batch_item_result(
            task_id,
            item_id,
            ok=True,
            message="编辑页状态补充",
            event_type="editor_written",
            readback={"session": "must-not-store"},
        )

        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "success")


if __name__ == "__main__":
    unittest.main()
