# -*- coding: utf-8 -*-
"""海外发布调度与国内草稿兼容性的离线回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import publish_service


def _video_payload(video: Path, platform_type: int, mode: str) -> dict:
    return {
        "type": platform_type,
        "contentType": "video",
        "runtimeMode": mode,
        "debugDryRun": mode == "preflight",
        "title": "离线路由测试",
        "description": "不会连接平台",
        "fileList": [str(video)],
        "accountList": ["offline-account.json"],
        "enableTimer": False,
    }


class PublishServiceRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.video = Path(self.temp.name) / "video.mp4"
        self.video.write_bytes(b"offline-video")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_domestic_draft_validation_remains_available(self) -> None:
        for platform_type in (2, 5):
            payload = _video_payload(self.video, platform_type, "draft")
            payload["debugDryRun"] = False
            prepared = publish_service._validate_payloads([payload])
            self.assertEqual(prepared[0]["type"], platform_type)

    def test_unsupported_draft_is_rejected_before_browser(self) -> None:
        payload = _video_payload(self.video, 8, "draft")
        payload["debugDryRun"] = False
        with self.assertRaisesRegex(ValueError, "草稿保存通道"):
            publish_service._validate_payloads([payload])

    def test_domestic_draft_routes_to_existing_draft_executor(self) -> None:
        payload = _video_payload(self.video, 2, "draft")
        payload["debugDryRun"] = False
        with (
            patch.object(
                publish_service,
                "post_video_batch_draft_tabs",
                return_value=[{"type": 2, "ok": True, "message": None}],
            ) as runner,
            patch.object(publish_service.task_service, "mark_task_running"),
            patch.object(publish_service.task_service, "mark_platform_result") as mark,
        ):
            publish_service._run_draft({"id": 101}, [payload])
        runner.assert_called_once_with([payload])
        self.assertTrue(mark.call_args.kwargs["ok"])
        self.assertEqual(mark.call_args.args[1], 2)

    def test_youtube_browser_publish_remains_locked(self) -> None:
        payload = _video_payload(self.video, 7, "publish")
        payload["debugDryRun"] = False
        with self.assertRaisesRegex(ValueError, "Meta 浏览器通道"):
            publish_service._validate_payloads([payload])

    def test_meta_browser_routes_only_after_confirmation_contract(self) -> None:
        payload = _video_payload(self.video, 8, "publish")
        payload["debugDryRun"] = False
        payload["metaBrowserPublishConfirmed"] = True
        payload["metaBrowserAutomationAcknowledged"] = True
        result = {
            "ok": True,
            "published": True,
            "scheduled": False,
            "message": "Meta 平台回读成功",
        }
        with (
            patch.object(
                publish_service.overseas_browser_publish,
                "run_meta_browser_publish_sync",
                return_value=result,
            ) as runner,
            patch.object(publish_service.task_service, "mark_task_running"),
            patch.object(publish_service.task_service, "record_task_event"),
            patch.object(publish_service.task_service, "mark_platform_result") as mark,
        ):
            publish_service._run_publish({"id": 103}, [payload])
        runner.assert_called_once_with(payload)
        self.assertTrue(mark.call_args.kwargs["ok"])
        self.assertEqual(mark.call_args.args[1], 8)


if __name__ == "__main__":
    unittest.main()
