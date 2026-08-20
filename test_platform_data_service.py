# -*- coding: utf-8 -*-
"""平台数据模型与快照服务的离线回归测试。"""

from __future__ import annotations

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
        )

        saved = platform_data_service.record_successful_sync(
            self.account_id, batch
        )
        summary = platform_data_service.account_data_summary(self.account_id)

        self.assertEqual(saved["status"], "success")
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


if __name__ == "__main__":
    unittest.main()
