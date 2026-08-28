# -*- coding: utf-8 -*-
"""TikTok 过期临时登录资料只在正常图形界面启动时恢复。"""

from types import SimpleNamespace
import sys
import unittest
from unittest.mock import MagicMock, patch

import desktop_native_app
from app_core.overseas_tiktok_system_login import TikTokSystemLoginError


class TikTokLoginStartupWiringTests(unittest.TestCase):
    def test_ui_test_path_does_not_recover_real_tiktok_staging(self) -> None:
        app = MagicMock()
        window = MagicMock()
        with (
            patch.object(desktop_native_app, "QApplication", return_value=app),
            patch.object(desktop_native_app, "configure_application") as configure,
            patch.object(desktop_native_app, "apply_style") as apply_style,
            patch.object(desktop_native_app, "MainWindow", return_value=window),
            patch.object(
                desktop_native_app,
                "recover_stale_tiktok_login_attempts",
            ) as recover,
            patch("builtins.print") as print_output,
        ):
            desktop_native_app.run_ui_test()

        recover.assert_not_called()
        configure.assert_called_once_with(app)
        apply_style.assert_called_once_with(app)
        window.show.assert_called_once_with()
        app.processEvents.assert_called_once_with()
        print_output.assert_called_once_with("NATIVE_DESKTOP_UI_OK")

    def test_window_creation_recovers_stale_attempts_before_constructing_main_window(self) -> None:
        events: list[str] = []
        window = MagicMock()
        controlled_cli = MagicMock()
        mcp_entry = MagicMock()
        fake_mcp_module = SimpleNamespace(run_stdio_server=mcp_entry)

        with (
            patch.object(
                desktop_native_app,
                "recover_stale_tiktok_login_attempts",
                side_effect=lambda: events.append("recover"),
            ),
            patch.object(
                desktop_native_app,
                "MainWindow",
                side_effect=lambda: events.append("window") or window,
            ),
            patch.object(
                desktop_native_app,
                "run_controlled_publish_cli",
                controlled_cli,
            ),
            patch.dict(sys.modules, {"app_core.oneclick_mcp_server": fake_mcp_module}),
        ):
            created = desktop_native_app.create_main_window()

        self.assertIs(created, window)
        self.assertEqual(events, ["recover", "window"])
        controlled_cli.assert_not_called()
        mcp_entry.assert_not_called()

    def test_recovery_failure_logs_only_stable_code_and_continues_startup(self) -> None:
        window = MagicMock()
        error = TikTokSystemLoginError(
            "tiktok_login_cleanup_failed",
            "sensitive /private/browser Cookie account verification QR",
        )
        with (
            patch.object(
                desktop_native_app,
                "recover_stale_tiktok_login_attempts",
                side_effect=error,
            ),
            patch.object(desktop_native_app, "MainWindow", return_value=window),
            self.assertLogs("desktop_native_app", level="WARNING") as captured,
        ):
            created = desktop_native_app.create_main_window()

        self.assertIs(created, window)
        rendered = "\n".join(captured.output)
        self.assertIn("tiktok_login_cleanup_failed", rendered)
        for secret in ("/private", "browser", "Cookie", "account", "verification", "QR"):
            self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
