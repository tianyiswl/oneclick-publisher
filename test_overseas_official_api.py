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
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialogButtonBox

from app_core import account_browser_service, account_service, overseas_api_service
from app_core.overseas import (
    EvidenceRecord,
    EvidenceStage,
    OverseasPlatform,
    UploadResult,
)
from ui.overseas_authorization_dialog import OverseasAuthorizationDialog
from ui.publish_page import OfficialApiActionConfirmDialog


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


if __name__ == "__main__":
    unittest.main()
