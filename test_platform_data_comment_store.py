# -*- coding: utf-8 -*-
"""匿名评论仓库的迁移、事务与隐私边界测试。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app_core import platform_data_comment_store as store
from app_core.platform_data_comment_models import (
    CommentClassification,
    CommentCollectionBatch,
    CommentInsightFailure,
    CommentRecord,
    InsightResult,
    TopicCandidate,
)


OBSERVED = "2026-08-23T10:21:00+08:00"
LATER = "2026-08-23T10:22:00+08:00"


def prepared_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE user_info (
            id INTEGER PRIMARY KEY,
            type INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE platform_contents (
            id INTEGER PRIMARY KEY,
            accountId INTEGER NOT NULL,
            platformType INTEGER NOT NULL,
            contentId TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            UNIQUE(accountId, platformType, contentId)
        )
        """
    )
    conn.execute("INSERT INTO user_info (id, type) VALUES (12, 3)")
    conn.execute("INSERT INTO user_info (id, type) VALUES (99, 3)")
    conn.execute(
        """
        INSERT INTO platform_contents (accountId, platformType, contentId, title)
        VALUES (12, 3, 'work-7', '作品七')
        """
    )
    conn.commit()
    store.create_comment_schema(conn)
    conn.commit()
    return conn


def record(
    key: str = "a" * 64,
    *,
    likes: int = 3,
    replies: int = 1,
    observed_at: str = OBSERVED,
) -> CommentRecord:
    return CommentRecord(
        content_id="work-7",
        comment_key=key,
        body="  保留原始空格  ",
        like_count=likes,
        reply_count=replies,
        commented_at="2026-08-23T10:20:30+08:00",
        observed_at=observed_at,
    )


def valid_batch(*, two_comments: bool = False, later: bool = False) -> CommentCollectionBatch:
    entries = (record(observed_at=LATER if later else OBSERVED),)
    if two_comments:
        entries += (record("b" * 64, observed_at=LATER if later else OBSERVED),)
    return CommentCollectionBatch(
        platform_type=3,
        source_mode="direct_session",
        content_id="work-7",
        comments=entries,
        accepted_count=len(entries),
        rejected_count=0,
        page_count=1,
        stop_reason="platform_end",
        warning_code="",
        platform_observed_at=LATER if later else OBSERVED,
        cleanup_receipt=None,
    )


