# -*- coding: utf-8 -*-
"""平台数据监测页离屏回归测试。"""

from __future__ import annotations

import math
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QSignalSpy
from PyQt6.QtGui import QPixmap
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

ACCOUNT_B = {
    **ACCOUNT,
    "id": 14,
    "profileName": "数据主体 B",
    "userName": "抖音账号 B",
}


def empty_summary() -> dict:
    summary = period_summary()
    summary["latestRun"] = None
    summary["platformObservedAt"] = None
    summary["localSyncedAt"] = None
    for metric in summary["metrics"].values():
        metric["value"] = None
        metric["availability"] = "missing"
        metric["observedDays"] = 0
    return summary


def period_summary(
    *,
    status: str = "success",
    error_code: str = "",
) -> dict:
    metrics = {
        "views": {
            "value": 321,
            "scope": "daily_increment",
            "unit": "count",
            "availability": "complete",
            "observedDays": 7,
        },
        "likes": {
            "value": 12,
            "scope": "daily_increment",
            "unit": "count",
            "availability": "partial",
            "observedDays": 3,
        },
        "comments": {
            "value": None,
            "scope": "daily_increment",
            "unit": "count",
            "availability": "missing",
            "observedDays": 0,
        },
        "shares": {
            "value": 4,
            "scope": "daily_increment",
            "unit": "count",
            "availability": "complete",
            "observedDays": 7,
        },
        "followers_net": {
            "value": 8,
            "scope": "daily_increment",
            "unit": "count",
            "availability": "complete",
            "observedDays": 7,
        },
        "profile_visits": {
            "value": 18,
            "scope": "daily_increment",
            "unit": "count",
            "availability": "complete",
            "observedDays": 7,
        },
        "followers_total": {
            "value": 2_000,
            "scope": "lifetime_total",
            "unit": "count",
            "availability": "complete",
            "observedDays": 1,
        },
    }
    return {
        "accountId": 12,
        "days": 7,
        "periodStart": "2026-08-14",
        "periodEnd": "2026-08-20",
        "metrics": metrics,
        "latestRun": {
            "status": status,
            "sourceMode": "direct_session",
            "errorCode": error_code,
            "metricCount": 13,
            "finishedAt": "2026-08-20T12:01:00+08:00",
        },
        "trustedSourceMode": "direct_session",
        "platformObservedAt": "2026-08-20T12:00:00+08:00",
        "localSyncedAt": "2026-08-20T12:01:00+08:00",
    }


def empty_trends() -> dict:
    return {
        "accountId": 12,
        "days": 7,
        "periodStart": "2026-08-14",
        "periodEnd": "2026-08-20",
        "items": [],
    }


def empty_contents() -> dict:
    return {
        "accountId": 12,
        "total": 0,
        "limit": 50,
        "offset": 0,
        "items": [],
    }


