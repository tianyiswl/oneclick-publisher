# -*- coding: utf-8 -*-
"""TikTok/YouTube 正式发布的离线契约测试，不访问平台。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app_core import overseas_video_publish
from uploader.youtube_uploader import main as youtube_uploader
from uploader.tk_uploader.main import (
    TiktokVideo,
    tiktok_publish_success_signal,
    tiktok_security_intervention_reason,
)
from uploader.youtube_uploader.main import (
    YouTubeVideo,
    youtube_content_list_receipt,
    youtube_preflight_receipt,
    youtube_publish_success_signal,
    youtube_security_intervention_reason,
)


class OverseasVideoPublishPolicyTests(unittest.TestCase):
    def _payload(self, root: Path, platform_type: int) -> dict:
        video = root / "video.mp4"
        video.write_bytes(b"offline-video")
        account = root / f"account-{platform_type}.json"
        account.write_text("{}", encoding="utf-8")
        return {
            "type": platform_type,
            "contentType": "video",
            "runtimeMode": "publish",
            "debugDryRun": False,
            "overseasVideoPublishConfirmed": True,
            "title": "海外正式发布测试",
            "description": "只验证离线契约",
            "tags": ["oneclick"],
            "fileList": [str(video)],
            "accountList": [account.name],
            "visibility": "public",
            "enableTimer": False,
            "notifySubscribers": True,
        }

    def test_tiktok_and_youtube_accept_minimum_immediate_publish_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with patch.object(overseas_video_publish, "COOKIE_DIR", root):
                for platform_type in (6, 7):
                    checked = overseas_video_publish.validate_overseas_video_publish_payload(
                        self._payload(root, platform_type)
                    )
                    self.assertTrue(checked["ok"], checked["errors"])

    def test_confirmation_and_immediate_publish_are_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 6)
            payload["overseasVideoPublishConfirmed"] = False
            payload["enableTimer"] = True
            payload["scheduleTime"] = "2026-08-12 10:00"
            with patch.object(overseas_video_publish, "COOKIE_DIR", root):
                checked = overseas_video_publish.validate_overseas_video_publish_payload(
                    payload
                )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("正式发布确认" in item for item in checked["errors"]))
        self.assertTrue(any("立即发布" in item for item in checked["errors"]))

    def test_unreadable_tiktok_fields_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 6)
            payload.update(
                {
                    "visibility": "private",
                    "aiGenerated": True,
                    "coverPath": str(root / "cover.png"),
                }
            )
            with patch.object(overseas_video_publish, "COOKIE_DIR", root):
                checked = overseas_video_publish.validate_overseas_video_publish_payload(
                    payload
                )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("公开可见" in item for item in checked["errors"]))
        self.assertTrue(any("AI 内容声明" in item for item in checked["errors"]))
        self.assertTrue(any("自定义封面" in item for item in checked["errors"]))

    def test_youtube_requires_supported_notification_readback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 7)
            payload["notifySubscribers"] = False
            with patch.object(overseas_video_publish, "COOKIE_DIR", root):
                checked = overseas_video_publish.validate_overseas_video_publish_payload(
                    payload
                )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("不通知订阅者" in item for item in checked["errors"]))

    def test_service_requires_verified_platform_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 7)
            with (
                patch.object(overseas_video_publish, "COOKIE_DIR", root),
                patch.dict(
                    overseas_video_publish.HANDLERS,
                    {
                        7: lambda *_args, **_kwargs: [
                            {
                                "status": "published",
                                "evidence": "studio_content_list:new-id:public",
                            }
                        ]
                    },
                ),
            ):
                result = overseas_video_publish.run_overseas_video_publish_sync(
                    payload
                )
        self.assertTrue(result["ok"])
        self.assertEqual(result["visibility"], "public")

    def test_empty_platform_receipt_is_never_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 6)
            with (
                patch.object(overseas_video_publish, "COOKIE_DIR", root),
                patch.dict(
                    overseas_video_publish.HANDLERS,
                    {6: lambda *_args, **_kwargs: [None]},
                ),
            ):
                with self.assertRaisesRegex(
                    overseas_video_publish.OverseasVideoPublishError,
                    "平台成功回执",
                ):
                    overseas_video_publish.run_overseas_video_publish_sync(payload)

    def test_youtube_private_save_is_not_reported_as_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 7)
            payload["visibility"] = "private"
            with (
                patch.object(overseas_video_publish, "COOKIE_DIR", root),
                patch.dict(
                    overseas_video_publish.HANDLERS,
                    {
                        7: lambda *_args, **_kwargs: [
                            {
                                "status": "saved",
                                "evidence": "studio_content_list:new-id:private",
                            }
                        ]
                    },
                ),
            ):
                result = overseas_video_publish.run_overseas_video_publish_sync(
                    payload
                )
        self.assertTrue(result["ok"])
        self.assertTrue(result["saved"])
        self.assertFalse(result["published"])
        self.assertEqual(result["visibility"], "private")

    def test_youtube_dialog_closed_without_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 7)
            with (
                patch.object(overseas_video_publish, "COOKIE_DIR", root),
                patch.dict(
                    overseas_video_publish.HANDLERS,
                    {
                        7: lambda *_args, **_kwargs: [
                            {
                                "status": "published",
                                "evidence": "studio_upload_dialog_closed:public",
                            }
                        ]
                    },
                ),
            ):
                with self.assertRaisesRegex(
                    overseas_video_publish.OverseasVideoPublishError,
                    "平台成功回执",
                ):
                    overseas_video_publish.run_overseas_video_publish_sync(
                        payload
                    )


class OverseasVideoPublishSignalTests(unittest.TestCase):
    def test_tiktok_success_and_security_signals_are_specific(self) -> None:
        self.assertIsNotNone(
            tiktok_publish_success_signal(
                url="https://www.tiktok.com/tiktokstudio/upload",
                feedback_text="Video posted successfully",
            )
        )
        self.assertIsNone(
            tiktok_publish_success_signal(
                url="https://www.tiktok.com/tiktokstudio/upload",
                feedback_text="Posting...",
            )
        )
        self.assertIsNotNone(
            tiktok_security_intervention_reason(
                "https://www.tiktok.com/challenge/verify",
                "",
            )
        )

    def test_youtube_success_and_security_signals_are_specific(self) -> None:
        self.assertIsNotNone(
            youtube_publish_success_signal(
                url="https://studio.youtube.com/channel/test/videos/upload",
                feedback_text="Video saved",
                upload_dialog_visible=False,
                final_button_visible=False,
                visibility="public",
            )
        )
        self.assertIsNone(
            youtube_publish_success_signal(
                url="https://studio.youtube.com/channel/test/videos/upload",
                feedback_text="",
                upload_dialog_visible=False,
                final_button_visible=False,
                visibility="public",
            )
        )
        self.assertIsNone(
            youtube_publish_success_signal(
                url="https://studio.youtube.com/channel/test/videos/upload",
                feedback_text="Saving...",
                upload_dialog_visible=True,
                final_button_visible=True,
                visibility="public",
            )
        )
        self.assertIsNotNone(
            youtube_security_intervention_reason(
                "https://accounts.google.com/signin/v2/challenge/pwd",
                "",
            )
        )
        self.assertNotIn(
            "登录状态已失效",
            youtube_security_intervention_reason(
                "https://accounts.google.com/signin/v2/challenge/pwd",
                "",
            ),
        )
        rejected = youtube_security_intervention_reason(
            "https://accounts.google.com/v3/signin/rejected",
            "此浏览器或应用可能不安全。请尝试使用其他浏览器。",
        )
        self.assertIn("拒绝当前浏览器", rejected)

    def test_youtube_content_list_receipt_requires_new_matching_video(self) -> None:
        rows = [
            {"videoId": "old-id", "title": "目标视频", "visibility": "private"},
            {"videoId": "new-id", "title": "目标视频", "visibility": "private"},
        ]
        self.assertEqual(
            youtube_content_list_receipt(
                rows,
                title="目标视频",
                visibility="private",
                preexisting_video_ids={"old-id"},
            ),
            "studio_content_list:new-id:private",
        )
        self.assertIsNone(
            youtube_content_list_receipt(
                rows,
                title="目标视频",
                visibility="public",
                preexisting_video_ids={"old-id"},
            )
        )
        self.assertIsNone(
            youtube_content_list_receipt(
                rows,
                title="目标视频",
                visibility="private",
                preexisting_video_ids=None,
            )
        )
        signal = youtube_publish_success_signal(
            url="https://studio.youtube.com/channel/test/videos/upload",
            feedback_text="",
            upload_dialog_visible=False,
            final_button_visible=False,
            visibility="private",
            content_list_receipt="studio_content_list:new-id:private",
        )
        self.assertEqual(signal, "studio_content_list:new-id:private")

    def test_youtube_preflight_receipt_requires_every_requested_field(self) -> None:
        required = {"video", "title", "description", "tags", "audience", "visibility"}
        receipt = youtube_preflight_receipt(
            verified_fields=required,
            required_fields=required,
            visibility="private",
        )
        self.assertEqual(receipt["status"], "preflight_ready")
        self.assertEqual(receipt["platformMutation"], "private_upload")
        self.assertEqual(set(receipt["verifiedFields"]), required)
        with self.assertRaisesRegex(RuntimeError, "逐字段回读"):
            youtube_preflight_receipt(
                verified_fields=required - {"tags"},
                required_fields=required,
                visibility="private",
            )

    def test_youtube_security_challenge_stops_without_auto_resume(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.url_reads = 0

            @property
            def url(self) -> str:
                self.url_reads += 1
                if self.url_reads == 1:
                    return "https://accounts.google.com/signin/v2/challenge/pwd"
                return "https://studio.youtube.com/channel/test/videos/upload"

        app = YouTubeVideo("测试", "/not/used.mp4", [], "/not/used.json")
        sleep = AsyncMock(return_value=None)
        with (
            patch.object(youtube_uploader, "_body_text", new=AsyncMock(return_value="")),
            patch.object(
                youtube_uploader,
                "reveal_page_window",
                new=AsyncMock(return_value=None),
            ),
            patch.object(youtube_uploader.asyncio, "sleep", new=sleep),
        ):
            with self.assertRaises(
                youtube_uploader.YouTubeManualInterventionRequired
            ):
                asyncio.run(app._wait_for_manual_intervention(Page()))
        sleep.assert_not_awaited()

    def test_tiktok_final_button_is_clicked_once_then_verified(self) -> None:
        app = TiktokVideo("测试", "/not/used.mp4", [], 0, "/not/used.json")
        app.publish_confirmed = True
        button = AsyncMock()
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        app._ensure_public_visibility = AsyncMock(return_value=None)
        app._post_button = AsyncMock(return_value=button)
        app._wait_for_publish_result = AsyncMock(
            return_value="platform_feedback:video posted successfully"
        )
        result = asyncio.run(app._publish_formally(AsyncMock(), AsyncMock()))
        button.click.assert_awaited_once()
        self.assertEqual(result["status"], "published")

    def test_youtube_final_button_is_clicked_once_then_verified(self) -> None:
        app = YouTubeVideo("测试", "/not/used.mp4", [], "/not/used.json")
        app.publish_confirmed = True
        button = AsyncMock()
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        app._final_button = AsyncMock(return_value=button)
        app._wait_for_publish_result = AsyncMock(
            return_value="studio_upload_dialog_closed:private"
        )
        result = asyncio.run(app._publish_formally(AsyncMock()))
        button.click.assert_awaited_once()
        self.assertEqual(result["status"], "saved")


if __name__ == "__main__":
    unittest.main()
