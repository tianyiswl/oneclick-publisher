# -*- coding: utf-8 -*-
"""平台数据同步编排测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app_core import platform_data_sync
from app_core.douyin_data_collector import DouyinDataCollectionError
from app_core.platform_data_models import (
    CollectionBatch,
    ContentRecord,
    MetricPoint,
)


def account_point() -> MetricPoint:
    return MetricPoint(
        entity_type="account",
        entity_key="account:12",
        metric_key="views",
        raw_metric_key="play",
        metric_value=125,
        metric_unit="count",
        metric_scope="daily_increment",
        period_start="2026-08-20",
        period_end="2026-08-20",
        observed_at="2026-08-20T12:00:00+08:00",
    )


def content_point() -> MetricPoint:
    return MetricPoint(
        entity_type="content",
        entity_key="aweme-1",
        metric_key="views",
        raw_metric_key="play",
        metric_value=400,
        metric_unit="count",
        metric_scope="lifetime_total",
        period_start="2026-08-20",
        period_end="2026-08-20",
        observed_at="2026-08-20T12:00:00+08:00",
    )


def valid_batch(source_mode: str, *, warning_code: str = "") -> CollectionBatch:
    return CollectionBatch(
        platform_type=3,
        source_mode=source_mode,
        metrics=(account_point(), content_point()),
        contents=(
            ContentRecord(
                content_id="aweme-1",
                title="作品一",
                cover_url="https://creator.douyin.com/cover/1.jpg",
                published_at="2026-08-20T12:00:00+08:00",
                content_status="published",
                content_type="video",
            ),
        ),
        account_metrics_available=True,
        content_data_available=True,
        platform_observed_at="2026-08-20T12:00:00+08:00",
        warning_code=warning_code,
    )


def account_only_batch(
    source_mode: str,
    *,
    warning_code: str = "content_list_unavailable",
) -> CollectionBatch:
    return CollectionBatch(
        platform_type=3,
        source_mode=source_mode,
        metrics=(account_point(),),
        contents=(),
        account_metrics_available=True,
        content_data_available=False,
        platform_observed_at="2026-08-20T12:00:00+08:00",
        warning_code=warning_code,
    )


class FakeCollector:
    def __init__(
        self,
        direct_result,
        *,
        browser_result=None,
    ) -> None:
        self.direct_result = direct_result
        self.browser_result = browser_result
        self.direct_calls = 0
        self.browser_calls = 0

    def collect_direct(self, account: dict) -> CollectionBatch:
        self.direct_calls += 1
        if isinstance(self.direct_result, BaseException):
            raise self.direct_result
        return self.direct_result

    def collect_browser_signed(self, account: dict, report=None) -> CollectionBatch:
        self.browser_calls += 1
        if isinstance(self.browser_result, BaseException):
            raise self.browser_result
        return self.browser_result


class PlatformDataSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account = {
            "id": 12,
            "type": 3,
            "filePath": "oneclick_3_safe.json",
            "authMode": "browser",
        }

    def test_direct_success_never_starts_browser_and_persists_once(self) -> None:
        """直连成功仍启动浏览器会浪费资源并扩大风控面。"""

        collector = FakeCollector(valid_batch("direct_session"))
        progress: list[dict] = []
        with patch.object(
            platform_data_sync.account_service,
            "list_accounts",
            return_value=[self.account],
        ), patch.object(
            platform_data_sync,
            "collector_for_platform",
            return_value=collector,
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_collection_sync",
            return_value={
                "accountId": 12,
                "status": "success",
                "sourceMode": "direct_session",
                "errorCode": "",
                "metricCount": 1,
            },
        ) as record_collection, patch.object(
            platform_data_sync.platform_data_service,
            "record_successful_sync",
            return_value={
                "accountId": 12,
                "status": "success",
                "sourceMode": "direct_session",
                "errorCode": "",
                "metricCount": 1,
            },
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_failed_sync",
        ) as record_failed:
            result = platform_data_sync.sync_account_data(
                12, report=progress.append
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result.get("contentCount"), 1)
        self.assertEqual((collector.direct_calls, collector.browser_calls), (1, 0))
        self.assertEqual(record_collection.call_count, 1)
        self.assertIs(record_collection.call_args.args[1], collector.direct_result)
        record_failed.assert_not_called()
        self.assertEqual(
            progress,
            [
                {"stage": "direct_session", "message": "正在读取已登录账号数据"},
                {"stage": "account_metrics", "message": "正在整理账号核心指标"},
                {"stage": "content_list", "message": "正在读取作品列表"},
                {"stage": "content_metrics", "message": "正在整理作品指标"},
                {"stage": "persisting", "message": "正在保存可信指标"},
                {"stage": "completed", "message": "数据同步完成"},
            ],
        )

    def test_only_fallback_allowed_error_starts_browser_signed(self) -> None:
        """普通网络错误不能自动启动浏览器；签名语义拒绝才允许兜底。"""

        collector = FakeCollector(
            DouyinDataCollectionError(
                "login_required",
                fallback_allowed=True,
            ),
            browser_result=valid_batch("browser_signed"),
        )
        with patch.object(
            platform_data_sync.account_service,
            "list_accounts",
            return_value=[self.account],
        ), patch.object(
            platform_data_sync,
            "collector_for_platform",
            return_value=collector,
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_collection_sync",
            return_value={
                "accountId": 12,
                "status": "success",
                "sourceMode": "browser_signed",
                "errorCode": "",
                "metricCount": 1,
            },
        ) as record_collection, patch.object(
            platform_data_sync.platform_data_service,
            "record_successful_sync",
            return_value={
                "accountId": 12,
                "status": "success",
                "sourceMode": "browser_signed",
                "errorCode": "",
                "metricCount": 1,
            },
        ):
            result = platform_data_sync.sync_account_data(12)

        self.assertEqual(result["sourceMode"], "browser_signed")
        self.assertEqual((collector.direct_calls, collector.browser_calls), (1, 1))
        self.assertEqual(record_collection.call_count, 1)

    def test_final_failure_is_persisted_once_with_fixed_public_payload(self) -> None:
        """最终错误只能落一次固定失败运行，不能泄露原异常。"""

        collector = FakeCollector(
            DouyinDataCollectionError(
                "direct_request_rejected",
                fallback_allowed=False,
            )
        )
        with patch.object(
            platform_data_sync.account_service,
            "list_accounts",
            return_value=[self.account],
        ), patch.object(
            platform_data_sync,
            "collector_for_platform",
            return_value=collector,
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_failed_sync",
            return_value={
                "accountId": 12,
                "status": "failed",
                "sourceMode": "direct_session",
                "errorCode": "direct_request_rejected",
                "metricCount": 0,
                "contentCount": 0,
            },
        ) as record_failed, patch.object(
            platform_data_sync.platform_data_service,
            "record_collection_sync",
        ) as record_success:
            result = platform_data_sync.sync_account_data(12)

        self.assertEqual(
            result,
            {
                "accountId": 12,
                "status": "failed",
                "sourceMode": "direct_session",
                "errorCode": "direct_request_rejected",
                "metricCount": 0,
                "contentCount": 0,
            },
        )
        self.assertEqual(record_failed.call_count, 1)
        record_success.assert_not_called()
        self.assertEqual(collector.browser_calls, 0)

    def test_account_only_batch_is_persisted_as_partial_success(self) -> None:
        """把部分批次按完成显示会诱导用户误判作品指标已经可信。"""

        collector = FakeCollector(account_only_batch("direct_session"))
        progress: list[dict] = []
        with patch.object(
            platform_data_sync.account_service,
            "list_accounts",
            return_value=[self.account],
        ), patch.object(
            platform_data_sync,
            "collector_for_platform",
            return_value=collector,
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_collection_sync",
            return_value={
                "accountId": 12,
                "status": "partial_success",
                "sourceMode": "direct_session",
                "errorCode": "content_list_unavailable",
                "metricCount": 1,
            },
        ) as record_collection, patch.object(
            platform_data_sync.platform_data_service,
            "record_successful_sync",
            return_value={
                "accountId": 12,
                "status": "partial_success",
                "sourceMode": "direct_session",
                "errorCode": "content_list_unavailable",
                "metricCount": 1,
            },
        ) as record_success:
            result = platform_data_sync.sync_account_data(12, report=progress.append)

        self.assertEqual(
            result,
            {
                "accountId": 12,
                "status": "partial_success",
                "sourceMode": "direct_session",
                "errorCode": "content_list_unavailable",
                "metricCount": 1,
                "contentCount": 0,
            },
        )
        self.assertEqual(record_collection.call_count, 1)
        self.assertIs(record_collection.call_args.args[1], collector.direct_result)
        record_success.assert_not_called()
        self.assertEqual(
            progress,
            [
                {"stage": "direct_session", "message": "正在读取已登录账号数据"},
                {"stage": "account_metrics", "message": "正在整理账号核心指标"},
                {"stage": "content_list", "message": "正在读取作品列表"},
                {"stage": "persisting", "message": "正在保存可信指标"},
                {"stage": "partial", "message": "部分数据已保存"},
            ],
        )

    def test_persistence_failed_result_emits_failed_not_partial(self) -> None:
        """落库失败若显示部分完成，会把未保存数据伪造成可用结果。"""

        collector = FakeCollector(valid_batch("direct_session"))
        progress: list[dict] = []
        with patch.object(
            platform_data_sync.account_service,
            "list_accounts",
            return_value=[self.account],
        ), patch.object(
            platform_data_sync,
            "collector_for_platform",
            return_value=collector,
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_collection_sync",
            return_value={
                "accountId": 12,
                "status": "failed",
                "sourceMode": "direct_session",
                "errorCode": "sync_persist_failed",
                "metricCount": 0,
            },
        ):
            result = platform_data_sync.sync_account_data(12, report=progress.append)

        self.assertEqual(
            result,
            {
                "accountId": 12,
                "status": "failed",
                "sourceMode": "direct_session",
                "errorCode": "sync_persist_failed",
                "metricCount": 2,
                "contentCount": 1,
            },
        )
        self.assertEqual(progress[-1], {"stage": "failed", "message": "数据同步未完成"})
        self.assertNotIn("partial", [event["stage"] for event in progress])
        self.assertNotIn("completed", [event["stage"] for event in progress])

    def test_content_truncation_warning_survives_partial_sync(self) -> None:
        """截断警告若被清空，调用方会把不完整作品列表当作全量。"""

        collector = FakeCollector(
            valid_batch(
                "direct_session", warning_code="content_list_truncated"
            )
        )
        with patch.object(
            platform_data_sync.account_service,
            "list_accounts",
            return_value=[self.account],
        ), patch.object(
            platform_data_sync,
            "collector_for_platform",
            return_value=collector,
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_collection_sync",
            return_value={
                "accountId": 12,
                "status": "partial_success",
                "sourceMode": "direct_session",
                "errorCode": "content_list_truncated",
                "metricCount": 1,
            },
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_successful_sync",
            return_value={
                "accountId": 12,
                "status": "partial_success",
                "sourceMode": "direct_session",
                "errorCode": "content_list_truncated",
                "metricCount": 1,
            },
        ):
            result = platform_data_sync.sync_account_data(12)

        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(result["errorCode"], "content_list_truncated")

    def test_public_failure_codes_are_allowlisted(self) -> None:
        """未知采集错误若外泄，会把服务端异常与攻击输入带进公开回执。"""

        for code in (
            "account_trends_unavailable",
            "content_list_unavailable",
            "content_list_truncated",
            "content_payload_invalid",
        ):
            with self.subTest(code=code):
                collector = FakeCollector(
                    DouyinDataCollectionError(code, fallback_allowed=False)
                )
                with patch.object(
                    platform_data_sync.account_service,
                    "list_accounts",
                    return_value=[self.account],
                ), patch.object(
                    platform_data_sync,
                    "collector_for_platform",
                    return_value=collector,
                ), patch.object(
                    platform_data_sync.platform_data_service,
                    "record_failed_sync",
                    return_value={
                        "accountId": 12,
                        "status": "failed",
                        "sourceMode": "direct_session",
                        "errorCode": code,
                        "metricCount": 0,
                    },
                ) as record_failed:
                    result = platform_data_sync.sync_account_data(12)

                self.assertEqual(result["errorCode"], code)
                self.assertNotIn("cause", result)
                self.assertEqual(record_failed.call_args.args[-1], code)

    def test_unknown_failure_code_is_replaced_before_recording(self) -> None:
        """删除错误码过滤会把攻击者传入的文本写入运行记录。"""

        collector = FakeCollector(
            DouyinDataCollectionError("attacker-controlled: secret", fallback_allowed=False)
        )
        progress: list[dict] = []
        with patch.object(
            platform_data_sync.account_service,
            "list_accounts",
            return_value=[self.account],
        ), patch.object(
            platform_data_sync,
            "collector_for_platform",
            return_value=collector,
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_failed_sync",
            return_value={
                "accountId": 12,
                "status": "failed",
                "sourceMode": "direct_session",
                "errorCode": "metric_payload_invalid",
                "metricCount": 0,
            },
        ) as record_failed:
            result = platform_data_sync.sync_account_data(12, report=progress.append)

        self.assertEqual(result["errorCode"], "metric_payload_invalid")
        self.assertNotIn("attacker-controlled", str(result))
        self.assertNotIn("attacker-controlled", str(progress))
        self.assertEqual(
            record_failed.call_args.args[-1], "metric_payload_invalid"
        )

    def test_callback_runtime_error_does_not_stop_sync(self) -> None:
        """进度消费者崩溃不能中断已经可保存的可信数据。"""

        collector = FakeCollector(valid_batch("direct_session"))
        with patch.object(
            platform_data_sync.account_service,
            "list_accounts",
            return_value=[self.account],
        ), patch.object(
            platform_data_sync,
            "collector_for_platform",
            return_value=collector,
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_collection_sync",
            return_value={
                "accountId": 12,
                "status": "success",
                "sourceMode": "direct_session",
                "errorCode": "",
                "metricCount": 1,
            },
        ), patch.object(
            platform_data_sync.platform_data_service,
            "record_successful_sync",
            return_value={
                "accountId": 12,
                "status": "success",
                "sourceMode": "direct_session",
                "errorCode": "",
                "metricCount": 1,
            },
        ):
            result = platform_data_sync.sync_account_data(
                12,
                report=lambda _event: (_ for _ in ()).throw(RuntimeError("gone")),
            )

        self.assertEqual(result["status"], "success")

    def test_callback_interrupts_are_not_swallowed(self) -> None:
        """吞掉进程中断会使终止请求被伪装成普通同步完成。"""

        for interrupt in (KeyboardInterrupt, SystemExit):
            with self.subTest(interrupt=interrupt.__name__):
                with self.assertRaises(interrupt):
                    platform_data_sync._emit(
                        lambda _event: (_ for _ in ()).throw(interrupt()),
                        "direct_session",
                        "正在读取已登录账号数据",
                    )


if __name__ == "__main__":
    unittest.main()
