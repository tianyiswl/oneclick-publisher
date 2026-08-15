# -*- coding: utf-8 -*-
"""抖音地点分页缓存的离线回归测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core.douyin_location_cache import (
    LOCATION_CACHE_CAPACITY,
    LocationCacheQuery,
    get_cached_locations,
    location_requires_revalidation,
    merge_platform_locations,
    record_location_publish_result,
)
from app_core import database


BASE_TIME = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)


def query(
    account_id: str,
    *,
    keyword: str = "银滩",
    commission_filter: str = "all",
) -> LocationCacheQuery:
    return LocationCacheQuery(
        account_id=account_id,
        scope="domestic",
        keyword=keyword,
        commission_filter=commission_filter,
    )


def candidates(count: int, *, start: int = 1) -> list[dict[str, object]]:
    return [
        {
            "poiId": f"poi-{index}",
            "name": f"地点 {index}",
            "address": f"广西北海市银海区银滩路 {index} 号",
            "distance": f"{index}m",
            "source": "douyin-visible-commerce-location",
            "commissionType": "commission",
            "productCount": index,
            "commissionProductCount": index,
            "commissionLabel": "返佣",
            "browserMarker": "must-not-persist",
        }
        for index in range(start, start + count)
    ]


def ids(result: dict[str, object]) -> list[str]:
    return [row["poiId"] for row in result["candidates"]]


def all_cached_ids(value: LocationCacheQuery, *, now: datetime) -> list[str]:
    return [
        poi_id
        for offset in range(0, LOCATION_CACHE_CAPACITY, 10)
        for poi_id in ids(get_cached_locations(value, offset=offset, now=now))
    ]


def cached_status(account_id: str, candidate: dict[str, object]) -> str:
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT status
            FROM douyin_location_cache
            WHERE accountId = ? AND scope = ? AND poiId = ?
              AND name = ? AND address = ? AND commissionType = ?
            """,
            (
                account_id,
                candidate["scope"],
                candidate["poiId"],
                candidate["name"],
                candidate["address"],
                candidate["commissionType"],
            ),
        ).fetchone()
    return row["status"] if row else ""


class DouyinLocationCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_patch = patch(
            "app_core.database.DB_PATH",
            Path(self.temporary_directory.name) / "location-cache.sqlite3",
        )
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.temporary_directory.cleanup()

    def test_cache_is_account_scoped_and_pages_ten_rows(self) -> None:
        """账号条件或十行分页丢失时，本测试必须失败。"""

        merge_platform_locations(query("account-a"), candidates(25), verified_at=BASE_TIME)
        merge_platform_locations(query("account-b"), candidates(3), verified_at=BASE_TIME)

        self.assertEqual(
            ids(get_cached_locations(query("account-a"), offset=0, limit=10, now=BASE_TIME)),
            [f"poi-{index}" for index in range(1, 11)],
        )
        self.assertEqual(
            ids(get_cached_locations(query("account-a"), offset=10, limit=10, now=BASE_TIME)),
            [f"poi-{index}" for index in range(11, 21)],
        )
        account_b_page = get_cached_locations(query("account-b"), now=BASE_TIME)
        self.assertEqual(len(account_b_page["candidates"]), 3)
        self.assertIs(account_b_page["requiresRevalidation"], False)

    def test_location_failure_marks_only_target_for_revalidation(self) -> None:
        """错误状态若泄漏到同账号其他地点，本测试必须失败。"""

        merged = merge_platform_locations(query("account-a"), candidates(2), verified_at=BASE_TIME)
        target, other = merged["candidates"]

        record_location_publish_result(
            "account-a",
            target,
            success=False,
            error_code="publish_location_readback_mismatch",
            occurred_at=BASE_TIME + timedelta(minutes=1),
        )

        rows = get_cached_locations(query("account-a"), now=BASE_TIME + timedelta(minutes=1))
        self.assertEqual(rows["candidates"], [other])
        self.assertEqual(cached_status("account-a", target), "needs_revalidation")
        self.assertFalse(location_requires_revalidation(other, now=BASE_TIME + timedelta(minutes=1)))

    def test_not_found_after_all_pages_marks_target_for_revalidation(self) -> None:
        """完整分页仍找不到目标时，真实缓存行必须转为待校对。"""

        merged = merge_platform_locations(
            query("account-a"),
            candidates(1),
            verified_at=BASE_TIME,
        )
        target = merged["candidates"][0]

        record_location_publish_result(
            "account-a",
            target,
            success=False,
            error_code="publish_location_not_found_after_all_pages",
            occurred_at=BASE_TIME + timedelta(minutes=1),
        )

        self.assertEqual(cached_status("account-a", target), "needs_revalidation")
        self.assertEqual(
            get_cached_locations(
                query("account-a"),
                now=BASE_TIME + timedelta(minutes=1),
            )["candidates"],
            [],
        )

    def test_interaction_and_cleanup_errors_do_not_change_cached_status(self) -> None:
        """纯点击或面板清理异常不能证明 POI 失效，缓存必须保持可复用。"""

        merged = merge_platform_locations(
            query("account-a"),
            candidates(2),
            verified_at=BASE_TIME,
        )
        for target, error_code in zip(
            merged["candidates"],
            (
                "publish_location_click_failed",
                "publish_location_cleanup_incomplete",
            ),
            strict=True,
        ):
            record_location_publish_result(
                "account-a",
                target,
                success=False,
                error_code=error_code,
                occurred_at=BASE_TIME + timedelta(minutes=1),
            )

        self.assertEqual(
            [
                cached_status("account-a", target)
                for target in merged["candidates"]
            ],
            ["reusable", "reusable"],
        )
        self.assertEqual(
            len(
                get_cached_locations(
                    query("account-a"),
                    now=BASE_TIME + timedelta(minutes=1),
                )["candidates"]
            ),
            2,
        )

    def test_two_consecutive_readback_mismatches_invalidate_only_that_location(self) -> None:
        """第二次找不到 POI 未失效，或误伤相邻 POI 时，本测试必须失败。"""

        merged = merge_platform_locations(query("account-a"), candidates(2), verified_at=BASE_TIME)
        target, other = merged["candidates"]
        for minute in (1, 2):
            record_location_publish_result(
                "account-a",
                target,
                success=False,
                error_code="publish_location_readback_mismatch",
                occurred_at=BASE_TIME + timedelta(minutes=minute),
            )

        rows = get_cached_locations(query("account-a"), now=BASE_TIME + timedelta(minutes=2))
        self.assertEqual(rows["candidates"], [other])
        self.assertEqual(cached_status("account-a", target), "invalid")

    def test_non_location_error_does_not_change_cached_location_status(self) -> None:
        """把上传等非地点错误写入地点状态时，本测试必须失败。"""

        merged = merge_platform_locations(query("account-a"), candidates(1), verified_at=BASE_TIME)
        target = merged["candidates"][0]
        record_location_publish_result(
            "account-a",
            target,
            success=False,
            error_code="publish_video_upload_failed",
            occurred_at=BASE_TIME + timedelta(minutes=1),
        )

        rows = get_cached_locations(query("account-a"), now=BASE_TIME + timedelta(minutes=1))
        self.assertEqual(rows["candidates"], [target])

    def test_expired_or_non_builtin_pagination_requests_are_not_reusable(self) -> None:
        """过期记录返回，或 bool/float 被当作 offset/limit 时，本测试必须失败。"""

        merged = merge_platform_locations(query("account-a"), candidates(1), verified_at=BASE_TIME)
        target = merged["candidates"][0]
        expiration_boundary = BASE_TIME + timedelta(days=7)

        self.assertEqual(get_cached_locations(query("account-a"), now=expiration_boundary)["candidates"], [])
        self.assertTrue(location_requires_revalidation(target, now=expiration_boundary))
        with self.assertRaises(ValueError):
            get_cached_locations(query("account-a"), offset=True, now=BASE_TIME)
        with self.assertRaises(ValueError):
            get_cached_locations(query("account-a"), limit=10.0, now=BASE_TIME)
        with self.assertRaises(ValueError):
            get_cached_locations(query("account-a"), limit=True, now=BASE_TIME)

    def test_cache_page_requires_revalidation_when_one_linked_row_needs_review(
        self,
    ) -> None:
        """混合结果中任一关联地点待校对时，不得把查询标成纯缓存可用。"""

        merged = merge_platform_locations(
            query("account-a"),
            candidates(20),
            verified_at=BASE_TIME,
        )
        record_location_publish_result(
            "account-a",
            merged["candidates"][-1],
            success=False,
            error_code="publish_location_candidate_ambiguous",
            occurred_at=BASE_TIME + timedelta(minutes=1),
        )

        page = get_cached_locations(
            query("account-a"),
            now=BASE_TIME + timedelta(minutes=1),
        )

        self.assertEqual(page["total"], 19)
        self.assertEqual(len(page["candidates"]), 10)
        self.assertIs(page.get("requiresRevalidation"), True)

    def test_cache_page_requires_revalidation_at_exact_seven_day_boundary(
        self,
    ) -> None:
        """一条关联地点恰满七天时必须后台校对，其余新鲜地点仍可展示。"""

        merge_platform_locations(
            query("account-a"),
            candidates(20),
            verified_at=BASE_TIME,
        )
        fresh_time = BASE_TIME + timedelta(days=6)
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE douyin_location_cache
                SET verifiedAt = ?
                WHERE accountId = ? AND poiId != ?
                """,
                (fresh_time.isoformat(), "account-a", "poi-20"),
            )

        page = get_cached_locations(
            query("account-a"),
            now=BASE_TIME + timedelta(days=7),
        )

        self.assertEqual(page["total"], 19)
        self.assertEqual(len(page["candidates"]), 10)
        self.assertIs(page.get("requiresRevalidation"), True)

    def test_pagination_is_fixed_to_ten_rows(self) -> None:
        """调用方可用 limit 绕过十条分页时，本测试必须失败。"""

        merge_platform_locations(query("account-a"), candidates(25), verified_at=BASE_TIME)

        first = get_cached_locations(query("account-a"), offset=0, now=BASE_TIME)
        second = get_cached_locations(query("account-a"), offset=10, now=BASE_TIME)
        self.assertEqual(len(first["candidates"]), 10)
        self.assertEqual(len(second["candidates"]), 10)
        with self.assertRaises(ValueError):
            get_cached_locations(query("account-a"), limit=100, now=BASE_TIME)

    def test_capacity_isolated_by_keyword_and_commission_filter(self) -> None:
        """一个查询组合的第 101 条影响其他组合时，本测试必须失败。"""

        merge_platform_locations(
            query("account-a", keyword="旧关键词"),
            candidates(LOCATION_CACHE_CAPACITY),
            verified_at=BASE_TIME,
        )
        merge_platform_locations(
            query("account-a", keyword="新关键词"),
            candidates(LOCATION_CACHE_CAPACITY, start=LOCATION_CACHE_CAPACITY + 1),
            verified_at=BASE_TIME + timedelta(minutes=1),
        )
        merge_platform_locations(
            query("account-a", keyword="返佣", commission_filter="all"),
            candidates(LOCATION_CACHE_CAPACITY, start=LOCATION_CACHE_CAPACITY * 2 + 1),
            verified_at=BASE_TIME + timedelta(minutes=2),
        )
        merge_platform_locations(
            query("account-a", keyword="返佣", commission_filter="commission"),
            candidates(LOCATION_CACHE_CAPACITY, start=LOCATION_CACHE_CAPACITY * 3 + 1),
            verified_at=BASE_TIME + timedelta(minutes=3),
        )

        now = BASE_TIME + timedelta(minutes=3)
        self.assertEqual(len(all_cached_ids(query("account-a", keyword="旧关键词"), now=now)), 100)
        self.assertEqual(len(all_cached_ids(query("account-a", keyword="新关键词"), now=now)), 100)
        self.assertEqual(
            len(all_cached_ids(query("account-a", keyword="返佣", commission_filter="all"), now=now)),
            100,
        )
        self.assertEqual(
            len(all_cached_ids(query("account-a", keyword="返佣", commission_filter="commission"), now=now)),
            100,
        )

    def test_capacity_keeps_first_hundred_platform_results_for_the_same_query(self) -> None:
        """同一查询超额时若淘汰首屏候选，本测试必须失败。"""

        location_query = query("account-a", keyword="容量")
        merge_platform_locations(
            location_query,
            candidates(LOCATION_CACHE_CAPACITY + 1),
            verified_at=BASE_TIME,
        )

        self.assertEqual(
            ids(get_cached_locations(location_query, offset=0, now=BASE_TIME)),
            [f"poi-{index}" for index in range(1, 11)],
        )
        self.assertEqual(len(all_cached_ids(location_query, now=BASE_TIME)), 100)


if __name__ == "__main__":
    unittest.main()