def failed_summary() -> dict:
    summary = period_summary(status="failed", error_code="login_required")
    summary["metrics"]["views"]["value"] = 125
    summary["latestRun"]["sourceMode"] = "browser_signed"
    return summary


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

    def _page(
        self,
        *,
        summary=None,
        trends=None,
        contents=None,
        accounts=None,
        runner=None,
    ) -> DataMonitorPage:
        account_rows = accounts or [ACCOUNT, {**ACCOUNT, "id": 13, "type": 1}]
        self.accounts_patch = patch(
            "ui.data_monitor_page.account_service.list_accounts",
            return_value=account_rows,
        )
        self.summary_patch = patch(
            "ui.data_monitor_page.platform_data_service.account_data_summary",
            return_value=summary if summary is not None else empty_summary(),
        )
        self.period_patch = patch(
            "ui.data_monitor_page.platform_data_service.account_period_summary",
            return_value=summary if summary is not None else empty_summary(),
        )
        self.trends_patch = patch(
            "ui.data_monitor_page.platform_data_service.account_daily_trends",
            return_value=trends or empty_trends(),
        )
        self.contents_patch = patch(
            "ui.data_monitor_page.platform_data_service.account_contents",
            return_value=contents or empty_contents(),
        )
        self.accounts_patch.start()
        self.summary_patch.start()
        self.period_mock = self.period_patch.start()
        self.trends_mock = self.trends_patch.start()
        self.contents_mock = self.contents_patch.start()
        self.addCleanup(self.accounts_patch.stop)
        self.addCleanup(self.summary_patch.stop)
        self.addCleanup(self.period_patch.stop)
        self.addCleanup(self.trends_patch.stop)
        self.addCleanup(self.contents_patch.stop)
        page = DataMonitorPage(task_runner=runner)
        self.addCleanup(page.deleteLater)
        return page

    def test_v2_cards_explain_scope_range_and_freshness(self) -> None:
        """卡片若丢失对象、口径和时间投影，孤立数字会被误解。"""

        page = self._page(summary=period_summary())

        self.assertTrue(hasattr(page, "range_combo"))
        self.assertEqual(page.range_combo.currentData(), 7)
        self.assertEqual(page.metric_titles["views"].text(), "账号区间新增播放")
        self.assertEqual(
            page.metric_titles["followers_total"].text(),
            "账号区间末粉丝总数",
        )
        self.assertEqual(page.metric_values["comments"].text(), "—")
        self.assertEqual(page.metric_captions["views"].text(), "完整数据")
        self.assertEqual(page.metric_captions["likes"].text(), "部分日期")
        self.assertEqual(page.metric_captions["comments"].text(), "暂未取得")
        self.assertIn("2026-08-14", page.period_label.text())
        self.assertIn("2026-08-20", page.period_label.text())
        self.assertEqual(page.source_label.text(), "数据来源：会话直连")
        self.assertEqual(
            page.platform_observed_label.text(),
            "平台观察时间：2026-08-20T12:00:00+08:00",
        )
        self.assertEqual(
            page.local_synced_label.text(),
            "本地同步时间：2026-08-20T12:01:00+08:00",
        )

    def test_range_switch_queries_local_data_without_sync(self) -> None:
        """时间切换若启动平台同步，会将本地浏览误变成外部读取。"""

        with patch(
            "ui.data_monitor_page.platform_data_sync.sync_account_data"
        ) as sync_mock:
            page = self._page()
            self.period_mock.reset_mock()
            self.trends_mock.reset_mock()
            self.contents_mock.reset_mock()

            page.range_combo.setCurrentIndex(page.range_combo.findData(30))
            self.app.processEvents()

        sync_mock.assert_not_called()
        self.period_mock.assert_called_once_with(12, 30)
        self.trends_mock.assert_called_once_with(12, 30)
        self.contents_mock.assert_not_called()

    def test_sparse_trend_and_content_table_preserve_missing_values(self) -> None:
        """稀疏日期或作品缺失值若补零，会制造不存在的趋势与互动。"""

        trends = {
            "accountId": 12,
            "days": 7,
            "periodStart": "2026-08-14",
            "periodEnd": "2026-08-20",
            "items": [
                {"date": "2026-08-18", "metrics": {"views": 10}},
                {"date": "2026-08-20", "metrics": {"views": 12}},
            ],
        }
        contents = {
            "accountId": 12,
            "total": 52,
            "limit": 50,
            "offset": 0,
            "items": [
                {
                    "contentId": "older",
                    "title": "旧作品",
                    "coverUrl": "",
                    "publishedAt": "2026-08-19T09:00:00+08:00",
                    "contentStatus": "published",
                    "contentType": "video",
                    "metrics": {"views": 10, "comments": 1},
                },
                {
                    "contentId": "newer",
                    "title": "新作品",
                    "coverUrl": "",
                    "publishedAt": "2026-08-20T09:00:00+08:00",
                    "contentStatus": "published",
                    "contentType": "video",
                    "metrics": {"views": 20, "likes": 2, "shares": 1},
                },
            ],
        }
        page = self._page(trends=trends, contents=contents)

        self.assertTrue(hasattr(page, "trend_chart"))
        self.assertEqual(
            page.trend_chart.series_for_test("views"),
            (("2026-08-18", 10), ("2026-08-20", 12)),
        )
        self.assertNotIn(
            ("2026-08-19", 0),
            page.trend_chart.series_for_test("views"),
        )
        self.assertEqual(page.content_table.item(0, 0).text(), "新作品")
        self.assertEqual(page.content_table.item(0, 5).text(), "—")

        self.period_mock.reset_mock()
        self.trends_mock.reset_mock()
        self.contents_mock.reset_mock()
        page.next_page_button.click()
        self.app.processEvents()

        self.contents_mock.assert_called_once_with(12, limit=50, offset=50)
        self.period_mock.assert_not_called()
        self.trends_mock.assert_not_called()

    def test_partial_success_is_not_rendered_as_complete(self) -> None:
        """账号趋势可用时不得把未取得的作品数据冒充为全部完成。"""

        page = self._page(
            summary=period_summary(
                status="partial_success",
                error_code="content_list_unavailable",
            )
        )

        self.assertEqual(
            page.status_label.text(),
            "账号趋势已更新，作品数据未取得",
        )
        self.assertNotIn("账号趋势和作品数据已更新", page.status_label.text())

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
            pool.tasks[0].signals.progressed.emit(
                {
                    "stage": "content_list",
                    "message": "Cookie=secret /private/path?token=secret",
                }
            )
            self.app.processEvents()
            self.assertEqual(page.status_label.text(), "正在读取作品列表…")
            pool.tasks[0].run()
            self.app.processEvents()

        self.assertNotIn("secret", page.status_label.text())
        self.assertNotIn("/private", page.status_label.text())
        self.assertFalse(runner.is_running("platform-data-sync:12"))

    def test_worker_error_status_survives_finished_cleanup(self) -> None:
        """worker 失败后 finished 只能恢复控件，不得用旧摘要覆盖失败。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(summary=period_summary(), runner=runner)
        self.period_mock.reset_mock()
        self.trends_mock.reset_mock()
        self.contents_mock.reset_mock()

        with patch(
            "ui.data_monitor_page.platform_data_sync.sync_account_data",
            side_effect=RuntimeError("Cookie=secret"),
        ):
            page.sync_button.click()
            pool.tasks[0].run()
            self.app.processEvents()

        self.assertEqual(page.status_label.text(), "数据同步未完成")
        self.assertTrue(page.sync_button.isEnabled())
        self.period_mock.assert_not_called()
        self.trends_mock.assert_not_called()
        self.contents_mock.assert_not_called()

    def test_successful_worker_refreshes_each_local_query_once(self) -> None:
        """成功终态只能完整刷新一次，不得在 finished 重复查库。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(summary=period_summary(), runner=runner)
        self.period_mock.reset_mock()
        self.trends_mock.reset_mock()
        self.contents_mock.reset_mock()

        with patch(
            "ui.data_monitor_page.platform_data_sync.sync_account_data",
            return_value={
                "accountId": 12,
                "status": "success",
                "sourceMode": "direct_session",
                "errorCode": "",
                "metricCount": 1,
                "contentCount": 1,
            },
        ):
            page.sync_button.click()
            pool.tasks[0].run()
            self.app.processEvents()

        self.period_mock.assert_called_once_with(12, 7)
        self.trends_mock.assert_called_once_with(12, 7)
        self.contents_mock.assert_called_once_with(12, limit=50, offset=0)

    def test_stale_account_callbacks_cannot_mutate_current_account(self) -> None:
        """A 任务迟到的任何回调都不得与当前 B 账号竞争共享页面状态。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(
            summary=period_summary(),
            accounts=[ACCOUNT, ACCOUNT_B],
            runner=runner,
        )

        page.sync_button.click()
        task_a = pool.tasks[0]
        page.account_combo.setCurrentIndex(page.account_combo.findData(14))
        page.sync_button.click()
        task_b = pool.tasks[1]
        task_b.signals.started.emit()
        self.app.processEvents()
        self.assertEqual(page.status_label.text(), "正在同步数据…")
        self.assertFalse(page.sync_button.isEnabled())
        self.period_mock.reset_mock()
        self.trends_mock.reset_mock()
        self.contents_mock.reset_mock()

        task_a.signals.started.emit()
        task_a.signals.progressed.emit({"stage": "content_list"})
        task_a.signals.succeeded.emit({"accountId": 12, "status": "success"})
        task_a.signals.failed.emit("Cookie=secret")
        task_a.signals.finished.emit()
        self.app.processEvents()

        self.assertEqual(page.status_label.text(), "正在同步数据…")
        self.assertFalse(page.sync_button.isEnabled())
        self.period_mock.assert_not_called()
        self.trends_mock.assert_not_called()
        self.contents_mock.assert_not_called()
        self.assertFalse(runner.is_running("platform-data-sync:12"))
        self.assertTrue(runner.is_running("platform-data-sync:14"))

        task_b.signals.failed.emit("cleanup")
        task_b.signals.finished.emit()
        self.app.processEvents()

    def test_malformed_nested_values_and_dates_fail_closed(self) -> None:
        """bool/非有限数、非法日期和嵌套容器不得进入文案或绘图。"""

        summary = period_summary()
        summary["periodStart"] = "2026-02-30"
        summary["periodEnd"] = "not-a-date"
        summary["metrics"]["views"]["value"] = True
        summary["metrics"]["views"]["availability"] = {"bad": True}
        summary["metrics"]["likes"]["value"] = math.nan
        summary["metrics"]["followers_net"]["value"] = math.inf
        summary["trustedSourceMode"] = ["direct_session"]
        trends = {
            "items": [
                {"date": "2026-02-30", "metrics": {"views": 1}},
                {"date": "2026-08-18", "metrics": {"views": math.nan}},
                {"date": "2026-08-19", "metrics": {"views": math.inf}},
                {"date": "2026-08-20", "metrics": {"views": 5}},
                {"date": "2026-08-21", "metrics": {"views": True}},
                {"date": "2026-08-22", "metrics": {"views": []}},
            ],
        }
        contents = {
            "accountId": 12,
            "total": 1,
            "limit": 50,
            "offset": 0,
            "items": [
                {
                    "contentId": "bad",
                    "title": ["bad"],
                    "coverUrl": "",
                    "publishedAt": "2026-02-30T09:00:00+08:00",
                    "contentStatus": {"bad": True},
                    "contentType": "video",
                    "metrics": {
                        "views": [],
                        "likes": {},
                        "comments": math.nan,
                        "shares": True,
                    },
                }
            ],
        }

        page = self._page(summary=summary, trends=trends, contents=contents)

        self.assertEqual(page.period_label.text(), "统计区间：—")
        self.assertEqual(page.metric_values["views"].text(), "—")
        self.assertEqual(page.metric_values["likes"].text(), "—")
        self.assertEqual(page.metric_values["followers_net"].text(), "—")
        self.assertEqual(
            page.trend_chart.series_for_test("views"),
            (("2026-08-20", 5),),
        )
        for column in range(7):
            self.assertEqual(page.content_table.item(0, column).text(), "—")

        page.trend_chart.resize(320, 180)
        canvas = QPixmap(page.trend_chart.size())
        page.trend_chart.render(canvas)

    def test_failed_latest_run_keeps_last_success_and_routes_login_required(self) -> None:
        """失败若清空历史或不提供登录入口，用户无法判断数据状态。"""

        page = self._page(summary=failed_summary())
        spy = QSignalSpy(page.request_account_management)

        self.assertEqual(page.metric_values["views"].text(), "125")
        self.assertEqual(page.source_label.text(), "数据来源：会话直连")
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
