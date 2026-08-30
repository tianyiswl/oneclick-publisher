import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QDate, QTime, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QWidget,
)

from app_core import task_service
from app_core.content_project_gateway import ContentProjectGateway, PublishProfileStore
from app_core.controlled_publish import (
    build_controlled_payloads,
    facebook_replay_fingerprint,
)
from app_core.wechat_verification import verification_broker
from test_controlled_publish_process import FacebookPagePublicEntryFixture
from ui.publish_page import PublishPage


class PublishPageWechatDraftQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dispose_page(self, page: PublishPage) -> None:
        page.wechat_draft_queue_timer.stop()
        page.task_timer.stop()
        page.close()
        page.deleteLater()
        self.app.processEvents()

    def test_wechat_draft_queue_is_explicit_and_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = PublishPage()
            page.configure_wechat_draft_queue(Path(directory))

            self.assertEqual(
                page.wechat_draft_queue_enabled.text(),
                "兼容通道：硅基进化公众号只保存草稿（不会发表）",
            )
            self.assertEqual(
                page.content_project_gateway_status.text(),
                "内容项目主通道：本机受控接口（默认预检）",
            )
            self.assertFalse(page.wechat_draft_queue_enabled.isChecked())
            self.assertFalse(page.wechat_draft_queue_timer.isActive())

            with (
                patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch.object(page, "_poll_wechat_draft_queue"),
            ):
                page.wechat_draft_queue_enabled.setChecked(True)
            self.app.processEvents()
            self.assertTrue(page.wechat_draft_queue_timer.isActive())

            page.wechat_draft_queue_enabled.setChecked(False)
            self.assertFalse(page.wechat_draft_queue_timer.isActive())
            self._dispose_page(page)

    def test_compatibility_gateway_bar_is_hidden_from_publish_center(self) -> None:
        page = PublishPage()

        gateway_bar = page.content_project_gateway_status.parentWidget()

        self.assertIsNotNone(gateway_bar)
        self.assertTrue(gateway_bar.isHidden())
        self._dispose_page(page)

    def test_running_draft_queue_surfaces_pending_native_wechat_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = PublishPage()
            page.configure_wechat_draft_queue(Path(directory))
            page.wechat_draft_queue_enabled.blockSignals(True)
            page.wechat_draft_queue_enabled.setChecked(True)
            page.wechat_draft_queue_enabled.blockSignals(False)
            with (
                patch.object(page.wechat_draft_queue_tasks, "is_running", return_value=True),
                patch.object(verification_broker, "pending_task_ids", return_value=(41,)),
                patch.object(page, "_show_wechat_verification_for_task") as show,
            ):
                page._poll_wechat_draft_queue()
            show.assert_called_once_with(41)
            self._dispose_page(page)


class PublishPageFacebookControlledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._facebook_feature = patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
            clear=False,
        )
        self._facebook_feature.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.video = Path(self.temporary.name) / "facebook.mp4"
        self.video.write_bytes(b"facebook-page-ui-video")

    def tearDown(self) -> None:
        self.temporary.cleanup()
        self._facebook_feature.stop()

    def _dispose_page(self, page: PublishPage) -> None:
        page.wechat_draft_queue_timer.stop()
        page.task_timer.stop()
        page.close()
        page.deleteLater()
        self.app.processEvents()

    def _wait_for_background_task(
        self,
        page: PublishPage,
        key: str,
        *,
        timeout: float = 1.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while page.account_health_tasks.is_running(key) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()

    @staticmethod
    def _account() -> dict:
        return {
            "id": 91,
            "type": 9,
            "platformName": "Facebook Reels",
            "filePath": "facebook-page.json",
            "profileName": "品牌主体",
            "userName": "Saved Facebook Page",
            "statusText": "正常",
            "healthStatus": "normal",
            "status": 1,
            "authMode": "browser",
            "accountReference": "1000000000006789",
            "remark": "",
        }

    def _payload(self, *, enable_timer: bool = False) -> dict:
        return {
            "type": 9,
            "contentType": "video",
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "debugDryRunHoldBrowser": False,
            "backgroundMode": False,
            "title": "Facebook Page 标题",
            "description": "Facebook Page 正文",
            "tags": ["OneClick"],
            "fileList": [str(self.video)],
            "accountList": ["facebook-page.json"],
            "accountIds": [91],
            "coverPath": "",
            "coverPaths": {},
            "visibility": "public",
            "enableTimer": enable_timer,
            "scheduleTime": "2026-09-01 09:00" if enable_timer else None,
            "originalDeclaration": False,
            "aiGenerated": False,
        }

    def test_facebook_ui_preflight_uses_standard_controlled_payload(self) -> None:
        page = PublishPage()
        page._account_rows = [self._account()]
        payloads = [self._payload()]

        with patch(
            "ui.publish_page.facebook_page_v1_enabled",
            return_value=True,
        ):
            page._prepare_facebook_page_payloads(payloads, "preflight")

        payload = payloads[0]
        self.assertIs(payload["facebookControlledPublish"], True)
        self.assertEqual(
            payload["facebookExpectedPageReference"],
            "1000000000006789",
        )
        self.assertEqual(
            payload["facebookFinalCaption"],
            "Facebook Page 标题\n\nFacebook Page 正文\n\n#OneClick",
        )
        for key in (
            "facebookCaptionSha256",
            "facebookVideoSha256",
            "facebookManifestIntentSha256",
        ):
            self.assertRegex(payload[key], r"[0-9a-f]{64}\Z")
        self.assertNotIn("metaBrowserPublishConfirmed", payload)
        self.assertNotIn("metaBrowserAutomationAcknowledged", payload)
        self._dispose_page(page)

    def test_ui_manifest_and_gateway_equivalent_outcomes_share_replay_fingerprint(self) -> None:
        root = Path(self.temporary.name)
        manifest = root / "manifest.json"
        (root / "body.md").write_text("Common body", encoding="utf-8")
        (root / "cover.png").write_bytes(b"required-source-cover")
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": "oneclick-content/v1",
                    "contentType": "video",
                    "title": "Common title",
                    "bodyFile": "body.md",
                    "tags": ["common"],
                    "assets": [self.video.name],
                    "covers": {"3:4": "cover.png"},
                    "preferredPlatforms": ["Facebook"],
                    "platformOverrides": {
                        "Facebook": {
                            "title": "Facebook Page 标题",
                            "body": "Facebook Page 正文",
                            "tags": ["OneClick"],
                        }
                    },
                    "aiDisclosure": {
                        "containsAiGeneratedContent": False,
                        "contentKinds": [],
                        "assetPaths": [],
                        "allowPlatformAutoDeclaration": False,
                    },
                    "debugDryRun": True,
                    "publishAllowed": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        account = self._account()
        request = {
            "projectId": "manifest-source",
            "manifestPath": str(manifest),
            "mode": "preflight",
            "targets": [
                {
                    "platform": "Facebook",
                    "accountId": 91,
                    "schedule": None,
                    "settings": {"visibility": "public"},
                }
            ],
        }
        manifest_payload = build_controlled_payloads(
            request,
            accounts=[account],
        )[0]

        gateway = ContentProjectGateway(
            profile_store=PublishProfileStore(root / "profiles.json"),
            accounts_provider=lambda: [account],
            submitter=lambda gateway_request: build_controlled_payloads(
                gateway_request,
                accounts=[account],
            )[0],
            runtime_conflict_checker=lambda: False,
            facebook_feature_enabled=lambda: True,
        )
        gateway.save_profile(
            "gateway-source",
            "Facebook Page",
            [{"platform": "Facebook Reels", "accountId": 91}],
        )
        gateway_payload = gateway.preflight_content(
            "gateway-source",
            str(manifest),
            settings={"Facebook Reels": {"visibility": "public"}},
        )

        page = PublishPage()
        page._account_rows = [account]
        ui_payload = {
            **self._payload(),
            "title": "Facebook Page 标题\u00a0  ",
            "description": "Facebook\u00a0Page 正文  \r\n",
            "tags": ["#OneClick", "OneClick"],
            "controlledManifestPath": "/another/ui/source.json",
            "contentProjectId": "ui-source",
        }
        with patch("ui.publish_page.facebook_page_v1_enabled", return_value=True):
            page._prepare_facebook_page_payloads([ui_payload], "preflight")

        fingerprints = {
            facebook_replay_fingerprint([manifest_payload]),
            facebook_replay_fingerprint([gateway_payload]),
            facebook_replay_fingerprint([ui_payload]),
        }
        self.assertEqual(len(fingerprints), 1)
        self._dispose_page(page)

    def test_page_selection_disables_unsupported_controls_without_clearing_values(self) -> None:
        page = PublishPage()
        page.cover_34.addItem("selected-cover.png", "/tmp/selected-cover.png")
        page.cover_34.setCurrentIndex(page.cover_34.count() - 1)
        page.platform_collections[9].setEditText("Selected collection")
        page.platform_schedule_enabled[9].setChecked(True)
        page.original_declaration.setChecked(True)
        page.ai_generated_content.setChecked(True)
        page.common_visibility.setCurrentIndex(
            page.common_visibility.findData("private")
        )
        page.common_schedule_enabled.setChecked(True)
        page._account_rows = [self._account()]
        page._selected_account_ids = {91}

        page.update_selected_labels()

        notice = page.findChild(QLabel, "facebookPageV1LimitNotice")
        cover_panel = page.findChild(QWidget, "platformCoverPanel9")
        self.assertIsNotNone(notice)
        self.assertIn("仅支持", notice.text())
        self.assertTrue(page.platform_collection_rows[9].isHidden())
        self.assertFalse(page.platform_collections[9].isEnabled())
        self.assertFalse(page.platform_schedule_enabled[9].isEnabled())
        self.assertTrue(cover_panel.isHidden())
        for control in (
            page.common_cover_panel,
            page.original_declaration,
            page.ai_generated_content,
            page.common_visibility,
            page.common_schedule_enabled,
            page.common_schedule_date,
            page.common_schedule_time,
        ):
            self.assertFalse(control.isEnabled())
        self.assertEqual(page.cover_34.currentData(), "/tmp/selected-cover.png")
        self.assertEqual(
            page.platform_collections[9].currentText(),
            "Selected collection",
        )
        self.assertTrue(page.original_declaration.isChecked())
        self.assertTrue(page.ai_generated_content.isChecked())
        self.assertEqual(page.common_visibility.currentData(), "private")
        self.assertTrue(page.common_schedule_enabled.isChecked())

        page._selected_account_ids.clear()
        page.update_selected_labels()
        for control in (
            page.common_cover_panel,
            page.original_declaration,
            page.ai_generated_content,
            page.common_visibility,
            page.common_schedule_enabled,
            page.common_schedule_date,
            page.common_schedule_time,
        ):
            self.assertTrue(control.isEnabled())
        self.assertEqual(page.cover_34.currentData(), "/tmp/selected-cover.png")
        self.assertTrue(page.original_declaration.isChecked())
        self.assertTrue(page.ai_generated_content.isChecked())
        self.assertEqual(page.common_visibility.currentData(), "private")
        self.assertTrue(page.common_schedule_enabled.isChecked())
        self._dispose_page(page)

    def test_facebook_ui_formal_uses_preflight_authorization_not_meta_flags(self) -> None:
        with FacebookPagePublicEntryFixture() as fixture:
            page = PublishPage()
            dialog_events: list[str] = []

            def accept_real_confirmation() -> None:
                dialog = QApplication.activeModalWidget()
                if dialog is None or not hasattr(dialog, "acknowledgement"):
                    dialog_events.append("missing")
                    return
                dialog.acknowledgement.setChecked(True)
                buttons = dialog.findChild(QDialogButtonBox)
                buttons.button(QDialogButtonBox.StandardButton.Ok).click()
                dialog_events.append("accepted")

            QTimer.singleShot(0, accept_real_confirmation)
            task = task_service.get_task(fixture.preflight_task_id)
            with fixture.stop_at_worker_start() as started:
                envelope = page.start_formal_publish_from_task(task)

            self.assertEqual(dialog_events, ["accepted"])
            fixture.assert_real_dispatch(self, envelope, started)
            self.assertEqual(page.active_task_id, envelope["taskId"])
            stored = task_service.get_task(int(envelope["taskId"]))
            payload = json.loads(str(stored["payloadJson"]))[0]
            self.assertNotIn("metaBrowserPublishConfirmed", payload)
            self.assertNotIn("metaBrowserAutomationAcknowledged", payload)
            self._dispose_page(page)

    def test_facebook_formal_cancel_creates_no_authorization(self) -> None:
        page = PublishPage()
        task = {
            "id": 17,
            "payloadJson": json.dumps([self._payload()], ensure_ascii=False),
        }

        with (
            patch.object(page, "confirm_meta_browser_publish", return_value=False),
            patch(
                "ui.publish_page.facebook_page_v1_enabled",
                return_value=True,
            ),
            patch(
                "ui.publish_page.controlled_publish.authorize_completed_check"
            ) as authorize,
            patch(
                "ui.publish_page.controlled_publish_process.submit_authorized_preflight_task"
            ) as submit,
        ):
            page.start_formal_publish_from_task(task)

        authorize.assert_not_called()
        submit.assert_not_called()
        self.assertIsNone(page.active_task_id)
        self._dispose_page(page)

    def test_unsupported_facebook_settings_stop_before_confirmation(self) -> None:
        page = PublishPage()
        page.preflight.setChecked(True)
        page._account_rows = [self._account()]
        cases = (
            ("cover_path", {"coverPath": "/tmp/selected-cover.png"}),
            ("cover_paths", {"coverPaths": {"3:4": "selected-cover.png"}}),
            ("collection", {"collectionName": "Selected collection"}),
            ("schedule", {"enableTimer": True, "scheduleTime": "2026-09-01 09:00"}),
            ("visibility", {"visibility": "private"}),
            ("original", {"originalDeclaration": True}),
            ("ai", {"aiGenerated": True}),
            ("ai_confirmation", {"aiDeclarationExplicitlyConfirmed": True}),
            (
                "ai_disclosure",
                {
                    "aiDisclosure": {
                        "containsAiGeneratedContent": True,
                        "contentKinds": ["video"],
                    }
                },
            ),
        )
        with (
            patch.object(page, "collect_payloads") as collect,
            patch("ui.publish_page.facebook_page_v1_enabled", return_value=True),
            patch("ui.publish_page.PublishConfirmDialog.exec") as confirm,
            patch.object(QMessageBox, "warning") as warning,
        ):
            for label, changes in cases:
                with self.subTest(label=label):
                    collect.return_value = [{**self._payload(), **changes}]
                    page.create_task()
                    confirm.assert_not_called()
                    warning.assert_called_once()
                    self.assertIn(
                        "Facebook Page",
                        str(warning.call_args.args[-1]),
                    )
                    warning.reset_mock()

        self._dispose_page(page)

    def test_stored_page_metadata_rejects_before_confirmation_authorization_or_worker(self) -> None:
        page = PublishPage()
        task = {
            "id": 17,
            "payloadJson": json.dumps(
                [
                    {
                        **self._payload(),
                        "coverPath": "/tmp/legacy-selected-cover.png",
                        "collectionName": "Legacy collection",
                    }
                ],
                ensure_ascii=False,
            ),
        }
        with (
            patch("ui.publish_page.facebook_page_v1_enabled", return_value=True),
            patch.object(page, "confirm_meta_browser_publish") as confirm,
            patch("ui.publish_page.controlled_publish.authorize_completed_check") as authorize,
            patch(
                "ui.publish_page.controlled_publish_process.submit_authorized_preflight_task"
            ) as submit,
            patch.object(QMessageBox, "warning") as warning,
        ):
            page.start_formal_publish_from_task(task)

        confirm.assert_not_called()
        authorize.assert_not_called()
        submit.assert_not_called()
        warning.assert_called_once()
        self.assertIn("Facebook Page", str(warning.call_args.args[-1]))
        self._dispose_page(page)

    def test_page_backend_open_is_non_blocking_deduplicated_and_fails_safely_on_ui_thread(self) -> None:
        page = PublishPage()
        account = self._account()
        entered = threading.Event()
        release = threading.Event()
        callback_threads: list[int] = []
        ui_thread_id = threading.get_ident()

        def blocked_open(_account: dict) -> bool:
            entered.set()
            if not release.wait(1.0):
                raise AssertionError("test did not release Page backend open")
            raise RuntimeError("secret-token-must-not-leak")

        original_error = page._show_facebook_page_backend_open_error

        def record_error(message: str) -> None:
            callback_threads.append(threading.get_ident())
            original_error(message)

        with (
            patch("ui.publish_page.facebook_page_v1_enabled", return_value=True),
            patch(
                "ui.publish_page.account_browser_service.open_account_backend",
                side_effect=blocked_open,
            ) as open_backend,
            patch.object(
                page,
                "_show_facebook_page_backend_open_error",
                side_effect=record_error,
            ),
        ):
            before = time.monotonic()
            page.open_account_backend(account)
            elapsed = time.monotonic() - before
            self.assertLess(elapsed, 0.1)
            self.assertTrue(entered.wait(0.5))

            page.open_account_backend(account)
            self.assertEqual(open_backend.call_count, 1)
            self.assertIn("正在打开", page.account_health_label.text())

            release.set()
            self._wait_for_background_task(
                page,
                "publish_facebook_backend_91",
            )

        self.assertEqual(callback_threads, [ui_thread_id])
        self.assertIn("打开失败", page.account_health_label.text())
        self.assertNotIn("secret-token", page.account_health_label.text())
        self.assertNotIn("secret-token", page.log.toPlainText())
        self._dispose_page(page)

    def test_page_backend_open_success_returns_on_ui_thread(self) -> None:
        page = PublishPage()
        account = self._account()
        worker_threads: list[int] = []
        callback_threads: list[int] = []
        ui_thread_id = threading.get_ident()

        def open_backend(_account: dict) -> bool:
            worker_threads.append(threading.get_ident())
            return True

        original_success = page._finish_facebook_page_backend_open

        def record_success(current_account: dict, reused: object) -> None:
            callback_threads.append(threading.get_ident())
            original_success(current_account, reused)

        with (
            patch("ui.publish_page.facebook_page_v1_enabled", return_value=True),
            patch(
                "ui.publish_page.account_browser_service.open_account_backend",
                side_effect=open_backend,
            ),
            patch.object(
                page,
                "_finish_facebook_page_backend_open",
                side_effect=record_success,
            ),
        ):
            page.open_account_backend(account)
            self._wait_for_background_task(
                page,
                "publish_facebook_backend_91",
            )

        self.assertEqual(callback_threads, [ui_thread_id])
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], ui_thread_id)
        self.assertIn("已切换到现有后台", page.account_health_label.text())
        self._dispose_page(page)

    def test_disabled_page_backend_entry_stops_before_background_work(self) -> None:
        page = PublishPage()
        with (
            patch("ui.publish_page.facebook_page_v1_enabled", return_value=False),
            patch.object(page.account_health_tasks, "run") as run,
            patch(
                "ui.publish_page.account_browser_service.open_account_backend"
            ) as open_backend,
        ):
            page.open_account_backend(self._account())

        run.assert_not_called()
        open_backend.assert_not_called()
        self.assertIn("尚未开启", page.account_health_label.text())
        self._dispose_page(page)

    def test_saved_page_name_and_id_tail_are_visible_but_default_off_hides_entry(self) -> None:
        account = self._account()
        page = PublishPage()
        with (
            patch(
                "ui.publish_page.account_service.list_publishable_accounts",
                return_value=[account],
            ),
            patch(
                "ui.publish_page.facebook_page_v1_enabled",
                return_value=True,
            ),
        ):
            page.refresh_accounts()

        self.assertEqual(page.account_list.count(), 1)
        visible = page.account_list.item(0).text()
        self.assertIn("Saved Facebook Page", visible)
        self.assertIn("6789", visible)

        with (
            patch(
                "ui.publish_page.account_service.list_publishable_accounts",
                return_value=[account],
            ),
            patch(
                "ui.publish_page.facebook_page_v1_enabled",
                return_value=False,
            ),
        ):
            page.refresh_accounts()

        self.assertEqual(page.account_list.count(), 0)
        self.assertEqual(page._account_rows, [])
        self._dispose_page(page)

    def test_waiting_verification_renders_one_action_without_failure(self) -> None:
        page = PublishPage()
        projection = {
            "taskId": 18,
            "status": "running",
            "items": [
                {
                    "platformType": 9,
                    "status": "waiting_user_verification",
                    "errorCode": "",
                    "actionRequired": {
                        "code": "facebook_verification_required",
                        "message": "请在同一可见窗口完成 Facebook 安全验证",
                    },
                }
            ],
        }

        self.assertTrue(page.render_controlled_task_action(projection))
        self.assertTrue(page.render_controlled_task_action(projection))
        self.assertEqual(
            page.task_status_label.text(),
            "需要处理：请在同一可见窗口完成 Facebook 安全验证",
        )
        self.assertNotIn("失败", page.task_status_label.text())
        matching_logs = [
            line
            for line in page.log.toPlainText().splitlines()
            if "Facebook 安全验证" in line
        ]
        self.assertEqual(len(matching_logs), 1)
        self._dispose_page(page)

    def test_parent_waiting_verification_keeps_task_active_and_polling(self) -> None:
        page = PublishPage()
        page.active_task_id = 18
        page.active_task_mode = "preflight"
        page._set_running(True, "running")
        page.task_timer.start()
        task = {
            "id": 18,
            "status": "waiting_user_verification",
            "dryRun": 1,
            "itemCount": 1,
            "successCount": 0,
            "failedCount": 0,
            "events": [],
            "items": [
                {
                    "platformType": 9,
                    "status": "waiting_user_verification",
                    "errorCode": "",
                    "receiptJson": json.dumps(
                        {
                            "phase": "waiting_user_verification",
                            "pageId": "1001",
                            "deadlineAt": "2026-08-30T05:10:00+00:00",
                            "timeoutSeconds": 600,
                        }
                    ),
                }
            ],
        }

        with patch("ui.publish_page.task_service.get_task", return_value=task):
            page.poll_task()

        self.assertEqual(page.active_task_id, 18)
        self.assertTrue(page.task_timer.isActive())
        self.assertNotIn("任务结束", page.log.toPlainText())
        self._dispose_page(page)

    def test_projection_failure_logs_one_safe_diagnostic_and_keeps_polling(self) -> None:
        page = PublishPage()
        page.active_task_id = 77
        task = {
            "id": 77,
            "status": "pending",
            "dryRun": 0,
            "itemCount": 1,
            "successCount": 0,
            "failedCount": 0,
            "events": [],
        }

        with (
            patch("ui.publish_page.task_service.get_task", return_value=task),
            patch(
                "ui.publish_page.douyin_verification_broker.request_for_task",
                return_value=None,
            ),
            patch(
                "ui.publish_page.controlled_publish.project_task",
                side_effect=ValueError("token=must-not-leak"),
            ),
        ):
            page.poll_task()
            page.poll_task()

        diagnostics = [
            line
            for line in page.log.toPlainText().splitlines()
            if "受控任务状态暂时无法安全解析" in line
        ]
        self.assertEqual(len(diagnostics), 1)
        self.assertNotIn("must-not-leak", page.log.toPlainText())
        self.assertEqual(page.active_task_id, 77)
        self._dispose_page(page)


class PublishPageYouTubeSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.video = Path(self.temporary.name) / "youtube.mp4"
        self.video.write_bytes(b"youtube-offline-video")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _dispose_page(self, page: PublishPage) -> None:
        page.wechat_draft_queue_timer.stop()
        page.task_timer.stop()
        page.close()
        page.deleteLater()
        self.app.processEvents()

    @staticmethod
    def _account() -> dict:
        return {
            "id": 71,
            "type": 7,
            "platformName": "YouTube",
            "filePath": "youtube-oauth:test-reference",
            "profileName": "YouTube 测试频道",
            "userName": "YouTube 测试频道",
            "statusText": "正常",
            "healthStatus": "normal",
            "remark": "",
            "authMode": "youtube_oauth",
            "oauthScopeVersion": 2,
            "accountReference": "UC-test-channel",
        }

    def _ready_page(self) -> PublishPage:
        page = PublishPage()
        page.content_type = "video"
        page._account_rows = [self._account()]
        page._selected_account_ids = {71}
        page._media_rows = [
            {
                "id": 1,
                "typeText": "视频",
                "filename": self.video.name,
                "file_path": str(self.video),
                "storedPath": str(self.video),
                "filesize": self.video.stat().st_size,
                "remark": "",
                "mediaCategory": "默认",
            }
        ]
        page._selected_media_ids = {1}
        page.common_title_input.setText("YouTube 测试标题")
        page.title_input.setPlainText("YouTube 测试正文")
        return page

    def test_youtube_defaults_private_and_requires_explicit_audience(self) -> None:
        page = PublishPage()

        self.assertEqual(page.platform_visibility[7].currentData(), "private")
        self.assertIsNone(page.youtube_made_for_kids.currentData())
        self._dispose_page(page)

    def test_youtube_scheduled_public_requires_own_schedule(self) -> None:
        page = self._ready_page()
        visibility = page.platform_visibility[7]
        visibility.setCurrentIndex(visibility.findData("scheduled_public"))
        audience = page.youtube_made_for_kids
        audience.setCurrentIndex(audience.findData(False))

        with self.assertRaisesRegex(
            ValueError, "YouTube 定时公开必须设置发布时间"
        ):
            page.collect_payloads(runtime_mode="publish")
        self._dispose_page(page)

    def test_youtube_formal_publish_requires_explicit_audience(self) -> None:
        page = self._ready_page()

        with self.assertRaisesRegex(
            ValueError, "YouTube 正式发布必须明确选择是否面向儿童"
        ):
            page.collect_payloads(runtime_mode="publish")
        self._dispose_page(page)

    def test_youtube_preflight_allows_audience_to_remain_unselected(self) -> None:
        page = self._ready_page()

        payload = page.collect_payloads(runtime_mode="preflight")[0]

        self.assertIsNone(payload["madeForKids"])
        self.assertTrue(payload["youtubeOfficialApi"])
        self.assertTrue(payload["debugDryRun"])
        self._dispose_page(page)

    def test_youtube_private_does_not_inherit_common_schedule(self) -> None:
        page = self._ready_page()
        page.common_schedule_enabled.setChecked(True)
        page.common_schedule_date.setDate(QDate.currentDate().addDays(1))
        page.common_schedule_time.setTime(QTime(9, 0))
        audience = page.youtube_made_for_kids
        audience.setCurrentIndex(audience.findData(False))

        payload = page.collect_payloads(runtime_mode="publish")[0]

        self.assertEqual(payload["visibility"], "private")
        self.assertFalse(payload["enableTimer"])
        self.assertIsNone(payload["scheduleTime"])
        self.assertTrue(payload["youtubeOfficialApi"])
        self.assertTrue(payload["backgroundMode"])
        self.assertEqual(payload["youtubeExpectedChannelId"], "UC-test-channel")
        self._dispose_page(page)

    def test_refresh_accounts_includes_publishable_oauth_channel(self) -> None:
        account = self._account()
        with (
            patch(
                "ui.publish_page.account_service.list_publishable_accounts",
                return_value=[account],
            ) as publishable,
            patch(
                "ui.publish_page.account_service.list_accounts",
                return_value=[],
            ),
        ):
            page = PublishPage()
            page.refresh_accounts()

        publishable.assert_called()
        self.assertEqual([row["id"] for row in page._account_rows], [71])
        self._dispose_page(page)

    def test_youtube_settings_save_and_restore_without_guessing_audience(self) -> None:
        page = PublishPage()
        visibility = page.platform_visibility[7]
        visibility.setCurrentIndex(visibility.findData("scheduled_public"))
        page.platform_schedule_enabled[7].setChecked(True)
        page.platform_schedule_dates[7].setDate(QDate.currentDate().addDays(2))
        page.platform_schedule_times[7].setTime(QTime(9, 30))
        page.youtube_notify_subscribers.setChecked(False)

        saved = page.payload_for_template()

        restored = PublishPage()
        restored._apply_content_payload(saved)
        self.assertEqual(
            restored.platform_visibility[7].currentData(), "scheduled_public"
        )
        self.assertIsNone(restored.youtube_made_for_kids.currentData())
        self.assertFalse(restored.youtube_notify_subscribers.isChecked())
        self.assertTrue(restored.platform_schedule_enabled[7].isChecked())
        self.assertEqual(
            restored.platform_schedule_times[7].time().toString("HH:mm"),
            "09:30",
        )
        self._dispose_page(restored)
        self._dispose_page(page)

    def test_youtube_summary_names_official_api_and_exact_settings(self) -> None:
        page = self._ready_page()
        visibility = page.platform_visibility[7]
        visibility.setCurrentIndex(visibility.findData("scheduled_public"))
        audience = page.youtube_made_for_kids
        audience.setCurrentIndex(audience.findData(False))
        page.youtube_notify_subscribers.setChecked(False)
        page.platform_schedule_enabled[7].setChecked(True)
        page.platform_schedule_dates[7].setDate(QDate.currentDate().addDays(2))
        page.platform_schedule_times[7].setTime(QTime(9, 30))
        payloads = page.collect_payloads(runtime_mode="publish")

        summary = page.build_publish_summary(payloads, "publish")

        self.assertIn("执行方式：YouTube 官方 API 后台处理", summary)
        self.assertNotIn("浏览器模式", summary)
        self.assertIn("YouTube 执行通道：官方 API 后台处理", summary)
        self.assertIn(
            "YouTube 测试频道 | YouTube | YouTube 测试频道 | 官方 OAuth 频道",
            summary,
        )
        self.assertNotIn("YouTube 测试频道 | 浏览器会话", summary)
        self.assertIn("谁可以看：定时公开", summary)
        self.assertIn("YouTube 受众：不面向儿童", summary)
        self.assertIn("订阅者通知：关闭", summary)
        self.assertIn("发布时间：", summary)
        self._dispose_page(page)

if __name__ == "__main__":
    unittest.main()
