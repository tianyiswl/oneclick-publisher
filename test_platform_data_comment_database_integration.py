# -*- coding: utf-8 -*-
"""评论洞察表结构接入共享数据库入口的集成回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import database


class PlatformDataCommentDatabaseIntegrationTests(unittest.TestCase):
    def test_ensure_schema_adds_comment_schema_idempotently_without_losing_data(
        self,
    ) -> None:
        """漏接建表入口或重复建表破坏旧数据时，本测试必须失败。"""

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "oneclick.db"
            with patch.object(database, "DB_PATH", database_path):
                database.ensure_schema()
                with database.open_connection(database_path) as conn:
                    account_id = int(
                        conn.execute(
                            """
                            INSERT INTO user_info
                                (type, filePath, userName, status, profileName,
                                 authMode)
                            VALUES (3, 'safe.json', '测试账号', 1, '测试主体',
                                    'browser')
                            """
                        ).lastrowid
                    )
                    sync_run_id = int(
                        conn.execute(
                            """
                            INSERT INTO platform_data_sync_runs
                                (accountId, platformType, sourceMode, status,
                                 metricCount, startedAt, finishedAt)
                            VALUES (?, 3, 'direct_session', 'success', 1,
                                    '2026-08-24T10:00:00+08:00',
                                    '2026-08-24T10:00:01+08:00')
                            """,
                            (account_id,),
                        ).lastrowid
                    )
                    conn.execute(
                        """
                        INSERT INTO platform_metric_snapshots
                            (syncRunId, accountId, platformType, entityType,
                             entityKey, metricKey, rawMetricKey, metricValue,
                             metricUnit, metricScope, periodStart, periodEnd,
                             observedAt, createdAt)
                        VALUES (?, ?, 3, 'content', 'work-1', 'comments',
                                'comment_count', 7, 'count', 'lifetime', '', '',
                                '2026-08-24T10:00:00+08:00',
                                '2026-08-24T10:00:01+08:00')
                        """,
                        (sync_run_id, account_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO platform_contents
                            (accountId, platformType, contentId, title, coverUrl,
                             publishedAt, contentStatus, contentType, firstSeenAt,
                             lastSeenAt)
                        VALUES (?, 3, 'work-1', '保留作品',
                                'https://example.invalid/cover.jpg',
                                '2026-08-23T09:00:00+08:00', 'published',
                                'video', '2026-08-24T10:00:00+08:00',
                                '2026-08-24T10:00:00+08:00')
                        """,
                        (account_id,),
                    )

                database.ensure_schema()
                database.ensure_schema()

                with database.open_connection(
                    database_path, row_factory=True
                ) as conn:
                    expected_tables = {
                        "platform_comment_sync_runs",
                        "platform_comments",
                        "comment_insight_runs",
                    }
                    expected_indexes = {
                        "idx_platform_comment_sync_runs_account_content",
                        "idx_platform_comments_latest_page",
                        "idx_comment_insight_runs_latest",
                    }
                    for name in expected_tables:
                        count = conn.execute(
                            """
                            SELECT COUNT(*) FROM sqlite_master
                            WHERE type = 'table' AND name = ?
                            """,
                            (name,),
                        ).fetchone()[0]
                        self.assertEqual(count, 1, name)
                    for name in expected_indexes:
                        count = conn.execute(
                            """
                            SELECT COUNT(*) FROM sqlite_master
                            WHERE type = 'index' AND name = ?
                            """,
                            (name,),
                        ).fetchone()[0]
                        self.assertEqual(count, 1, name)

                    self.assertEqual(
                        tuple(
                            conn.execute(
                                """
                                SELECT platformType, status, metricCount
                                FROM platform_data_sync_runs WHERE id = ?
                                """,
                                (sync_run_id,),
                            ).fetchone()
                        ),
                        (3, "success", 1),
                    )
                    self.assertEqual(
                        tuple(
                            conn.execute(
                                """
                                SELECT metricKey, metricValue
                                FROM platform_metric_snapshots
                                WHERE syncRunId = ?
                                """,
                                (sync_run_id,),
                            ).fetchone()
                        ),
                        ("comments", 7.0),
                    )
                    self.assertEqual(
                        tuple(
                            conn.execute(
                                """
                                SELECT title, contentStatus
                                FROM platform_contents
                                WHERE accountId = ? AND contentId = 'work-1'
                                """,
                                (account_id,),
                            ).fetchone()
                        ),
                        ("保留作品", "published"),
                    )


if __name__ == "__main__":
    unittest.main()
