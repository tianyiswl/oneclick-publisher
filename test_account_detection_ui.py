# -*- coding: utf-8 -*-
"""账号检测与启动页导航的本地 UI 契约测试。"""

import os
import importlib.util
import queue
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QInputDialog, QMenu, QMessageBox, QPushButton

from app_core import account_browser_service, account_service, login_service
from app_core.overseas_meta_page_identity import FacebookPageIdentity
from ui.account_page import AccountPage
from ui.login_dialog import LoginDialog
from ui.main_window import MainWindow
from ui.publish_page import PublishPage


class AccountDetectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_normal_session_check_never_opens_browser(self) -> None:
        page = AccountPage()
        with patch.object(
            account_browser_service,
            "open_account_backend",
        ) as open_backend, patch.object(QMessageBox, "warning") as warning:
            shown = page._present_validation_intervention(
                {"interventionRequired": []}
            )
        self.assertFalse(shown)
        open_backend.assert_not_called()
        warning.assert_not_called()
        page.close()

    def test_account_page_only_exposes_browser_binding(self) -> None:
        page = AccountPage()
        labels = {
            button.text() for button in page.findChildren(QPushButton)
        }
        self.assertIn("绑定账号", labels)
        self.assertFalse(any("API" in label for label in labels))
        self.assertFalse(any("开发者配置" in label for label in labels))
        page.close()

    def test_account_page_uses_managed_rows_while_publish_list_stays_separate(self) -> None:
        oauth_account = {
            "id": 71,
            "type": 7,
            "platformName": "YouTube",
            "profileName": "海外主体",
            "userName": "OAuth 测试频道",
            "status": 1,
            "healthStatus": "normal",
            "statusText": "正常",
            "remark": "",
            "authMode": "youtube_oauth",
            "filePath": "youtube-oauth:opaque-reference",
            "accountReference": "UC123",
        }
        with (
            patch.object(
                account_service,
                "list_managed_accounts",
                return_value=[oauth_account],
                create=True,
            ),
            patch.object(account_service, "list_accounts", return_value=[]),
        ):
            page = AccountPage()
            page.refresh()

        self.assertEqual(page.result_label.text(), "1 个账号")
        self.assertEqual(page.row_data(0)["accountReference"], "UC123")
        page.close()

    def test_unbound_facebook_page_projects_rebind_as_red_abnormal(self) -> None:
        projected = account_service._row_to_dict(
            {
                "id": 91,
                "type": 9,
                "filePath": "shared-meta.json",
                "userName": "旧 Facebook Page",
                "status": 1,
                "profileName": "Meta 主体",
                "avatarPath": None,
                "avatarUpdatedAt": None,
                "remark": "",
                "lastCheckedAt": None,
                "lastLoginAt": None,
                "authMode": "browser",
                "accountReference": "",
                "oauthScopeVersion": 1,
            }
        )

        self.assertTrue(projected["needsPageRebind"])
        self.assertEqual(projected["healthStatus"], "abnormal")
        with (
            patch.object(account_service, "list_managed_accounts", return_value=[projected]),
            patch.object(account_service, "list_profiles", return_value=["Meta 主体"]),
        ):
            page = AccountPage()
            page.refresh()

        self.assertEqual(page.row_data(0)["needsPageRebind"], True)
        self.assertEqual(page.table.item(0, 2).foreground().color().name(), "#dc2626")
        buttons = {
            item.text(): item
            for item in page.table.cellWidget(0, 5).findChildren(QPushButton)
        }
        self.assertFalse(buttons["打开后台"].isEnabled())
        page.close()

    def test_facebook_page_selection_prompt_uses_name_and_short_id_tail(self) -> None:
        request = login_service.FacebookPageSelectionRequest(
            (
                FacebookPageIdentity("1234561001", "同名", can_manage_content=True),
                FacebookPageIdentity("1234561002", "同名", can_manage_content=True),
            )
        )
        labels = LoginDialog.facebook_page_selection_labels(request)

        self.assertEqual(labels, ["同名 · …1001", "同名 · …1002"])
        self.assertFalse(any("123456" in label for label in labels))

    def test_facebook_page_login_entry_is_hidden_by_default_and_exposed_only_by_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            default_dialog = LoginDialog()
        self.assertLess(default_dialog.platform_combo.findData(9), 0)
        default_dialog.close()

        with patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
            clear=True,
        ):
            enabled_dialog = LoginDialog()
        self.assertGreaterEqual(enabled_dialog.platform_combo.findData(9), 0)
        enabled_dialog.close()

    def test_facebook_page_business_access_denied_shows_page_permission_guidance(self) -> None:
        session = MagicMock()
        session.manual_save_supported = False
        session.queue = queue.Queue()
        session.queue.put("ERROR:facebook_page_business_access_denied")
        with patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
            clear=True,
        ):
            dialog = LoginDialog(background_login=True)
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(9))
        dialog.session = session

        with patch.object(dialog, "reject") as reject:
            dialog.poll_messages()

        self.assertEqual(
            dialog.lifecycle_message,
            "登录失败：当前 Facebook 账号无法访问 Meta Business Suite。"
            "请确认已创建 Facebook Page，并拥有该 Page 的内容管理权限；未保存账号。",
        )
        self.assertNotIn("YouTube", dialog.log.toPlainText())
        reject.assert_called_once_with()
        dialog.close()

    def test_unknown_facebook_page_login_error_does_not_show_youtube_guidance(self) -> None:
        session = MagicMock()
        session.platform_type = 9
        session.manual_save_supported = False
        session.queue = queue.Queue()
        session.queue.put("ERROR:unexpected_facebook_login_error")
        with patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
            clear=True,
        ):
            dialog = LoginDialog(background_login=True)
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(9))
        dialog.session = session
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(7))

        with patch.object(dialog, "reject") as reject:
            dialog.poll_messages()

        self.assertEqual(
            dialog.lifecycle_message,
            "登录失败：Facebook Page 登录未完成，账号没有发生变化。",
        )
        self.assertNotIn("YouTube", dialog.log.toPlainText())
        reject.assert_called_once_with()
        dialog.close()

    def test_feature_flag_off_disables_saved_page_relogin_and_backend_actions(self) -> None:
        account = {
            "id": 91,
            "type": 9,
            "platformName": "Facebook Page",
            "profileName": "Meta 主体",
            "userName": "已保存 Page",
            "status": 1,
            "healthStatus": "normal",
            "statusText": "正常",
            "authMode": "browser",
            "filePath": "page.json",
            "accountReference": "1001",
        }
        page = AccountPage()

        with patch.dict(os.environ, {}, clear=True):
            actions = page._actions(account)

        buttons = {item.text(): item for item in actions.findChildren(QPushButton)}
        menu_actions = [
            action
            for menu in actions.findChildren(QMenu)
            for action in menu.actions()
        ]
        relogin = next(action for action in menu_actions if action.text() == "重新登录")
        refresh = next(
            action for action in menu_actions if action.text() == "刷新账号信息"
        )
        self.assertFalse(buttons["打开后台"].isEnabled())
        self.assertFalse(relogin.isEnabled())
        self.assertFalse(refresh.isEnabled())
        self.assertIn("功能未开启", buttons["打开后台"].toolTip())
        self.assertIn("功能未开启", refresh.toolTip())
        page.close()

    def test_feature_flag_off_disables_saved_page_single_account_detection(self) -> None:
        account = {
            "id": 91,
            "type": 9,
            "platformName": "Facebook Page",
            "profileName": "Meta 主体",
            "userName": "已保存 Page",
            "status": 1,
            "healthStatus": "normal",
            "statusText": "正常",
            "authMode": "browser",
            "filePath": "page.json",
            "accountReference": "1001",
        }
        page = AccountPage()

        with patch.dict(os.environ, {}, clear=True):
            disabled_actions = page._actions(account)
        disabled_check = next(
            action
            for menu in disabled_actions.findChildren(QMenu)
            for action in menu.actions()
            if action.text() == "检测登录状态"
        )

        with patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
            clear=True,
        ):
            enabled_actions = page._actions(account)
        enabled_check = next(
            action
            for menu in enabled_actions.findChildren(QMenu)
            for action in menu.actions()
            if action.text() == "检测登录状态"
        )

        self.assertFalse(disabled_check.isEnabled())
        self.assertIn("功能未开启", disabled_check.toolTip())
        self.assertTrue(enabled_check.isEnabled())
        with patch.object(page, "start_validation") as start:
            disabled_check.trigger()
            start.assert_not_called()
            enabled_check.trigger()
            start.assert_called_once_with([91])
        page.close()

    def test_feature_flag_off_bulk_detection_excludes_saved_pages(self) -> None:
        accounts = [
            {"id": 7, "type": 7},
            {"id": 91, "type": 9},
            {"id": 3, "type": 3},
        ]
        page = AccountPage()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                account_service,
                "list_managed_accounts",
                return_value=accounts,
            ),
            patch.object(page, "start_validation") as start,
        ):
            page.check_all()
        start.assert_called_once_with([7, 3])

        with (
            patch.dict(
                os.environ,
                {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                clear=True,
            ),
            patch.object(page, "start_validation") as start,
        ):
            page.check_all()
        start.assert_called_once_with(None)
        page.close()

    def test_feature_flag_off_bulk_detection_does_not_fall_back_to_all_when_only_pages_exist(self) -> None:
        page = AccountPage()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                account_service,
                "list_managed_accounts",
                return_value=[{"id": 91, "type": 9}],
            ),
            patch.object(page, "start_validation") as start,
        ):
            page.check_all()

        start.assert_not_called()
        self.assertIn("没有可检测", page.status_label.text())
        page.close()

    def test_feature_flag_off_automatic_recheck_excludes_saved_pages(self) -> None:
        accounts = [
            {"id": 7, "type": 7},
            {"id": 91, "type": 9},
        ]
        page = AccountPage()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                account_service,
                "list_managed_accounts",
                return_value=accounts,
            ),
            patch.object(
                account_service,
                "accounts_requiring_check",
                return_value=[7, 91],
            ),
            patch.object(page, "start_validation") as start,
        ):
            page.auto_check_stale_accounts()
        start.assert_called_once_with([7], silent=True, invalid_status=2)

        with (
            patch.dict(
                os.environ,
                {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                clear=True,
            ),
            patch.object(
                account_service,
                "accounts_requiring_check",
                return_value=[7, 91],
            ),
            patch.object(page, "start_validation") as start,
        ):
            page.auto_check_stale_accounts()
        start.assert_called_once_with(
            [7, 91],
            silent=True,
            invalid_status=2,
        )
        page.close()

    def test_feature_flag_off_automatic_recheck_skips_page_only_queue(self) -> None:
        page = AccountPage()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                account_service,
                "list_managed_accounts",
                return_value=[{"id": 91, "type": 9}],
            ),
            patch.object(
                account_service,
                "accounts_requiring_check",
                return_value=[91],
            ),
            patch.object(page, "start_validation") as start,
        ):
            page.auto_check_stale_accounts()

        start.assert_not_called()
        page.close()

    def test_feature_flag_off_rejects_saved_page_actions_before_dialog_or_worker(self) -> None:
        account = {
            "id": 91,
            "type": 9,
            "platformName": "Facebook Page",
            "profileName": "Meta 主体",
            "userName": "已保存 Page",
            "status": 1,
            "authMode": "browser",
            "filePath": "page.json",
            "accountReference": "1001",
        }
        page = AccountPage()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ui.account_page.LoginDialog") as login_dialog,
            patch.object(
                account_browser_service,
                "open_account_backend",
            ) as open_backend,
            patch.object(page.tasks, "run") as run_task,
            patch.object(QMessageBox, "warning") as warning,
        ):
            page.relogin(account)
            self.assertEqual(warning.call_count, 1)
            self.assertIn("功能未开启", warning.call_args.args[2])
            warning.reset_mock()

            page.open_backend(account)
            self.assertEqual(warning.call_count, 1)
            self.assertIn("功能未开启", warning.call_args.args[2])

            warning.reset_mock()
            page.refresh_avatar(account)
            self.assertEqual(warning.call_count, 1)
            self.assertIn("功能未开启", warning.call_args.args[2])

        login_dialog.assert_not_called()
        open_backend.assert_not_called()
        run_task.assert_not_called()
        page.close()

    def test_facebook_page_prompt_selects_the_exact_id_behind_the_label(self) -> None:
        request = login_service.FacebookPageSelectionRequest(
            (
                FacebookPageIdentity("1234561001", "同名", can_manage_content=True),
                FacebookPageIdentity("1234561002", "同名", can_manage_content=True),
            )
        )
        dialog = LoginDialog()
        dialog.session = MagicMock()

        with patch.object(
            QInputDialog,
            "getItem",
            return_value=("同名 · …1002", True),
        ):
            dialog._handle_facebook_page_selection(request)

        dialog.session.select_facebook_page.assert_called_once_with("1234561002")
        dialog.close()

    def test_facebook_page_prompt_cancel_is_clean_and_saves_nothing(self) -> None:
        request = login_service.FacebookPageSelectionRequest(
            (FacebookPageIdentity("1001", "Page", can_manage_content=True),)
        )
        dialog = LoginDialog()
        dialog.session = MagicMock()
        dialog.timer.start()

        with (
            patch.object(QInputDialog, "getItem", return_value=("", False)),
            patch.object(dialog, "reject") as reject,
        ):
            dialog._handle_facebook_page_selection(request)

        dialog.session.cancel.assert_called_once_with()
        dialog.session.select_facebook_page.assert_not_called()
        self.assertEqual(dialog.lifecycle_message, "登录已取消，未保存 Facebook Page。")
        self.assertFalse(dialog.timer.isActive())
        reject.assert_called_once_with()
        dialog.close()

    def test_youtube_oauth_account_actions_stay_enabled(self) -> None:
        account = {
            "id": 71,
            "type": 7,
            "platformName": "YouTube",
            "profileName": "海外主体",
            "userName": "OAuth 测试频道",
            "status": 1,
            "healthStatus": "normal",
            "statusText": "正常",
            "remark": "",
            "authMode": "youtube_oauth",
            "filePath": "youtube-oauth:opaque-reference",
            "accountReference": "UC_safe",
        }
        page = AccountPage()
        actions = page._actions(account)
        buttons = {item.text(): item for item in actions.findChildren(QPushButton)}

        self.assertTrue(buttons["打开后台"].isEnabled())
        menu_actions = [
            action
            for menu in actions.findChildren(QMenu)
            for action in menu.actions()
        ]
        refresh = next(action for action in menu_actions if action.text() == "刷新账号信息")
        self.assertTrue(refresh.isEnabled())
        page.close()

    def test_legacy_youtube_oauth_action_is_named_as_permission_upgrade(self) -> None:
        account = {
            "id": 71,
            "type": 7,
            "platformName": "YouTube",
            "profileName": "海外主体",
            "userName": "OAuth 测试频道",
            "status": 1,
            "healthStatus": "pending",
            "statusText": "需要升级发布权限",
            "remark": "",
            "authMode": "youtube_oauth",
            "oauthScopeVersion": 1,
            "filePath": "youtube-oauth:opaque-reference",
            "accountReference": "UC_safe",
        }
        page = AccountPage()

        actions = page._actions(account)
        menu_actions = [
            action
            for menu in actions.findChildren(QMenu)
            for action in menu.actions()
        ]

        self.assertTrue(
            any(action.text() == "升级 YouTube 发布权限" for action in menu_actions)
        )
        self.assertFalse(any(action.text() == "重新登录" for action in menu_actions))
        page.close()

    def test_youtube_oauth_backend_uses_system_browser_and_saved_channel_id(self) -> None:
        account = {
            "id": 71,
            "type": 7,
            "authMode": "youtube_oauth",
            "accountReference": "UC_safe",
            "filePath": "youtube-oauth:opaque-reference",
        }

        with patch(
            "app_core.account_browser_service.webbrowser.open",
            return_value=True,
        ) as open_browser:
            reused = account_browser_service.open_account_backend(account)

        self.assertFalse(reused)
        open_browser.assert_called_once_with(
            "https://studio.youtube.com/channel/UC_safe"
        )

    def test_account_page_backend_open_is_non_blocking_deduplicated_and_reports_on_ui_thread(self) -> None:
        account = {
            "id": 9,
            "type": 1,
            "platformName": "小红书",
            "profileName": "AI",
            "userName": "海风",
            "status": 1,
            "authMode": "browser",
            "filePath": "oneclick_1_test.json",
        }
        started = threading.Event()
        release = threading.Event()
        callback_threads: list[int] = []
        ui_thread_id = threading.get_ident()

        def blocked_open(_account: dict) -> bool:
            started.set()
            release.wait(0.5)
            raise RuntimeError("受控后台启动失败")

        page = AccountPage()
        with (
            patch.object(
                account_browser_service,
                "open_account_backend",
                side_effect=blocked_open,
            ) as open_backend,
            patch.object(
                QMessageBox,
                "warning",
                side_effect=lambda *_args: callback_threads.append(threading.get_ident()),
            ) as warning,
        ):
            before = time.monotonic()
            page.open_backend(account)
            elapsed = time.monotonic() - before

            self.assertLess(elapsed, 0.1)
            self.assertTrue(started.wait(0.5))
            page.open_backend(account)
            self.assertEqual(open_backend.call_count, 1)
            self.assertIn("正在打开后台", page.status_label.text())

            release.set()
            deadline = time.monotonic() + 1.0
            while (
                page.tasks.is_running("open_account_backend:9")
                and time.monotonic() < deadline
            ):
                self.app.processEvents()
                time.sleep(0.005)
            self.app.processEvents()

            self.assertFalse(page.tasks.is_running("open_account_backend:9"))
            warning.assert_called_once()
            self.assertIn("受控后台启动失败", warning.call_args.args[2])
            self.assertEqual(callback_threads, [ui_thread_id])

        page.close()

    def test_youtube_system_browser_login_disables_manual_save_fallback(self) -> None:
        class OAuthSession:
            manual_save_supported = False

            def __init__(self) -> None:
                self.queue: queue.Queue[str] = queue.Queue()
                self.queue.put("BROWSER_OPENED")

            def cancel(self) -> None:
                pass

            def save(self) -> None:
                raise AssertionError("manual save must stay disabled")

        dialog = LoginDialog(background_login=True)
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(7))
        dialog.profile_input.setCurrentText("海外主体")
        with patch("ui.login_dialog.login_service.start_login", return_value=OAuthSession()):
            dialog.start_login()
            dialog.poll_messages()

        self.assertFalse(dialog.save_btn.isEnabled())
        self.assertIn("官方登录页面", dialog.qr_label.text())
        dialog.close()

    def test_tiktok_dialog_explains_system_chrome_and_disables_manual_save(self) -> None:
        class TikTokSession:
            manual_save_supported = False

            def __init__(self) -> None:
                self.queue: queue.Queue[str] = queue.Queue()

            def cancel(self) -> None:
                pass

            def save(self) -> None:
                raise AssertionError("TikTok manual save must stay disabled")

            def complete_login(self) -> None:
                pass

        dialog = LoginDialog(background_login=True)
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(6))
        dialog.reset_login_prompt()
        self.assertEqual(
            dialog.qr_label.text(),
            "请点击“开始登录”，一键发将打开系统 Chrome 专用临时窗口；"
            "登录完成后可关闭该窗口，或点击“完成登录并保存”。",
        )
        self.assertTrue(dialog.save_btn.isHidden())
        self.assertFalse(dialog.complete_btn.isHidden())
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(1))
        self.assertFalse(dialog.save_btn.isHidden())
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(6))
        dialog.profile_input.setCurrentText("TikTok 测试")
        with patch("ui.login_dialog.login_service.start_login", return_value=TikTokSession()):
            dialog.start_login()
        self.assertFalse(dialog.save_btn.isEnabled())
        self.assertFalse(dialog.complete_btn.isEnabled())
        dialog.close()

    def test_tiktok_dialog_enables_complete_only_after_system_browser_opens(self) -> None:
        class TikTokSession:
            manual_save_supported = False

            def __init__(self) -> None:
                self.queue: queue.Queue[str] = queue.Queue()
                self.completed = 0

            def complete_login(self) -> None:
                self.completed += 1

        session = TikTokSession()
        dialog = LoginDialog(background_login=True)
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(6))
        dialog.session = session

        self.assertFalse(dialog.complete_btn.isEnabled())
        session.queue.put("SYSTEM_BROWSER_OPENED")
        dialog.poll_messages()
        self.assertTrue(dialog.complete_btn.isEnabled())
        session.queue.put("WAITING_BROWSER_EXIT")
        dialog.poll_messages()
        self.assertTrue(dialog.complete_btn.isEnabled())

        dialog.complete_tiktok_login()

        self.assertEqual(session.completed, 1)
        self.assertFalse(dialog.complete_btn.isEnabled())
        self.assertIn("正在核验", dialog.qr_label.text())
        dialog.close()

    def test_tiktok_dialog_consumes_each_system_browser_lifecycle_message(self) -> None:
        expected = {
            "OPENING_SYSTEM_BROWSER": "正在准备系统 Chrome 专用临时窗口...",
            "SYSTEM_BROWSER_OPENED": "系统 Chrome 专用临时窗口已打开。",
            "WAITING_BROWSER_EXIT": "请在系统 Chrome 专用临时窗口完成 TikTok 登录后关闭该窗口，或点击“完成登录并保存”。",
            "VALIDATING_TIKTOK_SESSION": "专用窗口已关闭，正在核对 TikTok 登录状态...",
            "CLEANING_LOGIN_ATTEMPT": "正在清理 TikTok 临时登录资料...",
        }
        forbidden = ("Cookie 导出", "默认资料", "避开 Google", "手动保存会话")

        for lifecycle, visible_text in expected.items():
            with self.subTest(lifecycle=lifecycle):
                session = MagicMock()
                session.manual_save_supported = False
                session.queue = queue.Queue()
                session.queue.put(lifecycle)
                dialog = LoginDialog(background_login=True)
                dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(6))
                dialog.session = session

                dialog.poll_messages()

                self.assertEqual(dialog.qr_label.text(), visible_text)
                self.assertFalse(dialog.save_btn.isEnabled())
                rendered = dialog.qr_label.text() + "\n" + dialog.log.toPlainText()
                for phrase in forbidden:
                    self.assertNotIn(phrase, rendered)
                dialog.close()

    def test_tiktok_dialog_maps_every_stable_error_without_security_bypass_advice(self) -> None:
        expected = {
            "tiktok_system_browser_unavailable": "未找到可用的系统 Chrome 或 Edge。",
            "tiktok_login_attempt_timeout": "等待登录超时，未保存账号。",
            "tiktok_login_profile_busy": "TikTok 临时登录资料仍被浏览器占用。",
            "tiktok_session_scope_invalid": "登录状态包含超出 TikTok 的数据，已拒绝保存。",
            "tiktok_session_missing": "未检测到可用的 TikTok 登录状态。",
            "tiktok_session_expired": "TikTok 登录状态已失效。",
            "tiktok_identity_missing": "已读取 TikTok 登录状态，但未找到唯一公开账号。",
            "tiktok_identity_probe_required": "已读取登录状态，但当前页面账号入口发生变化；已生成安全诊断，未保存账号。",
            "tiktok_account_identity_ambiguous": "TikTok 返回了多个公开账号，已停止保存。",
            "tiktok_account_invalid": "TikTok 未返回唯一可核对账号。",
            "tiktok_account_identity_mismatch": "当前 TikTok 账号与原记录不一致。",
            "tiktok_login_cleanup_failed": "临时登录资料清理失败，已停止保存账号。",
            "tiktok_login_commit_failed": "TikTok 会话未能安全写入账号库。",
        }
        forbidden = ("Cookie 导出", "默认资料", "避开 Google", "手动保存会话")

        for error_code, error_text in expected.items():
            with self.subTest(error_code=error_code):
                session = MagicMock()
                session.manual_save_supported = False
                session.queue = queue.Queue()
                session.queue.put(f"ERROR:{error_code}")
                dialog = LoginDialog(background_login=True)
                dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(6))
                dialog.session = session
                with patch.object(dialog, "reject") as reject:
                    dialog.poll_messages()

                self.assertEqual(dialog.lifecycle_message, f"登录失败：{error_text}")
                self.assertFalse(dialog.save_btn.isEnabled())
                reject.assert_called_once_with()
                rendered = dialog.lifecycle_message + "\n" + dialog.log.toPlainText()
                for phrase in forbidden:
                    self.assertNotIn(phrase, rendered)
                dialog.close()

    def test_tiktok_dialog_accepts_only_the_fixed_safe_probe_summary_shape(self) -> None:
        safe_summary = (
            "app=present_no_handle/present_no_handle;top=1/1;controls=2/2;"
            "semantic=1/1;paths=app_context/app_context;families=1/1;"
            "rehydration=0/0;consistent=true"
        )
        session = MagicMock()
        session.manual_save_supported = False
        session.queue = queue.Queue()
        session.queue.put(f"IDENTITY_PROBE_SUMMARY:{safe_summary}")
        session.queue.put("IDENTITY_PROBE_SUMMARY:dom-text-secret")
        session.queue.put("ERROR:tiktok_identity_probe_required")
        dialog = LoginDialog(background_login=True)
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(6))
        dialog.session = session

        with patch.object(dialog, "reject"):
            dialog.poll_messages()

        rendered = dialog.log.toPlainText()
        self.assertIn(safe_summary, rendered)
        self.assertNotIn("dom-text-secret", rendered)
        self.assertEqual(
            dialog.lifecycle_message,
            "登录失败：已读取登录状态，但当前页面账号入口发生变化；"
            f"已生成安全诊断，未保存账号。 安全诊断：{safe_summary}",
        )
        dialog.close()

    def test_tiktok_account_saved_uses_silent_readback_before_accepting(self) -> None:
        session = MagicMock()
        session.manual_save_supported = False
        session.queue = queue.Queue()
        session.queue.put("ACCOUNT_SAVED:44")
        dialog = LoginDialog(background_login=True)
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(6))
        dialog.session = session

        with (
            patch.object(dialog, "_verify_saved_account") as readback,
            patch.object(dialog, "accept") as accept,
        ):
            dialog.poll_messages()

        readback.assert_called_once_with(44)
        accept.assert_not_called()
        self.assertFalse(dialog.save_btn.isEnabled())
        dialog.close()

    def test_tiktok_cancelled_rejects_without_saving(self) -> None:
        session = MagicMock()
        session.manual_save_supported = False
        session.queue = queue.Queue()
        session.queue.put("CANCELLED")
        dialog = LoginDialog(background_login=True)
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(6))
        dialog.session = session

        with patch.object(dialog, "reject") as reject:
            dialog.poll_messages()

        self.assertEqual(dialog.lifecycle_message, "登录已取消，未修改账号会话。")
        self.assertFalse(dialog.save_btn.isEnabled())
        reject.assert_called_once_with()
        dialog.close()

    def test_expired_account_cookie_is_checked_before_being_marked_pending(self) -> None:
        """超过 24 小时先静默复核 Cookie，复核失败才进入待检测状态。"""

        page = AccountPage()
        with patch.object(
            account_service, "accounts_requiring_check", return_value=[7, 8]
        ), patch.object(
            account_service, "facebook_page_v1_enabled", return_value=True
        ), patch.object(page, "start_validation") as start:
            page.auto_check_stale_accounts()

        start.assert_called_once_with([7, 8], silent=True, invalid_status=2)
        page.close()

    def test_removed_authorization_modules_are_not_importable(self) -> None:
        self.assertIsNone(
            importlib.util.find_spec("app_core.overseas_api_service")
        )
        self.assertIsNone(
            importlib.util.find_spec("ui.overseas_authorization_dialog")
        )

    def test_abnormal_session_only_prompts_manual_relogin(self) -> None:
        page = AccountPage()
        account = {
            "id": 9,
            "type": 1,
            "platformName": "小红书",
            "profileName": "AI",
            "userName": "海风",
            "filePath": "oneclick_1_test.json",
        }
        with patch.object(
            account_browser_service,
            "open_account_backend",
        ) as open_backend, patch.object(QMessageBox, "warning") as warning:
            shown = page._present_validation_intervention(
                {"interventionRequired": [account]}
            )
        self.assertTrue(shown)
        open_backend.assert_not_called()
        warning.assert_called_once()
        message = str(warning.call_args.args[2])
        self.assertIn("只更新账号状态", message)
        self.assertIn("重新登录", message)
        page.close()

    def test_youtube_channel_mismatch_prompt_is_not_reported_as_generic_logout(self) -> None:
        page = AccountPage()
        account = {
            "id": 71,
            "type": 7,
            "platformName": "YouTube",
            "profileName": "海外主体",
            "userName": "OAuth 测试频道",
            "authMode": "youtube_oauth",
            "authIssueCode": "youtube_channel_identity_mismatch",
        }

        with patch.object(QMessageBox, "warning") as warning:
            shown = page._present_validation_intervention(
                {"interventionRequired": [account]}
            )

        self.assertTrue(shown)
        message = warning.call_args.args[2]
        self.assertIn("频道身份不一致", message)
        self.assertIn("重新授权原频道", message)
        page.close()

    def test_publish_account_check_never_opens_login_page(self) -> None:
        account = {
            "id": 9,
            "type": 1,
            "platformName": "小红书",
            "profileName": "AI",
            "userName": "海风",
            "filePath": "oneclick_1_test.json",
        }
        page = MagicMock()
        with patch.object(
            account_browser_service,
            "open_account_backend",
        ) as open_backend, patch.object(QMessageBox, "warning") as warning:
            shown = PublishPage._present_account_login_intervention(
                page,
                {"interventionRequired": [account]},
            )
        self.assertTrue(shown)
        open_backend.assert_not_called()
        warning.assert_called_once()
        self.assertIn("不会打开平台登录页", warning.call_args.args[2])

    def test_publish_startup_page_keeps_navigation_and_content_in_sync(self) -> None:
        window = MainWindow()
        window.show()
        window.set_current_page_by_key("publish")
        self.app.processEvents()
        self.assertIs(window.tabs.currentWidget(), window.publish)
        self.assertEqual(window.tabs.currentIndex(), 3)
        self.assertTrue(window.nav_buttons[3].isChecked())
        self.assertEqual(window.current_workspace_label.text(), "发布中心")
        window.close()

    def test_douyin_graphic_matrix_is_independent_navigation_page(self) -> None:
        window = MainWindow()
        try:
            labels = [label for label, _page, _icon in window.page_definitions]
            self.assertEqual(labels[3:6], ["发布中心", "抖音图文矩阵", "抖音带货"])
            window.set_current_page_by_key("douyin_graphic_matrix")
            self.assertIs(window.tabs.currentWidget(), window.douyin_graphic_matrix)
            self.assertEqual(window.current_workspace_label.text(), "抖音图文矩阵")
        finally:
            window.accounts.stop_auto_checking()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
