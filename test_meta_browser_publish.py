# -*- coding: utf-8 -*-
"""Meta 浏览器确认式发布的离线安全回归。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialogButtonBox

from app_core import overseas_browser_publish
from app_core.overseas_meta_errors import FacebookPagePublishError
from app_core.meta_browser_policy import (
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
from uploader.meta_uploader.page_form import FacebookPageFormSnapshot


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

    def test_page_security_wait_emits_shared_execution_lifecycle(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        class Page:
            url = "https://www.facebook.com/checkpoint/"

        app = MetaReelVideo(
            "",
            "video.mp4",
            [],
            "facebook-page.json",
            target_platform="facebook",
            facebook_expected_page_id="1001",
            execution_progress=lambda stage, receipt: events.append(
                (stage, dict(receipt))
            ),
        )

        async def exercise() -> None:
            reasons = iter(("Meta 要求完成账号安全检查", None))
            with (
                patch(
                    "uploader.meta_uploader.main.meta_security_intervention_reason",
                    side_effect=lambda *_args: next(reasons),
                ),
                patch("uploader.meta_uploader.main._body_text", new=AsyncMock(return_value="")),
                patch("uploader.meta_uploader.main.reveal_page_window", new=AsyncMock()),
                patch("uploader.meta_uploader.main.asyncio.sleep", new=AsyncMock()),
            ):
                await app._wait_for_manual_intervention(Page())

        asyncio.run(exercise())

        self.assertEqual(
            [stage for stage, _receipt in events],
            [
                "waiting_user_verification",
                "verification_heartbeat",
                "verification_resolved",
            ],
        )
        self.assertTrue(
            all(receipt == {"pageId": "1001"} for _stage, receipt in events)
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

    def test_validation_blocks_options_the_browser_cannot_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            account = root / "meta.json"
            account.write_text("{}", encoding="utf-8")
            payload = self._payload(video, account.name)
            payload.update({"shareToFeed": False, "aiGenerated": True})
            with patch.object(overseas_browser_publish, "COOKIE_DIR", root):
                checked = overseas_browser_publish.validate_meta_browser_publish_payload(
                    payload
                )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("仅 Reels" in item for item in checked["errors"]))
        self.assertTrue(any("AI 声明" in item for item in checked["errors"]))

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
            target_platform="instagram",
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

    def test_facebook_type9_preflight_and_formal_share_form_adapter_only(self) -> None:
        class ExplodingBoolean:
            def __bool__(self):
                raise AssertionError("type 9 must not read legacy confirmation flags")

        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "clip.mp4"
            video.write_bytes(b"video")
            expected_hash = hashlib.sha256(video.read_bytes()).hexdigest()
            snapshot = FacebookPageFormSnapshot(
                page_id="1001",
                content_kind="reel",
                video_name="clip.mp4",
                video_count=1,
                caption="正文 #标签",
                visibility="public",
                final_action_label="Publish",
                final_action_ready=True,
            )
            for dry_run in (True, False):
                with self.subTest(dry_run=dry_run):
                    page = AsyncMock()
                    page.url = "https://business.facebook.com/latest/home/"
                    form = AsyncMock()
                    form.fill_and_readback.return_value = snapshot
                    app = MetaReelVideo(
                        "ignored legacy title",
                        str(video),
                        ["ignored"],
                        str(Path(raw) / "meta.json"),
                        target_platform="facebook",
                        description="ignored legacy description",
                        dry_run=dry_run,
                        publish_confirmed=ExplodingBoolean(),
                        automation_acknowledged=ExplodingBoolean(),
                        facebook_expected_page_id="1001",
                        facebook_video_sha256=expected_hash,
                        facebook_final_caption="正文 #标签",
                    )
                    app.external_page = page
                    app.external_context = AsyncMock()
                    app.external_browser = object()
                    app._publish_formally = AsyncMock(
                        side_effect=AssertionError(
                            "type 9 must not use the generic final-click path"
                        )
                    )
                    app._set_destination = AsyncMock(
                        side_effect=AssertionError(
                            "type 9 must not use generic destination text"
                        )
                    )
                    with (
                        patch(
                            "uploader.meta_uploader.main.FacebookPageFormAdapter",
                            return_value=form,
                        ),
                        patch(
                            "uploader.meta_uploader.main.reveal_page_window",
                            new=AsyncMock(),
                        ),
                        patch(
                            "uploader.meta_uploader.main.meta_publish_success_signal",
                            side_effect=AssertionError(
                                "type 9 must not use generic success text or routes"
                            ),
                        ),
                        patch("uploader.meta_uploader.main.meta_logger.success"),
                    ):
                        result = asyncio.run(app.upload(object()))
                    self.assertEqual(result, snapshot)
                    form.fill_and_readback.assert_awaited_once()
                    expectation = form.fill_and_readback.await_args.args[0]
                    self.assertEqual(expectation.page_id, "1001")
                    self.assertEqual(expectation.content_kind, "reel")
                    self.assertEqual(expectation.video_name, str(video))
                    self.assertEqual(expectation.video_sha256, expected_hash)
                    self.assertEqual(expectation.caption, "正文 #标签")
                    app._publish_formally.assert_not_awaited()
                    app._set_destination.assert_not_awaited()

    def test_facebook_type9_generic_route_fails_before_browser(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "clip.mp4"
            video.write_bytes(b"video")
            app = MetaReelVideo(
                "legacy title",
                str(video),
                [],
                str(Path(raw) / "meta.json"),
                target_platform="facebook",
                dry_run=False,
                publish_confirmed=True,
                automation_acknowledged=True,
            )
            with patch(
                "uploader.meta_uploader.main.launch_publish_browser",
                side_effect=AssertionError("generic type 9 must stop before browser"),
            ):
                with self.assertRaises(FacebookPagePublishError) as raised:
                    asyncio.run(app.upload(object()))
        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_form_readback_failed",
        )

    def test_facebook_type9_does_not_persist_session_after_form_readback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "clip.mp4"
            video.write_bytes(b"video")
            snapshot = FacebookPageFormSnapshot(
                page_id="1001",
                content_kind="reel",
                video_name="clip.mp4",
                video_count=1,
                caption="正文",
                visibility="public",
                final_action_label="Publish",
                final_action_ready=True,
            )
            app = MetaReelVideo(
                "ignored",
                str(video),
                [],
                str(Path(raw) / "meta.json"),
                target_platform="facebook",
                dry_run=True,
                dry_run_hold_browser=True,
                facebook_expected_page_id="1001",
                facebook_video_sha256=hashlib.sha256(
                    video.read_bytes()
                ).hexdigest(),
                facebook_final_caption="正文",
            )
            browser = AsyncMock()
            context = AsyncMock()
            page = AsyncMock()
            page.url = "https://business.facebook.com/latest/home/"
            context.new_page.return_value = page
            form = AsyncMock()
            form.fill_and_readback.return_value = snapshot
            with (
                patch(
                    "uploader.meta_uploader.main.launch_publish_browser",
                    new=AsyncMock(return_value=browser),
                ),
                patch(
                    "uploader.meta_uploader.main.new_publish_context",
                    new=AsyncMock(return_value=context),
                ),
                patch(
                    "uploader.meta_uploader.main.set_init_script",
                    new=AsyncMock(return_value=context),
                ),
                patch(
                    "uploader.meta_uploader.main.reveal_page_window",
                    new=AsyncMock(),
                ),
                patch(
                    "uploader.meta_uploader.main.FacebookPageFormAdapter",
                    return_value=form,
                ),
                patch(
                    "uploader.meta_uploader.main.keep_browser_open_for_dry_run",
                    new=AsyncMock(
                        side_effect=AssertionError(
                            "type 9 must not persist or hold the session"
                        )
                    ),
                ),
                patch(
                    "uploader.meta_uploader.main.save_context_storage_state",
                    new=AsyncMock(
                        side_effect=AssertionError(
                            "type 9 must not write session state"
                        )
                    ),
                ),
                patch("uploader.meta_uploader.main.meta_logger.success"),
            ):
                result = asyncio.run(app.upload(object()))
        self.assertEqual(result, snapshot)
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()


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
