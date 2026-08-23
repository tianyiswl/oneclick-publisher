# -*- coding: utf-8 -*-
"""平台数据监测页离屏回归测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLineEdit,
    QPushButton,
)

from app_core import database, platform_data_service, platform_data_sync
from app_core.platform_data_comment_models import CommentInsightFailure
from app_core.platform_data_comment_service import COMMENT_PROGRESS
from app_core.platform_data_comment_settings import BASE_URL_KEY, MODEL_KEY
from app_core import platform_data_collectors
from app_core.platform_data_collection_errors import PlatformDataCollectionError
from app_core.platform_data_models import CollectionBatch, MetricPoint
from ui.background_task import BackgroundTaskRunner
from ui.data_monitor_page import DataMonitorPage, _CommentAiSettingsDialog


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

XHS_ACCOUNT = {
    **ACCOUNT,
    "id": 21,
    "type": 1,
    "platformName": "小红书",
    "profileName": "硅基探索",
    "userName": "硅基探索",
}

XHS_ACCOUNT_SAME_ID = {
    **XHS_ACCOUNT,
    "id": 14,
    "profileName": "硅基探索 B",
    "userName": "硅基探索 B",
}

XHS_ACCOUNT_WITH_DOUYIN_ID = {
    **XHS_ACCOUNT,
    "id": 12,
}

WECHAT_DEMO_ACCOUNT = {
    "id": 30,
    "type": 10,
    "platformName": "公众号",
    "profileName": "一键发示例主体",
    "userName": "一键发公众号（演示）",
    "filePath": "__oneclick_demo_wechat__.json",
    "remark": "演示账号：未连接真实公众号，不可执行登录或发布",
}

WECHAT_REAL_ACCOUNT = {
    "id": 31,
    "type": 10,
    "platformName": "公众号",
    "profileName": "公众号主体",
    "userName": "公众号账号",
    "filePath": "oneclick_10_safe.json",
    "remark": "",
}

CHANNELS_REAL_ACCOUNT = {
    "id": 20,
    "type": 2,
    "platformName": "视频号",
    "profileName": "视频号主体",
    "userName": "视频号账号",
    "filePath": "oneclick_2_safe.json",
    "remark": "",
}

BILIBILI_REAL_ACCOUNT = {
    "id": 21,
    "type": 5,
    "platformName": "B站",
    "profileName": "B站主体",
    "userName": "B站账号",
    "filePath": "oneclick_5_safe.json",
    "remark": "",
}

KUAISHOU_REAL_ACCOUNT = {
    "id": 22,
    "type": 4,
    "platformName": "快手",
    "profileName": "快手主体",
    "userName": "快手账号",
    "filePath": "oneclick_4_safe.json",
    "remark": "",
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


def available_contents() -> dict:
    return {
        "accountId": 12,
        "total": 1,
        "limit": 50,
        "offset": 0,
        "availability": "available",
        "warningCode": "",
        "items": [
            {
                "contentId": "content-1",
                "title": "已有作品",
                "coverUrl": "",
                "publishedAt": "2026-08-20T09:00:00+08:00",
                "contentStatus": "published",
                "contentType": "video",
                "metrics": {"views": 20},
            }
        ],
    }


def available_douyin_contents() -> dict:
    payload = available_contents()
    payload["total"] = 2
    payload["coveredCount"] = 2
    payload["items"] = [
        {
            "contentId": "work-7",
            "title": "同名作品",
            "coverUrl": "",
            "publishedAt": "2026-08-21T09:00:00+08:00",
            "contentStatus": "published",
            "contentType": "video",
            "metrics": {"views": 20, "comments": 2},
        },
        {
            "contentId": "work-8",
            "title": "同名作品",
            "coverUrl": "",
            "publishedAt": "2026-08-20T09:00:00+08:00",
            "contentStatus": "published",
            "contentType": "video",
            "metrics": {"views": 10, "comments": 1},
        },
    ]
    return payload


def comment_panel_payload(content_id: str = "work-7") -> dict:
    work_label = "第二篇" if content_id == "work-8" else "第一篇"
    first = {
        "ref": "C001",
        "body": f"{work_label}的质疑原文",
        "likeCount": 5,
        "replyCount": 1,
        "commentedAt": "2026-08-21T10:00:00+08:00",
        "labels": ["质疑"],
    }
    second = {
        "ref": "C002",
        "body": f"{work_label}的真实经历",
        "likeCount": 2,
        "replyCount": 0,
        "commentedAt": "2026-08-21T09:30:00+08:00",
        "labels": ["真实经历", "选题建议"],
    }
    return {
        "title": "同名作品",
        "lastSyncAt": "2026-08-21T10:05:00+08:00",
        "comments": [first, second],
        "candidates": [
            {
                "title": "回答这条质疑",
                "reason": "解释读者关心的问题",
                "evidence": [
                    {
                        **first,
                        "body": "不得显示的伪造证据正文",
                    }
                ],
            }
        ],
        "aiStatus": "success",
        "aiErrorCode": "",
    }


class FakeSettings:
    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})
        self.synced = 0

    def value(self, key: str, default=None):
        return self.values.get(key, default)

    def setValue(self, key: str, value: object) -> None:  # noqa: N802
        self.values[key] = value

    def sync(self) -> None:
        self.synced += 1


class FakeSecretStore:
    def __init__(self) -> None:
        self.read_count = 0
        self.writes: list[str] = []
        self.delete_count = 0

    def read(self):
        self.read_count += 1
        raise AssertionError("AI 设置对话框不得读回现有密钥")

    def write(self, secret: str) -> None:
        self.writes.append(secret)

    def delete(self) -> None:
        self.delete_count += 1


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
        return ["platform-data-sync:3:12"]

    def cancel_pending(self, key: str) -> bool:
        self.cancelled.append(key)
        return False

    def wait_for_finished(self, key: str, timeout_seconds: float) -> bool:
        self.waited.append((key, timeout_seconds))
        return self.wait_result


class MultiPrefixBlockingRunner(BlockingRunner):
    def __init__(self, *, wait_result: bool) -> None:
        super().__init__(wait_result=wait_result)
        self.prefixes: list[tuple[str, ...]] = []

    def active_keys_with_prefixes(self, prefixes: tuple[str, ...]) -> list[str]:
        self.prefixes.append(prefixes)
        return [
            "platform-data-sync:3:12",
            "platform-comment-sync:3:12:work-7",
        ]

    def is_running(self, _key: str) -> bool:
        return False


class _DirectBatchCollector:
    def __init__(self, batch: CollectionBatch) -> None:
        self.batch = batch

    def collect_direct(self, _account: dict) -> CollectionBatch:
        return self.batch

    def collect_browser_signed(self, _account: dict) -> CollectionBatch:
        raise AssertionError("直连成功不应启动浏览器")


class _CleanupFailureCollector:
    def collect_direct(self, _account: dict) -> CollectionBatch:
        raise PlatformDataCollectionError("browser_cleanup_incomplete")

    def collect_browser_signed(self, _account: dict) -> CollectionBatch:
        raise AssertionError("清理失败不应重试浏览器")


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
        registered_platforms=None,
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
        self.registry_patch = patch(
            "ui.data_monitor_page.registered_platform_types",
            return_value=registered_platforms,
            create=True,
        ) if registered_platforms is not None else None
        self.accounts_patch.start()
        self.summary_patch.start()
        self.period_mock = self.period_patch.start()
        self.trends_mock = self.trends_patch.start()
        self.contents_mock = self.contents_patch.start()
        if self.registry_patch is not None:
            self.registry_patch.start()
        self.addCleanup(self.accounts_patch.stop)
        self.addCleanup(self.summary_patch.stop)
        self.addCleanup(self.period_patch.stop)
        self.addCleanup(self.trends_patch.stop)
        self.addCleanup(self.contents_patch.stop)
        if self.registry_patch is not None:
            self.addCleanup(self.registry_patch.stop)
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

    def test_mixed_contributing_sources_render_controlled_label(self) -> None:
        """多个可信来源参与区间汇总时，页面必须显式标记混合来源。"""

        summary = period_summary()
        summary["trustedSourceMode"] = "mixed"

        page = self._page(summary=summary)

        self.assertEqual(page.source_label.text(), "数据来源：混合来源")

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

    def test_content_header_distinguishes_unavailable_history_and_true_zero(
        self,
    ) -> None:
        """作品本次未取得时不得显示为已取得零条。"""

        page = self._page()

        page._set_contents(
            {
                "availability": "unavailable",
                "warningCode": "content_list_unavailable",
                "total": 0,
                "items": [],
            }
        )
        self.assertEqual(page.content_page_label.text(), "作品数据暂未取得")
        self.assertNotIn("0 条", page.content_page_label.text())

        page._set_contents(
            {
                "availability": "unavailable",
                "warningCode": "content_payload_invalid",
                "total": 2,
                "items": [],
            }
        )
        self.assertEqual(
            page.content_page_label.text(),
            "本次未取得，显示历史 2 条",
        )

        page._set_contents(
            {
                "availability": "partial",
                "warningCode": "content_list_truncated",
                "total": 2,
                "coveredCount": 2,
                "items": [],
            }
        )
        self.assertEqual(
            page.content_page_label.text(),
            "仅取得最近 2 条 · 1-2",
        )

        page._set_contents(
            {
                "availability": "available",
                "warningCode": "",
                "total": 0,
                "items": [],
            }
        )
        self.assertEqual(page.content_page_label.text(), "已取得 0 条")

    def test_xhs_missing_content_metadata_renders_dash_and_fixed_caption(self) -> None:
        """平台缺少标题和日期时，页面只能显示不可得，不能补造元数据。"""

        page = self._page(
            accounts=[ACCOUNT, XHS_ACCOUNT],
            registered_platforms=(1, 3),
        )
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
        page._set_contents(
            {
                "availability": "available",
                "warningCode": "",
                "total": 1,
                "items": [
                    {
                        "contentId": "0123456789abcdef01234567",
                        "title": "",
                        "publishedAt": "",
                        "contentStatus": "unavailable",
                        "contentType": "unavailable",
                        "metrics": {"views": 20},
                    }
                ],
            }
        )

        self.assertEqual(page.content_table.item(0, 0).text(), "—")
        self.assertEqual(page.content_table.item(0, 1).text(), "—")
        self.assertEqual(page.content_metadata_label.text(), "平台未提供标题与发布时间")

    def test_xhs_login_failure_uses_fixed_platform_specific_copy(self) -> None:
        """小红书登录失效不能复用抖音文案或显示底层错误内容。"""

        summary = failed_summary()
        summary["latestRun"]["errorCode"] = "login_required"
        page = self._page(
            summary=summary,
            accounts=[ACCOUNT, XHS_ACCOUNT],
            registered_platforms=(1, 3),
        )
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))

        self.assertEqual(page.status_label.text(), "同步失败：需要重新登录小红书")
        self.assertTrue(page.relogin_button.isVisibleTo(page))

    def test_xhs_cards_distinguish_unsupported_from_snapshot_pending(self) -> None:
        """合同不提供的卡片与尚待跨日快照的卡片必须使用不同文案。"""

        summary = empty_summary()
        summary["metrics"]["views"]["availability"] = "unsupported"
        summary["metrics"]["followers_net"]["availability"] = "missing"
        page = self._page(
            summary=summary,
            accounts=[XHS_ACCOUNT],
            registered_platforms=(1,),
        )

        self.assertEqual(page.metric_captions["views"].text(), "平台未提供")
        self.assertEqual(page.metric_captions["followers_net"].text(), "暂未取得")

    def test_xhs_sync_persists_real_sqlite_then_page_reads_zero_contents_and_failure(self) -> None:
        """同步、事务和页面必须共享同一份本地数据，清理失败不能冒充成功。"""

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        db_patch = patch.object(database, "DB_PATH", Path(tempdir.name) / "data.db")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database.ensure_schema()
        with database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, authMode)
                VALUES (1, 'xhs-e2e.json', '小红书测试账号', 1, '小红书测试主体', 'browser')
                """
            )
            account_id = int(cursor.lastrowid)
        day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()
        batch = CollectionBatch(
            platform_type=1,
            source_mode="direct_session",
            metrics=(
                MetricPoint(
                    entity_type="account",
                    entity_key=f"account:{account_id}",
                    metric_key="followers_total",
                    raw_metric_key="fans_count",
                    metric_value=12,
                    metric_unit="count",
                    metric_scope="lifetime_total",
                    period_start=day,
                    period_end=day,
                    observed_at=f"{day}T12:00:00+08:00",
                ),
            ),
            contents=(),
            account_metrics_available=True,
            content_data_available=True,
            platform_observed_at=f"{day}T12:00:00+08:00",
        )
        original_factory = platform_data_collectors._COLLECTOR_FACTORIES[1]
        self.addCleanup(
            lambda: platform_data_collectors._COLLECTOR_FACTORIES.__setitem__(
                1, original_factory
            )
        )
        platform_data_collectors._COLLECTOR_FACTORIES[1] = (
            lambda **_dependencies: _DirectBatchCollector(batch)
        )

        success = platform_data_sync.sync_account_data(account_id)
        self.assertEqual(
            success,
            {
                "accountId": account_id,
                "status": "success",
                "sourceMode": "direct_session",
                "errorCode": "",
                "metricCount": 1,
                "contentCount": 0,
            },
        )
        self.assertEqual(
            platform_data_service.account_contents(account_id)["availability"],
            "available",
        )

        page = DataMonitorPage()
        self.addCleanup(page.deleteLater)
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
        self.app.processEvents()
        self.assertEqual(page.account_combo.currentData(), account_id)
        self.assertEqual(page.metric_values["followers_total"].text(), "12")
        self.assertEqual(page.content_page_label.text(), "已取得 0 条")

        platform_data_collectors._COLLECTOR_FACTORIES[1] = (
            lambda **_dependencies: _CleanupFailureCollector()
        )
        failed = platform_data_sync.sync_account_data(account_id)
        latest = platform_data_service.account_data_summary(account_id)
        page.refresh()
        self.app.processEvents()

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["errorCode"], "browser_cleanup_incomplete")
        self.assertEqual(
            latest["latestRun"],
            {
                "status": "failed",
                "sourceMode": "direct_session",
                "errorCode": "browser_cleanup_incomplete",
                "metricCount": 0,
                "finishedAt": latest["latestRun"]["finishedAt"],
            },
        )
        self.assertEqual(latest["metrics"], {"followers_total": 12})
        self.assertEqual(page.status_label.text(), "同步失败：浏览器会话未能完整关闭")
        self.assertEqual(page.metric_values["followers_total"].text(), "12")
        self.assertEqual(page.content_page_label.text(), "作品数据暂未取得")

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

    def test_partial_truncated_status_reports_recent_content_count(self) -> None:
        """作品列表截断且已有保存记录时，顶部应说明最近取得的条数。"""

        summary = period_summary(
            status="partial_success",
            error_code="content_list_truncated",
        )
        contents = available_contents()
        contents.update(
            {
                "accountId": 21,
                "availability": "partial",
                "warningCode": "content_list_truncated",
                "total": 2,
                "coveredCount": 2,
                "items": [
                    *contents["items"],
                    {
                        "contentId": "content-2",
                        "title": "另一条已有作品",
                        "coverUrl": "",
                        "publishedAt": "2026-08-19T09:00:00+08:00",
                        "contentStatus": "published",
                        "contentType": "video",
                        "metrics": {"views": 10},
                    },
                ],
            }
        )
        page = self._page(
            summary=summary,
            contents=contents,
            accounts=[XHS_ACCOUNT],
            registered_platforms=(1,),
        )

        self.assertEqual(
            page.status_label.text(),
            "账号趋势已更新，已取得最近 2 条作品",
        )
        self.assertNotIn("作品数据未取得", page.status_label.text())

    def test_partial_truncated_status_requires_matching_content_state(self) -> None:
        """截断批次与作品查询状态不一致时不得声称已取得最近作品。"""

        base_contents = available_contents()
        base_contents.update(
            {
                "availability": "partial",
                "warningCode": "content_list_truncated",
                "total": 2,
                "coveredCount": 2,
            }
        )
        cases = (
            {"availability": "available"},
            {"availability": "unavailable"},
            {"warningCode": "content_payload_invalid"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                contents = {**base_contents, **overrides}
                page = self._page(
                    summary=period_summary(
                        status="partial_success",
                        error_code="content_list_truncated",
                    ),
                    contents=contents,
                )
                self.assertEqual(
                    page.status_label.text(),
                    "账号趋势已更新，作品数据未取得",
                )

    def test_partial_truncated_status_rejects_uncontrolled_covered_count(
        self,
    ) -> None:
        """截断作品数量必须是不超过总数的正整数。"""

        for covered_count in (0, -1, "2", 3):
            with self.subTest(covered_count=covered_count):
                contents = available_contents()
                contents.update(
                    {
                        "availability": "partial",
                        "warningCode": "content_list_truncated",
                        "total": 2,
                        "coveredCount": covered_count,
                    }
                )
                page = self._page(
                    summary=period_summary(
                        status="partial_success",
                        error_code="content_list_truncated",
                    ),
                    contents=contents,
                )
                self.assertEqual(
                    page.status_label.text(),
                    "账号趋势已更新，作品数据未取得",
                )

    def test_content_list_unavailable_with_history_keeps_unavailable_status(
        self,
    ) -> None:
        """作品查询不可用时，即使保留历史条数也不得显示为本次取得。"""

        contents = available_contents()
        contents.update(
            {
                "availability": "unavailable",
                "warningCode": "content_list_unavailable",
                "total": 2,
                "coveredCount": 2,
            }
        )
        page = self._page(
            summary=period_summary(
                status="partial_success",
                error_code="content_list_unavailable",
            ),
            contents=contents,
        )

        self.assertEqual(
            page.status_label.text(),
            "账号趋势已更新，作品数据未取得",
        )

    def test_registered_platform_accounts_are_listed_and_missing_metrics_render_dash(self) -> None:
        """平台过滤或缺失显示回退会把未支持账号或伪零暴露给用户。"""

        page = self._page()

        self.assertEqual(
            [page.platform_combo.itemData(i) for i in range(page.platform_combo.count())],
            [3, 1],
        )
        self.assertEqual(page.account_combo.count(), 1)
        self.assertEqual(page.account_combo.currentData(), 12)
        self.assertEqual(
            {label.text() for label in page.metric_values.values()},
            {"—"},
        )

    def test_domestic_registered_platforms_show_only_real_accounts(self) -> None:
        """演示账号进入监测选择器会让不可执行账号冒充可同步主体。"""

        page = self._page(
            accounts=[
                WECHAT_DEMO_ACCOUNT,
                WECHAT_REAL_ACCOUNT,
                CHANNELS_REAL_ACCOUNT,
                BILIBILI_REAL_ACCOUNT,
                KUAISHOU_REAL_ACCOUNT,
            ],
            registered_platforms=(2, 4, 5, 10),
        )

        self.assertEqual(
            [page.platform_combo.itemText(i) for i in range(page.platform_combo.count())],
            ["公众号", "视频号", "B站", "快手"],
        )
        self.assertEqual(page.platform_combo.currentData(), 10)
        self.assertEqual(page.account_combo.currentData(), WECHAT_REAL_ACCOUNT["id"])
        self.assertNotIn(
            WECHAT_DEMO_ACCOUNT["id"],
            [page.account_combo.itemData(i) for i in range(page.account_combo.count())],
        )

    def test_monitor_excludes_a_platform_with_only_demo_accounts(self) -> None:
        """仅有演示账号的平台必须完全从监测页消失。"""

        page = self._page(
            accounts=[WECHAT_DEMO_ACCOUNT],
            registered_platforms=(10,),
        )

        self.assertEqual(page.platform_combo.count(), 0)
        self.assertEqual(page.account_combo.count(), 0)

    def test_late_result_from_previous_domestic_platform_keeps_current_subject(self) -> None:
        """旧视频号任务迟到完成时不能把页面切回已选的 B 站主体。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(
            accounts=[CHANNELS_REAL_ACCOUNT, BILIBILI_REAL_ACCOUNT],
            registered_platforms=(2, 5),
            runner=runner,
        )

        self.assertEqual(page._current_subject_identity(), (2, 20))
        page.sync_button.click()
        channels_task = pool.tasks[0]
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(5))
        self.assertEqual(page._current_subject_identity(), (5, 21))

        channels_task.signals.started.emit()
        channels_task.signals.succeeded.emit(
            {"accountId": 20, "status": "success"}
        )
        channels_task.signals.finished.emit()
        self.app.processEvents()

        self.assertEqual(page._current_subject_identity(), (5, 21))

    def test_fixed_failure_copies_cover_platform_and_local_failures(self) -> None:
        """固定错误码若回退为异常类别，用户无法判断下一步且可能暴露异常原文。"""

        expected = {
            "login_required": "需要重新登录视频号",
            "verification_required": "需要完成视频号验证",
            "metric_payload_invalid": "平台页面或数据接口已变化",
            "browser_cleanup_incomplete": "浏览器会话未能完整关闭",
            "sync_persist_failed": "本地数据保存失败",
            "validation_readback_mismatch": "本地数据读回失败",
        }
        for code, text in expected.items():
            with self.subTest(code=code):
                summary = failed_summary()
                summary["latestRun"]["errorCode"] = code
                page = self._page(
                    summary=summary,
                    accounts=[CHANNELS_REAL_ACCOUNT],
                    registered_platforms=(2,),
                )
                self.assertEqual(page.status_label.text(), f"同步失败：{text}")
                self.assertNotIn("Cookie=secret", page.status_label.text())

    def test_platform_then_subject_selectors_filter_and_restore_per_platform(self) -> None:
        """平台切换若复用全局主体记忆，会把另一个平台的主体带回来。"""

        page = self._page(
            accounts=[ACCOUNT, ACCOUNT_B, XHS_ACCOUNT],
            registered_platforms=(1, 3),
        )

        self.assertEqual(
            [page.platform_combo.itemData(i) for i in range(page.platform_combo.count())],
            [3, 1],
        )
        self.assertEqual(page.account_combo.count(), 2)
        page.account_combo.setCurrentIndex(page.account_combo.findData(14))
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
        self.assertEqual(page.account_combo.count(), 1)
        self.assertEqual(page.account_combo.currentData(), 21)
        self.assertEqual(page.account_combo.currentText(), "硅基探索｜硅基探索")
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(3))
        self.assertEqual(page.account_combo.currentData(), 14)

    def test_same_subject_id_isolated_between_platforms(self) -> None:
        """主体 ID 相同时，平台记忆若未分桶会选回错误主体。"""

        page = self._page(
            accounts=[ACCOUNT, ACCOUNT_B, XHS_ACCOUNT_SAME_ID, XHS_ACCOUNT],
            registered_platforms=(1, 3),
        )

        page.account_combo.setCurrentIndex(page.account_combo.findData(14))
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
        page.account_combo.setCurrentIndex(page.account_combo.findData(21))
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(3))

        self.assertEqual(page.account_combo.currentData(), 14)
        self.assertEqual(page.account_combo.currentText(), "数据主体 B｜抖音账号 B")

    def test_switch_platform_clears_previous_rows_before_new_account_readback(self) -> None:
        """切换平台后旧主体的作品行必须先清空，不能短暂冒充新主体数据。"""

        page = self._page(
            accounts=[ACCOUNT, XHS_ACCOUNT],
            contents=available_contents(),
            registered_platforms=(1, 3),
        )

        self.assertGreater(page.content_table.rowCount(), 0)
        with patch.object(page, "_render_data") as render:
            page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
            self.assertEqual(page.content_table.rowCount(), 0)
            render.assert_called_once_with(include_contents=True)

    def test_platform_without_real_subject_is_hidden(self) -> None:
        """没有真实主体的平台不能以空选择器形式冒充可同步平台。"""

        page = self._page(
            accounts=[ACCOUNT],
            registered_platforms=(1, 3),
        )

        self.assertEqual(
            [page.platform_combo.itemData(i) for i in range(page.platform_combo.count())],
            [3],
        )
        self.assertEqual(page.account_combo.currentData(), ACCOUNT["id"])

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
        self.assertFalse(runner.is_running("platform-data-sync:3:12"))

    def test_xhs_sync_click_uses_official_response_and_atomic_database_readback(self) -> None:
        """正式同步不能被要求在同一页面同时找到账号卡片和作品详情卡片。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        calls: list[tuple[int, dict]] = []

        def sync(account_id: int, report, **kwargs) -> dict:
            calls.append((account_id, kwargs))
            report({"stage": "completed", "message": "ignored"})
            return {
                "accountId": account_id,
                "status": "success",
                "sourceMode": "browser_signed",
                "errorCode": "",
                "metricCount": 2,
                "contentCount": 1,
            }

        with patch("ui.data_monitor_page.platform_data_sync.sync_account_data", sync):
            page = self._page(accounts=[XHS_ACCOUNT], runner=runner)
            page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
            self.app.processEvents()
            page.sync_button.click()
            pool.tasks[0].run()
            self.app.processEvents()

        self.assertEqual(len(calls), 1)
        account_id, kwargs = calls[0]
        self.assertEqual(account_id, XHS_ACCOUNT["id"])
        self.assertEqual(kwargs, {})

    def test_douyin_sync_click_keeps_ordinary_sync_arguments(self) -> None:
        """把小红书验收参数误传给抖音会改变已有同步协议。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        calls: list[tuple[int, dict]] = []

        def sync(account_id: int, report, **kwargs) -> dict:
            calls.append((account_id, kwargs))
            return {
                "accountId": account_id,
                "status": "success",
                "sourceMode": "direct_session",
                "errorCode": "",
                "metricCount": 2,
                "contentCount": 1,
            }

        with patch("ui.data_monitor_page.platform_data_sync.sync_account_data", sync):
            page = self._page(accounts=[ACCOUNT], runner=runner)
            page.sync_button.click()
            pool.tasks[0].run()
            self.app.processEvents()

        self.assertEqual(calls, [(ACCOUNT["id"], {})])

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

    def test_controlled_xhs_failure_diagnostic_is_visible_after_sync(self) -> None:
        """受控定位信息若被刷新覆盖，下一次真实失败仍无法判断落点。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(accounts=[XHS_ACCOUNT], runner=runner)
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
        self.app.processEvents()

        result = {
            "accountId": XHS_ACCOUNT["id"],
            "status": "failed",
            "sourceMode": "browser_signed",
            "errorCode": "metric_payload_invalid",
            "metricCount": 0,
            "contentCount": 0,
            "diagnostics": {
                "cleanup": {"closed": True, "aliveResourceCount": 0},
                "failure": {
                    "endpoint": "account_home",
                    "stage": "json_decode",
                    "reason": "invalid_json",
                },
            },
        }
        with patch(
            "ui.data_monitor_page.platform_data_sync.sync_account_data",
            return_value=result,
        ):
            page.sync_button.click()
            pool.tasks[0].run()
            self.app.processEvents()

        self.assertEqual(
            page.status_label.text(),
            "同步失败：平台页面或数据接口已变化（接口=账号主页；阶段=JSON解析；原因=JSON格式无效）",
        )

    def test_immediate_persist_failure_uses_fixed_copy_without_exception_text(self) -> None:
        """本地保存异常作为受控结果返回时，页面必须立刻显示固定文案。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(accounts=[XHS_ACCOUNT], runner=runner)
        with patch(
            "ui.data_monitor_page.platform_data_sync.sync_account_data",
            return_value={
                "accountId": XHS_ACCOUNT["id"],
                "status": "failed",
                "sourceMode": "browser_signed",
                "errorCode": "sync_persist_failed",
                "metricCount": 0,
                "contentCount": 0,
            },
        ):
            page.sync_button.click()
            pool.tasks[0].run()
            self.app.processEvents()

        self.assertEqual(page.status_label.text(), "同步失败：本地数据保存失败")
        self.assertNotIn("SQLite", page.status_label.text())

    def test_immediate_login_required_result_shows_relogin_action(self) -> None:
        """本轮回调已确认登录失效时，不应等下一次摘要重读才出现登录入口。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(accounts=[XHS_ACCOUNT], runner=runner)
        self.assertFalse(page.relogin_button.isVisibleTo(page))
        with patch(
            "ui.data_monitor_page.platform_data_sync.sync_account_data",
            return_value={
                "accountId": XHS_ACCOUNT["id"],
                "status": "failed",
                "sourceMode": "browser_signed",
                "errorCode": "login_required",
                "metricCount": 0,
                "contentCount": 0,
            },
        ):
            page.sync_button.click()
            pool.tasks[0].run()
            self.app.processEvents()

        self.assertEqual(page.status_label.text(), "同步失败：需要重新登录小红书")
        self.assertTrue(page.relogin_button.isVisibleTo(page))

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
        self.assertFalse(runner.is_running("platform-data-sync:3:12"))
        self.assertTrue(runner.is_running("platform-data-sync:3:14"))

        task_b.signals.failed.emit("cleanup")
        task_b.signals.finished.emit()
        self.app.processEvents()

    def test_stale_cross_platform_same_id_callbacks_cannot_mutate_current_page(self) -> None:
        """同 ID 的旧平台任务回调若只按 ID 门禁，会污染当前平台页面。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(
            summary=period_summary(),
            contents=available_contents(),
            accounts=[ACCOUNT, XHS_ACCOUNT_WITH_DOUYIN_ID],
            registered_platforms=(1, 3),
            runner=runner,
        )

        page.sync_button.click()
        douyin_task = pool.tasks[0]
        page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
        page.sync_button.click()
        xhs_task = pool.tasks[1]
        xhs_task.signals.started.emit()
        self.app.processEvents()
        self.assertEqual(page.platform_combo.currentData(), 1)
        self.assertEqual(page.account_combo.currentData(), 12)
        self.assertEqual(page.status_label.text(), "正在同步数据…")
        self.assertFalse(page.sync_button.isEnabled())
        self.assertEqual(page.content_table.rowCount(), 1)
        self.period_mock.reset_mock()
        self.trends_mock.reset_mock()
        self.contents_mock.reset_mock()

        douyin_task.signals.started.emit()
        douyin_task.signals.progressed.emit({"stage": "content_list"})
        douyin_task.signals.succeeded.emit({"accountId": 12, "status": "success"})
        douyin_task.signals.failed.emit("stale")
        douyin_task.signals.finished.emit()
        self.app.processEvents()

        self.assertEqual(page.platform_combo.currentData(), 1)
        self.assertEqual(page.account_combo.currentData(), 12)
        self.assertEqual(page.status_label.text(), "正在同步数据…")
        self.assertFalse(page.sync_button.isEnabled())
        self.assertEqual(page.content_table.rowCount(), 1)
        self.period_mock.assert_not_called()
        self.trends_mock.assert_not_called()
        self.contents_mock.assert_not_called()
        self.assertFalse(runner.is_running("platform-data-sync:3:12"))
        self.assertTrue(runner.is_running("platform-data-sync:1:12"))

        xhs_task.signals.failed.emit("cleanup")
        xhs_task.signals.finished.emit()
        self.app.processEvents()

    def test_offscreen_account_error_persists_until_next_sync_starts(self) -> None:
        """未落库的 A 失败必须跨账号切换保留，且只由 A 的新一轮覆盖。"""

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
        task_a.signals.started.emit()
        self.app.processEvents()
        page.account_combo.setCurrentIndex(page.account_combo.findData(14))
        task_a.signals.failed.emit("not persisted")
        task_a.signals.finished.emit()
        self.app.processEvents()

        page.account_combo.setCurrentIndex(page.account_combo.findData(12))
        self.assertEqual(page.status_label.text(), "数据同步未完成")
        self.assertTrue(page.sync_button.isEnabled())

        page.sync_button.click()
        next_task_a = pool.tasks[1]
        next_task_a.signals.started.emit()
        self.app.processEvents()
        self.assertEqual(page.status_label.text(), "正在同步数据…")

        next_task_a.signals.failed.emit("cleanup")
        next_task_a.signals.finished.emit()
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

    def test_comment_work_selection_is_single_and_uses_only_stable_content_id(self) -> None:
        """如果表格多选或把整行数据藏入 UserRole，后台任务会用错作品或扩大敏感数据面。"""

        page = self._page(contents=available_douyin_contents())

        self.assertEqual(
            page.content_table.selectionMode(),
            QAbstractItemView.SelectionMode.SingleSelection,
        )
        self.assertIsNone(page.content_table.selected_content())
        self.assertFalse(page.comment_sync_button.isEnabled())
        self.assertEqual(
            page.content_table.item(0, 0).data(Qt.ItemDataRole.UserRole),
            "work-7",
        )
        self.assertIsNone(
            page.content_table.item(0, 1).data(Qt.ItemDataRole.UserRole)
        )
        with patch(
            "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
            return_value=comment_panel_payload("work-8"),
        ):
            page.content_table.selectRow(1)
        self.assertEqual(
            page.content_table.selected_content(),
            {"contentId": "work-8", "title": "同名作品"},
        )

    def test_comment_sync_is_disabled_without_selection_and_unavailable_is_explicit(self) -> None:
        """未取得作品时如果被误解成没有评论，用户会对空数据做出错误判断。"""

        page = self._page(contents=empty_contents())

        self.assertFalse(page.comment_panel.isHidden())
        self.assertFalse(page.comment_sync_button.isEnabled())
        self.assertEqual(
            page.comment_status_label.text(),
            "抖音作品列表尚未取得，暂时无法同步评论",
        )

    def test_non_douyin_platform_hides_and_disables_comment_panel(self) -> None:
        """首版若在其他平台露出入口，会让不支持的平台冒充可用。"""

        page = self._page(
            accounts=[XHS_ACCOUNT],
            contents=available_douyin_contents(),
            registered_platforms=(1,),
        )

        self.assertTrue(page.comment_panel.isHidden())
        self.assertFalse(page.comment_sync_button.isEnabled())
        self.assertFalse(page.comment_ai_settings_button.isEnabled())

    def test_selected_work_loads_only_its_local_comments_and_latest_insight(self) -> None:
        """作品标题可能相同，面板必须只按选中作品的稳定 ID 读回。"""

        page = self._page(contents=available_douyin_contents())
        calls: list[tuple[int, str, str, int]] = []

        def load(account_id: int, content_id: str, label: str, limit: int) -> dict:
            calls.append((account_id, content_id, label, limit))
            return comment_panel_payload(content_id)

        with patch(
            "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
            side_effect=load,
        ):
            page.content_table.selectRow(1)

        self.assertEqual(calls, [(12, "work-8", "全部", 100)])
        self.assertEqual(page.comment_table.rowCount(), 2)
        self.assertEqual(page.comment_table.item(0, 0).text(), "第二篇的质疑原文")
        self.assertEqual(page.comment_candidate_tree.topLevelItemCount(), 1)
        self.assertIn(
            "回答这条质疑",
            page.comment_candidate_tree.topLevelItem(0).text(0),
        )

    def test_comment_progress_accepts_only_exact_fixed_stage_and_message(self) -> None:
        """工作线程的自由文本若进入页面，异常、路径或凭据可能被暴露。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(contents=available_douyin_contents(), runner=runner)
        with patch(
            "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
            return_value=comment_panel_payload(),
        ):
            page.content_table.selectRow(0)
        page.comment_sync_button.click()
        task = pool.tasks[0]
        task.signals.started.emit()
        self.app.processEvents()
        baseline = page.comment_status_label.text()

        task.signals.progressed.emit(
            {"stage": "collecting", "message": "Cookie=secret /private/path"}
        )
        task.signals.progressed.emit(
            {
                "stage": "collecting",
                "message": COMMENT_PROGRESS["collecting"],
                "raw": "secret",
            }
        )
        self.app.processEvents()
        self.assertEqual(page.comment_status_label.text(), baseline)

        task.signals.progressed.emit(
            {"stage": "collecting", "message": COMMENT_PROGRESS["collecting"]}
        )
        self.app.processEvents()
        self.assertEqual(
            page.comment_status_label.text(), COMMENT_PROGRESS["collecting"]
        )
        self.assertNotIn("secret", page.comment_status_label.text())

    def test_stale_comment_callbacks_cannot_overwrite_current_work(self) -> None:
        """旧作品任务迟到时，不得覆盖用户刚选中的另一篇作品。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(contents=available_douyin_contents(), runner=runner)
        with patch(
            "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
            side_effect=lambda _account, content, _label, _limit: comment_panel_payload(content),
        ):
            page.content_table.selectRow(0)
            page.comment_sync_button.click()
            old_task = pool.tasks[0]
            page.content_table.selectRow(1)
            expected_status = page.comment_status_label.text()
            expected_body = page.comment_table.item(0, 0).text()

            old_task.signals.started.emit()
            old_task.signals.progressed.emit(
                {"stage": "collecting", "message": COMMENT_PROGRESS["collecting"]}
            )
            old_task.signals.succeeded.emit({"status": "success", "errorCode": ""})
            old_task.signals.failed.emit("Cookie=secret")
            old_task.signals.finished.emit()
            self.app.processEvents()

        self.assertEqual(page.content_table.selected_content()["contentId"], "work-8")
        self.assertEqual(page.comment_status_label.text(), expected_status)
        self.assertEqual(page.comment_table.item(0, 0).text(), expected_body)

    def test_stale_comment_callbacks_cannot_cross_account_or_platform(self) -> None:
        """仅比对作品 ID 会让同 ID 的旧账号或旧平台任务污染当前页面。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(
            contents=available_douyin_contents(),
            accounts=[ACCOUNT, ACCOUNT_B, XHS_ACCOUNT_WITH_DOUYIN_ID],
            registered_platforms=(1, 3),
            runner=runner,
        )
        with patch(
            "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
            side_effect=lambda _account, content, _label, _limit: comment_panel_payload(content),
        ):
            page.content_table.selectRow(0)
            page.comment_sync_button.click()
            old_task = pool.tasks[0]
            page.account_combo.setCurrentIndex(page.account_combo.findData(14))
            page.content_table.selectRow(0)
            account_status = page.comment_status_label.text()
            account_body = page.comment_table.item(0, 0).text()

            old_task.signals.progressed.emit(
                {"stage": "collecting", "message": COMMENT_PROGRESS["collecting"]}
            )
            old_task.signals.succeeded.emit({"status": "success", "errorCode": ""})
            old_task.signals.failed.emit("private")
            self.app.processEvents()
            self.assertEqual(page._current_comment_identity(), (3, 14, "work-7"))
            self.assertEqual(page.comment_status_label.text(), account_status)
            self.assertEqual(page.comment_table.item(0, 0).text(), account_body)

            page.platform_combo.setCurrentIndex(page.platform_combo.findData(1))
            platform_status = page.comment_status_label.text()
            old_task.signals.progressed.emit(
                {"stage": "collecting", "message": COMMENT_PROGRESS["collecting"]}
            )
            old_task.signals.succeeded.emit({"status": "success", "errorCode": ""})
            old_task.signals.finished.emit()
            self.app.processEvents()

        self.assertTrue(page.comment_panel.isHidden())
        self.assertEqual(page.comment_status_label.text(), platform_status)
        self.assertEqual(page.comment_table.rowCount(), 0)

    def test_older_generation_for_same_work_cannot_overwrite_new_task(self) -> None:
        """同一作品的旧任务信号如果晚到，不得覆盖新一代任务进度。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(contents=available_douyin_contents(), runner=runner)
        with patch(
            "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
            return_value=comment_panel_payload(),
        ):
            page.content_table.selectRow(0)
            page.comment_sync_button.click()
            old_task = pool.tasks[0]
            old_task.signals.finished.emit()
            self.app.processEvents()
            page.comment_sync_button.click()
            new_task = pool.tasks[1]
            new_task.signals.started.emit()
            new_task.signals.progressed.emit(
                {"stage": "collecting", "message": COMMENT_PROGRESS["collecting"]}
            )
            self.app.processEvents()

            old_task.signals.succeeded.emit(
                {"status": "failed", "errorCode": "comment_payload_invalid"}
            )
            old_task.signals.failed.emit("private traceback")
            old_task.signals.finished.emit()
            self.app.processEvents()

        self.assertEqual(
            page.comment_status_label.text(), COMMENT_PROGRESS["collecting"]
        )
        self.assertFalse(page.comment_sync_button.isEnabled())

    def test_comment_sync_is_keyed_by_platform_account_and_content(self) -> None:
        """同一作品重复点击只能入队一个受控任务。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(contents=available_douyin_contents(), runner=runner)
        with patch(
            "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
            return_value=comment_panel_payload(),
        ):
            page.content_table.selectRow(0)
        page.comment_sync_button.click()
        page.comment_sync_button.click()

        self.assertEqual(len(pool.tasks), 1)
        self.assertEqual(
            list(runner.active),
            ["platform-comment-sync:3:12:work-7"],
        )

    def test_comment_filter_and_candidate_evidence_use_canonical_local_rows(self) -> None:
        """选题证据若直接信任候选对象，可能展示不在本地评论列表中的伪造正文。"""

        page = self._page(contents=available_douyin_contents())
        with patch(
            "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
            return_value=comment_panel_payload(),
        ):
            page.content_table.selectRow(0)

        page.comment_filter_combo.setCurrentIndex(
            page.comment_filter_combo.findText("质疑")
        )
        self.app.processEvents()
        self.assertEqual(page.comment_table.rowCount(), 1)
        self.assertEqual(page.comment_table.item(0, 0).text(), "第一篇的质疑原文")
        self.assertIsNone(
            page.comment_table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        )
        evidence = page.comment_candidate_tree.topLevelItem(0).child(0).text(0)
        self.assertIn("第一篇的质疑原文", evidence)
        self.assertNotIn("不得显示的伪造证据正文", evidence)

    def test_comment_panel_has_no_platform_write_actions_or_identifiers(self) -> None:
        """评论洞察不是评论管理，不得出现任何平台写操作或内部标识。"""

        page = self._page(contents=available_douyin_contents())
        with patch(
            "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
            return_value=comment_panel_payload(),
        ):
            page.content_table.selectRow(0)

        action_texts = [
            control.text()
            for control in page.comment_panel.findChildren(QPushButton)
        ]
        for forbidden in ("回复", "删除", "点赞", "发布"):
            self.assertTrue(all(forbidden not in text for text in action_texts))
        visible = " ".join(
            page.comment_table.item(row, column).text()
            for row in range(page.comment_table.rowCount())
            for column in range(page.comment_table.columnCount())
        )
        visible += " " + page.comment_candidate_tree.topLevelItem(0).text(0)
        visible += " " + page.comment_candidate_tree.topLevelItem(0).child(0).text(0)
        for forbidden in ("work-7", "C001", "commentKey", "accountId", "contentId"):
            self.assertNotIn(forbidden, visible)

    def test_comment_schema_failure_uses_fixed_copy_and_resets_buttons(self) -> None:
        """集成线尚未接建表入口时，查询或同步失败不得崩溃或暴露 SQLite 异常。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(contents=available_douyin_contents(), runner=runner)
        with patch(
            "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
            side_effect=CommentInsightFailure("comment_payload_invalid"),
        ):
            page.content_table.selectRow(0)
        self.assertEqual(
            page.comment_status_label.text(), "评论数据暂未接通，请稍后再试"
        )

        with patch(
            "ui.data_monitor_page.platform_data_comment_service.sync_comments",
            return_value={"status": "failed", "errorCode": "comment_payload_invalid"},
        ):
            page.comment_sync_button.click()
            pool.tasks[0].run()
            self.app.processEvents()
        self.assertEqual(
            page.comment_status_label.text(), "评论数据暂未接通，请稍后再试"
        )
        self.assertTrue(page.comment_sync_button.isEnabled())
        self.assertTrue(page.comment_ai_settings_button.isEnabled())

    def test_successful_comment_sync_reloads_panel_and_resets_buttons(self) -> None:
        """成功终态若没有重读本地数据并恢复按钮，用户会看到旧评论或无法再次同步。"""

        pool = QueuedPool()
        runner = BackgroundTaskRunner()
        runner.pool = pool
        page = self._page(contents=available_douyin_contents(), runner=runner)
        payload_reads = 0

        def load(*_args) -> dict:
            nonlocal payload_reads
            payload_reads += 1
            return comment_panel_payload()

        with (
            patch(
                "ui.data_monitor_page.platform_data_comment_service.comment_panel_payload",
                side_effect=load,
            ),
            patch(
                "ui.data_monitor_page.platform_data_comment_service.sync_comments",
                return_value={"status": "success", "errorCode": ""},
            ),
        ):
            page.content_table.selectRow(0)
            page.comment_sync_button.click()
            self.assertFalse(page.comment_sync_button.isEnabled())
            self.assertFalse(page.comment_ai_settings_button.isEnabled())
            pool.tasks[0].run()
            self.app.processEvents()

        self.assertEqual(payload_reads, 2)
        self.assertEqual(page.comment_table.rowCount(), 2)
        self.assertTrue(page.comment_sync_button.isEnabled())
        self.assertTrue(page.comment_ai_settings_button.isEnabled())

    def test_ai_settings_dialog_never_reads_existing_secret_and_uses_fake_native_store(self) -> None:
        """设置页若读回旧密钥，它就会进入可见控件和 UI 内存。"""

        settings = FakeSettings(
            {
                BASE_URL_KEY: "https://ai.example.com/v1",
                MODEL_KEY: "model-old",
            }
        )
        secret_store = FakeSecretStore()
        dialog = _CommentAiSettingsDialog(
            settings=settings,
            secret_store=secret_store,
        )
        self.addCleanup(dialog.deleteLater)

        self.assertEqual(secret_store.read_count, 0)
        self.assertEqual(dialog.secret_input.text(), "")
        self.assertEqual(
            dialog.secret_input.echoMode(), QLineEdit.EchoMode.Password
        )
        self.assertIn("已配置", dialog.secret_status_label.text())
        dialog.model_input.setText("model-new")
        dialog.secret_input.setText("new-private-key")
        dialog.save_button.click()

        self.assertEqual(secret_store.read_count, 0)
        self.assertEqual(secret_store.writes, ["new-private-key"])
        self.assertEqual(settings.values[BASE_URL_KEY], "https://ai.example.com/v1")
        self.assertEqual(settings.values[MODEL_KEY], "model-new")
        self.assertEqual(dialog.secret_input.text(), "")

    def test_ai_settings_clear_is_explicit_and_close_has_no_side_effect(self) -> None:
        """关闭对话框不能更改凭据；只有显式清除才能删除密钥。"""

        settings = FakeSettings()
        secret_store = FakeSecretStore()
        dialog = _CommentAiSettingsDialog(
            settings=settings,
            secret_store=secret_store,
            secret_configured=False,
        )
        self.addCleanup(dialog.deleteLater)
        dialog.reject()
        self.assertEqual(secret_store.writes, [])
        self.assertEqual(secret_store.delete_count, 0)
        self.assertEqual(settings.synced, 0)

        clear_dialog = _CommentAiSettingsDialog(
            settings=settings,
            secret_store=secret_store,
            secret_configured=True,
        )
        self.addCleanup(clear_dialog.deleteLater)
        clear_dialog.clear_secret_button.click()
        self.assertEqual(secret_store.read_count, 0)
        self.assertEqual(secret_store.delete_count, 1)
        self.assertIn("未配置", clear_dialog.secret_status_label.text())

    def test_shutdown_collects_data_and_comment_task_prefixes(self) -> None:
        """客户端关闭时遗漏评论任务，会让浏览器会话留在后台。"""

        runner = MultiPrefixBlockingRunner(wait_result=True)
        page = self._page(runner=runner)

        self.assertTrue(page.shutdown())
        self.assertEqual(
            runner.prefixes,
            [("platform-data-sync", "platform-comment-sync")],
        )
        self.assertEqual(
            runner.cancelled,
            [
                "platform-data-sync:3:12",
                "platform-comment-sync:3:12:work-7",
            ],
        )
        self.assertEqual(len(runner.waited), 2)

    def test_shutdown_refuses_when_running_sync_cannot_finish(self) -> None:
        """采集 worker 未归零时主窗口不能继续关闭全局浏览器。"""

        runner = BlockingRunner(wait_result=False)
        page = self._page(runner=runner)

        self.assertFalse(page.shutdown())
        self.assertEqual(runner.cancelled, ["platform-data-sync:3:12"])
        self.assertEqual(len(runner.waited), 1)
        self.assertLessEqual(runner.waited[0][1], 5.0)


if __name__ == "__main__":
    unittest.main()
