# -*- coding: utf-8 -*-
"""抖音带货批量载荷及排期的离线回归测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from app_core.douyin_commerce_batch_service import (
    DouyinCommerceBatchError,
    apply_interval_schedule,
    item_publish_payload,
    prepare_batch_for_execution,
    validate_batch_payload,
)


class DouyinCommerceBatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.media_paths = []
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            path = root / name
            path.write_bytes(b"local-video")
            self.media_paths.append(str(path))
        self.shanghai_now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(second=0, microsecond=0)
        self.schedule_start = self.shanghai_now + timedelta(days=1)
        self.schedule_override = self.schedule_start + timedelta(hours=6)
        self.batch = {
            "type": 3,
            "workflow": "douyin-commerce-batch",
            "commerceMode": "local-group-buy",
            "contentType": "video",
            "accountList": ["oneclick_3_offline.json"],
            "shared": {
                "title": "北海团购视频",
                "description": "逐条验证官方地点和发布时间。",
                "tags": ["北海", "团购", "北海"],
                "selectedMusic": {
                    "musicId": "music-001",
                    "title": "出埃及记",
                    "creator": "石Yuchi",
                    "duration": "01:08",
                },
                "contentDeclaration": "内容由AI生成",
            },
            "publishMode": "interval-schedule",
            "schedule": {
                "timezone": "Asia/Shanghai",
                "startTime": self.schedule_start.strftime("%Y-%m-%d %H:%M"),
                "intervalMinutes": 30,
            },
            "items": [
                self._item(self.media_paths[0]),
                self._item(self.media_paths[1]),
                self._item(
                    self.media_paths[2],
                    schedule_time_override=self.schedule_override.strftime("%Y-%m-%d %H:%M"),
                ),
            ],
            "cookie": "must-not-enter-contract",
            "verificationCode": "must-not-enter-contract",
            "browserState": {"session": "must-not-enter-contract"},
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _item(media_path: str, schedule_time_override: str = "") -> dict[str, object]:
        return {
            "mediaPath": media_path,
            "locationPreset": {
                "poiId": "poi-beihai-001",
                "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区银滩大道中段",
                "scope": "domestic",
            },
            "scheduleTimeOverride": schedule_time_override,
        }

    def test_interval_schedule_defaults_to_shanghai_and_allows_item_override(self) -> None:
        batch = validate_batch_payload(self.batch, now=self.shanghai_now)
        result = apply_interval_schedule(batch, now=self.shanghai_now)

        self.assertEqual(result["items"][0]["scheduleTime"], self.schedule_start.strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(
            result["items"][1]["scheduleTime"],
            (self.schedule_start + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M"),
        )
        self.assertEqual(result["items"][2]["scheduleTime"], self.schedule_override.strftime("%Y-%m-%d %H:%M"))
        self.assertTrue(all(item["enableTimer"] for item in result["items"]))

    def test_batch_rejects_more_than_twenty_items_or_more_than_one_account(self) -> None:
        with self.assertRaisesRegex(DouyinCommerceBatchError, "1 至 20"):
            validate_batch_payload({**self.batch, "items": self.batch["items"] * 7})
        with self.assertRaisesRegex(DouyinCommerceBatchError, "一个"):
            validate_batch_payload({**self.batch, "accountList": ["a.json", "b.json"]})

    def test_item_payload_keeps_single_video_workflow_and_excludes_sensitive_fields(self) -> None:
        scheduled = apply_interval_schedule(
            validate_batch_payload(self.batch, now=self.shanghai_now), now=self.shanghai_now
        )
        payload = item_publish_payload(scheduled, scheduled["items"][0])

        self.assertEqual(payload["workflow"], "douyin-commerce")
        self.assertEqual(payload["batchWorkflow"], "douyin-commerce-batch")
        self.assertEqual(payload["accountList"], ["oneclick_3_offline.json"])
        self.assertEqual(payload["fileList"], [self.media_paths[0]])
        self.assertNotIn("cookie", payload)
        self.assertNotIn("commerceStore", payload)

    def test_batch_contract_whitelist_excludes_sensitive_fields(self) -> None:
        raw = {
            **self.batch,
            "shared": {**self.batch["shared"], "cookie": "no", "verificationCode": "no"},
            "items": [
                {**self.batch["items"][0], "browser": {"state": "no"}, "cookie": "no"}
            ],
        }

        checked = validate_batch_payload(raw, now=self.shanghai_now)

        self.assertEqual(
            set(checked),
            {"type", "workflow", "commerceMode", "contentType", "accountFile", "shared", "publishMode", "schedule", "items"},
        )
        self.assertNotIn("cookie", checked["shared"])
        self.assertNotIn("verificationCode", checked["shared"])
        self.assertNotIn("browser", checked["items"][0])
        self.assertNotIn("cookie", checked["items"][0])

    def test_non_numeric_platform_type_raises_batch_error(self) -> None:
        with self.assertRaisesRegex(DouyinCommerceBatchError, "抖音平台"):
            validate_batch_payload({**self.batch, "type": "not-a-number"})

    def test_explicit_clock_rejects_a_non_future_interval_schedule(self) -> None:
        expired = {
            **self.batch,
            "schedule": {
                **self.batch["schedule"],
                "startTime": self.shanghai_now.strftime("%Y-%m-%d %H:%M"),
            },
        }

        with self.assertRaisesRegex(DouyinCommerceBatchError, "晚于当前北京时间"):
            apply_interval_schedule(expired, now=self.shanghai_now)

    def test_immediate_items_never_keep_a_schedule_time(self) -> None:
        immediate = dict(self.batch, publishMode="immediate", schedule={})
        checked = apply_interval_schedule(
            validate_batch_payload(immediate, now=self.shanghai_now), now=self.shanghai_now
        )

        self.assertFalse(checked["items"][0]["enableTimer"])
        self.assertNotIn("scheduleTime", checked["items"][0])
        self.assertEqual(item_publish_payload(checked, checked["items"][0])["scheduleTime"], "")

    def test_execution_preparation_always_adds_explicit_per_item_timer_fields(self) -> None:
        immediate = prepare_batch_for_execution(
            {**self.batch, "publishMode": "immediate", "schedule": {}},
            now=self.shanghai_now,
        )
        self.assertTrue(all(item["enableTimer"] is False for item in immediate["items"]))
        self.assertTrue(all("scheduleTime" not in item for item in immediate["items"]))

        scheduled = prepare_batch_for_execution(self.batch, now=self.shanghai_now)
        self.assertTrue(all(item["enableTimer"] is True for item in scheduled["items"]))
        self.assertTrue(all(item["scheduleTime"] for item in scheduled["items"]))


if __name__ == "__main__":
    unittest.main()
