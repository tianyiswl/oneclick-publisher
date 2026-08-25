# -*- coding: utf-8 -*-
"""抖音地点分页缓存的离线回归测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app_core.douyin_location_cache import (
    LOCATION_CACHE_CAPACITY,
    DouyinLocationCacheError,
    LocationCacheQuery,
    get_cached_locations,
    load_location_search_plan,
    location_requires_revalidation,
    merge_platform_locations_for_queries,
    merge_platform_locations,
    reconcile_platform_locations,
    record_location_selection,
    record_location_publish_result,
    save_location_search_plan,
)
from app_core.douyin_location_search_plan import (
    advance_after_page,
    build_location_search_plan,
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


def cached_revalidation_failures(
    account_id: str,
    candidate: dict[str, object],
) -> int:
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT revalidationFailures
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
    return int(row["revalidationFailures"]) if row else -1


class DouyinLocationCacheTests(unittest.TestCase):
    @staticmethod
    def commission_candidate(poi_id: str) -> dict[str, object]:
        return {
            "poiId": poi_id,
            "name": f"JOYMARK {poi_id}",
            "address": "广东省广州市测试路1号",
            "commissionType": "commission",
        }

    @staticmethod
    def location_query(
        account_id: str,
        keyword: str,
        commission_filter: str,
    ) -> LocationCacheQuery:
        return LocationCacheQuery(
            account_id,
            "domestic",
            keyword,
            commission_filter,
        )

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

    def test_location_search_progress_round_trips_atomically(self) -> None:
        cache_query = LocationCacheQuery("7", "domestic", "广东joymark", "commission")
        plan = advance_after_page(
            build_location_search_plan("广东joymark"),
            has_more=False,
            eligible_total=4,
        )
        save_location_search_plan(cache_query, plan, eligible_total=4)
        restored = load_location_search_plan(cache_query)
        self.assertEqual(restored, plan)

    def test_same_candidate_can_be_associated_with_province_and_city_queries(self) -> None:
        province = LocationCacheQuery("7", "domestic", "广东joymark", "commission")
        city = LocationCacheQuery("7", "domestic", "广州joymark", "commission")
        merge_platform_locations_for_queries(
            [province, city],
            [self.commission_candidate("gd-1")],
        )
        self.assertEqual(get_cached_locations(province, excluded_identities=[])["total"], 1)
        self.assertEqual(get_cached_locations(city, excluded_identities=[])["total"], 1)

    def test_progress_isolated_by_account_keyword_and_commission_filter(self) -> None:
        plan = build_location_search_plan("广东joymark")
        save_location_search_plan(
            self.location_query("7", "广东joymark", "commission"),
            plan,
            eligible_total=4,
        )
        self.assertIsNone(
            load_location_search_plan(
                self.location_query("8", "广东joymark", "commission")
            )
        )
        self.assertIsNone(
            load_location_search_plan(
                self.location_query("7", "广西joymark", "commission")
            )
        )
        self.assertIsNone(
            load_location_search_plan(
                self.location_query("7", "广东joymark", "all")
            )
        )

    def test_progress_plan_keyword_must_match_its_cache_key(self) -> None:
        cache_query = self.location_query("7", "广东joymark", "commission")
        with self.assertRaisesRegex(DouyinLocationCacheError, "关键词"):
            save_location_search_plan(
                cache_query,
                build_location_search_plan("广西joymark"),
                eligible_total=0,
            )

        save_location_search_plan(
            cache_query,
            build_location_search_plan("广东joymark"),
            eligible_total=0,
        )
        with database.connect() as conn:
            row = conn.execute(
                "SELECT planJson FROM douyin_location_search_progress"
            ).fetchone()
            payload = json.loads(row["planJson"])
            payload["originalKeyword"] = "广西joymark"
            conn.execute(
                "UPDATE douyin_location_search_progress SET planJson = ?",
                (json.dumps(payload, ensure_ascii=False),),
            )
        with self.assertRaisesRegex(DouyinLocationCacheError, "关键词"):
            load_location_search_plan(cache_query)

    def test_multi_query_association_keeps_foshan_out_of_guangzhou(self) -> None:
        province = self.location_query("7", "广东joymark", "commission")
        guangzhou = self.location_query("7", "广州joymark", "commission")
        beijing = self.location_query("7", "北京joymark", "commission")
        foshan_candidate = self.commission_candidate("gd-fs-1")
        foshan_candidate["address"] = "广东省佛山市测试路1号"

        merge_platform_locations_for_queries(
            [province, guangzhou, beijing],
            [foshan_candidate],
        )

        self.assertEqual(get_cached_locations(province, excluded_identities=[])["total"], 1)
        self.assertEqual(get_cached_locations(guangzhou, excluded_identities=[])["total"], 0)
        self.assertEqual(get_cached_locations(beijing, excluded_identities=[])["total"], 0)

    def test_invalid_progress_json_is_reported_instead_of_marked_complete(self) -> None:
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO douyin_location_search_progress "
                "(accountId, scope, keyword, commissionFilter, planJson, eligibleTotal, updatedAt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "7",
                    "domestic",
                    "广东joymark",
                    "commission",
                    "{broken",
                    0,
                    "2026-08-26T00:00:00+00:00",
                ),
            )
        with self.assertRaisesRegex(DouyinLocationCacheError, "进度"):
            load_location_search_plan(
                self.location_query("7", "广东joymark", "commission")
            )

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

    def test_confirmed_exhaustion_reconciles_returned_and_missing_revalidation_rows(
        self,
    ) -> None:
        """只更新返回行会让缺失 POI 永远停在待校对，第二次缺失也不会失效。"""

        merged = merge_platform_locations(
            query("account-a"),
            candidates(2),
            verified_at=BASE_TIME,
        )
        returned, missing = merged["candidates"]
        record_location_publish_result(
            "account-a",
            missing,
            success=False,
            error_code="publish_location_candidate_ambiguous",
            occurred_at=BASE_TIME + timedelta(minutes=1),
        )

        reconcile_platform_locations(
            query("account-a"),
            [returned],
            confirmed_exhausted=True,
            verified_at=BASE_TIME + timedelta(minutes=2),
        )
        self.assertEqual(cached_status("account-a", returned), "reusable")
        self.assertEqual(cached_status("account-a", missing), "needs_revalidation")
        self.assertEqual(cached_revalidation_failures("account-a", missing), 1)

        reconcile_platform_locations(
            query("account-a"),
            [returned],
            confirmed_exhausted=True,
            verified_at=BASE_TIME + timedelta(minutes=3),
        )
        self.assertEqual(cached_status("account-a", missing), "invalid")
        self.assertEqual(cached_revalidation_failures("account-a", missing), 2)

    def test_empty_or_unconfirmed_revalidation_only_marks_confirmed_missing_rows(
        self,
    ) -> None:
        """空平台集合可在确认穷尽后计一次缺失，部分页则绝不能计数。"""

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
            error_code="publish_location_candidate_ambiguous",
            occurred_at=BASE_TIME + timedelta(minutes=1),
        )

        reconcile_platform_locations(
            query("account-a"),
            [],
            confirmed_exhausted=False,
            verified_at=BASE_TIME + timedelta(minutes=2),
        )
        self.assertEqual(cached_revalidation_failures("account-a", target), 0)
        self.assertEqual(cached_status("account-a", target), "needs_revalidation")

        reconcile_platform_locations(
            query("account-a"),
            [],
            confirmed_exhausted=True,
            verified_at=BASE_TIME + timedelta(minutes=3),
        )
        self.assertEqual(cached_revalidation_failures("account-a", target), 1)
        self.assertEqual(cached_status("account-a", target), "needs_revalidation")

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

    def test_repeated_capacity_replacement_cleans_orphan_entities(self) -> None:
        """Replacing a full query repeatedly must not grow orphan entities."""

        location_query = query("account-a", keyword="bounded-entities")
        for page in range(3):
            merge_platform_locations(
                location_query,
                candidates(
                    LOCATION_CACHE_CAPACITY,
                    start=page * LOCATION_CACHE_CAPACITY + 1,
                ),
                verified_at=BASE_TIME + timedelta(minutes=page),
            )

        with database.connect() as conn:
            entity_count = conn.execute(
                "SELECT COUNT(*) AS count FROM douyin_location_cache"
            ).fetchone()["count"]
            orphan_count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM douyin_location_cache AS cache
                WHERE NOT EXISTS (
                    SELECT 1 FROM douyin_location_cache_keywords AS keyword
                    WHERE keyword.locationCacheId = cache.id
                )
                """
            ).fetchone()["count"]
        self.assertEqual(entity_count, LOCATION_CACHE_CAPACITY)
        self.assertEqual(orphan_count, 0)

    def test_capacity_retains_hundred_reusable_when_invalid_and_expired_history_exists(
        self,
    ) -> None:
        """Invalid/expired history must not consume the 100 reusable slots."""

        location_query = query("account-a", keyword="lifecycle-capacity")
        stale = candidates(2)
        merge_platform_locations(location_query, stale, verified_at=BASE_TIME)
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE douyin_location_cache
                SET status = 'invalid', lastFailureAt = ?, lastErrorCode = ?
                WHERE accountId = ? AND poiId = ?
                """,
                (
                    (BASE_TIME + timedelta(minutes=1)).isoformat(),
                    "publish_location_readback_mismatch",
                    "account-a",
                    "poi-1",
                ),
            )
            conn.execute(
                """
                UPDATE douyin_location_cache
                SET verifiedAt = ?
                WHERE accountId = ? AND poiId = ?
                """,
                (
                    (BASE_TIME - timedelta(days=8)).isoformat(),
                    "account-a",
                    "poi-2",
                ),
            )

        fresh = candidates(LOCATION_CACHE_CAPACITY, start=1001)
        now = BASE_TIME + timedelta(minutes=2)
        merge_platform_locations(location_query, fresh, verified_at=now)

        self.assertEqual(
            all_cached_ids(location_query, now=now),
            [candidate["poiId"] for candidate in fresh],
        )
        with database.connect() as conn:
            linked_history = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM douyin_location_cache AS cache
                JOIN douyin_location_cache_keywords AS keyword
                  ON keyword.locationCacheId = cache.id
                WHERE keyword.accountId = ? AND keyword.scope = ?
                  AND keyword.keyword = ? AND keyword.commissionFilter = ?
                  AND cache.poiId IN ('poi-1', 'poi-2')
                """,
                (
                    location_query.account_id,
                    location_query.scope,
                    location_query.keyword,
                    location_query.commission_filter,
                ),
            ).fetchone()["count"]
        self.assertEqual(linked_history, 0)

    def test_lifecycle_fields_drive_reusable_eviction_priority(self) -> None:
        """Recent publish and selection signals must survive capacity eviction."""

        location_query = query("account-a", keyword="lifecycle-rank")
        initial = candidates(LOCATION_CACHE_CAPACITY)
        merge_platform_locations(location_query, initial, verified_at=BASE_TIME)
        selected = {**initial[-1], "scope": location_query.scope}
        published = {**initial[-2], "scope": location_query.scope}
        failed = {**initial[-3], "scope": location_query.scope}
        record_location_selection(
            "account-a",
            selected,
            occurred_at=BASE_TIME + timedelta(minutes=1),
        )
        record_location_publish_result(
            "account-a",
            published,
            success=True,
            query=location_query,
            occurred_at=BASE_TIME + timedelta(minutes=2),
        )
        record_location_publish_result(
            "account-a",
            failed,
            success=False,
            error_code="publish_location_readback_mismatch",
            occurred_at=BASE_TIME + timedelta(minutes=3),
        )

        newcomer = candidates(1, start=1001)[0]
        now = BASE_TIME + timedelta(minutes=4)
        merge_platform_locations(location_query, [newcomer], verified_at=now)

        retained = set(all_cached_ids(location_query, now=now))
        self.assertEqual(len(retained), LOCATION_CACHE_CAPACITY)
        self.assertIn(selected["poiId"], retained)
        self.assertIn(published["poiId"], retained)
        self.assertIn(newcomer["poiId"], retained)
        self.assertNotIn(failed["poiId"], retained)
        with database.connect() as conn:
            selected_row = conn.execute(
                "SELECT firstSeenAt, lastSelectedAt FROM douyin_location_cache "
                "WHERE accountId = ? AND poiId = ?",
                ("account-a", selected["poiId"]),
            ).fetchone()
            published_row = conn.execute(
                "SELECT lastPublishSuccessAt, lastErrorCode "
                "FROM douyin_location_cache WHERE accountId = ? AND poiId = ?",
                ("account-a", published["poiId"]),
            ).fetchone()
        self.assertEqual(selected_row["firstSeenAt"], BASE_TIME.isoformat())
        self.assertEqual(
            selected_row["lastSelectedAt"],
            (BASE_TIME + timedelta(minutes=1)).isoformat(),
        )
        self.assertEqual(
            published_row["lastPublishSuccessAt"],
            (BASE_TIME + timedelta(minutes=2)).isoformat(),
        )
        self.assertEqual(published_row["lastErrorCode"], "")

    def test_late_selection_never_revives_invalid_evicted_row(self) -> None:
        """A late selection may preserve history but cannot certify validity."""

        location_query = query("account-a", keyword="late-invalid-selection")
        target = candidates(1)[0]
        merge_platform_locations(location_query, [target], verified_at=BASE_TIME)
        record_location_publish_result(
            "account-a",
            {**target, "scope": location_query.scope},
            success=False,
            error_code="publish_location_not_found_after_all_pages",
            occurred_at=BASE_TIME + timedelta(minutes=1),
        )
        reconcile_platform_locations(
            location_query,
            [],
            confirmed_exhausted=True,
            verified_at=BASE_TIME + timedelta(minutes=2),
        )
        self.assertEqual(
            cached_status(
                "account-a", {**target, "scope": location_query.scope}
            ),
            "invalid",
        )

        fresh = candidates(LOCATION_CACHE_CAPACITY, start=1001)
        merge_platform_locations(
            location_query,
            fresh,
            verified_at=BASE_TIME + timedelta(minutes=3),
        )
        record_location_selection(
            "account-a",
            {**target, "scope": location_query.scope},
            query=location_query,
            occurred_at=BASE_TIME + timedelta(minutes=4),
        )

        self.assertNotIn(
            target["poiId"],
            all_cached_ids(
                location_query,
                now=BASE_TIME + timedelta(minutes=4),
            ),
        )
        self.assertIn(
            cached_status(
                "account-a", {**target, "scope": location_query.scope}
            ),
            ("", "invalid"),
        )

    def test_publish_failure_after_eviction_blocks_late_selection_restore(
        self,
    ) -> None:
        """A newer location failure must invalidate an older eviction snapshot."""

        location_query = query("account-a", keyword="failure-after-eviction")
        initial = candidates(LOCATION_CACHE_CAPACITY)
        target = {**initial[-1], "scope": location_query.scope}
        merge_platform_locations(location_query, initial, verified_at=BASE_TIME)
        merge_platform_locations(
            location_query,
            [candidates(1, start=1001)[0]],
            verified_at=BASE_TIME + timedelta(minutes=1),
        )
        self.assertEqual(cached_status("account-a", target), "")

        record_location_publish_result(
            "account-a",
            target,
            success=False,
            error_code="publish_location_readback_mismatch",
            occurred_at=BASE_TIME + timedelta(minutes=2),
        )
        record_location_selection(
            "account-a",
            target,
            query=location_query,
            occurred_at=BASE_TIME + timedelta(minutes=3),
        )

        self.assertNotIn(
            target["poiId"],
            all_cached_ids(
                location_query,
                now=BASE_TIME + timedelta(minutes=3),
            ),
        )
        self.assertNotEqual(cached_status("account-a", target), "reusable")

    def test_public_cache_boundary_rejects_unknown_enums_and_filter_mismatch(
        self,
    ) -> None:
        """Unknown scope/filter values and incompatible candidates fail closed."""

        with self.assertRaises(ValueError):
            get_cached_locations(
                LocationCacheQuery("account-a", "world", "银滩", "all"),
                now=BASE_TIME,
            )
        with self.assertRaises(ValueError):
            get_cached_locations(
                LocationCacheQuery("account-a", "domestic", "银滩", "paid"),
                now=BASE_TIME,
            )
        no_commission = {
            **candidates(1)[0],
            "commissionType": "no_commission",
            "commissionProductCount": 0,
            "commissionLabel": "无佣",
        }
        with self.assertRaises(ValueError):
            merge_platform_locations(
                query("account-a", commission_filter="commission"),
                [no_commission],
                verified_at=BASE_TIME,
            )

    def test_database_connections_enable_foreign_keys_and_cleanup_old_orphans(
        self,
    ) -> None:
        """Every managed connection enables FK and migration removes old orphans."""

        database.ensure_schema()
        raw = sqlite3.connect(database.DB_PATH)
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute(
                """
                INSERT INTO douyin_location_cache_keywords (
                    locationCacheId, accountId, scope, keyword,
                    commissionFilter, position
                ) VALUES (999999, 'account-a', 'domestic', '遗留', 'all', 0)
                """
            )
            raw.commit()
        finally:
            raw.close()

        database.ensure_schema()
        with database.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM douyin_location_cache_keywords "
                    "WHERE locationCacheId = 999999"
                ).fetchone()[0],
                0,
            )

    def test_excluded_identity_pagination_survives_platform_reorder(self) -> None:
        """A mutable merge between pages must not skip or repeat cache rows."""

        location_query = query("account-a", keyword="stable-pages")
        original = candidates(20)
        merge_platform_locations(location_query, original, verified_at=BASE_TIME)
        first = get_cached_locations(
            location_query,
            excluded_identities=[],
            now=BASE_TIME,
        )
        seen = list(first["candidates"])

        newcomer = candidates(1, start=1001)[0]
        now = BASE_TIME + timedelta(minutes=1)
        merge_platform_locations(
            location_query,
            [newcomer, *original],
            verified_at=now,
        )
        while True:
            page = get_cached_locations(
                location_query,
                excluded_identities=seen,
                now=now,
            )
            seen.extend(page["candidates"])
            if page["hasMore"] is False:
                break

        seen_ids = [candidate["poiId"] for candidate in seen]
        self.assertEqual(seen_ids[:10], [f"poi-{index}" for index in range(1, 11)])
        self.assertEqual(len(seen_ids), 21)
        self.assertEqual(len(set(seen_ids)), 21)
        self.assertEqual(set(seen_ids), {row["poiId"] for row in [*original, newcomer]})

    def test_publish_success_upserts_empty_and_previously_evicted_targets(self) -> None:
        """A verified publish creates the entity and frozen query association."""

        empty_query = query("account-a", keyword="publish-empty")
        empty_target = {**candidates(1, start=2001)[0], "scope": "domestic"}
        record_location_publish_result(
            "account-a",
            empty_target,
            success=True,
            query=empty_query,
            occurred_at=BASE_TIME,
        )
        self.assertEqual(
            ids(get_cached_locations(empty_query, now=BASE_TIME)),
            ["poi-2001"],
        )

        evicted_query = query("account-a", keyword="publish-evicted")
        seeded = candidates(LOCATION_CACHE_CAPACITY + 1, start=3001)
        merge_platform_locations(evicted_query, seeded, verified_at=BASE_TIME)
        evicted_target = {**seeded[-1], "scope": "domestic"}
        self.assertNotIn(
            evicted_target["poiId"],
            all_cached_ids(evicted_query, now=BASE_TIME),
        )

        published_at = BASE_TIME + timedelta(minutes=1)
        record_location_publish_result(
            "account-a",
            evicted_target,
            success=True,
            query=evicted_query,
            occurred_at=published_at,
        )
        retained = all_cached_ids(evicted_query, now=published_at)
        self.assertEqual(len(retained), LOCATION_CACHE_CAPACITY)
        self.assertIn(evicted_target["poiId"], retained)


if __name__ == "__main__":
    unittest.main()
