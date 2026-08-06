# -*- coding: utf-8 -*-
"""抖音带货批量任务记录的回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import (
    database,
    douyin_commerce_batch_executor,
    publish_service,
    publish_runtime,
    task_service,
)
from utils import publish_tasks


class DouyinCommerceBatchTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "database.db"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.publish_task_db_patch = patch.object(publish_tasks, "DB_PATH", self.db_path)
        self.publish_task_db_patch.start()
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
        self.publish_task_db_patch.stop()
        self.db_patch.stop()
        self.tempdir.cleanup()

    @staticmethod
    def _write_final_receipt(*args, **kwargs) -> None:
        """通过未来批量执行器唯一允许导入的内部写入模块执行。"""

        from app_core._douyin_commerce_batch_receipt_writer import (
            _write_final_batch_receipt,
        )

        _write_final_batch_receipt(*args, **kwargs)

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

    def test_generic_platform_result_cannot_mark_batch_items_success(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]

        with self.assertRaisesRegex(ValueError, "批量执行器"):
            task_service.mark_platform_result(
                task_id,
                3,
                ok=True,
                message="调用方伪造的平台成功",
                content_type="video",
                event_type="platform_publish_receipt",
            )

        self.assertEqual(
            [item["status"] for item in task_service.get_task(task_id)["items"]],
            ["pending", "pending", "pending"],
        )

    def test_caller_cannot_construct_private_receipt_and_mark_item_success(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        class ForgedReceipt:
            event_type = "platform_scheduled_receipt"
            readback = {
                "scheduleTime": "2026-08-07 09:00",
                "timezone": "Asia/Shanghai",
            }

        for forged_readback in (
            ForgedReceipt(),
            {
                "scheduleTime": "2026-08-07 09:00",
                "timezone": "Asia/Shanghai",
            },
        ):
            task_service.mark_batch_item_result(
                task_id,
                item_id,
                ok=True,
                message="调用方伪造的平台已定时",
                event_type="platform_scheduled_receipt",
                readback=forged_readback,
            )
            self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "running")

        with self.assertRaises(AttributeError):
            getattr(task_service, "_mark_controlled_batch_receipt")(
                task_id, item_id, ForgedReceipt(), "伪造的平台已定时"
            )

    def test_internal_writer_rejects_utc_missing_fields_and_invalid_events(self) -> None:
        class ForgedTimezone:
            def __ne__(self, _other: object) -> bool:
                return False

        invalid_receipts = (
            (
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-08-07 09:00"},
                "UTC",
            ),
            (
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-08-07 09:00"},
                ForgedTimezone(),
            ),
            (
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-08-07 09:00", "timezone": "UTC"},
                "Asia/Shanghai",
            ),
            (
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-08-07 09:00"},
                "Asia/Shanghai",
            ),
            ("platform_scheduled_receipt", {}, "Asia/Shanghai"),
            (
                "platform_scheduled_receipt",
                {"scheduleTime": "2026-02-30 09:00"},
                "Asia/Shanghai",
            ),
            (
                "platform_publish_receipt",
                {"publishedAt": "2026-08-06 10:00", "timezone": "Asia/Shanghai"},
                "Asia/Shanghai",
            ),
            (
                "platform_publish_receipt",
                {"platformPostId": "post-001", "publishedAt": "2026-08-06 10:00"},
                "Asia/Shanghai",
            ),
            (
                "platform_publish_receipt",
                {
                    "platformPostId": "post-001",
                    "publishedAt": "2026-02-30 10:00",
                    "timezone": "Asia/Shanghai",
                },
                "Asia/Shanghai",
            ),
            ("platform_publish_receipt", {"platformPostId": True}, "Asia/Shanghai"),
            (
                "platform_publish_receipt",
                {"platformPostId": "post-001", "cookie": "must-not-store"},
                "Asia/Shanghai",
            ),
            ("editor_written", {"platformPostId": "post-001"}, "Asia/Shanghai"),
        )

        for event_type, readback, timezone in invalid_receipts:
            with self.subTest(event_type=event_type, readback=readback, timezone=timezone):
                task = task_service.create_douyin_batch_task(self.batch)
                task_id = task["id"]
                item_id = task_service.get_task(task_id)["items"][0]["id"]

                with self.assertRaises(ValueError):
                    self._write_final_receipt(
                        task_id,
                        item_id,
                        event_type=event_type,
                        readback=readback,
                        timezone=timezone,
                        message="无效最终回执",
                    )

                self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "pending")

    def test_internal_writer_rejects_manually_constructed_receipt_object(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        class ForgedReceipt:
            scheduleTime = "2026-08-07 09:00"

        with self.assertRaises(ValueError):
            self._write_final_receipt(
                task_id,
                item_id,
                event_type="platform_scheduled_receipt",
                readback=ForgedReceipt(),
                timezone="Asia/Shanghai",
                message="伪造的平台已定时",
            )

        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "pending")

    def test_internal_writer_accepts_legal_platform_receipt_and_persists_timezone(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        self._write_final_receipt(
            task_id,
            item_id,
            event_type="platform_scheduled_receipt",
            readback={
                "scheduleTime": "2026-08-07 09:00",
                "timezone": "Asia/Shanghai",
            },
            timezone="Asia/Shanghai",
            message="平台已定时",
        )

        detail = task_service.get_task(task_id)
        self.assertEqual(detail["items"][0]["status"], "success")
        self.assertEqual(json.loads(detail["events"][-1]["detailJson"])["timezone"], "Asia/Shanghai")

        published_task = task_service.create_douyin_batch_task(self.batch)
        published_task_id = published_task["id"]
        published_item_id = task_service.get_task(published_task_id)["items"][0]["id"]
        self._write_final_receipt(
            published_task_id,
            published_item_id,
            event_type="platform_publish_receipt",
            readback={
                "platformPostId": "post-001",
                "publishedAt": "2026-08-07 09:00",
                "timezone": "Asia/Shanghai",
            },
            timezone="Asia/Shanghai",
            message="平台已发布",
        )

        self.assertEqual(
            task_service.get_task(published_task_id)["items"][0]["status"],
            "success",
        )

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
        self._write_final_receipt(
            task_id,
            item_id,
            event_type="platform_scheduled_receipt",
            readback={
                "scheduleTime": "2026-08-07 09:00",
                "timezone": "Asia/Shanghai",
            },
            timezone="Asia/Shanghai",
            message="平台已定时",
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

    def test_legacy_task_success_writers_cannot_bypass_batch_receipts(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]

        legacy_writers = (
            lambda: publish_tasks.mark_items(task_id, "success", "伪造成功"),
            lambda: publish_tasks.mark_platform_results(
                task_id, [{"type": 3, "ok": True}], "伪造平台成功"
            ),
            lambda: publish_tasks.complete_task(task_id, "伪造完成"),
        )
        for writer in legacy_writers:
            with self.subTest(writer=writer), self.assertRaisesRegex(ValueError, "批量执行器"):
                writer()
            self.assertEqual(
                [item["status"] for item in task_service.get_task(task_id)["items"]],
                ["pending", "pending", "pending"],
            )

    def test_legacy_publish_runtime_rejects_batch_before_any_success_writer(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        payload = json.loads(task_service.get_task(task_id)["payloadJson"])[0]

        for runner, arguments in (
            (publish_runtime.execute_single_publish, (payload, task)),
            (publish_runtime.execute_batch_publish, ([payload], task)),
        ):
            with self.subTest(runner=runner.__name__):
                result = runner(*arguments)
                self.assertEqual(result["code"], 409)
                self.assertIn("批量执行器", result["msg"])
                self.assertEqual(
                    [item["status"] for item in task_service.get_task(task_id)["items"]],
                    ["pending", "pending", "pending"],
                )

    def test_generic_desktop_publish_service_cannot_create_a_success_path_for_batch_item(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        batch_payload = json.loads(task_service.get_task(task["id"])["payloadJson"])[0]

        with self.assertRaisesRegex(ValueError, "批量执行器"):
            publish_service._validate_payloads([batch_payload])

        generic_task = task_service.create_pending_task(
            [batch_payload], mode="oneclick_publish"
        )
        with self.assertRaisesRegex(ValueError, "批量执行器"):
            task_service.mark_platform_result(
                generic_task["id"],
                3,
                ok=True,
                message="通用服务伪造成功",
                content_type="video",
                event_type="platform_publish",
            )
        self.assertEqual(
            task_service.get_task(generic_task["id"])["items"][0]["status"],
            "pending",
        )

    def test_batch_executor_bridge_requires_timezoned_receipt_and_writes_legal_result(self) -> None:
        task = task_service.create_douyin_batch_task(self.batch)
        task_id = task["id"]
        item_id = task_service.get_task(task_id)["items"][0]["id"]

        with self.assertRaisesRegex(
            douyin_commerce_batch_executor.DouyinCommerceBatchExecutorError,
            "Asia/Shanghai",
        ):
            douyin_commerce_batch_executor.write_verified_platform_result(
                task_id,
                item_id,
                {
                    "ok": True,
                    "scheduled": True,
                    "message": "平台已定时",
                    "scheduledReadback": {"scheduledAt": "2026-08-07 09:00"},
                },
            )
        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "pending")

        douyin_commerce_batch_executor.write_verified_platform_result(
            task_id,
            item_id,
            {
                "ok": True,
                "scheduled": True,
                "message": "平台已定时",
                "scheduledReadback": {
                    "scheduledAt": "2026-08-07 09:00",
                    "timezone": "Asia/Shanghai",
                },
            },
        )
        self.assertEqual(task_service.get_task(task_id)["items"][0]["status"], "success")


if __name__ == "__main__":
    unittest.main()
