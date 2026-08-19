# -*- coding: utf-8 -*-
"""平台数据监测页离屏回归测试。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from ui.background_task import BackgroundTaskRunner
from ui.data_monitor_page import DataMonitorPage


ACCOUNT = {
    "id": 12,
    "type": 3,
    "platformName": "抖音",
    "profileName": "数据主体",
    "userName": "抖音账号",
    "filePath": "oneclick_3_safe.json",
}


def empty_summary() -> dict:
    return {
        "accountId": 12,
        "latestSuccessAt": None,
        "latestRun": None,
        "metrics": {},
        "observedAt": {},
    }


def failed_summary() -> dict:
    return {
        "accountId": 12,
        "latestSuccessAt": "2026-08-20T12:00:00+08:00",
        "latestRun": {
            "status": "failed",
            "sourceMode": "browser_signed",
            "errorCode": "login_required",
            "metricCount": 0,
            "finishedAt": "2026-08-20T12:05:00+08:00",
        },
        "metrics": {"views": 125},
        "observedAt": {"views": "2026-08-20T12:00:00+08:00"},
    }


class QueuedPool:
    def __init__(self) -> None:
        self.tasks: list[object] = []

    def start(self, task) -> None:
        self.tasks.append(task)


class BlockingRunner:
    def __init__(self, *, wait_result: bool) -> None:
        self.wait_result = wait_result
        self.cancelled: list[str] = []
        self.waited: list[tuple[str, float]] = []

    def active_keys_with_prefixes(self, prefixes: tuple[str, ...]) -> list[str]:
        return ["platform-data-sync:12"]

    def cancel_pending(self, key: str) -> bool:
        self.cancelled.append(key)
        return False

    def wait_for_finished(self, key: str, timeout_seconds: float) -> bool:
        self.waited.append((key, timeout_seconds))
        return self.wait_result


class DataMonitorPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _page(self, *, summary=None, runner=None) -> DataMonitorPage:
        accounts = [ACCOUNT, {**ACCOUNT, "id": 13, "type": 1}]
        self.accounts_patch = patch(
            "ui.data_monitor_page.account_service.list_accounts",
            return_value=accounts,
        )
        self.summary_patch = patch(
            "ui.data_monitor_page.platform_data_service.account_data_summary",
            return_value=summary or empty_summary(),
        )
        self.accounts_patch.start()
        self.summary_patch.start()
        self.addCleanup(self.accounts_patch.stop)
        self.addCleanup(self.summary_patch.stop)
        page = DataMonitorPage(task_runner=runner)
        self.addCleanup(page.deleteLater)
        return page

    def test_only_douyin_accounts_are_listed_and_missing_metrics_render_dash(self) -> None:
        """平台过滤或缺失显示回退会把未支持账号或伪零暴露给用户。"""

        page = self._page()

        self.assertEqual(page.account_combo.count(), 1)
        self.assertEqual(page.account_combo.currentData(), 12)
        self.assertEqual(
            {label.text() for label in page.metric_values.values()},
            {"—"},
        )

    def test_sync_uses_real_runner_dedupes_and_ignores_untrusted_progress_text(self) -> None:
        """同账号重复入队或透传 worker 文案都会破坏同步安全边界。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(runner=runner)

        def sync(_account_id: int, report) -> dict:
            report(
                {
                    "stage": "browser_signed",
                    "message": "Cookie=secret /private/path?token=secret",
                }
            )
            return {
                "accountId": 12,
                "status": "success",
                "sourceMode": "browser_signed",
                "errorCode": "",
                "metricCount": 1,
            }

        with patch("ui.data_monitor_page.platform_data_sync.sync_account_data", sync):
            page.sync_button.click()
            page.sync_button.click()
            self.assertEqual(len(pool.tasks), 1)
            pool.tasks[0].run()
            self.app.processEvents()

        self.assertNotIn("secret", page.status_label.text())
        self.assertNotIn("/private", page.status_label.text())
        self.assertFalse(runner.is_running("platform-data-sync:12"))

    def test_failed_latest_run_keeps_last_success_and_routes_login_required(self) -> None:
        """失败若清空历史或不提供登录入口，用户无法判断数据状态。"""

        page = self._page(summary=failed_summary())
        spy = QSignalSpy(page.request_account_management)

        self.assertEqual(page.metric_values["views"].text(), "125")
        self.assertIn("需要重新登录", page.status_label.text())
        self.assertTrue(page.relogin_button.isVisibleTo(page))
        page.relogin_button.click()
        self.assertEqual(len(spy), 1)

    def test_shutdown_refuses_when_running_sync_cannot_finish(self) -> None:
        """采集 worker 未归零时主窗口不能继续关闭全局浏览器。"""

        runner = BlockingRunner(wait_result=False)
        page = self._page(runner=runner)

        self.assertFalse(page.shutdown())
        self.assertEqual(runner.cancelled, ["platform-data-sync:12"])
        self.assertEqual(len(runner.waited), 1)
        self.assertLessEqual(runner.waited[0][1], 5.0)


if __name__ == "__main__":
    unittest.main()
