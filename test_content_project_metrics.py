# -*- coding: utf-8 -*-

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app_core import database
from app_core.content_project_metrics import ContentProjectMetricsService


class ContentProjectMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "database.db"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        database.ensure_schema()
        self.now = datetime(2026, 8, 26, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.calls: list[int] = []
        self.failed_accounts: set[int] = set()
        self.platform_types = {31: 3, 11: 1}
        with database.connect() as conn:
            conn.executemany(
                """
                INSERT INTO user_info
                    (id, type, filePath, userName, status, profileName)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                [
                    (31, 3, "douyin.json", "抖音主体", "抖音主体"),
                    (11, 1, "xhs.json", "小红书主体", "小红书主体"),
                ],
            )
        self.profile = {
            "projectId": "project-a",
            "targets": [{"platform": "抖音", "accountId": 31}],
        }
        self.two_account_profile = {
            "projectId": "project-a",
            "targets": [
                {"platform": "抖音", "accountId": 31},
                {"platform": "小红书", "accountId": 11},
            ],
        }
        self.service = ContentProjectMetricsService(
            sync_account=self._sync_account,
            now=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tempdir.cleanup()

    def _insert_run(
        self,
        account_id: int,
        *,
        status: str,
        finished_at: datetime,
        error_code: str = "",
    ) -> None:
        with database.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_data_sync_runs
                    (accountId, platformType, sourceMode, status, errorCode,
                     metricCount, startedAt, finishedAt)
                VALUES (?, ?, 'direct_session', ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    self.platform_types[account_id],
                    status,
                    error_code,
                    0 if status == "failed" else 3,
                    finished_at.isoformat(),
                    finished_at.isoformat(),
                ),
            )

    def _sync_account(self, account_id: int) -> dict:
        self.calls.append(account_id)
        failed = account_id in self.failed_accounts
        self._insert_run(
            account_id,
            status="failed" if failed else "success",
            finished_at=self.now,
            error_code="login_required" if failed else "",
        )
        return {
            "accountId": account_id,
            "status": "failed" if failed else "success",
            "sourceMode": "direct_session",
            "errorCode": "login_required" if failed else "",
            "metricCount": 0 if failed else 3,
            "contentCount": 0 if failed else 2,
        }

    def test_two_projects_reuse_same_account_success_on_same_beijing_day(self) -> None:
        first = self.service.sync_project("project-a", self.profile)
        second = self.service.sync_project("project-b", self.profile)

        self.assertEqual(first["accounts"][0]["action"], "synced")
        self.assertEqual(second["accounts"][0]["action"], "reused")
        self.assertEqual(self.calls, [31])

    def test_recent_failure_returns_cooldown_without_calling_collector(self) -> None:
        self._insert_run(
            31,
            status="failed",
            finished_at=self.now - timedelta(minutes=10),
            error_code="login_required",
        )

        result = self.service.sync_project("project-a", self.profile)

        self.assertEqual(result["accounts"][0]["action"], "cooldown")
        self.assertEqual(result["accounts"][0]["errorCode"], "login_required")
        self.assertEqual(self.calls, [])

    def test_failure_older_than_cooldown_allows_new_attempt(self) -> None:
        self._insert_run(
            31,
            status="failed",
            finished_at=self.now - timedelta(minutes=31),
            error_code="login_required",
        )

        result = self.service.sync_project("project-a", self.profile)

        self.assertEqual(result["accounts"][0]["action"], "synced")
        self.assertEqual(self.calls, [31])

    def test_one_account_failure_does_not_skip_next_account(self) -> None:
        self.failed_accounts = {31}

        result = self.service.sync_project("project-a", self.two_account_profile)

        self.assertEqual(
            [row["status"] for row in result["accounts"]],
            ["failed", "success"],
        )
        self.assertEqual(self.calls, [31, 11])

    def test_status_is_read_only_and_reports_current_policy(self) -> None:
        self._insert_run(31, status="success", finished_at=self.now)

        result = self.service.sync_status("project-a", self.profile)

        self.assertEqual(result["accounts"][0]["action"], "reused")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
