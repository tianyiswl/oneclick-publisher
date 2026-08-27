# -*- coding: utf-8 -*-

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app_core import database, task_service
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
        self.platform_types = {31: 3, 11: 1, 2: 10}
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
                    (2, 10, "wechat.json", "硅基进化", "硅基进化"),
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

    def _create_project_task(
        self,
        project_id: str,
        *,
        phase: str,
        status: str,
        platform_post_id: str | None,
        platform_type: int = 3,
        account_file: str = "douyin.json",
    ) -> int:
        mode = "oneclick_publish" if phase == "formal" else "oneclick_preflight"
        task = task_service.create_pending_task(
            [
                {
                    "type": platform_type,
                    "contentType": "article" if platform_type == 10 else "video",
                    "title": f"{project_id}-{phase}",
                    "fileList": [f"/{project_id}-{phase}.mp4"],
                    "accountList": [account_file],
                    "debugDryRun": phase == "preflight",
                    "contentProjectId": project_id,
                }
            ],
            mode=mode,
        )
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET status = ?, finishedAt = ? WHERE id = ?",
                (status, self.now.isoformat(), task["id"]),
            )
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = ?, platformPostId = ?, publishedAt = ?, finishedAt = ?
                WHERE taskId = ?
                """,
                (
                    status,
                    platform_post_id,
                    self.now.isoformat() if platform_post_id else None,
                    self.now.isoformat(),
                    task["id"],
                ),
            )
        return int(task["id"])

    def _insert_content_metric(self, content_id: str, views: int) -> None:
        observed = self.now.isoformat()
        with database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO platform_data_sync_runs
                    (accountId, platformType, sourceMode, status, errorCode,
                     metricCount, startedAt, finishedAt)
                VALUES (31, 3, 'direct_session', 'success', '', 1, ?, ?)
                """,
                (observed, observed),
            )
            run_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO platform_contents
                    (accountId, platformType, contentId, title, coverUrl,
                     publishedAt, contentStatus, contentType, firstSeenAt, lastSeenAt)
                VALUES (31, 3, ?, ?, '', ?, 'published', 'video', ?, ?)
                """,
                (content_id, f"作品-{content_id}", observed, observed, observed),
            )
            conn.execute(
                """
                INSERT INTO platform_metric_snapshots
                    (syncRunId, accountId, platformType, entityType, entityKey,
                     metricKey, rawMetricKey, metricValue, metricUnit,
                     metricScope, periodStart, periodEnd, observedAt, createdAt)
                VALUES (?, 31, 3, 'content', ?, 'views', 'play_count', ?, 'count',
                        'lifetime_total', '2026-08-26', '2026-08-26', ?, ?)
                """,
                (run_id, content_id, views, observed, observed),
            )

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

    def test_project_query_never_returns_other_projects_content(self) -> None:
        self._create_project_task(
            "project-a", phase="formal", status="success", platform_post_id="work-a"
        )
        self._create_project_task(
            "project-b", phase="formal", status="success", platform_post_id="work-b"
        )
        self._insert_content_metric("work-a", 123)
        self._insert_content_metric("work-b", 456)

        result = self.service.get_project_metrics("project-a", self.profile, 7)

        self.assertEqual([item["contentId"] for item in result["contents"]], ["work-a"])
        self.assertEqual(result["contents"][0]["metrics"]["views"], 123)
        self.assertEqual(result["accounts"][0]["scope"], "account")

    def test_success_item_without_platform_id_is_missing_not_published(self) -> None:
        task_id = self._create_project_task(
            "project-a", phase="formal", status="success", platform_post_id=None
        )

        result = self.service.get_project_metrics("project-a", self.profile, 1)

        item = next(row for row in result["contents"] if row["taskId"] == task_id)
        self.assertIsNone(item["contentId"])
        self.assertEqual(item["availability"], "missing")
        self.assertTrue(all(value is None for value in item["metrics"].values()))

    def test_known_publish_waiting_for_platform_statistics_is_pending(self) -> None:
        """已有发表回执但平台尚未结算数据时，不能把作品本身报成缺失。"""

        task_id = self._create_project_task(
            "project-a", phase="formal", status="success", platform_post_id="today-work"
        )

        result = self.service.get_project_metrics("project-a", self.profile, 1)

        item = next(row for row in result["contents"] if row["taskId"] == task_id)
        self.assertEqual(item["contentId"], "today-work")
        self.assertEqual(item["availability"], "pending")
        self.assertTrue(all(value is None for value in item["metrics"].values()))

    def test_wechat_profile_alias_keeps_confirmed_publish_in_project_results(self) -> None:
        """“公众号”和“微信公众号”必须归一，否则真实文章会被本地过滤。"""

        task_id = self._create_project_task(
            "silicon-evolution",
            phase="formal",
            status="success",
            platform_post_id="wechat-post",
            platform_type=10,
            account_file="wechat.json",
        )
        profile = {
            "projectId": "silicon-evolution",
            "targets": [{"platform": "微信公众号", "accountId": 2}],
        }

        result = self.service.get_project_metrics(
            "silicon-evolution", profile, 1
        )

        item = next(row for row in result["contents"] if row["taskId"] == task_id)
        self.assertEqual(item["platform"], "微信公众号")
        self.assertEqual(item["contentId"], "wechat-post")
        self.assertEqual(item["availability"], "pending")

    def test_preflight_and_failed_tasks_do_not_enter_project_contents(self) -> None:
        preflight_task = self._create_project_task(
            "project-a", phase="preflight", status="success", platform_post_id="preflight"
        )
        failed_task = self._create_project_task(
            "project-a", phase="formal", status="failed", platform_post_id="failed"
        )
        success_task = self._create_project_task(
            "project-a", phase="formal", status="success", platform_post_id="work-a"
        )
        self._insert_content_metric("work-a", 123)

        result = self.service.get_project_metrics("project-a", self.profile, 1)
        task_ids = {row["taskId"] for row in result["contents"]}

        self.assertNotIn(preflight_task, task_ids)
        self.assertNotIn(failed_task, task_ids)
        self.assertEqual(task_ids, {success_task})


if __name__ == "__main__":
    unittest.main()
