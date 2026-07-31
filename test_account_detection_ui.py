# -*- coding: utf-8 -*-
"""账号检测与启动页导航的本地 UI 契约测试。"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from app_core import account_browser_service
from ui.account_page import AccountPage
from ui.main_window import MainWindow


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

    def test_abnormal_session_opens_only_official_intervention_page(self) -> None:
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
            return_value=False,
        ) as open_backend, patch.object(QMessageBox, "warning") as warning:
            shown = page._present_validation_intervention(
                {"interventionRequired": [account]}
            )
        self.assertTrue(shown)
        open_backend.assert_called_once_with(account)
        warning.assert_called_once()
        page.close()

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
