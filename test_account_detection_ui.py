# -*- coding: utf-8 -*-
"""账号检测与启动页导航的本地 UI 契约测试。"""

import os
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton

from app_core import account_browser_service
from ui.account_page import AccountPage
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


if __name__ == "__main__":
    unittest.main()
