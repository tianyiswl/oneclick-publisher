# -*- coding: utf-8 -*-
"""快手创作者服务平台已审核响应合同测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from app_core.domestic_data_collector import CapturedJson
from app_core.kuaishou_data_collector import KuaishouDataCollector
from app_core.platform_data_collection_errors import PlatformDataCollectionError
from app_core.platform_data_models import CollectionFailure


def kuaishou_captures(
    *,
    followers_total: int = 120,
    content_id: str = "work-1",
    views: int | None = 66,
    likes: int | None = 7,
    comments: int | None = 1,
    shares: int | None = 2,
    favorites: int | None = 3,
    total_count: int = 1,
) -> tuple[CapturedJson, ...]:
    item = {
        "photoId": content_id,
        "title": "sample",
        "video": True,
    }
    for key, value in (
        ("playCount", views),
        ("likeCount", likes),
        ("commentCount", comments),
        ("shareCount", shares),
        ("collectCount", favorites),
    ):
        if value is not None:
            item[key] = value
    return (
        CapturedJson(
            endpoint="account_home",
            phase="home",
            path="/rest/cp/creator/pc/home/userInfo",
            payload={
                "result": 1,
                "data": {
                    "coreUserInfo": {
                        "userId": 99,
                        "userName": "sample",
                        "fansNum": followers_total,
                    }
                },
            },
        ),
        CapturedJson(
            endpoint="content_list",
            phase="home",
            path="/rest/cp/creator/analysis/pc/home/photo/list",
            payload={
                "result": 1,
                "data": {
                    "photoList": {
                        "photoItems": [item],
                        "totalCount": total_count,
                    }
                },
            },
        ),
    )


class KuaishouDataCollectorTests(unittest.TestCase):
    def parse(self, captures: tuple[CapturedJson, ...]):
        with patch(
            "app_core.kuaishou_data_collector._utc_now",
            return_value=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
        ):
            return KuaishouDataCollector.parse_captures(9, captures)

    def test_parser_maps_work_identifiers_and_metrics(self) -> None:
        """作品 ID 或指标字段错位会把数据记到错误作品。"""

        batch = self.parse(kuaishou_captures())

        points = {
            (point.entity_key, point.metric_key): point.metric_value
            for point in batch.metrics
        }
        self.assertEqual(batch.platform_type, 4)
        self.assertEqual(batch.contents[0].content_id, "work-1")
        self.assertEqual(points[("account:9", "followers_total")], 120)
        self.assertEqual(points[("work-1", "views")], 66)
        self.assertEqual(points[("work-1", "likes")], 7)
        self.assertEqual(points[("work-1", "comments")], 1)
        self.assertEqual(points[("work-1", "shares")], 2)
        self.assertEqual(points[("work-1", "favorites")], 3)

    def test_parser_does_not_zero_fill_absent_share_or_favorite(self) -> None:
        """响应缺少分享或收藏时不能把未知伪造成零。"""

        batch = self.parse(
            kuaishou_captures(shares=None, favorites=None)
        )

        keys = {
            point.metric_key
            for point in batch.metrics
            if point.entity_key == "work-1"
        }
        self.assertEqual(keys, {"views", "likes", "comments"})

    def test_parser_marks_partial_first_page_as_truncated(self) -> None:
        """作品总数超过当前响应时不能声称列表完整。"""

        batch = self.parse(kuaishou_captures(total_count=2))

        self.assertEqual(batch.warning_code, "content_list_truncated")

    def test_parser_rejects_boolean_metric(self) -> None:
        """Python 布尔值不能绕过非负整数指标校验。"""

        with self.assertRaises(CollectionFailure) as raised:
            self.parse(kuaishou_captures(views=True))

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_verification_error_is_retryable_and_not_retried_in_one_collect(self) -> None:
        """验证码页必须一次停止，交给后续人工恢复而非会话内循环。"""

        calls = 0

        def collector_factory(_config, **_dependencies):
            class VerificationCollector:
                def collect(self, _account):
                    nonlocal calls
                    calls += 1
                    raise PlatformDataCollectionError("verification_required")

            return VerificationCollector()

        collector = KuaishouDataCollector(
            domestic_collector_factory=collector_factory
        )

        with self.assertRaises(PlatformDataCollectionError) as raised:
            collector.collect(
                {"id": 9, "type": 4, "filePath": "state.json"}
            )

        self.assertEqual(raised.exception.error_code, "verification_required")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
