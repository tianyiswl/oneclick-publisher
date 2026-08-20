# -*- coding: utf-8 -*-
"""平台数据同步编排测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app_core import platform_data_sync
from app_core.douyin_data_collector import DouyinDataCollectionError
from app_core.platform_data_models import CollectionBatch, MetricPoint


def valid_batch(source_mode: str) -> CollectionBatch:
    return CollectionBatch(
        platform_type=3,
        source_mode=source_mode,
        metrics=(
            MetricPoint(
                entity_type="account",
                entity_key="account:12",
                metric_key="views",
                raw_metric_key="play",
                metric_value=125,
                metric_unit="count",
                observed_at="2026-08-20T12:00:00+08:00",
            ),
        ),
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
            "record_successful_sync",
            return_value={
                "accountId": 12,
                "status": "success",
                "sourceMode": "direct_session",
                "errorCode": "",
                "metricCount": 1,
            },
        ) as record_success, patch.object(
            platform_data_sync.platform_data_service,
            "record_failed_sync",
        ) as record_failed:
            result = platform_data_sync.sync_account_data(
                12, report=progress.append
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual((collector.direct_calls, collector.browser_calls), (1, 0))
        self.assertEqual(record_success.call_count, 1)
        record_failed.assert_not_called()
        self.assertEqual(
            [event["stage"] for event in progress],
            ["direct_session", "persisting", "completed"],
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
            "record_successful_sync",
            return_value={
                "accountId": 12,
                "status": "success",
                "sourceMode": "browser_signed",
                "errorCode": "",
                "metricCount": 1,
            },
        ) as record_success:
            result = platform_data_sync.sync_account_data(12)

        self.assertEqual(result["sourceMode"], "browser_signed")
        self.assertEqual((collector.direct_calls, collector.browser_calls), (1, 1))
        self.assertEqual(record_success.call_count, 1)

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
            },
        ) as record_failed, patch.object(
            platform_data_sync.platform_data_service,
            "record_successful_sync",
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
            },
        )
        self.assertEqual(record_failed.call_count, 1)
        record_success.assert_not_called()
        self.assertEqual(collector.browser_calls, 0)


if __name__ == "__main__":
    unittest.main()
