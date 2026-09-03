# -*- coding: utf-8 -*-
"""主窗口退出顺序的离线回归测试。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QLabel

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

    def test_source_live_mode_is_unmistakable_in_window_and_sidebar(self) -> None:
        """共用正式账号时若没有醒目标识，用户会把源码联调误认成正式客户端。"""

        with patch.dict(os.environ, {"YIJIANFA_SOURCE_LIVE_DATA": "1"}):
            window = MainWindow()
        try:
            runtime_title = window.findChild(QLabel, "localWorkspaceTitle")
            runtime_detail = window.findChild(QLabel, "localWorkspaceVersion")
            self.assertIn("源码联调", window.windowTitle())
            self.assertEqual(runtime_title.text(), "源码联调模式")
            self.assertEqual(runtime_detail.text(), "共用正式账号数据 · 发布仍需确认")
        finally:
            window.accounts.stop_auto_checking()
            window.deleteLater()

    def test_data_monitor_is_a_real_page_and_shutdown_precedes_global_cleanup(self) -> None:
        """数据监测不能继续留在开发中，且采集任务必须先于全局浏览器关闭。"""

        window = MainWindow()
        event = QCloseEvent()
        order: list[str] = []
        try:
            labels = [label for label, _page, _icon in window.page_definitions]
            coming_soon = [label for label, _icon in window.coming_soon_definitions]
            self.assertIn("数据监测", labels)
            self.assertNotIn("数据监测", coming_soon)
            with patch.object(
                window.douyin_commerce,
                "shutdown",
                side_effect=lambda: order.append("commerce") or True,
            ), patch.object(
                window.data_monitor,
                "shutdown",
                side_effect=lambda: order.append("data") or True,
            ), patch.object(
                window.accounts,
                "stop_auto_checking",
                side_effect=lambda: order.append("accounts"),
            ), patch(
                "ui.main_window.account_browser_service.close_all_backend_sessions",
                side_effect=lambda *, wait: order.append("global"),
            ):
                window.closeEvent(event)
        finally:
            window.deleteLater()

        self.assertEqual(order, ["commerce", "data", "accounts", "global"])

    def test_close_event_stops_when_data_monitor_shutdown_fails(self) -> None:
        """数据采集未收束时不得停止账号检测或关闭全局浏览器。"""

        window = MainWindow()
        event = QCloseEvent()
        try:
            with patch.object(
                window.douyin_commerce,
                "shutdown",
                return_value=True,
            ), patch.object(
                window.data_monitor,
                "shutdown",
                return_value=False,
            ), patch.object(
                window.accounts,
                "stop_auto_checking",
            ) as stop_auto, patch(
                "ui.main_window.account_browser_service.close_all_backend_sessions"
            ) as close_all:
                window.closeEvent(event)
        finally:
            window.deleteLater()

        self.assertFalse(event.isAccepted())
        stop_auto.assert_not_called()
        close_all.assert_not_called()

    def test_automatic_montage_is_real_page_and_blocks_close_before_other_cleanup(self) -> None:
        """本地编码仍在运行时必须先阻止退出，不能继续关闭平台会话。"""

        window = MainWindow()
        event = QCloseEvent()
        order: list[str] = []
        try:
            labels = [label for label, _page, _icon in window.page_definitions]
            self.assertIn("自动混剪", labels)
            window.set_current_page_by_key("montage")
            self.assertEqual(window.current_workspace_label.text(), "自动混剪")
            with patch.object(
                window.automatic_montage,
                "shutdown",
                side_effect=lambda: order.append("montage") or False,
            ), patch.object(
                window.douyin_commerce,
                "shutdown",
                side_effect=lambda: order.append("commerce") or True,
            ), patch.object(
                window.accounts,
                "stop_auto_checking",
                side_effect=lambda: order.append("accounts"),
            ):
                window.closeEvent(event)
        finally:
            window.deleteLater()

        self.assertFalse(event.isAccepted())
        self.assertEqual(order, ["montage"])


if __name__ == "__main__":
    unittest.main()
