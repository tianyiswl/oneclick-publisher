# -*- coding: utf-8 -*-
"""账号检测与启动页导航的本地 UI 契约测试。"""

import os
import importlib.util
import queue
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QPushButton

from app_core import account_browser_service, account_service
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

    def test_expired_account_cookie_is_checked_before_being_marked_pending(self) -> None:
        """超过 24 小时先静默复核 Cookie，复核失败才进入待检测状态。"""

        page = AccountPage()
        with patch.object(
            account_service, "accounts_requiring_check", return_value=[7, 8]
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
