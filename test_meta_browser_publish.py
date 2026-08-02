# -*- coding: utf-8 -*-
"""Meta 浏览器确认式发布的离线安全回归。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialogButtonBox

from app_core import overseas_browser_publish
from app_core.overseas.meta.browser_policy import (
    META_BROWSER_AUTOMATION_ACKNOWLEDGED,
    META_BROWSER_PUBLISH_CONFIRMED,
    browser_publish_confirmation_valid,
)
from ui.publish_page import MetaBrowserPublishConfirmDialog
from uploader.meta_uploader.main import (
    FORMAL_LOCK_MESSAGE,
    MetaReelVideo,
    meta_publish_success_signal,
    meta_security_intervention_reason,
)


class MetaBrowserPolicyTests(unittest.TestCase):
    def test_two_independent_flags_are_required(self) -> None:
        self.assertFalse(browser_publish_confirmation_valid({}))
        self.assertFalse(
            browser_publish_confirmation_valid(
                {META_BROWSER_PUBLISH_CONFIRMED: True}
            )
        )
        self.assertTrue(
            browser_publish_confirmation_valid(
                {
                    META_BROWSER_PUBLISH_CONFIRMED: True,
                    META_BROWSER_AUTOMATION_ACKNOWLEDGED: True,
                }
            )
        )

    def test_security_intervention_and_success_are_specific(self) -> None:
        self.assertEqual(
            meta_security_intervention_reason(
                "https://www.facebook.com/checkpoint/",
                "",
            ),
            "Meta 要求完成账号安全检查",
        )
        self.assertIsNone(
            meta_security_intervention_reason(
                "https://business.facebook.com/latest/composer/",
                "Create reel",
            )
        )
        self.assertIsNotNone(
            meta_publish_success_signal(
                url="https://business.facebook.com/latest/composer/",
                feedback_text="Your reel was published",
                scheduled=False,
            )
        )
        self.assertIsNone(
            meta_publish_success_signal(
                url="https://business.facebook.com/latest/composer/",
                feedback_text="Publishing…",
                scheduled=False,
            )
        )


class MetaBrowserServiceTests(unittest.TestCase):
    def _payload(self, video: Path, account_file: str) -> dict:
        return {
            "type": 8,
            "contentType": "video",
            "runtimeMode": "publish",
            "debugDryRun": False,
            "title": "Meta 测试",
            "description": "离线验证",
            "tags": ["oneclick"],
            "fileList": [str(video)],
            "accountList": [account_file],
            "accountAuthModes": ["browser"],
            "visibility": "public",
            "enableTimer": False,
            META_BROWSER_PUBLISH_CONFIRMED: True,
            META_BROWSER_AUTOMATION_ACKNOWLEDGED: True,
        }

    def test_validation_blocks_missing_confirmation_before_browser(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            account = root / "meta.json"
            account.write_text("{}", encoding="utf-8")
            payload = self._payload(video, account.name)
            payload[META_BROWSER_PUBLISH_CONFIRMED] = False
            with patch.object(overseas_browser_publish, "COOKIE_DIR", root):
                checked = overseas_browser_publish.validate_meta_browser_publish_payload(
                    payload
                )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("两项独立确认" in item for item in checked["errors"]))

    def test_verified_wrapper_requires_platform_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            account = root / "meta.json"
            account.write_text("{}", encoding="utf-8")
            payload = self._payload(video, account.name)
            with (
                patch.object(overseas_browser_publish, "COOKIE_DIR", root),
                patch.dict(
                    overseas_browser_publish.HANDLERS,
                    {8: lambda *_args, **_kwargs: [
                        {
                            "status": "published",
                            "evidence": "platform_feedback:published",
                        }
                    ]},
                ),
            ):
                result = overseas_browser_publish.run_meta_browser_publish_sync(
                    payload
                )
        self.assertTrue(result["ok"])
        self.assertTrue(result["published"])
        self.assertFalse(result["scheduled"])

    def test_empty_receipt_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            account = root / "meta.json"
            account.write_text("{}", encoding="utf-8")
            payload = self._payload(video, account.name)
            with (
                patch.object(overseas_browser_publish, "COOKIE_DIR", root),
                patch.dict(
                    overseas_browser_publish.HANDLERS,
                    {8: lambda *_args, **_kwargs: [None]},
                ),
            ):
                with self.assertRaisesRegex(
                    overseas_browser_publish.OverseasBrowserPublishError,
                    "平台成功回执",
                ):
                    overseas_browser_publish.run_meta_browser_publish_sync(payload)


class MetaBrowserUploaderTests(unittest.TestCase):
    def test_formal_upload_is_locked_without_confirmation(self) -> None:
        app = MetaReelVideo(
            "测试",
            "/not/used.mp4",
            [],
            "/not/used.json",
            target_platform="instagram",
            dry_run=False,
        )
        with self.assertRaisesRegex(RuntimeError, FORMAL_LOCK_MESSAGE):
            asyncio.run(app.upload(object()))

    def test_final_button_click_requires_readback_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            app = MetaReelVideo(
                "测试",
                str(video),
                [],
                str(Path(raw) / "meta.json"),
                target_platform="instagram",
                dry_run=False,
                publish_confirmed=True,
                automation_acknowledged=True,
            )
            page = AsyncMock()
            page.url = "https://business.facebook.com/latest/composer/"
            action = AsyncMock()
            app._wait_for_manual_intervention = AsyncMock(return_value=None)
            app._wait_for_action_button = AsyncMock(return_value=action)
            app._wait_for_publish_result = AsyncMock(
                return_value="platform_feedback:published successfully"
            )
            result = asyncio.run(app._publish_formally(page))
        action.click.assert_awaited_once()
        self.assertEqual(result["status"], "published")

    def test_main_returns_verified_upload_result(self) -> None:
        app = MetaReelVideo(
            "测试",
            "/not/used.mp4",
            [],
            "/not/used.json",
            target_platform="facebook",
            dry_run=False,
            publish_confirmed=True,
            automation_acknowledged=True,
        )
        app.upload = AsyncMock(
            return_value={
                "status": "published",
                "evidence": "platform_feedback:published",
            }
        )
        result = asyncio.run(app.main())
        self.assertEqual(result["status"], "published")


class MetaBrowserDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_confirm_button_requires_checkbox(self) -> None:
        dialog = MetaBrowserPublishConfirmDialog()
        self.addCleanup(dialog.close)
        buttons = dialog.findChild(QDialogButtonBox)
        confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.assertFalse(confirm.isEnabled())
        dialog.acknowledgement.setChecked(True)
        self.assertTrue(confirm.isEnabled())


if __name__ == "__main__":
    unittest.main()
