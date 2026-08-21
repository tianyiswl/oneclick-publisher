# -*- coding: utf-8 -*-
"""B站创作中心已审核响应合同测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from app_core.bilibili_data_collector import BilibiliDataCollector
from app_core.domestic_data_collector import CapturedJson
from app_core.platform_data_models import CollectionFailure


def bilibili_captures(
    *,
    followers_total: int = 120,
    content_id: str = "BV1TEST",
    aid: int = 123,
    title: str = "sample",
    published_at: int = 1_776_722_400,
    views: int | None = 300,
    likes: int | None = 12,
    comments: int | None = 2,
    favorites: int | None = 6,
    shares: int | None = 1,
    count: int = 1,
    page_number: int = 1,
    page_size: int = 10,
    phase: str = "submission",
) -> tuple[CapturedJson, ...]:
    stat = {
        "aid": aid,
        "coin": 0,
        "danmaku": 0,
        "dislike": 0,
        "fav_g": 0,
        "his_rank": 0,
        "like_g": 0,
        "now_rank": 0,
        "vt": 0,
        "vv": 0,
    }
    for key, value in (
        ("view", views),
        ("like", likes),
        ("reply", comments),
        ("favorite", favorites),
        ("share", shares),
    ):
        if value is not None:
            stat[key] = value
    return (
        CapturedJson(
            endpoint="account_home",
            phase=phase,
            path="/x/web-interface/nav",
            payload={
                "code": 0,
                "data": {"isLogin": True, "mid": 99, "uname": "sample"},
                "message": "0",
                "ttl": 1,
            },
        ),
        CapturedJson(
            endpoint="account_base",
            phase=phase,
            path="/x/web/data/index/stat",
            payload={
                "code": 0,
                "data": {
                    "fan_recent_thirty": None,
                    "fan_status": 1,
                    "inc_coin": 0,
                    "inc_elec": 0,
                    "inc_fav": 0,
                    "inc_like": 0,
                    "inc_share": 0,
                    "incr_click": 0,
                    "incr_dm": 0,
                    "incr_fans": 0,
                    "incr_reply": 0,
                    "incr_vt": 0,
                    "log_date": 20260821,
                    "mode": 0,
                    "total_click": 0,
                    "total_coin": 0,
                    "total_dm": 0,
                    "total_elec": 0,
                    "total_fans": followers_total,
                    "total_fav": 0,
                    "total_like": 0,
                    "total_reply": 0,
                    "total_share": 0,
                    "total_vt": 0,
                },
                "message": "0",
                "ttl": 1,
            },
        ),
        CapturedJson(
            endpoint="content_list",
            phase=phase,
            path="/x/web/archives",
            payload={
                "code": 0,
                "data": {
                    "apply_count": {},
                    "arc_audits": [
                        {
                            "Archive": {
                                "aid": aid,
                                "bvid": content_id,
                                "cover": "",
                                "ctime": published_at,
                                "had_passed": True,
                                "is_only_self": 0,
                                "online_time": published_at,
                                "ptime": published_at,
                                "state": 0,
                                "title": title,
                            },
                            "Videos": [],
                            "stat": stat,
                        }
                    ],
                    "archives": None,
                    "class": {"is_pubing": 0, "not_pubed": 0, "pubed": count},
                    "page": {"count": count, "pn": page_number, "ps": page_size},
                    "play_type": 0,
                },
                "message": "0",
                "ttl": 1,
            },
        ),
    )


class BilibiliDataCollectorTests(unittest.TestCase):
    def parse(self, captures: tuple[CapturedJson, ...]):
        with patch(
            "app_core.bilibili_data_collector._utc_now",
            return_value=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
        ):
            return BilibiliDataCollector.parse_captures(8, captures)

    def test_parser_maps_account_and_archive_metrics(self) -> None:
        """账号与稿件字段映射错位会把指标写到错误主体。"""

        batch = self.parse(bilibili_captures())

        points = {
            (point.entity_key, point.metric_key): point.metric_value
            for point in batch.metrics
        }
        self.assertEqual(batch.platform_type, 5)
        self.assertEqual(points[("account:8", "followers_total")], 120)
        self.assertEqual(points[("BV1TEST", "views")], 300)
        self.assertEqual(points[("BV1TEST", "likes")], 12)
        self.assertEqual(points[("BV1TEST", "comments")], 2)
        self.assertEqual(points[("BV1TEST", "favorites")], 6)
        self.assertEqual(points[("BV1TEST", "shares")], 1)
        self.assertEqual(batch.contents[0].content_id, "BV1TEST")
        self.assertEqual(batch.contents[0].title, "sample")

    def test_parser_maps_archive_metrics_without_zero_filling(self) -> None:
        """响应缺少的评论不能被伪造成零。"""

        batch = self.parse(bilibili_captures(comments=None, shares=None))

        keys = {
            point.metric_key
            for point in batch.metrics
            if point.entity_key == "BV1TEST"
        }
        self.assertEqual(keys, {"views", "likes", "favorites"})

    def test_parser_returns_account_only_without_manufacturing_archives(self) -> None:
        """稿件响应缺失时不能伪造空壳作品。"""

        batch = self.parse(bilibili_captures()[:2])

        self.assertTrue(batch.account_metrics_available)
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.contents, ())
        self.assertEqual(batch.warning_code, "content_list_unavailable")

    def test_parser_marks_incomplete_archive_page_as_truncated(self) -> None:
        """稿件总数超过当前页范围时不能声称已读全。"""

        batch = self.parse(bilibili_captures(count=11, page_size=10))

        self.assertEqual(batch.warning_code, "content_list_truncated")

    def test_parser_keeps_exact_official_bvid(self) -> None:
        """作品身份必须直接使用响应的 BVID，不能从标题或 AID 猜测。"""

        batch = self.parse(bilibili_captures(content_id="BV1ExactCase"))

        self.assertEqual(batch.contents[0].content_id, "BV1ExactCase")
        self.assertEqual(
            {point.entity_key for point in batch.metrics if point.entity_type == "content"},
            {"BV1ExactCase"},
        )

    def test_parser_rejects_archive_without_bvid(self) -> None:
        """只有 AID 时不能自行补 `av` 前缀或改造作品 ID。"""

        with self.assertRaises(CollectionFailure) as raised:
            self.parse(bilibili_captures(content_id=""))

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_parser_rejects_conflicting_duplicate_bvid(self) -> None:
        """同一 BVID 出现不同累计值时不能任取一份。"""

        first = bilibili_captures()
        second = bilibili_captures(views=301)
        with self.assertRaises(CollectionFailure) as raised:
            self.parse(first + (second[-1],))

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_parser_rejects_boolean_metric(self) -> None:
        """Python 布尔值不能绕过整数指标校验。"""

        with self.assertRaises(CollectionFailure) as raised:
            self.parse(bilibili_captures(views=True))

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_parser_rejects_missing_logged_in_identity(self) -> None:
        """没有已登录官方身份回执时不能接受其他数据。"""

        with self.assertRaises(CollectionFailure) as raised:
            self.parse(bilibili_captures()[1:])

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_collect_uses_account_then_submission_navigation_contracts(self) -> None:
        """直接打开稿件页不返回账号统计，必须先完成账号阶段再采集稿件。"""

        captures = bilibili_captures()
        account_captures = bilibili_captures(phase="account")

        def collector_factory(config, *, parse_captures, **_dependencies):
            class ContractCollector:
                def collect(self, account):
                    if (
                        "/x/web/archives" in config.endpoint_by_path
                        and config.navigation_by_phase["submission"].startswith("/")
                    ):
                        raise CollectionFailure("metric_payload_invalid")
                    selected = (
                        account_captures[:2]
                        if "/x/web/data/index/stat" in config.endpoint_by_path
                        else captures[-1:]
                    )
                    return parse_captures(account["id"], selected)

            return ContractCollector()

        with patch(
            "app_core.bilibili_data_collector._utc_now",
            return_value=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
        ):
            batch = BilibiliDataCollector(
                domestic_collector_factory=collector_factory
            ).collect({"id": 8, "type": 5, "filePath": "state.json"})

        self.assertEqual(batch.contents[0].content_id, "BV1TEST")
        self.assertEqual(
            {point.metric_key for point in batch.metrics if point.entity_type == "account"},
            {"followers_total"},
        )


if __name__ == "__main__":
    unittest.main()
