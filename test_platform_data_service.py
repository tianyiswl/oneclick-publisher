# -*- coding: utf-8 -*-
"""平台数据模型与快照服务的离线回归测试。"""

from __future__ import annotations

from datetime import date
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import database, platform_data_service
from app_core.platform_data_models import (
    CollectionBatch,
    CollectionFailure,
    ContentRecord,
    MetricPoint,
)


class PlatformDataServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "database.db"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        database.ensure_schema()
        with database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, authMode)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (3, "oneclick_3_safe.json", "数据账号", 1, "数据主体", "browser"),
            )
            self.account_id = int(cursor.lastrowid)

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tempdir.cleanup()

    def _point(
        self,
        metric_key: str,
        value: int | float,
        *,
        observed_at: str = "2026-08-20T12:00:00+08:00",
    ) -> MetricPoint:
        return MetricPoint(
            entity_type="account",
            entity_key=f"account:{self.account_id}",
            metric_key=metric_key,
            raw_metric_key={"views": "play", "likes": "digg"}.get(
                metric_key, metric_key
            ),
            metric_value=value,
            metric_unit="count",
            metric_scope="daily_increment",
            period_start="2026-08-19",
            period_end="2026-08-19",
            observed_at=observed_at,
        )

    def _v2_point(
        self,
        metric_key: str,
        value: int | float,
        *,
        day: str,
        entity_type: str = "account",
        entity_key: str | None = None,
        metric_scope: str = "daily_increment",
    ) -> MetricPoint:
        return MetricPoint(
            entity_type=entity_type,
            entity_key=entity_key or f"account:{self.account_id}",
            metric_key=metric_key,
            raw_metric_key={
                "views": "play",
                "likes": "digg",
                "comments": "comment",
                "followers_total": "fans",
            }.get(metric_key, metric_key),
            metric_value=value,
            metric_unit="count",
            metric_scope=metric_scope,
            period_start=day,
            period_end=day,
            observed_at=f"{day}T23:00:00+08:00",
        )

    def _content(self, content_id: str = "aweme-1") -> ContentRecord:
        return ContentRecord(
            content_id=content_id,
            title=f"作品{content_id[-1]}",
            cover_url=(
                f"https://creator.douyin.com/cover/{content_id[-1]}.jpg"
                "?temporary_token=private#preview"
            ),
            published_at=f"2026-08-1{content_id[-1]}T10:00:00+08:00",
            content_status="published",
            content_type="video",
        )

    def _table_counts(self) -> tuple[int, int, int]:
        with database.connect() as conn:
            return tuple(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "platform_data_sync_runs",
                    "platform_metric_snapshots",
                    "platform_contents",
                )
            )

    def test_v2_models_require_explicit_scope_and_period(self) -> None:
        """缺少口径或自然日范围会让不同统计语义混入同一指标。"""

        daily = MetricPoint(
            entity_type="account",
            entity_key=f"account:{self.account_id}",
            metric_key="views",
            raw_metric_key="play_cnt",
            metric_value=125,
            metric_unit="count",
            metric_scope="daily_increment",
            period_start="2026-08-19",
            period_end="2026-08-19",
            observed_at="2026-08-20T12:00:00+08:00",
        )
        content = ContentRecord(
            content_id="aweme-1",
            title="作品一",
            cover_url="https://creator.douyin.com/cover/1.jpg",
            published_at="2026-08-19T10:00:00+08:00",
            content_status="published",
            content_type="video",
        )
        self.assertEqual(daily.metric_scope, "daily_increment")
        self.assertEqual(content.content_id, "aweme-1")

        invalid_points = (
            {"metric_scope": ""},
            {"metric_scope": "unbounded"},
            {"period_start": "2026-08-32"},
            {"period_start": "2026-08-20", "period_end": "2026-08-19"},
            {"entity_type": "content", "metric_scope": "daily_increment"},
            {"entity_type": "account", "metric_key": "views", "metric_scope": "lifetime_total"},
        )
        point_values = {
            "entity_type": "account",
            "entity_key": f"account:{self.account_id}",
            "metric_key": "views",
            "raw_metric_key": "play_cnt",
            "metric_value": 125,
            "metric_unit": "count",
            "metric_scope": "daily_increment",
            "period_start": "2026-08-19",
            "period_end": "2026-08-19",
            "observed_at": "2026-08-20T12:00:00+08:00",
        }
        for overrides in invalid_points:
            with self.subTest(overrides=overrides):
                with self.assertRaises(CollectionFailure):
                    MetricPoint(**(point_values | overrides))

        duplicate = MetricPoint(**point_values)
        with self.assertRaises(CollectionFailure):
            CollectionBatch(
                platform_type=3,
                source_mode="direct_session",
                metrics=(daily, duplicate),
                contents=(content,),
                account_metrics_available=True,
                content_data_available=True,
                platform_observed_at="2026-08-20T12:00:00+08:00",
            )

    def test_v2_models_reject_controlled_field_whitespace(self) -> None:
        """受控字段首尾空白不得绕过白名单、日期或批次身份。"""

        point_values = {
            "entity_type": "account",
            "entity_key": f"account:{self.account_id}",
            "metric_key": "views",
            "raw_metric_key": "play_cnt",
            "metric_value": 125,
            "metric_unit": "count",
            "metric_scope": "daily_increment",
            "period_start": "2026-08-19",
            "period_end": "2026-08-19",
            "observed_at": "2026-08-20T12:00:00+08:00",
        }
        for field, whitespace_value in (
            ("entity_type", " account "),
            ("metric_scope", " daily_increment "),
            ("period_start", " 2026-08-19 "),
            ("period_end", " 2026-08-19 "),
        ):
            with self.subTest(field=field):
                with self.assertRaises(CollectionFailure):
                    MetricPoint(**(point_values | {field: whitespace_value}))

        canonical = MetricPoint(**point_values)
        with self.assertRaises(CollectionFailure):
            CollectionBatch(
                platform_type=3,
                source_mode="direct_session",
                metrics=(
                    canonical,
                    MetricPoint(
                        **(
                            point_values
                            | {"metric_scope": " daily_increment "}
                        )
                    ),
                ),
                contents=(),
                account_metrics_available=True,
                content_data_available=False,
                platform_observed_at="2026-08-20T12:00:00+08:00",
            )

    def test_collection_batch_enforces_flag_entity_and_warning_invariants(
        self,
    ) -> None:
        """批次可用性、实体和 warning 任一矛盾都必须固定拒绝。"""

        account_point = self._v2_point("views", 10, day="2026-08-19")
        content_point = self._v2_point(
            "views",
            100,
            day="2026-08-19",
            entity_type="content",
            entity_key="aweme-1",
            metric_scope="lifetime_total",
        )
        unmatched_content_point = self._v2_point(
            "views",
            101,
            day="2026-08-19",
            entity_type="content",
            entity_key="aweme-2",
            metric_scope="lifetime_total",
        )
        content = self._content("aweme-1")
        common = {
            "platform_type": 3,
            "source_mode": "direct_session",
            "platform_observed_at": "2026-08-20T12:00:00+08:00",
        }
        invalid_batches = {
            "available_account_without_account_point": {
                "metrics": (content_point,),
                "contents": (content,),
                "account_metrics_available": True,
                "content_data_available": True,
                "warning_code": "",
            },
            "unavailable_account_with_account_point": {
                "metrics": (account_point,),
                "contents": (),
                "account_metrics_available": False,
                "content_data_available": False,
                "warning_code": "content_list_unavailable",
            },
            "product_batch_without_account": {
                "metrics": (content_point,),
                "contents": (content,),
                "account_metrics_available": False,
                "content_data_available": True,
                "warning_code": "account_trends_unavailable",
            },
            "unavailable_content_with_metadata": {
                "metrics": (account_point,),
                "contents": (content,),
                "account_metrics_available": True,
                "content_data_available": False,
                "warning_code": "content_list_unavailable",
            },
            "unavailable_content_with_point": {
                "metrics": (account_point, content_point),
                "contents": (content,),
                "account_metrics_available": True,
                "content_data_available": False,
                "warning_code": "content_list_unavailable",
            },
            "available_content_without_point": {
                "metrics": (account_point,),
                "contents": (content,),
                "account_metrics_available": True,
                "content_data_available": True,
                "warning_code": "",
            },
            "available_content_with_unmatched_point": {
                "metrics": (account_point, unmatched_content_point),
                "contents": (content,),
                "account_metrics_available": True,
                "content_data_available": True,
                "warning_code": "",
            },
            "unavailable_warning_with_available_content": {
                "metrics": (account_point, content_point),
                "contents": (content,),
                "account_metrics_available": True,
                "content_data_available": True,
                "warning_code": "content_payload_invalid",
            },
            "truncated_warning_with_unavailable_content": {
                "metrics": (account_point,),
                "contents": (),
                "account_metrics_available": True,
                "content_data_available": False,
                "warning_code": "content_list_truncated",
            },
            "missing_warning_with_unavailable_content": {
                "metrics": (account_point,),
                "contents": (),
                "account_metrics_available": True,
                "content_data_available": False,
                "warning_code": "",
            },
        }
        for name, values in invalid_batches.items():
            with self.subTest(name=name):
                with self.assertRaises(CollectionFailure) as raised:
                    CollectionBatch(**common, **values)
                self.assertEqual(
                    raised.exception.error_code,
                    "metric_payload_invalid",
                )

        complete = CollectionBatch(
            **common,
            metrics=(account_point, content_point),
            contents=(content,),
            account_metrics_available=True,
            content_data_available=True,
        )
        truncated = CollectionBatch(
            **common,
            metrics=(account_point, content_point),
            contents=(content,),
            account_metrics_available=True,
            content_data_available=True,
            warning_code="content_list_truncated",
        )
        unavailable = CollectionBatch(
            **common,
            metrics=(account_point,),
            contents=(),
            account_metrics_available=True,
            content_data_available=False,
            warning_code="content_list_unavailable",
        )
        self.assertEqual(
            (complete.warning_code, truncated.warning_code, unavailable.warning_code),
            ("", "content_list_truncated", "content_list_unavailable"),
        )

    def test_collection_batch_rejects_duplicate_content_ids(self) -> None:
        """同批次重复作品 ID 会使元数据与累计指标失去唯一对应。"""

        account_point = self._v2_point("views", 10, day="2026-08-19")
        content_point = self._v2_point(
            "views",
            100,
            day="2026-08-19",
            entity_type="content",
            entity_key="aweme-1",
            metric_scope="lifetime_total",
        )
        duplicate = ContentRecord(
            content_id="aweme-1",
            title="重复作品",
            cover_url="https://creator.douyin.com/cover/duplicate.jpg",
            published_at="2026-08-19T11:00:00+08:00",
            content_status="published",
            content_type="video",
        )

        with self.assertRaises(CollectionFailure) as raised:
            CollectionBatch(
                platform_type=3,
                source_mode="direct_session",
                metrics=(account_point, content_point),
                contents=(self._content("aweme-1"), duplicate),
                account_metrics_available=True,
                content_data_available=True,
                platform_observed_at="2026-08-20T12:00:00+08:00",
            )

        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

    def test_schema_migrates_legacy_metrics_without_inventing_scope(self) -> None:
        """旧快照没有口径和范围，迁移后不得伪造成可汇总的 V2 数据。"""

        legacy_path = Path(self.tempdir.name) / "legacy.db"
        with database.open_connection(legacy_path) as conn:
            conn.execute(
                """
                CREATE TABLE user_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type INTEGER NOT NULL,
                    filePath TEXT NOT NULL,
                    userName TEXT NOT NULL,
                    status INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE platform_data_sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accountId INTEGER NOT NULL,
                    platformType INTEGER NOT NULL,
                    sourceMode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    errorCode TEXT NOT NULL DEFAULT '',
                    metricCount INTEGER NOT NULL DEFAULT 0,
                    startedAt TEXT NOT NULL,
                    finishedAt TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE platform_metric_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    syncRunId INTEGER NOT NULL,
                    accountId INTEGER NOT NULL,
                    platformType INTEGER NOT NULL,
                    entityType TEXT NOT NULL,
                    entityKey TEXT NOT NULL,
                    metricKey TEXT NOT NULL,
                    rawMetricKey TEXT NOT NULL,
                    metricValue REAL NOT NULL,
                    metricUnit TEXT NOT NULL,
                    observedAt TEXT NOT NULL,
                    createdAt TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO user_info (id, type, filePath, userName, status)
                VALUES (9, 3, 'oneclick_3_safe.json', '数据账号', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO platform_data_sync_runs
                    (id, accountId, platformType, sourceMode, status, errorCode,
                     metricCount, startedAt, finishedAt)
                VALUES (1, 9, 3, 'direct_session', 'success', '', 1,
                        '2026-08-20T12:00:00+08:00',
                        '2026-08-20T12:00:00+08:00')
                """
            )
            conn.execute(
                """
                INSERT INTO platform_metric_snapshots
                    (syncRunId, accountId, platformType, entityType, entityKey,
                     metricKey, rawMetricKey, metricValue, metricUnit, observedAt,
                     createdAt)
                VALUES (1, 9, 3, 'account', 'account:9', 'views', 'play',
                        125, 'count', '2026-08-20T12:00:00+08:00',
                        '2026-08-20T12:00:00+08:00')
                """
            )

        with patch.object(database, "DB_PATH", legacy_path):
            database.ensure_schema()
            database.ensure_schema()
            with database.open_connection(legacy_path, row_factory=True) as conn:
                legacy = conn.execute(
                    """
                    SELECT metricScope, periodStart, periodEnd
                    FROM platform_metric_snapshots
                    WHERE id = 1
                    """
                ).fetchone()
                v2_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM platform_metric_snapshots
                    WHERE metricScope != ''
                      AND periodStart != ''
                      AND periodEnd != ''
                    """
                ).fetchone()[0]

        self.assertEqual(tuple(legacy), ("", "", ""))
        self.assertEqual(v2_count, 0)

    def test_platform_contents_schema_is_idempotent(self) -> None:
        """重复启动既要保留表结构，也要继续拒绝同账号的重复作品。"""

        database.ensure_schema()
        database.ensure_schema()
        with database.connect() as conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(platform_contents)")
            }
            self.assertTrue(
                {
                    "accountId",
                    "platformType",
                    "contentId",
                    "title",
                    "coverUrl",
                    "publishedAt",
                    "contentStatus",
                    "contentType",
                    "firstSeenAt",
                    "lastSeenAt",
                }.issubset(columns)
            )
            row = (
                self.account_id,
                3,
                "aweme-1",
                "作品一",
                "https://creator.douyin.com/cover/1.jpg",
                "2026-08-19T10:00:00+08:00",
                "published",
                "video",
                "2026-08-20T12:00:00+08:00",
                "2026-08-20T12:00:00+08:00",
            )
            conn.execute(
                """
                INSERT INTO platform_contents
                    (accountId, platformType, contentId, title, coverUrl,
                     publishedAt, contentStatus, contentType, firstSeenAt, lastSeenAt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO platform_contents
                        (accountId, platformType, contentId, title, coverUrl,
                         publishedAt, contentStatus, contentType, firstSeenAt, lastSeenAt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )

    def test_success_persists_only_present_metrics_and_never_invents_zero(self) -> None:
        """删除缺失保护会把 likes 伪造成 0，本测试必须失败。"""

        batch = CollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            metrics=(self._point("views", 125),),
            contents=(),
            account_metrics_available=True,
            content_data_available=False,
            platform_observed_at="2026-08-20T12:00:00+08:00",
            warning_code="content_list_unavailable",
        )

        saved = platform_data_service.record_successful_sync(
            self.account_id, batch
        )
        summary = platform_data_service.account_data_summary(self.account_id)

        self.assertEqual(saved["status"], "partial_success")
        self.assertEqual(saved["metricCount"], 1)
        self.assertEqual(summary["metrics"], {"views": 125})
        self.assertEqual(
            summary["observedAt"],
            {"views": "2026-08-20T12:00:00+08:00"},
        )
        self.assertNotIn("likes", summary["metrics"])

    def test_failed_sync_preserves_last_successful_snapshot(self) -> None:
        """失败同步若清空成功快照，数据监测页会丢失可信历史。"""

        platform_data_service.record_successful_sync(
            self.account_id,
            CollectionBatch(
                platform_type=3,
                source_mode="direct_session",
                metrics=(self._point("views", 125),),
                contents=(),
                account_metrics_available=True,
                content_data_available=False,
                platform_observed_at="2026-08-20T12:00:00+08:00",
                warning_code="content_list_unavailable",
            ),
        )

        failed = platform_data_service.record_failed_sync(
            self.account_id,
            3,
            "browser_signed",
            "login_required",
        )
        summary = platform_data_service.account_data_summary(self.account_id)

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(summary["metrics"], {"views": 125})
        self.assertEqual(
            summary["latestRun"]["errorCode"],
            "login_required",
        )
        self.assertEqual(summary["latestRun"]["sourceMode"], "browser_signed")

    def test_legacy_summary_uses_latest_account_period_and_ignores_content(
        self,
    ) -> None:
        """30 日乱序账号点必须按日期取最新，同名作品指标不得覆盖。"""

        with database.connect() as conn:
            run_id = int(
                conn.execute(
                    """
                    INSERT INTO platform_data_sync_runs
                        (accountId, platformType, sourceMode, status,
                         errorCode, metricCount, startedAt, finishedAt)
                    VALUES (?, 3, 'direct_session', 'success', '', 8, ?, ?)
                    """,
                    (
                        self.account_id,
                        "2026-08-20T12:00:00+08:00",
                        "2026-08-20T12:01:00+08:00",
                    ),
                ).lastrowid
            )
            rows = (
                (
                    "account",
                    f"account:{self.account_id}",
                    "views",
                    122,
                    "daily_increment",
                    "2026-07-22",
                    "2026-07-22",
                    "2026-08-20T12:00:01+08:00",
                ),
                (
                    "account",
                    f"account:{self.account_id}",
                    "views",
                    190,
                    "daily_increment",
                    "2026-08-19",
                    "2026-08-19",
                    "2026-08-20T12:00:02+08:00",
                ),
                (
                    "account",
                    f"account:{self.account_id}",
                    "views",
                    150,
                    "daily_increment",
                    "2026-08-05",
                    "2026-08-05",
                    "2026-08-20T12:00:03+08:00",
                ),
                (
                    "account",
                    f"account:{self.account_id}",
                    "comments",
                    19,
                    "daily_increment",
                    "2026-08-19",
                    "2026-08-19",
                    "2026-08-20T12:00:04+08:00",
                ),
                (
                    "account",
                    f"account:{self.account_id}",
                    "comments",
                    5,
                    "daily_increment",
                    "2026-08-05",
                    "2026-08-05",
                    "2026-08-20T12:00:05+08:00",
                ),
                (
                    "content",
                    "aweme-same-name",
                    "views",
                    9_999,
                    "lifetime_total",
                    "2026-08-20",
                    "2026-08-20",
                    "2026-08-20T12:00:06+08:00",
                ),
                (
                    "account",
                    f"account:{self.account_id}",
                    "likes",
                    7,
                    "",
                    "",
                    "",
                    "2026-08-20T12:00:07+08:00",
                ),
                (
                    "content",
                    "aweme-same-name",
                    "likes",
                    8_888,
                    "",
                    "",
                    "",
                    "2026-08-20T12:00:08+08:00",
                ),
            )
            conn.executemany(
                """
                INSERT INTO platform_metric_snapshots
                    (syncRunId, accountId, platformType, entityType, entityKey,
                     metricKey, rawMetricKey, metricValue, metricUnit,
                     metricScope, periodStart, periodEnd, observedAt, createdAt)
                VALUES (?, ?, 3, ?, ?, ?, ?, ?, 'count', ?, ?, ?, ?, ?)
                """,
                (
                    (
                        run_id,
                        self.account_id,
                        entity_type,
                        entity_key,
                        metric_key,
                        metric_key,
                        value,
                        metric_scope,
                        period_start,
                        period_end,
                        observed_at,
                        observed_at,
                    )
                    for (
                        entity_type,
                        entity_key,
                        metric_key,
                        value,
                        metric_scope,
                        period_start,
                        period_end,
                        observed_at,
                    ) in rows
                ),
            )

        summary = platform_data_service.account_data_summary(self.account_id)

        self.assertEqual(
            summary["metrics"],
            {"views": 190, "comments": 19, "likes": 7},
        )
        self.assertEqual(
            summary["observedAt"],
            {
                "views": "2026-08-20T12:00:02+08:00",
                "comments": "2026-08-20T12:00:04+08:00",
                "likes": "2026-08-20T12:00:07+08:00",
            },
        )

    def test_metric_insert_failure_rolls_back_run_and_all_snapshots(self) -> None:
        """运行头与指标必须同事务，否则半写入会伪造一次成功同步。"""

        batch = CollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            metrics=(self._point("views", 125), self._point("likes", 9)),
            contents=(),
            account_metrics_available=True,
            content_data_available=False,
            platform_observed_at="2026-08-20T12:00:00+08:00",
            warning_code="content_list_unavailable",
        )
        original = platform_data_service._insert_metric_snapshot
        calls = 0

        def fail_second(conn, *, sync_run_id, account_id, platform_type, point):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("sensitive database failure")
            return original(
                conn,
                sync_run_id=sync_run_id,
                account_id=account_id,
                platform_type=platform_type,
                point=point,
            )

        with patch.object(
            platform_data_service,
            "_insert_metric_snapshot",
            side_effect=fail_second,
        ):
            with self.assertRaises(CollectionFailure) as raised:
                platform_data_service.record_successful_sync(
                    self.account_id, batch
                )

        self.assertEqual(raised.exception.error_code, "sync_persist_failed")
        self.assertIsNone(raised.exception.__cause__)
        with database.connect() as conn:
            run_count = conn.execute(
                "SELECT COUNT(*) FROM platform_data_sync_runs"
            ).fetchone()[0]
            metric_count = conn.execute(
                "SELECT COUNT(*) FROM platform_metric_snapshots"
            ).fetchone()[0]
        self.assertEqual((run_count, metric_count), (0, 0))

    def test_models_reject_unsupported_or_non_finite_metrics(self) -> None:
        """未知指标、布尔值和非有限数字不得穿过公共模型边界。"""

        invalid_cases = (
            ("secret_cookie", 1),
            ("views", True),
            ("views", math.nan),
            ("views", math.inf),
        )
        for metric_key, value in invalid_cases:
            with self.subTest(metric_key=metric_key, value=value):
                with self.assertRaises(CollectionFailure):
                    self._point(metric_key, value)

    def test_public_results_do_not_expose_session_or_response_material(self) -> None:
        """服务公开载荷只能含 ID、状态、来源、错误码、数量和指标。"""

        platform_data_service.record_successful_sync(
            self.account_id,
            CollectionBatch(
                platform_type=3,
                source_mode="direct_session",
                metrics=(self._point("views", 125),),
                contents=(),
                account_metrics_available=True,
                content_data_available=False,
                platform_observed_at="2026-08-20T12:00:00+08:00",
                warning_code="content_list_unavailable",
            ),
        )

        summary = platform_data_service.account_data_summary(self.account_id)
        serialized = repr(summary).lower()
        for forbidden in (
            "cookie",
            "sessionid",
            "responsebody",
            "oneclick_3_safe.json",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_v2_batch_persists_atomically(self) -> None:
        """阶段异常必须撤销本轮 run、指标和作品，不能留下半批次。"""

        metrics = (
            self._v2_point("views", 125, day="2026-08-20"),
            self._v2_point("likes", 9, day="2026-08-20"),
            self._v2_point(
                "views",
                400,
                day="2026-08-20",
                entity_type="content",
                entity_key="aweme-1",
                metric_scope="lifetime_total",
            ),
        )
        batch = CollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            metrics=metrics,
            contents=(self._content(),),
            account_metrics_available=True,
            content_data_available=True,
            platform_observed_at="2026-08-20T23:30:00+08:00",
        )

        saved = platform_data_service.record_collection_sync(
            self.account_id, batch
        )
        self.assertEqual(saved["status"], "success")
        self.assertEqual(saved["errorCode"], "")
        self.assertEqual(saved["metricCount"], 3)
        self.assertEqual(self._table_counts(), (1, 3, 1))
        with database.connect() as conn:
            run = conn.execute(
                "SELECT id, status, metricCount FROM platform_data_sync_runs"
            ).fetchone()
            owners = {
                row[0]
                for row in conn.execute(
                    "SELECT syncRunId FROM platform_metric_snapshots"
                )
            }
            scopes = [
                tuple(row)
                for row in conn.execute(
                    """
                    SELECT entityType, entityKey, metricKey, metricScope,
                           periodStart, periodEnd
                    FROM platform_metric_snapshots
                    ORDER BY id
                    """
                )
            ]
        self.assertEqual((run["status"], run["metricCount"]), ("success", 3))
        self.assertEqual(owners, {int(run["id"])})
        self.assertEqual(
            scopes,
            [
                (
                    "account",
                    f"account:{self.account_id}",
                    "views",
                    "daily_increment",
                    "2026-08-20",
                    "2026-08-20",
                ),
                (
                    "account",
                    f"account:{self.account_id}",
                    "likes",
                    "daily_increment",
                    "2026-08-20",
                    "2026-08-20",
                ),
                (
                    "content",
                    "aweme-1",
                    "views",
                    "lifetime_total",
                    "2026-08-20",
                    "2026-08-20",
                ),
            ],
        )

        original_insert = platform_data_service._insert_metric_snapshot
        insert_calls = 0

        def fail_second_metric(
            conn, *, sync_run_id, account_id, platform_type, point
        ):
            nonlocal insert_calls
            insert_calls += 1
            if insert_calls == 2:
                raise RuntimeError("private database detail")
            return original_insert(
                conn,
                sync_run_id=sync_run_id,
                account_id=account_id,
                platform_type=platform_type,
                point=point,
            )

        failures = (
            ("second_metric", "_insert_metric_snapshot", fail_second_metric),
            ("content_upsert", "_upsert_content", RuntimeError("private content detail")),
            ("run_finalization", "_finalize_sync_run", RuntimeError("private run detail")),
        )
        for label, target, side_effect in failures:
            with self.subTest(stage=label):
                insert_calls = 0
                with patch.object(
                    platform_data_service, target, side_effect=side_effect
                ):
                    with self.assertRaises(CollectionFailure) as raised:
                        platform_data_service.record_collection_sync(
                            self.account_id, batch
                        )
                self.assertEqual(
                    raised.exception.error_code, "sync_persist_failed"
                )
                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual(self._table_counts(), (1, 3, 1))

    def test_xhs_persistence_is_atomic_and_keeps_douyin_account_untouched(self) -> None:
        """按平台和账号隔离，重跑小红书不得改写抖音主体的最新快照。"""

        with database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, authMode)
                VALUES (1, 'oneclick_1_safe.json', '小红书账号', 1, '硅基探索', 'browser')
                """
            )
            xhs_account_id = int(cursor.lastrowid)

        def batch(view_count: int) -> CollectionBatch:
            return CollectionBatch(
                platform_type=1,
                source_mode="browser_signed",
                metrics=(
                    MetricPoint(
                        entity_type="account",
                        entity_key=f"account:{xhs_account_id}",
                        metric_key="followers_total",
                        raw_metric_key="fans_count",
                        metric_value=12,
                        metric_unit="count",
                        metric_scope="lifetime_total",
                        period_start="2026-08-20",
                        period_end="2026-08-20",
                        observed_at="2026-08-20T12:00:00+08:00",
                    ),
                    MetricPoint(
                        entity_type="content",
                        entity_key="0123456789abcdef01234567",
                        metric_key="views",
                        raw_metric_key="view_count",
                        metric_value=view_count,
                        metric_unit="count",
                        metric_scope="lifetime_total",
                        period_start="2026-08-20",
                        period_end="2026-08-20",
                        observed_at="2026-08-20T12:00:00+08:00",
                    ),
                ),
                contents=(
                    ContentRecord(
                        content_id="0123456789abcdef01234567",
                        title="",
                        cover_url="",
                        published_at="",
                        content_status="unavailable",
                        content_type="unavailable",
                    ),
                ),
                account_metrics_available=True,
                content_data_available=True,
                platform_observed_at="2026-08-20T12:00:00+08:00",
            )

        douyin_batch = CollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            metrics=(self._v2_point("views", 9, day="2026-08-20"),),
            contents=(),
            account_metrics_available=True,
            content_data_available=False,
            platform_observed_at="2026-08-20T12:00:00+08:00",
            warning_code="content_list_unavailable",
        )
        platform_data_service.record_collection_sync(self.account_id, douyin_batch)
        platform_data_service.record_collection_sync(xhs_account_id, batch(400))
        platform_data_service.record_collection_sync(xhs_account_id, batch(450))

        with database.connect() as conn:
            rows = conn.execute(
                """
                SELECT accountId, platformType, status
                FROM platform_data_sync_runs
                ORDER BY id
                """
            ).fetchall()
            xhs_content = conn.execute(
                """
                SELECT accountId, platformType, title, publishedAt
                FROM platform_contents
                WHERE accountId = ? AND platformType = 1
                """,
                (xhs_account_id,),
            ).fetchone()
        self.assertEqual(
            [(row["accountId"], row["platformType"]) for row in rows],
            [(self.account_id, 3), (xhs_account_id, 1), (xhs_account_id, 1)],
        )
        self.assertEqual(
            tuple(xhs_content), (xhs_account_id, 1, "", "")
        )
        self.assertEqual(
            platform_data_service.account_contents(xhs_account_id)["items"][0]["metrics"],
            {"views": 450},
        )
        self.assertEqual(
            platform_data_service.account_contents(self.account_id)["total"], 0
        )

    def test_validated_xhs_mismatch_rolls_back_new_snapshots_before_failed_run(self) -> None:
        """删掉同事务读回校验会把与官方页不一致的新快照提交为成功。"""

        with database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, authMode)
                VALUES (1, 'oneclick_1_validation.json', '小红书账号', 1, '验收主体', 'browser')
                """
            )
            account_id = int(cursor.lastrowid)

        def xhs_batch(followers: int, views: int) -> CollectionBatch:
            return CollectionBatch(
                platform_type=1,
                source_mode="browser_signed",
                metrics=(
                    MetricPoint(
                        entity_type="account",
                        entity_key=f"account:{account_id}",
                        metric_key="followers_total",
                        raw_metric_key="fans_count",
                        metric_value=followers,
                        metric_unit="count",
                        metric_scope="lifetime_total",
                        period_start="2026-08-20",
                        period_end="2026-08-20",
                        observed_at="2026-08-20T12:00:00+08:00",
                    ),
                    MetricPoint(
                        entity_type="content",
                        entity_key="0123456789abcdef01234567",
                        metric_key="views",
                        raw_metric_key="view_count",
                        metric_value=views,
                        metric_unit="count",
                        metric_scope="lifetime_total",
                        period_start="2026-08-20",
                        period_end="2026-08-20",
                        observed_at="2026-08-20T12:00:00+08:00",
                    ),
                ),
                contents=(
                    ContentRecord(
                        content_id="0123456789abcdef01234567",
                        title="",
                        cover_url="",
                        published_at="",
                        content_status="unavailable",
                        content_type="unavailable",
                    ),
                ),
                account_metrics_available=True,
                content_data_available=True,
                platform_observed_at="2026-08-20T12:00:00+08:00",
            )

        platform_data_service.record_collection_sync(account_id, xhs_batch(100, 200))
        writer = getattr(platform_data_service, "record_validated_collection_sync", None)
        self.assertTrue(callable(writer))

        with self.assertRaises(CollectionFailure) as caught:
            writer(
                account_id,
                xhs_batch(125, 400),
                {
                    "account": {"followersTotal": 125},
                    "content": {
                        "contentId": "0123456789abcdef01234567",
                        "views": 401,
                    },
                },
            )
        self.assertEqual(caught.exception.error_code, "validation_readback_mismatch")
        platform_data_service.record_failed_sync(
            account_id, 1, "browser_signed", "validation_readback_mismatch"
        )

        self.assertEqual(self._table_counts(), (2, 2, 1))
        with database.connect() as conn:
            old_views = conn.execute(
                """
                SELECT metricValue FROM platform_metric_snapshots
                WHERE accountId = ? AND entityType = 'content' AND metricKey = 'views'
                """,
                (account_id,),
            ).fetchone()[0]
            latest = conn.execute(
                """
                SELECT status, errorCode FROM platform_data_sync_runs
                WHERE accountId = ? ORDER BY id DESC LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        self.assertEqual(old_views, 200)
        self.assertEqual(tuple(latest), ("failed", "validation_readback_mismatch"))

    def test_xhs_confirmed_zero_contents_remains_available_not_missing(self) -> None:
        """小红书已确认零作品不是作品列表缺失，也不能显示为失败。"""

        with database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, authMode)
                VALUES (1, 'oneclick_1_empty.json', '零作品账号', 1, '零作品主体', 'browser')
                """
            )
            account_id = int(cursor.lastrowid)
        batch = CollectionBatch(
            platform_type=1,
            source_mode="browser_signed",
            metrics=(
                MetricPoint(
                    entity_type="account",
                    entity_key=f"account:{account_id}",
                    metric_key="followers_total",
                    raw_metric_key="fans_count",
                    metric_value=12,
                    metric_unit="count",
                    metric_scope="lifetime_total",
                    period_start="2026-08-20",
                    period_end="2026-08-20",
                    observed_at="2026-08-20T12:00:00+08:00",
                ),
            ),
            contents=(),
            account_metrics_available=True,
            content_data_available=True,
            platform_observed_at="2026-08-20T12:00:00+08:00",
        )

        saved = platform_data_service.record_collection_sync(account_id, batch)
        contents = platform_data_service.account_contents(account_id)

        self.assertEqual(saved["status"], "success")
        self.assertEqual(
            (contents["availability"], contents["warningCode"], contents["total"]),
            ("available", "", 0),
        )

    def test_period_ranges_end_at_beijing_yesterday_across_calendar_boundaries(
        self,
    ) -> None:
        """北京时间零点后仍须以已结算的昨日作为区间末日。"""

        cases = (
            (
                date(2027, 1, 1),
                {
                    1: ("2026-12-31", "2026-12-31"),
                    7: ("2026-12-25", "2026-12-31"),
                    30: ("2026-12-02", "2026-12-31"),
                },
            ),
            (
                date(2026, 3, 1),
                {
                    1: ("2026-02-28", "2026-02-28"),
                    7: ("2026-02-22", "2026-02-28"),
                    30: ("2026-01-30", "2026-02-28"),
                },
            ),
        )
        for beijing_today, expected_by_days in cases:
            with self.subTest(beijing_today=beijing_today):
                with patch.object(
                    platform_data_service,
                    "_beijing_today",
                    return_value=beijing_today,
                ):
                    for days, expected in expected_by_days.items():
                        _safe_days, period_start, period_end = (
                            platform_data_service._period_range(days)
                        )
                        self.assertEqual((period_start, period_end), expected)

    def test_period_summary_distinguishes_complete_partial_and_missing(self) -> None:
        """范围或缺失判断退化时，稀疏数据会被补零或旧数据污染。"""

        with database.connect() as conn:
            run_id = int(
                conn.execute(
                    """
                    INSERT INTO platform_data_sync_runs
                        (accountId, platformType, sourceMode, status, errorCode,
                         metricCount, startedAt, finishedAt)
                    VALUES (?, 3, 'direct_session', 'partial_success',
                            'content_list_unavailable', 13, ?, ?)
                    """,
                    (
                        self.account_id,
                        "2026-08-20T23:30:00+08:00",
                        "2026-08-20T23:31:00+08:00",
                    ),
                ).lastrowid
            )
            points = [
                self._v2_point("views", 40, day="2026-07-22"),
                self._v2_point("views", 30, day="2026-08-15"),
                self._v2_point("views", 20, day="2026-08-19"),
                self._v2_point("views", 10, day="2026-08-20"),
                self._v2_point(
                    "followers_total",
                    1000,
                    day="2026-08-18",
                    metric_scope="lifetime_total",
                ),
                self._v2_point(
                    "followers_total",
                    1020,
                    day="2026-08-20",
                    metric_scope="lifetime_total",
                ),
            ]
            points.extend(
                self._v2_point("likes", 1, day=day)
                for day in (
                    "2026-08-14",
                    "2026-08-15",
                    "2026-08-16",
                    "2026-08-17",
                    "2026-08-18",
                    "2026-08-19",
                    "2026-08-20",
                )
            )
            for point in points:
                conn.execute(
                    """
                    INSERT INTO platform_metric_snapshots
                        (syncRunId, accountId, platformType, entityType,
                         entityKey, metricKey, rawMetricKey, metricValue,
                         metricUnit, metricScope, periodStart, periodEnd,
                         observedAt, createdAt)
                    VALUES (?, ?, 3, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        self.account_id,
                        point.entity_type,
                        point.entity_key,
                        point.metric_key,
                        point.raw_metric_key,
                        point.metric_value,
                        point.metric_unit,
                        point.metric_scope,
                        point.period_start,
                        point.period_end,
                        point.observed_at,
                        point.observed_at,
                    ),
                )
            conn.execute(
                """
                INSERT INTO platform_metric_snapshots
                    (syncRunId, accountId, platformType, entityType, entityKey,
                     metricKey, rawMetricKey, metricValue, metricUnit,
                     metricScope, periodStart, periodEnd, observedAt, createdAt)
                VALUES (?, ?, 3, 'account', ?, 'views', 'legacy_play', 999,
                        'count', '', '', '', ?, ?)
                """,
                (
                    run_id,
                    self.account_id,
                    f"account:{self.account_id}",
                    "2026-08-20T22:00:00+08:00",
                    "2026-08-20T22:00:00+08:00",
                ),
            )

        with patch.object(
            platform_data_service,
            "_beijing_today",
            return_value=date(2026, 8, 21),
            create=True,
        ):
            one_day = platform_data_service.account_period_summary(
                self.account_id, 1
            )
            summary = platform_data_service.account_period_summary(
                self.account_id, 7
            )
            month = platform_data_service.account_period_summary(
                self.account_id, 30
            )
            trends = platform_data_service.account_daily_trends(
                self.account_id, 30
            )

        self.assertEqual(
            (summary["periodStart"], summary["periodEnd"]),
            ("2026-08-14", "2026-08-20"),
        )
        self.assertEqual(summary["days"], 7)
        self.assertEqual(
            summary["metrics"]["views"],
            {
                "value": 60,
                "scope": "daily_increment",
                "unit": "count",
                "availability": "partial",
                "observedDays": 3,
            },
        )
        self.assertEqual(summary["metrics"]["likes"]["availability"], "complete")
        self.assertEqual(summary["metrics"]["likes"]["value"], 7)
        self.assertEqual(
            summary["metrics"]["comments"],
            {
                "value": None,
                "scope": "daily_increment",
                "unit": "count",
                "availability": "missing",
                "observedDays": 0,
            },
        )
        self.assertEqual(summary["metrics"]["followers_total"]["value"], 1020)
        self.assertEqual(summary["metrics"]["followers_total"]["scope"], "lifetime_total")
        self.assertEqual(one_day["metrics"]["views"]["availability"], "complete")
        self.assertEqual(month["metrics"]["views"]["value"], 100)
        self.assertEqual(summary["latestRun"]["status"], "partial_success")
        self.assertEqual(summary["platformObservedAt"], "2026-08-20T23:00:00+08:00")
        self.assertEqual(summary["localSyncedAt"], "2026-08-20T23:31:00+08:00")
        self.assertEqual(
            [item["date"] for item in trends["items"]],
            [
                "2026-07-22",
                "2026-08-14",
                "2026-08-15",
                "2026-08-16",
                "2026-08-17",
                "2026-08-18",
                "2026-08-19",
                "2026-08-20",
            ],
        )
        self.assertNotIn("rawMetricKey", repr(summary))

        for invalid_days in (True, "7", 7.0, 0, 2, 31):
            with self.subTest(days=invalid_days):
                for query in (
                    platform_data_service.account_period_summary,
                    platform_data_service.account_daily_trends,
                ):
                    with self.assertRaises(CollectionFailure) as raised:
                        query(self.account_id, invalid_days)
                    self.assertEqual(
                        raised.exception.error_code, "metric_payload_invalid"
                    )

    def test_content_query_returns_latest_totals_without_internal_fields(self) -> None:
        """作品查询只返回最新累计值和公开元数据，不泄露采集字段。"""

        first = CollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            metrics=(
                self._v2_point("comments", 1, day="2026-08-19"),
                self._v2_point(
                    "views",
                    400,
                    day="2026-08-19",
                    entity_type="content",
                    entity_key="aweme-1",
                    metric_scope="lifetime_total",
                ),
            ),
            contents=(self._content("aweme-1"),),
            account_metrics_available=True,
            content_data_available=True,
            platform_observed_at="2026-08-19T23:30:00+08:00",
        )
        second = CollectionBatch(
            platform_type=3,
            source_mode="browser_signed",
            metrics=(
                self._v2_point("comments", 2, day="2026-08-20"),
                self._v2_point(
                    "views",
                    450,
                    day="2026-08-20",
                    entity_type="content",
                    entity_key="aweme-1",
                    metric_scope="lifetime_total",
                ),
                self._v2_point(
                    "likes",
                    25,
                    day="2026-08-20",
                    entity_type="content",
                    entity_key="aweme-1",
                    metric_scope="lifetime_total",
                ),
                self._v2_point(
                    "views",
                    80,
                    day="2026-08-20",
                    entity_type="content",
                    entity_key="aweme-2",
                    metric_scope="lifetime_total",
                ),
            ),
            contents=(self._content("aweme-1"), self._content("aweme-2")),
            account_metrics_available=True,
            content_data_available=True,
            platform_observed_at="2026-08-20T23:30:00+08:00",
        )
        platform_data_service.record_collection_sync(self.account_id, first)
        platform_data_service.record_collection_sync(self.account_id, second)

        page = platform_data_service.account_contents(
            self.account_id, limit=1, offset=0
        )
        next_page = platform_data_service.account_contents(
            self.account_id, limit=1, offset=1
        )

        self.assertEqual(
            (page["total"], page["limit"], page["offset"]), (2, 1, 0)
        )
        self.assertEqual(page["items"][0]["contentId"], "aweme-2")
        self.assertEqual(page["items"][0]["metrics"], {"views": 80})
        self.assertEqual(next_page["items"][0]["contentId"], "aweme-1")
        self.assertEqual(
            next_page["items"][0]["metrics"], {"views": 450, "likes": 25}
        )

        serialized = repr((page, next_page)).lower()
        for forbidden in (
            "rawmetrickey",
            "sync_run_id",
            "syncpersist",
            "cookie",
            "sessionid",
            "responsebody",
            "select ",
            "oneclick_3_safe.json",
            "?",
        ):
            self.assertNotIn(forbidden, serialized)

        invalid_pages = (
            {"limit": True, "offset": 0},
            {"limit": "1", "offset": 0},
            {"limit": 1.0, "offset": 0},
            {"limit": 0, "offset": 0},
            {"limit": 101, "offset": 0},
            {"limit": 1, "offset": True},
            {"limit": 1, "offset": "0"},
            {"limit": 1, "offset": 0.0},
            {"limit": 1, "offset": -1},
        )
        for values in invalid_pages:
            with self.subTest(values=values):
                with self.assertRaises(CollectionFailure) as raised:
                    platform_data_service.account_contents(
                        self.account_id, **values
                    )
                self.assertEqual(
                    raised.exception.error_code, "metric_payload_invalid"
                )

    def test_account_contents_exposes_latest_availability_and_trusted_time(
        self,
    ) -> None:
        """本次作品失败不得把历史作品报成实时零条。"""

        def account_point(day: str, value: int) -> MetricPoint:
            return self._v2_point("views", value, day=day)

        def content_point(day: str, value: int) -> MetricPoint:
            return self._v2_point(
                "views",
                value,
                day=day,
                entity_type="content",
                entity_key="aweme-1",
                metric_scope="lifetime_total",
            )

        with patch.object(
            platform_data_service,
            "_now",
            return_value="2026-08-19T12:01:00+08:00",
        ):
            platform_data_service.record_collection_sync(
                self.account_id,
                CollectionBatch(
                    platform_type=3,
                    source_mode="direct_session",
                    metrics=(
                        account_point("2026-08-18", 10),
                        content_point("2026-08-18", 400),
                    ),
                    contents=(self._content("aweme-1"),),
                    account_metrics_available=True,
                    content_data_available=True,
                    platform_observed_at="2026-08-19T12:00:00+08:00",
                ),
            )
        with patch.object(
            platform_data_service,
            "_now",
            return_value="2026-08-20T12:01:00+08:00",
        ):
            platform_data_service.record_collection_sync(
                self.account_id,
                CollectionBatch(
                    platform_type=3,
                    source_mode="browser_signed",
                    metrics=(account_point("2026-08-19", 20),),
                    contents=(),
                    account_metrics_available=True,
                    content_data_available=False,
                    platform_observed_at="2026-08-20T12:00:00+08:00",
                    warning_code="content_list_unavailable",
                ),
            )

        unavailable = platform_data_service.account_contents(self.account_id)

        self.assertEqual(unavailable["availability"], "unavailable")
        self.assertEqual(unavailable["warningCode"], "content_list_unavailable")
        self.assertEqual(unavailable["total"], 1)
        self.assertEqual(
            unavailable["platformObservedAt"],
            "2026-08-19T12:00:00+08:00",
        )
        self.assertEqual(
            unavailable["localSyncedAt"],
            "2026-08-19T12:01:00+08:00",
        )

        with patch.object(
            platform_data_service,
            "_now",
            return_value="2026-08-20T13:01:00+08:00",
        ):
            platform_data_service.record_collection_sync(
                self.account_id,
                CollectionBatch(
                    platform_type=3,
                    source_mode="browser_signed",
                    metrics=(
                        account_point("2026-08-19", 21),
                        content_point("2026-08-19", 450),
                    ),
                    contents=(self._content("aweme-1"),),
                    account_metrics_available=True,
                    content_data_available=True,
                    platform_observed_at="2026-08-20T13:00:00+08:00",
                    warning_code="content_list_truncated",
                ),
            )

        partial = platform_data_service.account_contents(self.account_id)

        self.assertEqual(partial["availability"], "partial")
        self.assertEqual(partial["warningCode"], "content_list_truncated")
        self.assertEqual(
            partial["platformObservedAt"], "2026-08-20T13:00:00+08:00"
        )
        self.assertEqual(
            partial["localSyncedAt"], "2026-08-20T13:01:00+08:00"
        )

        with database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, authMode)
                VALUES (3, 'missing.json', '无作品证据', 1,
                        '无作品证据', 'browser')
                """
            )
            missing_account_id = int(cursor.lastrowid)
        missing = platform_data_service.account_contents(missing_account_id)
        self.assertEqual(missing["availability"], "missing")
        self.assertEqual(missing["warningCode"], "")
        self.assertIsNone(missing["platformObservedAt"])
        self.assertIsNone(missing["localSyncedAt"])

    def test_account_contents_rejects_uncontrolled_data_times(self) -> None:
        """损坏或含路径的历史时间文本不得穿过公开作品查询。"""

        content_point = self._v2_point(
            "views",
            100,
            day="2026-08-19",
            entity_type="content",
            entity_key="aweme-1",
            metric_scope="lifetime_total",
        )
        platform_data_service.record_collection_sync(
            self.account_id,
            CollectionBatch(
                platform_type=3,
                source_mode="direct_session",
                metrics=(
                    self._v2_point("views", 10, day="2026-08-19"),
                    content_point,
                ),
                contents=(self._content("aweme-1"),),
                account_metrics_available=True,
                content_data_available=True,
                platform_observed_at="2026-08-20T12:00:00+08:00",
            ),
        )
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE platform_data_sync_runs
                SET startedAt = '/private/path?token=secret',
                    finishedAt = 'not-a-timestamp'
                WHERE accountId = ?
                """,
                (self.account_id,),
            )

        payload = platform_data_service.account_contents(self.account_id)

        self.assertEqual(payload["availability"], "available")
        self.assertIsNone(payload["platformObservedAt"])
        self.assertIsNone(payload["localSyncedAt"])
        self.assertNotIn("private", repr(payload).lower())
        self.assertNotIn("secret", repr(payload).lower())

    def test_collection_warning_code_is_fixed_and_marks_partial(self) -> None:
        """批次 warning 不能借用登录错误或任意文本污染部分成功回执。"""

        values = {
            "platform_type": 3,
            "source_mode": "direct_session",
            "metrics": (self._v2_point("views", 10, day="2026-08-20"),),
            "contents": (),
            "account_metrics_available": True,
            "content_data_available": False,
            "platform_observed_at": "2026-08-20T23:30:00+08:00",
        }
        saved = platform_data_service.record_collection_sync(
            self.account_id,
            CollectionBatch(
                **values,
                warning_code="content_list_unavailable",
            ),
        )
        self.assertEqual(saved["status"], "partial_success")
        self.assertEqual(saved["errorCode"], "content_list_unavailable")

        with self.assertRaises(CollectionFailure) as raised:
            platform_data_service.record_collection_sync(
                self.account_id,
                CollectionBatch(**values, warning_code="login_required"),
            )
        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")
        self.assertEqual(self._table_counts(), (1, 1, 0))

    def test_legacy_success_entry_preserves_partial_batch_status(self) -> None:
        """旧调用入口不得把真实采集器的作品不可用批次升级为成功。"""

        saved = platform_data_service.record_successful_sync(
            self.account_id,
            CollectionBatch(
                platform_type=3,
                source_mode="direct_session",
                metrics=(self._v2_point("views", 10, day="2026-08-20"),),
                contents=(),
                account_metrics_available=True,
                content_data_available=False,
                platform_observed_at="2026-08-20T23:30:00+08:00",
                warning_code="content_list_unavailable",
            ),
        )

        self.assertEqual(saved["status"], "partial_success")
        self.assertEqual(saved["errorCode"], "content_list_unavailable")
        with database.connect() as conn:
            run = conn.execute(
                "SELECT status, errorCode FROM platform_data_sync_runs"
            ).fetchone()
        self.assertEqual(
            (run["status"], run["errorCode"]),
            ("partial_success", "content_list_unavailable"),
        )

    def test_lifetime_availability_requires_period_end_observation(self) -> None:
        """累计值只有窗口末日有观测才完整，较早点不能冒充完整。"""

        def persist_total(day: str, value: int) -> None:
            platform_data_service.record_collection_sync(
                self.account_id,
                CollectionBatch(
                    platform_type=3,
                    source_mode="direct_session",
                    metrics=(
                        self._v2_point(
                            "followers_total",
                            value,
                            day=day,
                            metric_scope="lifetime_total",
                        ),
                    ),
                    contents=(),
                    account_metrics_available=True,
                    content_data_available=False,
                    platform_observed_at=f"{day}T23:30:00+08:00",
                    warning_code="content_list_unavailable",
                ),
            )

        persist_total("2026-08-14", 900)
        with patch.object(
            platform_data_service,
            "_beijing_today",
            return_value=date(2026, 8, 21),
        ):
            week_before_end = platform_data_service.account_period_summary(
                self.account_id, 7
            )
            day_without_total = platform_data_service.account_period_summary(
                self.account_id, 1
            )

        self.assertEqual(
            week_before_end["metrics"]["followers_total"]["availability"],
            "partial",
        )
        self.assertEqual(
            week_before_end["metrics"]["followers_total"]["value"], 900
        )
        self.assertEqual(
            day_without_total["metrics"]["followers_total"]["availability"],
            "missing",
        )

        persist_total("2026-08-20", 920)
        with patch.object(
            platform_data_service,
            "_beijing_today",
            return_value=date(2026, 8, 21),
        ):
            week_at_end = platform_data_service.account_period_summary(
                self.account_id, 7
            )
        self.assertEqual(
            week_at_end["metrics"]["followers_total"]["availability"],
            "complete",
        )
        self.assertEqual(
            week_at_end["metrics"]["followers_total"]["value"], 920
        )

    def test_collection_rejects_metric_identity_mismatch_without_writes(self) -> None:
        """错账号和孤儿作品指标必须在落库前拒绝，不能留下运行头。"""

        wrong_account = CollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            metrics=(
                self._v2_point(
                    "views",
                    10,
                    day="2026-08-20",
                    entity_key="account:999",
                ),
            ),
            contents=(),
            account_metrics_available=True,
            content_data_available=False,
            platform_observed_at="2026-08-20T23:30:00+08:00",
            warning_code="content_list_unavailable",
        )
        with self.assertRaises(CollectionFailure) as raised:
            platform_data_service.record_collection_sync(
                self.account_id, wrong_account
            )
        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")

        with self.assertRaises(CollectionFailure) as raised:
            CollectionBatch(
                platform_type=3,
                source_mode="direct_session",
                metrics=(
                    self._v2_point("views", 10, day="2026-08-20"),
                    self._v2_point(
                        "views",
                        40,
                        day="2026-08-20",
                        entity_type="content",
                        entity_key="aweme-orphan",
                        metric_scope="lifetime_total",
                    ),
                ),
                contents=(self._content("aweme-1"),),
                account_metrics_available=True,
                content_data_available=True,
                platform_observed_at="2026-08-20T23:30:00+08:00",
            )
        self.assertEqual(raised.exception.error_code, "metric_payload_invalid")
        self.assertEqual(self._table_counts(), (0, 0, 0))

    def test_public_cover_url_allows_only_official_https_hosts(self) -> None:
        """作品封面只可公开受控抖音 HTTPS 主域，不能透出任意网址。"""

        urls = {
            "allowed-creator": (
                "https://creator.douyin.com/cover/1.jpg?token=private#preview"
            ),
            "allowed-pic": "https://img.douyinpic.com/cover/2.jpg",
            "allowed-cdn": "https://a.b.douyincdn.com/cover/3.jpg",
            "http-official": "http://creator.douyin.com/cover/4.jpg",
            "localhost": "https://localhost/cover/5.jpg",
            "loopback": "https://127.0.0.1/cover/6.jpg",
            "evil": "https://evil.example/cover/7.jpg",
            "fake-suffix": "https://douyin.com.evil.example/cover/8.jpg",
        }
        contents = tuple(
            ContentRecord(
                content_id=content_id,
                title=content_id,
                cover_url=cover_url,
                published_at="2026-08-20T10:00:00+08:00",
                content_status="published",
                content_type="video",
            )
            for content_id, cover_url in urls.items()
        )
        metrics = tuple(
            self._v2_point(
                "views",
                index,
                day="2026-08-20",
                entity_type="content",
                entity_key=content.content_id,
                metric_scope="lifetime_total",
            )
            for index, content in enumerate(contents, start=1)
        )
        platform_data_service.record_collection_sync(
            self.account_id,
            CollectionBatch(
                platform_type=3,
                source_mode="direct_session",
                metrics=(
                    self._v2_point("comments", 1, day="2026-08-20"),
                    *metrics,
                ),
                contents=contents,
                account_metrics_available=True,
                content_data_available=True,
                platform_observed_at="2026-08-20T23:30:00+08:00",
            ),
        )

        page = platform_data_service.account_contents(
            self.account_id, limit=100
        )
        covers = {
            item["contentId"]: item["coverUrl"] for item in page["items"]
        }
        self.assertEqual(
            covers,
            {
                "allowed-creator": "https://creator.douyin.com/cover/1.jpg",
                "allowed-pic": "https://img.douyinpic.com/cover/2.jpg",
                "allowed-cdn": "https://a.b.douyincdn.com/cover/3.jpg",
                "http-official": "",
                "localhost": "",
                "loopback": "",
                "evil": "",
                "fake-suffix": "",
            },
        )

    def test_v2_freshness_ignores_legacy_blank_scope_runs(self) -> None:
        """旧 blank-scope 尝试可见，但不得覆盖可信 V2 新鲜度。"""

        with patch.object(
            platform_data_service,
            "_now",
            return_value="2026-08-19T23:31:00+08:00",
        ):
            platform_data_service.record_collection_sync(
                self.account_id,
                CollectionBatch(
                    platform_type=3,
                    source_mode="direct_session",
                    metrics=(
                        self._v2_point("views", 10, day="2026-08-19"),
                    ),
                    contents=(),
                    account_metrics_available=True,
                    content_data_available=False,
                    platform_observed_at="2026-08-19T23:30:00+08:00",
                    warning_code="content_list_unavailable",
                ),
            )

        with database.connect() as conn:
            legacy_run_id = int(
                conn.execute(
                    """
                    INSERT INTO platform_data_sync_runs
                        (accountId, platformType, sourceMode, status,
                         errorCode, metricCount, startedAt, finishedAt)
                    VALUES (?, 3, 'browser_signed', 'success', '', 1, ?, ?)
                    """,
                    (
                        self.account_id,
                        "2026-08-20T23:30:00+08:00",
                        "2026-08-20T23:31:00+08:00",
                    ),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO platform_metric_snapshots
                    (syncRunId, accountId, platformType, entityType, entityKey,
                     metricKey, rawMetricKey, metricValue, metricUnit,
                     metricScope, periodStart, periodEnd, observedAt, createdAt)
                VALUES (?, ?, 3, 'account', ?, 'views', 'legacy_play', 999,
                        'count', '', '', '', ?, ?)
                """,
                (
                    legacy_run_id,
                    self.account_id,
                    f"account:{self.account_id}",
                    "2026-08-20T23:30:00+08:00",
                    "2026-08-20T23:31:00+08:00",
                ),
            )

        with patch.object(
            platform_data_service,
            "_beijing_today",
            return_value=date(2026, 8, 20),
        ):
            summary = platform_data_service.account_period_summary(
                self.account_id, 7
            )

        self.assertEqual(
            summary["latestRun"],
            {
                "status": "success",
                "sourceMode": "browser_signed",
                "errorCode": "",
                "metricCount": 1,
                "finishedAt": "2026-08-20T23:31:00+08:00",
            },
        )
        self.assertEqual(
            summary["platformObservedAt"], "2026-08-19T23:00:00+08:00"
        )
        self.assertEqual(
            summary["localSyncedAt"], "2026-08-19T23:31:00+08:00"
        )

    def test_latest_attempt_reports_failure_without_replacing_v2_freshness(self) -> None:
        """最新失败必须公开，但不能覆盖此前可信 V2 数据的新鲜度。"""

        with patch.object(
            platform_data_service,
            "_now",
            return_value="2026-08-19T23:31:00+08:00",
        ):
            platform_data_service.record_collection_sync(
                self.account_id,
                CollectionBatch(
                    platform_type=3,
                    source_mode="direct_session",
                    metrics=(
                        self._v2_point("views", 10, day="2026-08-19"),
                        self._v2_point(
                            "views",
                            100,
                            day="2026-08-19",
                            entity_type="content",
                            entity_key="aweme-1",
                            metric_scope="lifetime_total",
                        ),
                    ),
                    contents=(self._content("aweme-1"),),
                    account_metrics_available=True,
                    content_data_available=True,
                    platform_observed_at="2026-08-19T23:30:00+08:00",
                ),
            )
        with patch.object(
            platform_data_service,
            "_now",
            return_value="2026-08-20T23:31:00+08:00",
        ):
            platform_data_service.record_failed_sync(
                self.account_id,
                3,
                "browser_signed",
                "login_required",
            )

        with patch.object(
            platform_data_service,
            "_beijing_today",
            return_value=date(2026, 8, 20),
        ):
            summary = platform_data_service.account_period_summary(
                self.account_id, 7
            )

        self.assertEqual(
            summary["latestRun"],
            {
                "status": "failed",
                "sourceMode": "browser_signed",
                "errorCode": "login_required",
                "metricCount": 0,
                "finishedAt": "2026-08-20T23:31:00+08:00",
            },
        )
        self.assertEqual(
            summary["platformObservedAt"], "2026-08-19T23:00:00+08:00"
        )
        self.assertEqual(
            summary["localSyncedAt"], "2026-08-19T23:31:00+08:00"
        )
        self.assertEqual(summary.get("trustedSourceMode"), "direct_session")
        self.assertEqual(summary["metrics"]["views"]["value"], 10)

    def test_period_provenance_uses_only_rows_contributing_to_each_window(self) -> None:
        """来源与时间必须跟随当前区间实际参与汇总的 rows。"""

        def record(
            source_mode: str,
            day: str,
            value: int,
            *,
            metric_key: str = "views",
            metric_scope: str = "daily_increment",
            platform_observed_at: str,
            finished_at: str,
        ) -> None:
            with patch.object(platform_data_service, "_now", return_value=finished_at):
                platform_data_service.record_collection_sync(
                    self.account_id,
                    CollectionBatch(
                        platform_type=3,
                        source_mode=source_mode,
                        metrics=(
                            self._v2_point(
                                metric_key,
                                value,
                                day=day,
                                metric_scope=metric_scope,
                            ),
                        ),
                        contents=(),
                        account_metrics_available=True,
                        content_data_available=False,
                        platform_observed_at=platform_observed_at,
                        warning_code="content_list_unavailable",
                    ),
                )

        record(
            "direct_session",
            "2026-07-25",
            10,
            platform_observed_at="2026-07-25T23:00:00+08:00",
            finished_at="2026-07-25T23:31:00+08:00",
        )
        record(
            "browser_signed",
            "2026-08-19",
            20,
            platform_observed_at="2026-08-19T23:00:00+08:00",
            finished_at="2026-08-19T23:31:00+08:00",
        )
        record(
            "direct_session",
            "2026-08-18",
            5_000,
            metric_key="followers_total",
            platform_observed_at="2026-08-18T23:00:00+08:00",
            finished_at="2026-08-18T23:31:00+08:00",
        )
        record(
            "direct_session",
            "2026-08-17",
            4_900,
            metric_key="followers_total",
            metric_scope="lifetime_total",
            platform_observed_at="2026-08-17T23:00:00+08:00",
            finished_at="2026-08-17T23:31:00+08:00",
        )
        record(
            "browser_signed",
            "2026-08-19",
            5_100,
            metric_key="followers_total",
            metric_scope="lifetime_total",
            platform_observed_at="2026-08-19T23:00:00+08:00",
            finished_at="2026-08-19T23:32:00+08:00",
        )
        record(
            "direct_session",
            "2026-07-21",
            999,
            platform_observed_at="2026-08-20T23:00:00+08:00",
            finished_at="2026-08-20T23:31:00+08:00",
        )

        with patch.object(
            platform_data_service,
            "_beijing_today",
            return_value=date(2026, 8, 21),
        ):
            seven_days = platform_data_service.account_period_summary(
                self.account_id, 7
            )
            thirty_days = platform_data_service.account_period_summary(
                self.account_id, 30
            )
            no_rows = platform_data_service.account_period_summary(
                self.account_id, 1
            )

        self.assertEqual(seven_days["trustedSourceMode"], "browser_signed")
        self.assertEqual(seven_days["metrics"]["views"]["value"], 20)
        self.assertEqual(
            seven_days["metrics"]["followers_total"]["value"], 5_100
        )
        self.assertEqual(
            seven_days["platformObservedAt"], "2026-08-19T23:00:00+08:00"
        )
        self.assertEqual(
            seven_days["localSyncedAt"], "2026-08-19T23:32:00+08:00"
        )
        self.assertEqual(thirty_days["trustedSourceMode"], "mixed")
        self.assertEqual(thirty_days["metrics"]["views"]["value"], 30)
        self.assertEqual(
            thirty_days["platformObservedAt"], "2026-08-19T23:00:00+08:00"
        )
        self.assertEqual(
            thirty_days["localSyncedAt"], "2026-08-19T23:32:00+08:00"
        )
        self.assertIsNone(no_rows["trustedSourceMode"])
        self.assertIsNone(no_rows["platformObservedAt"])
        self.assertIsNone(no_rows["localSyncedAt"])
        self.assertEqual(
            seven_days["latestRun"],
            {
                "status": "partial_success",
                "sourceMode": "direct_session",
                "errorCode": "content_list_unavailable",
                "metricCount": 1,
                "finishedAt": "2026-08-20T23:31:00+08:00",
            },
        )


if __name__ == "__main__":
    unittest.main()
