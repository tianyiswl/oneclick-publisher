# -*- coding: utf-8 -*-
"""海外官方 OAuth/API 通道的离线回归。

测试不启动浏览器、不读取真实令牌、不访问任何平台。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialogButtonBox

from app_core import account_browser_service, account_service, overseas_api_service
from app_core.overseas import (
    EvidenceRecord,
    EvidenceStage,
    OverseasPlatform,
    PublicationMode,
    UploadRequest,
    UploadResult,
)
from app_core.overseas.meta.api import MetaApiClient
from app_core.overseas.meta.capability import (
    _facebook_fields_match,
    _instagram_fields_match,
)
from app_core.overseas.meta.credentials import MetaPageAsset
from app_core.overseas.youtube.api import YouTubeApiClient
from app_core.overseas.youtube.capability import _metadata_readback
from ui.overseas_authorization_dialog import OverseasAuthorizationDialog
from ui.publish_page import OfficialApiActionConfirmDialog, PublishPage


def _official_payload(video: Path, *, platform_type: int = 7) -> dict:
    return {
        "type": platform_type,
        "contentType": "video",
        "runtimeMode": "preflight",
        "debugDryRun": True,
        "title": "海外官方通道测试",
        "description": "离线验证",
        "fileList": [str(video)],
        "accountList": [f"official-api:{platform_type}:account-1"],
        "accountAuthModes": ["official_api"],
        "accountReferences": ["account-1"],
        "accountDisplayNames": ["测试账号"],
        "enableTimer": False,
        "officialApiConfirmed": False,
    }


class OfficialAccountStorageTests(unittest.TestCase):
    def test_save_official_account_only_persists_non_sensitive_reference(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "database.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE user_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type INTEGER NOT NULL,
                    filePath TEXT,
                    userName TEXT,
                    status INTEGER,
                    profileName TEXT,
                    remark TEXT,
                    lastLoginAt TEXT,
                    lastCheckedAt TEXT,
                    authMode TEXT,
                    accountReference TEXT
                )
                """
            )
            connection.commit()
            connection.close()

            @contextmanager
            def connect_test(*_args, **_kwargs):
                conn = sqlite3.connect(database)
                conn.row_factory = sqlite3.Row
                try:
                    yield conn
                    conn.commit()
                finally:
                    conn.close()

            with patch.object(account_service, "connect", connect_test):
                account_id = account_service.save_official_api_account(
                    7,
                    "测试主体",
                    "channel-123",
                    "测试频道",
                )
            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT * FROM user_info WHERE id = ?", (account_id,)
            ).fetchone()
            columns = [item[1] for item in connection.execute("PRAGMA table_info(user_info)")]
            connection.close()
            data = dict(zip(columns, row))
            self.assertEqual(data["authMode"], "official_api")
            self.assertEqual(data["accountReference"], "channel-123")
            self.assertEqual(data["filePath"], "official-api:7:channel-123")
            serialized = " ".join(str(value) for value in data.values())
            self.assertNotIn("access_token", serialized.lower())
            self.assertNotIn("refresh_token", serialized.lower())

    def test_official_backend_opens_plain_url_without_cookie(self) -> None:
        account = {"id": 5, "type": 7, "authMode": "official_api"}
        with patch.object(account_browser_service.webbrowser, "open", return_value=True) as opener:
            reused = account_browser_service.open_account_backend(account)
        self.assertFalse(reused)
        opener.assert_called_once_with("https://studio.youtube.com/", new=2)


class OfficialPayloadGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.video = Path(self.temp.name) / "video.mp4"
        self.video.write_bytes(b"video")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _ready_status(_platform_type: int) -> dict:
        return {
            "clientConfigured": True,
            "tokenConfigured": True,
            "accessTokenValid": True,
            "scopeGranted": True,
        }

    def test_preflight_is_local_and_requires_exact_official_account(self) -> None:
        payload = _official_payload(self.video)
        with (
            patch.object(overseas_api_service, "official_status", self._ready_status),
            patch.object(overseas_api_service, "validate_official_account", return_value=True),
        ):
            result = overseas_api_service.run_official_preflight_sync(payload)
        self.assertTrue(result["ok"])
        self.assertFalse(result["externalCall"])
        self.assertFalse(result["uploaded"])
        self.assertFalse(result["published"])

        payload["accountAuthModes"] = ["browser"]
        with (
            patch.object(overseas_api_service, "official_status", self._ready_status),
            patch.object(overseas_api_service, "validate_official_account", return_value=True),
        ):
            checked = overseas_api_service.validate_official_preflight_payload(payload)
        self.assertFalse(checked["ok"])
        self.assertTrue(any("混用浏览器" in item for item in checked["errors"]))

    def test_formal_publish_requires_second_confirmation(self) -> None:
        payload = _official_payload(self.video)
        payload.update({"runtimeMode": "publish", "debugDryRun": False})
        with patch.object(
            overseas_api_service,
            "validate_official_account",
            return_value=True,
        ):
            checked = overseas_api_service.validate_official_publish_payload(payload)
        self.assertFalse(checked["ok"])
        self.assertTrue(any("上传确认" in item for item in checked["errors"]))
        payload["officialApiConfirmed"] = True
        with patch.object(
            overseas_api_service,
            "validate_official_account",
            return_value=True,
        ):
            checked = overseas_api_service.validate_official_publish_payload(payload)
        self.assertTrue(checked["ok"])

    def test_tiktok_public_direct_post_stays_closed(self) -> None:
        payload = _official_payload(self.video, platform_type=6)
        payload.update(
            {
                "runtimeMode": "publish",
                "debugDryRun": False,
                "officialApiConfirmed": True,
            }
        )
        with patch.object(
            overseas_api_service,
            "validate_official_account",
            return_value=True,
        ):
            checked = overseas_api_service.validate_official_publish_payload(payload)
        self.assertFalse(checked["ok"])
        self.assertTrue(any("应用审核" in item for item in checked["errors"]))

    def test_tiktok_inbox_upload_is_not_reported_as_published(self) -> None:
        payload = _official_payload(self.video, platform_type=6)
        payload.update(
            {
                "runtimeMode": "draft",
                "debugDryRun": False,
                "officialApiConfirmed": True,
            }
        )

        class FakeCapability:
            async def upload(self, _request):
                return UploadResult(
                    platform=OverseasPlatform.TIKTOK,
                    accepted=True,
                    operation_reference="publish-id-1",
                    evidence=(
                        EvidenceRecord(
                            stage=EvidenceStage.PUBLICATION,
                            verified=True,
                            message="SEND_TO_USER_INBOX",
                            recorded_at=datetime.now(timezone.utc),
                        ),
                        EvidenceRecord(
                            stage=EvidenceStage.RESULT_READBACK,
                            verified=True,
                            message="SEND_TO_USER_INBOX",
                            recorded_at=datetime.now(timezone.utc),
                        ),
                    ),
                )

        with (
            patch.object(overseas_api_service, "official_status", self._ready_status),
            patch.object(overseas_api_service, "validate_official_account", return_value=True),
            patch.object(overseas_api_service, "TikTokCapability", return_value=FakeCapability()),
        ):
            result = overseas_api_service.run_official_inbox_upload_sync(payload)
        self.assertTrue(result["ok"])
        self.assertTrue(result["uploadedToInbox"])
        self.assertFalse(result["published"])

    def test_tiktok_processing_state_is_not_reported_as_inbox_ready(self) -> None:
        payload = _official_payload(self.video, platform_type=6)
        payload.update(
            {
                "runtimeMode": "draft",
                "debugDryRun": False,
                "officialApiConfirmed": True,
            }
        )

        class FakeCapability:
            async def upload(self, _request):
                return UploadResult(
                    platform=OverseasPlatform.TIKTOK,
                    accepted=True,
                    operation_reference="publish-id-processing",
                    evidence=(
                        EvidenceRecord(
                            stage=EvidenceStage.PUBLICATION,
                            verified=False,
                            message="PROCESSING_UPLOAD",
                            recorded_at=datetime.now(timezone.utc),
                        ),
                    ),
                )

        with (
            patch.object(overseas_api_service, "official_status", self._ready_status),
            patch.object(overseas_api_service, "validate_official_account", return_value=True),
            patch.object(overseas_api_service, "TikTokCapability", return_value=FakeCapability()),
        ):
            result = overseas_api_service.run_official_inbox_upload_sync(payload)
        self.assertFalse(result["ok"])
        self.assertFalse(result["uploadedToInbox"])
        self.assertTrue(result["processing"])
        self.assertFalse(result["published"])

    def test_status_does_not_expose_private_paths(self) -> None:
        class FakeStore:
            def status(self):
                return {
                    "clientConfigured": True,
                    "tokenConfigured": False,
                    "clientConfigPath": "/private/client.json",
                    "tokenPath": "/private/token.json",
                }

        with patch.object(overseas_api_service, "YouTubeCredentialStore", FakeStore):
            status = overseas_api_service.official_status(7)
        self.assertNotIn("clientConfigPath", status)
        self.assertNotIn("tokenPath", status)

    def test_official_publish_forwards_platform_specific_options(self) -> None:
        payload = _official_payload(self.video, platform_type=8)
        payload.update(
            {
                "runtimeMode": "publish",
                "debugDryRun": False,
                "officialApiConfirmed": True,
                "shareToFeed": False,
                "notifySubscribers": False,
            }
        )
        captured: list[UploadRequest] = []

        class FakeCapability:
            async def upload(self, request):
                captured.append(request)
                return UploadResult(
                    platform=OverseasPlatform.INSTAGRAM,
                    accepted=True,
                    operation_reference="media:account-1:media-1",
                )

        with (
            patch.object(
                overseas_api_service,
                "validate_official_publish_payload",
                return_value={
                    "ok": True,
                    "errors": [],
                    "platformType": 8,
                    "accountReferences": ["account-1"],
                    "files": [self.video],
                },
            ),
            patch.object(
                overseas_api_service,
                "InstagramCapability",
                return_value=FakeCapability(),
            ),
        ):
            result = overseas_api_service.run_official_publish_sync(payload)

        self.assertTrue(result["ok"])
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].share_to_feed)
        self.assertFalse(captured[0].notify_subscribers)