class CommentStoreTests(unittest.TestCase):
    def test_schema_creates_only_anonymous_tables_and_query_indexes(self):
        """删除任何表、索引或隐私边界时，此测试应失败。"""

        conn = prepared_connection()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        comment_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(platform_comments)")
        }

        self.assertTrue(
            {
                "platform_comment_sync_runs",
                "platform_comments",
                "comment_insight_runs",
            }.issubset(tables)
        )
        self.assertTrue(
            {
                "idx_platform_comment_sync_runs_account_content",
                "idx_platform_comments_latest_page",
                "idx_comment_insight_runs_latest",
            }.issubset(indexes)
        )
        self.assertNotIn("platformCommentId", comment_columns)
        self.assertNotIn("authorId", comment_columns)
        self.assertNotIn("authorName", comment_columns)

    def test_content_lookup_requires_the_current_douyin_account_to_own_content(self):
        """移除账号或作品归属判断会允许错误账号开始同步。"""

        conn = prepared_connection()

        self.assertEqual(
            store.content_for_comment_sync(conn, 12, "work-7"),
            {"contentId": "work-7", "title": "作品七"},
        )
        self.assertIsNone(store.content_for_comment_sync(conn, 99, "work-7"))
        self.assertIsNone(store.content_for_comment_sync(conn, 12, "missing"))

    def test_batch_finalization_and_comment_upserts_commit_together(self):
        """若遗漏最终状态或评论写入，提交后两个结果就不会同时可见。"""

        conn = prepared_connection()
        with conn:
            receipt = store.persist_comment_batch(conn, 12, valid_batch(two_comments=True))

        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["insertedCount"], 2)
        run = conn.execute(
            "SELECT status, acceptedCount, insertedCount FROM platform_comment_sync_runs"
        ).fetchone()
        self.assertEqual(tuple(run), ("success", 2, 2))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM platform_comments").fetchone()[0], 2
        )

    def test_batch_failure_rolls_back_run_and_comments(self):
        """最终写入失败不得留下运行记录或半批评论。"""

        conn = prepared_connection()
        batch = valid_batch(two_comments=True)
        with patch.object(store, "_finalize_run", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(sqlite3.OperationalError):
                with conn:
                    store.persist_comment_batch(conn, 12, batch)

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM platform_comment_sync_runs").fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM platform_comments").fetchone()[0],
            0,
        )

    def test_known_comments_update_counts_without_deleting_absent_history(self):
        """把已知评论当新增或清除未返回历史评论都会让此测试失败。"""

        conn = prepared_connection()
        with conn:
            store.persist_comment_batch(conn, 12, valid_batch(two_comments=True))
        changed = CommentCollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            content_id="work-7",
            comments=(record(likes=9, replies=4, observed_at=LATER),),
            accepted_count=1,
            rejected_count=0,
            page_count=1,
            stop_reason="known_comment",
            warning_code="",
            platform_observed_at=LATER,
            cleanup_receipt=None,
        )
        with conn:
            receipt = store.persist_comment_batch(conn, 12, changed)

        updated = conn.execute(
            """
            SELECT likeCount, replyCount, firstSeenAt, lastSeenAt
            FROM platform_comments WHERE commentKey = ?
            """,
            ("a" * 64,),
        ).fetchone()
        self.assertEqual(receipt["insertedCount"], 0)
        self.assertEqual(receipt["updatedCount"], 1)
        self.assertEqual(tuple(updated), (9, 4, OBSERVED, LATER))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM platform_comments").fetchone()[0], 2
        )
        self.assertEqual(store.known_comment_keys(conn, 12, "work-7"), frozenset({"a" * 64, "b" * 64}))

    def test_comment_and_insight_queries_use_their_latest_indexes(self):
        """移除最新读取索引会让 SQLite 回退到扫描或临时排序。"""

        conn = prepared_connection()
        with conn:
            store.persist_comment_batch(conn, 12, valid_batch(two_comments=True))
            store.persist_insight_result(
                conn,
                12,
                "work-7",
                provider_type="openai_compatible",
                model_name="test-model",
                prompt_version="v1",
                schema_version=1,
                input_fingerprint="f" * 64,
                comment_count=2,
                result=InsightResult(
                    classifications=(CommentClassification("a" * 64, ("追问",)),),
                    candidates=(TopicCandidate("下一期", "回答问题", ("a" * 64,)),),
                    known_comment_keys=frozenset({"a" * 64, "b" * 64}),
                ),
            )

        comment_plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM platform_comments
            WHERE accountId = 12 AND contentId = 'work-7'
            ORDER BY commentedAt DESC, id DESC LIMIT 20
            """
        ).fetchall()
        insight_plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM comment_insight_runs
            WHERE accountId = 12 AND contentId = 'work-7'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchall()

        self.assertTrue(any("idx_platform_comments_latest_page" in row[3] for row in comment_plan))
        self.assertTrue(any("idx_comment_insight_runs_latest" in row[3] for row in insight_plan))
        self.assertEqual(len(store.list_comment_rows(conn, 12, "work-7")), 2)
        self.assertEqual(store.latest_insight(conn, 12, "work-7")["status"], "success")

    def test_failed_ai_receipt_keeps_saved_comments_unchanged(self):
        """AI 失败若改写评论表，会污染已经成功保存的同步结果。"""

        conn = prepared_connection()
        with conn:
            store.persist_comment_batch(conn, 12, valid_batch())
        before = [dict(row) for row in conn.execute("SELECT * FROM platform_comments")]
        with conn:
            failed = store.persist_insight_result(
                conn,
                12,
                "work-7",
                provider_type="openai_compatible",
                model_name="test-model",
                prompt_version="v1",
                schema_version=1,
                input_fingerprint="f" * 64,
                comment_count=1,
                result=None,
                error_code="comment_ai_timeout",
            )
        after = [dict(row) for row in conn.execute("SELECT * FROM platform_comments")]

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["errorCode"], "comment_ai_timeout")
        self.assertEqual(after, before)

    def test_failed_comment_sync_records_failure_after_ownership_check(self):
        """失败运行也必须绑定当前账号拥有的抖音作品，不能写入越权记录。"""

        conn = prepared_connection()
        with conn:
            receipt = store.record_failed_comment_sync(
                conn, 12, "work-7", "browser_signed", "comment_login_required"
            )

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["errorCode"], "comment_login_required")
        with self.assertRaises(CommentInsightFailure) as raised:
            store.record_failed_comment_sync(
                conn, 99, "work-7", "browser_signed", "comment_login_required"
            )
        self.assertEqual(raised.exception.error_code, "comment_content_unavailable")


if __name__ == "__main__":
    unittest.main()
