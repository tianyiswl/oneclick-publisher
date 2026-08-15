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


def query(account_id: str, *, keyword: str = "银滩") -> LocationCacheQuery:
    return LocationCacheQuery(
        account_id=account_id,
        scope="domestic",
        keyword=keyword,
        commission_filter="all",
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
        self.assertEqual(
            len(get_cached_locations(query("account-b"), now=BASE_TIME)["candidates"]),
            3,
        )

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
        after_expiry = BASE_TIME + timedelta(days=7, seconds=1)

        self.assertEqual(get_cached_locations(query("account-a"), now=after_expiry)["candidates"], [])
        self.assertTrue(location_requires_revalidation(target, now=after_expiry))
        with self.assertRaises(ValueError):
            get_cached_locations(query("account-a"), offset=True, now=BASE_TIME)
        with self.assertRaises(ValueError):
            get_cached_locations(query("account-a"), limit=10.0, now=BASE_TIME)

    def test_capacity_evicts_oldest_cached_location_and_keyword_links(self) -> None:
        """超过容量后仍能由旧关键词读取被淘汰地点时，本测试必须失败。"""

        merge_platform_locations(
            query("account-a", keyword="旧关键词"),
            candidates(LOCATION_CACHE_CAPACITY),
            verified_at=BASE_TIME,
        )
        merge_platform_locations(
            query("account-a", keyword="新关键词"),
            candidates(1, start=LOCATION_CACHE_CAPACITY + 1),
            verified_at=BASE_TIME + timedelta(minutes=1),
        )

        old = get_cached_locations(query("account-a", keyword="旧关键词"), limit=100, now=BASE_TIME + timedelta(minutes=1))
        new = get_cached_locations(query("account-a", keyword="新关键词"), now=BASE_TIME + timedelta(minutes=1))
        self.assertEqual(len(old["candidates"]), LOCATION_CACHE_CAPACITY - 1)
        self.assertNotIn("poi-1", ids(old))
        self.assertEqual(ids(new), [f"poi-{LOCATION_CACHE_CAPACITY + 1}"])


if __name__ == "__main__":
    unittest.main()
