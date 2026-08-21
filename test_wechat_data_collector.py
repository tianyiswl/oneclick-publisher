# -*- coding: utf-8 -*-
"""微信公众号已审核响应合同测试。"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core.domestic_data_collector import CapturedJson
from app_core.platform_data_collection_errors import PlatformDataCollectionError
from app_core.platform_data_models import CollectionFailure
from app_core.wechat_data_collector import WechatDataCollector


def wechat_captures(
    *,
    followers_total: int = 120,
    followers_net: int = 3,
    account_date: str = "2026-04-20",
    server_time: int = 1_776_722_400,
    load_done: int = 1,
    category_count: int = 1,
    article_id: int = 1001,
    item_index: int = 1,
    article_date: str = "2026-04-19",
    title: str = "sample",
    views: int = 80,
    next_offset: int = 0,
) -> tuple[CapturedJson, ...]:
    return (
        CapturedJson(
            endpoint="account_base",
            phase="account",
            path="/misc/useranalysis",
            payload={
                "base_resp": {"ret": 0, "svr_time": server_time},
                "category_list": [
                    {
                        "user_source": 0,
                        "list": [
                            {
                                "date": account_date,
                                "new_user": 5,
                                "cancel_user": 2,
                                "netgain_user": followers_net,
                                "cumulate_user": followers_total,
                            }
                        ],
                    }
                    for _ in range(category_count)
                ],
                "load_done": load_done,
            },
        ),
        CapturedJson(
            endpoint="content_list",
            phase="content",
            path="/misc/appmsganalysis",
            payload={
                "base_resp": {"ret": 0},
                "jump_list": [],
                "user_source_list": [],
                "article_list": [
                    {
                        "ref_date": article_date,
                        "title": title,
                        "msg_id": article_id,
                        "item_idx": item_index,
                        "total_read_uv": views,
                        "read_uv_ratio": 0.5,
                        "tendency_list": "[]",
                    }
                ],
                "one_article_jump_stat": [],
                "next_offset": next_offset,
            },
        ),
    )


class WechatDataCollectorTests(unittest.TestCase):
    def test_parser_maps_account_and_article_metrics(self) -> None:
        """字段映射错位会把关注或阅读数据写到错误主体。"""

        batch = WechatDataCollector.parse_captures(
            account_id=5,
            captures=wechat_captures(),
        )

        self.assertEqual(batch.platform_type, 10)
        points = {
            (point.entity_type, point.entity_key, point.metric_key): point.metric_value
            for point in batch.metrics
        }
        self.assertEqual(points[("account", "account:5", "followers_total")], 120)
        self.assertEqual(points[("account", "account:5", "followers_net")], 3)
        self.assertEqual(points[("content", "1001:1", "views")], 80)
        self.assertEqual(batch.contents[0].content_id, "1001:1")
        self.assertEqual(batch.contents[0].title, "sample")
        self.assertEqual(batch.warning_code, "")

    def test_parser_returns_account_only_without_manufacturing_articles(self) -> None:
        """文章响应缺失时不能用空壳作品伪造完整同步。"""

        batch = WechatDataCollector.parse_captures(
            account_id=5,
            captures=wechat_captures()[:1],
        )

        self.assertTrue(batch.account_metrics_available)
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.contents, ())
        self.assertEqual(batch.warning_code, "content_list_unavailable")

    def test_parser_marks_positive_next_offset_as_truncated(self) -> None:
        """存在下一页时不能把当前页文章冒充完整列表。"""

        batch = WechatDataCollector.parse_captures(
            account_id=5,
            captures=wechat_captures(next_offset=10),
        )

        self.assertTrue(batch.content_data_available)
        self.assertEqual(batch.warning_code, "content_list_truncated")

    def test_parser_deduplicates_identical_account_contract_responses(self) -> None:
        """同页重复返回同一合同不能制造重复指标或误报格式变化。"""

        captures = wechat_captures()
        batch = WechatDataCollector.parse_captures(
            5, (captures[0], captures[0], captures[1])
        )

        account_points = [
            point for point in batch.metrics if point.entity_type == "account"
        ]
        self.assertEqual(len(account_points), 2)

    def test_parser_uses_completed_account_response_and_latest_observation(self) -> None:
        """加载中响应应跳过；相同完成指标的较新服务时间只更新观测时间。"""

        partial = wechat_captures(load_done=0)
        first = wechat_captures()
        later = wechat_captures(server_time=1_776_722_460)
        batch = WechatDataCollector.parse_captures(
            5, (partial[0], first[0], later[0], first[1])
        )

        self.assertEqual(batch.platform_observed_at, "2026-04-21T06:01:00+08:00")

    def test_parser_ignores_segmented_account_breakdown_when_total_exists(self) -> None:
        """来源分组不能与单组总览相加，否则会重复计算关注数据。"""

        aggregate = wechat_captures()
        segmented = wechat_captures(category_count=2)[0]

        batch = WechatDataCollector.parse_captures(
            5, (aggregate[0], segmented, aggregate[1])
        )

        self.assertEqual(
            len([point for point in batch.metrics if point.entity_type == "account"]),
            2,
        )

    def test_parser_normalizes_compact_beijing_article_date(self) -> None:
        """公众号紧凑日期必须严格转成北京时间自然日。"""

        batch = WechatDataCollector.parse_captures(
            5, wechat_captures(article_date="20260419")
        )

        self.assertEqual(batch.contents[0].published_at, "2026-04-19T00:00:00+08:00")

    def test_parser_normalizes_slash_beijing_article_date(self) -> None:
        """公众号真实列表的斜杠日期必须严格转成北京时间自然日。"""

        batch = WechatDataCollector.parse_captures(
            5, wechat_captures(article_date="2026/04/19")
        )

        self.assertEqual(batch.contents[0].published_at, "2026-04-19T00:00:00+08:00")

    def test_parser_deduplicates_identical_content_contract_responses(self) -> None:
        """文章列表重复回执不能制造重复作品。"""

        captures = wechat_captures()
        batch = WechatDataCollector.parse_captures(
            5, (captures[0], captures[1], captures[1])
        )

        self.assertEqual(len(batch.contents), 1)

    def test_parser_rejects_conflicting_duplicate_content_responses(self) -> None:
        """同一文章的累计阅读冲突时不能任取一份。"""

        first = wechat_captures()
        second = wechat_captures(views=81)
        with self.assertRaises(CollectionFailure) as raised:
            WechatDataCollector.parse_captures(
                5, (first[0], first[1], second[1])
            )

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_parser_rejects_conflicting_duplicate_account_responses(self) -> None:
        """同批次重复账号响应不一致时不能任取一份。"""

        first = wechat_captures()
        second = wechat_captures(followers_total=121)
        with self.assertRaises(CollectionFailure) as raised:
            WechatDataCollector.parse_captures(
                5, (first[0], second[0], first[1])
            )

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_parser_rejects_malformed_dates_and_boolean_metrics(self) -> None:
        """宽松日期或布尔数字会污染按北京时间汇总的快照。"""

        bad_date = wechat_captures(account_date="2026-4-20")
        bad_metric = wechat_captures(views=True)

        for captures in (bad_date, bad_metric):
            with self.subTest(captures=captures):
                with self.assertRaises(CollectionFailure) as raised:
                    WechatDataCollector.parse_captures(5, captures)
                self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_demo_account_without_state_remains_session_state_missing(self) -> None:
        """注册 type=10 不能让演示占位账号看起来像真实采集成功。"""

        with tempfile.TemporaryDirectory() as root, patch(
            "app_core.domestic_data_collector.COOKIE_DIR", Path(root)
        ):
            collector = WechatDataCollector(browser_factory=lambda: object())
            with self.assertRaises(PlatformDataCollectionError) as raised:
                collector.collect(
                    {
                        "id": 5,
                        "type": 10,
                        "filePath": "__oneclick_demo_wechat__.json",
                    }
                )

        self.assertEqual(raised.exception.error_code, "session_state_missing")


if __name__ == "__main__":
    unittest.main()