class OfficialApiFieldMappingTests(unittest.TestCase):
    def test_youtube_description_limit_uses_utf8_bytes(self) -> None:
        request = UploadRequest(
            account_reference="channel-1",
            video_path=Path("video.mp4"),
            title="标题",
            description="中" * 1667,
        )
        with self.assertRaisesRegex(ValueError, "5000 字节"):
            YouTubeApiClient._upload_resource(request, category_id="22")

    def test_meta_caption_is_rejected_instead_of_truncated(self) -> None:
        request = UploadRequest(
            account_reference="ig-1",
            video_path=Path("video.mp4"),
            title="标题",
            description="a" * 2200,
        )
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            request = UploadRequest(
                account_reference="ig-1",
                video_path=video,
                title=request.title,
                description=request.description,
            )
            asset = MetaPageAsset(
                page_id="page-1",
                page_name="主页",
                page_access_token="token-for-offline-test",
                instagram_account_id="ig-1",
                instagram_username="offline",
            )
            client = MetaApiClient.__new__(MetaApiClient)
            with self.assertRaisesRegex(ValueError, "不会静默截断"):
                client.create_instagram_reel(asset, request)

    def test_youtube_upload_session_forwards_notification_choice(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            request = UploadRequest(
                account_reference="channel-1",
                video_path=video,
                title="标题",
                notify_subscribers=False,
            )
            client = YouTubeApiClient.__new__(YouTubeApiClient)
            response = MagicMock()
            response.status_code = 200
            response.headers = {
                "Location": (
                    "https://www.googleapis.com/upload/youtube/v3/videos"
                    "?upload_id=offline"
                )
            }
            client._request = MagicMock(return_value=response)

            client._start_upload_session(request, category_id="22")

        call = client._request.call_args
        self.assertEqual(call.kwargs["params"]["notifySubscribers"], "false")

    def test_instagram_container_forwards_share_to_feed_choice(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            request = UploadRequest(
                account_reference="ig-1",
                video_path=video,
                title="标题",
                share_to_feed=False,
            )
            asset = MetaPageAsset(
                page_id="page-1",
                page_name="主页",
                page_access_token="token-for-offline-test",
                instagram_account_id="ig-1",
                instagram_username="offline",
            )
            client = MetaApiClient.__new__(MetaApiClient)
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {"id": "container-1"}
            client._request = MagicMock(return_value=response)

            container_id = client.create_instagram_reel(asset, request)

        self.assertEqual(container_id, "container-1")
        call = client._request.call_args
        self.assertEqual(call.kwargs["data"]["share_to_feed"], "false")

    def test_youtube_metadata_and_exact_schedule_are_read_back(self) -> None:
        scheduled_at = datetime(2026, 8, 8, 10, 30, tzinfo=timezone.utc)
        request = UploadRequest(
            account_reference="channel-1",
            video_path=Path("video.mp4"),
            title="标题",
            description="描述",
            tags=("oneclick", "AI"),
            mode=PublicationMode.SCHEDULED,
            scheduled_at=scheduled_at,
            made_for_kids=True,
            ai_generated=True,
        )
        video = {
            "snippet": {
                "title": "标题",
                "description": "描述",
                "tags": ["AI", "oneclick"],
            },
            "status": {
                "selfDeclaredMadeForKids": True,
                "containsSyntheticMedia": True,
                "publishAt": "2026-08-08T10:30:00Z",
            },
        }
        matched, message = _metadata_readback(video, request)
        self.assertTrue(matched)
        self.assertIn("已逐项回读", message)

        video["status"]["publishAt"] = "2026-08-08T10:31:00Z"
        matched, message = _metadata_readback(video, request)
        self.assertFalse(matched)
        self.assertIn("定时时间", message)

    def test_instagram_and_facebook_fields_require_exact_readback(self) -> None:
        request = UploadRequest(
            account_reference="meta-1",
            video_path=Path("video.mp4"),
            title="标题",
            description="描述",
            tags=("oneclick",),
            ai_generated=True,
        )
        instagram_matched, _ = _instagram_fields_match(
            {
                "media_product_type": "REELS",
                "caption": "标题\n\n描述\n\n#oneclick",
                "is_ai_generated": True,
            },
            request,
        )
        self.assertTrue(instagram_matched)
        facebook_matched, message = _facebook_fields_match(
            {
                "title": "标题",
                "description": "标题\n\n错误描述\n\n#oneclick",
            },
            request,
        )
        self.assertFalse(facebook_matched)
        self.assertIn("描述", message)

    def test_facebook_waits_for_terminal_publish_state(self) -> None:
        client = MetaApiClient.__new__(MetaApiClient)
        client.sleep = MagicMock()
        client.read_facebook_reel = MagicMock(
            side_effect=[
                {
                    "id": "video-1",
                    "status": {
                        "publishing_phase": {"status": "not_started"}
                    },
                },
                {
                    "id": "video-1",
                    "status": {"publishing_phase": {"status": "complete"}},
                },
            ]
        )
        asset = MetaPageAsset(
            page_id="page-1",
            page_name="主页",
            page_access_token="offline-token",
        )
        result = client.wait_facebook_reel(
            "video-1",
            asset,
            attempts=2,
            interval_seconds=0,
        )
        self.assertEqual(
            result["status"]["publishing_phase"]["status"],
            "complete",
        )
        self.assertEqual(client.read_facebook_reel.call_count, 2)


class OfficialUiGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_confirmation_requires_explicit_checkbox(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            payload = _official_payload(Path(raw) / "video.mp4")
            dialog = OfficialApiActionConfirmDialog([payload])
            self.addCleanup(dialog.close)
            buttons = dialog.findChild(QDialogButtonBox)
            confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
            self.assertFalse(confirm.isEnabled())
            dialog.acknowledgement.setChecked(True)
            self.assertTrue(confirm.isEnabled())

    def test_authorization_dialog_construction_is_offline(self) -> None:
        with patch.object(
            overseas_api_service,
            "official_status",
            return_value={
                "clientConfigured": False,
                "tokenConfigured": False,
                "accessTokenValid": False,
                "scopeGranted": False,
            },
        ) as status:
            dialog = OverseasAuthorizationDialog()
            self.addCleanup(dialog.close)
        status.assert_called()
        self.assertFalse(dialog.success)

    def test_platform_specific_options_are_visible_and_saved(self) -> None:
        page = PublishPage()
        self.addCleanup(page.close)
        self.assertIsNotNone(page.youtube_made_for_kids)
        self.assertIsNotNone(page.youtube_notify_subscribers)
        self.assertIsNotNone(page.instagram_share_to_feed)
        self.assertFalse(page.youtube_made_for_kids.isChecked())
        self.assertTrue(page.youtube_notify_subscribers.isChecked())
        self.assertTrue(page.instagram_share_to_feed.isChecked())

        page.youtube_made_for_kids.setChecked(True)
        page.youtube_notify_subscribers.setChecked(False)
        page.instagram_share_to_feed.setChecked(False)
        template = page.payload_for_template()
        self.assertTrue(template["youtubeMadeForKids"])
        self.assertFalse(template["youtubeNotifySubscribers"])
        self.assertFalse(template["instagramShareToFeed"])

        page._apply_content_payload(
            {
                "youtubeMadeForKids": False,
                "youtubeNotifySubscribers": True,
                "instagramShareToFeed": True,
            }
        )
        self.assertFalse(page.youtube_made_for_kids.isChecked())
        self.assertTrue(page.youtube_notify_subscribers.isChecked())
        self.assertTrue(page.instagram_share_to_feed.isChecked())


if __name__ == "__main__":
    unittest.main()
