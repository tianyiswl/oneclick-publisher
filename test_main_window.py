# -*- coding: utf-8 -*-
"""主窗口退出顺序的离线回归测试。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication

from ui.main_window import MainWindow


class MainWindowShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_close_event_shuts_down_commerce_before_all_browser_sessions(self) -> None:
        """若全局浏览器先关，采集代际将失去可审计的统一收口。"""

        window = MainWindow()
        order: list[str] = []
        try:
            with patch.object(
                window.douyin_commerce,
                "shutdown",
                create=True,
                side_effect=lambda: order.append("commerce_shutdown") or True,
            ), patch.object(
                window.accounts,
                "stop_auto_checking",
                side_effect=lambda: order.append("account_checks_stopped"),
            ), patch(
                "ui.main_window.account_browser_service.close_all_backend_sessions",
                side_effect=lambda *, wait: order.append(
                    f"backend_sessions_closed:{wait}"
                ),
            ):
                window.closeEvent(QCloseEvent())
        finally:
            window.deleteLater()

        self.assertEqual(
            order,
            [
                "commerce_shutdown",
                "account_checks_stopped",
                "backend_sessions_closed:True",
            ],
        )

    def test_close_event_stops_before_global_cleanup_when_commerce_shutdown_fails(self) -> None:
        """采集器未在截止内归零时，主窗口必须拒绝关闭且不动全局浏览器。"""

        window = MainWindow()
        event = QCloseEvent()
        try:
            with patch.object(
                window.douyin_commerce, "shutdown", return_value=False
            ) as shutdown, patch.object(
                window.accounts, "stop_auto_checking"
            ) as stop_auto_checking, patch(
                "ui.main_window.account_browser_service.close_all_backend_sessions"
            ) as close_all:
                window.closeEvent(event)
        finally:
            window.deleteLater()

        shutdown.assert_called_once_with()
        self.assertFalse(event.isAccepted())
        stop_auto_checking.assert_not_called()
        close_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
