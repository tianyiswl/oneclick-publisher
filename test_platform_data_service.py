# -*- coding: utf-8 -*-
"""平台数据模型与快照服务的离线回归测试。"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import database, platform_data_service
from app_core.platform_data_models import (
    CollectionBatch,
    CollectionFailure,
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
            observed_at=observed_at,
        )

    def test_success_persists_only_present_metrics_and_never_invents_zero(self) -> None:
        """删除缺失保护会把 likes 伪造成 0，本测试必须失败。"""

        batch = CollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            metrics=(self._point("views", 125),),
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
