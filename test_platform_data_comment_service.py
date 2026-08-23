# -*- coding: utf-8 -*-
"""手动评论同步服务的事务、隐私和编排边界测试。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import tempfile
import unittest

from app_core import platform_data_comment_service as service
from app_core import platform_data_comment_store as store
from app_core.platform_data_collection_errors import CleanupReceipt
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
PUBLIC_RESULT_KEYS = {
    "status",
    "errorCode",
    "acceptedCount",
    "insertedCount",
    "updatedCount",
    "rejectedCount",
    "pageCount",
    "stopReason",
    "warningCode",
    "cleanup",
    "aiStatus",
    "aiErrorCode",
}


def record(
    key: str = "a" * 64,
    *,
    body: str = "为什么会这样？",
    likes: int = 3,
    replies: int = 1,
    observed_at: str = OBSERVED,
) -> CommentRecord:
    return CommentRecord(
        content_id="work-7",
        comment_key=key,
        body=body,
        like_count=likes,
        reply_count=replies,
        commented_at="2026-08-23T10:20:30+08:00",
        observed_at=observed_at,
    )


def valid_batch(
    *,
    records: tuple[CommentRecord, ...] | None = None,
    stop_reason: str = "platform_end",
    warning_code: str = "",
) -> CommentCollectionBatch:
    entries = records or (
        record(),
        record("b" * 64, body="我也遇到过。"),
    )
    return CommentCollectionBatch(
        platform_type=3,
        source_mode="browser_signed",
        content_id="work-7",
        comments=entries,
        accepted_count=len(entries),
        rejected_count=0,
        page_count=1,
        stop_reason=stop_reason,
        warning_code=warning_code,
        platform_observed_at=LATER,
        cleanup_receipt=CleanupReceipt(closed=True, alive_resource_count=0),
    )


def current_fingerprint(conn: sqlite3.Connection) -> str:
    """按服务合同中的当前评论顺序生成测试快照指纹。"""

    digest = hashlib.sha256()
    rows = conn.execute(
        """
        SELECT commentKey, body FROM platform_comments
        WHERE accountId = 12 AND platformType = 3 AND contentId = 'work-7'
        ORDER BY commentedAt DESC, id DESC
        """
    ).fetchall()
    for row in rows:
        digest.update(row["commentKey"].encode("ascii"))
        digest.update(b"\x1f")
        digest.update(row["body"].encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


class FixedCollector:
    def __init__(self, outcome, *, on_collect=None) -> None:
        self.outcome = outcome
        self.on_collect = on_collect
        self.calls = 0

    def collect(self, account, content_id, known_keys, limit=100, report=None):
        self.calls += 1
        if self.on_collect is not None:
            self.on_collect(account, content_id, known_keys, limit, report)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FixedAi:
    model_name = "test-model"

    def __init__(self, outcome, *, on_analyze=None) -> None:
        self.outcome = outcome
        self.on_analyze = on_analyze
        self.calls = 0

    def analyze(self, title, comments):
        self.calls += 1
        if self.on_analyze is not None:
            self.on_analyze(title, comments)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class CommentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            """
            CREATE TABLE user_info (
                id INTEGER PRIMARY KEY,
                type INTEGER NOT NULL,
                filePath TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
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
        self.conn.executemany(
            "INSERT INTO user_info (id, type, filePath) VALUES (?, ?, ?)",
            (
                (12, 3, "douyin-12.json"),
                (22, 1, "xiaohongshu-22.json"),
                (99, 3, "douyin-99.json"),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO platform_contents (accountId, platformType, contentId, title)
            VALUES (12, 3, 'work-7', '作品七')
            """
        )
        self.conn.execute(
            """
            INSERT INTO platform_contents (accountId, platformType, contentId, title)
            VALUES (22, 1, 'xhs-1', '小红书作品')
            """
        )
        store.create_comment_schema(self.conn)
        self.conn.commit()

        @contextmanager
        def connect():
            with self.conn:
                yield self.conn

        self.connect = connect

    def tearDown(self) -> None:
        self.conn.close()

    def _sync(self, collector, **kwargs):
        return service.sync_comments(
            12,
            "work-7",
            collector_factory=lambda: collector,
            connect_factory=self.connect,
            **kwargs,
        )

    def _persist_existing(self) -> None:
        with self.conn:
            store.persist_comment_batch(
                self.conn,
                12,
                valid_batch(records=(record(),)),
            )

    def test_sync_requires_owned_douyin_content_id_and_never_uses_title(self):
        """若标题或跨账号作品能通过定位，采集器就会读取错误对象。"""

        collector = FixedCollector(valid_batch())
        cases = (
            (99, "work-7"),
            (22, "xhs-1"),
            (12, "作品七"),
            (12, "missing"),
        )

        for account_id, content_id in cases:
            result = service.sync_comments(
                account_id,
                content_id,
                collector_factory=lambda: collector,
                connect_factory=self.connect,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["errorCode"], "comment_content_unavailable")
            self.assertEqual(set(result), PUBLIC_RESULT_KEYS)

        self.assertEqual(collector.calls, 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM platform_comment_sync_runs"
            ).fetchone()[0],
            0,
        )

    def test_known_keys_are_loaded_before_collection(self):
        """遗漏已知键预读会让后续同步重复翻页和重复识别旧评论。"""

        self._persist_existing()
        seen = {}

        def inspect(account, content_id, known_keys, limit, report):
            seen.update(
                account=dict(account),
                content_id=content_id,
                known_keys=known_keys,
                limit=limit,
                report=report,
            )

        batch = valid_batch(
            records=(record(likes=9, observed_at=LATER),),
            stop_reason="known_comment",
        )
        result = self._sync(FixedCollector(batch, on_collect=inspect))

        self.assertEqual(result["status"], "success")
        self.assertEqual(seen["known_keys"], frozenset({"a" * 64}))
        self.assertEqual(seen["content_id"], "work-7")
        self.assertEqual(seen["limit"], 100)
        self.assertEqual(seen["account"], {"id": 12, "type": 3, "filePath": "douyin-12.json"})
        self.assertIsNone(seen["report"])

    def test_comments_commit_before_ai_failure_and_remain_visible(self):
        """若 AI 与评论共用事务，AI 超时会让已经采到的评论消失。"""

        def assert_committed(title, comments):
            self.assertEqual(title, "作品七")
            self.assertEqual(len(comments), 2)
            count = self.conn.execute(
                "SELECT COUNT(*) FROM platform_comments"
            ).fetchone()[0]
            self.assertEqual(count, 2)

        ai = FixedAi(
            CommentInsightFailure("comment_ai_timeout", retryable=True),
            on_analyze=assert_committed,
        )
        result = self._sync(
            FixedCollector(valid_batch()),
            ai_provider_factory=lambda: ai,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["aiStatus"], "failed")
        self.assertEqual(result["aiErrorCode"], "comment_ai_timeout")
        self.assertEqual(ai.calls, 1)
        panel = service.comment_panel_payload(
            12, "work-7", connect_factory=self.connect
        )
        self.assertEqual(len(panel["comments"]), 2)
        insight_row = self.conn.execute(
            "SELECT status, errorCode FROM comment_insight_runs"
        ).fetchone()
        self.assertEqual(tuple(insight_row), ("failed", "comment_ai_timeout"))

    def test_collector_failure_preserves_old_comments_and_records_one_run(self):
        """采集失败不得清空旧评论，也不得暗中重试制造多条失败运行。"""

        self._persist_existing()
        before = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT commentKey, body, likeCount, replyCount FROM platform_comments"
            )
        ]
        failure = CommentInsightFailure("comment_login_required")
        failure.cleanup_receipt = CleanupReceipt(
            closed=True, alive_resource_count=0
        )
        collector = FixedCollector(failure)

        result = self._sync(collector)

        after = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT commentKey, body, likeCount, replyCount FROM platform_comments"
            )
        ]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errorCode"], "comment_login_required")
        self.assertEqual(result["cleanup"], {"closed": True, "aliveResourceCount": 0})
        self.assertEqual(before, after)
        self.assertEqual(collector.calls, 1)
        failed = self.conn.execute(
            """
            SELECT COUNT(*) FROM platform_comment_sync_runs
            WHERE status = 'failed' AND errorCode = 'comment_login_required'
            """
        ).fetchone()[0]
        self.assertEqual(failed, 1)

    def test_each_service_call_runs_once_without_hidden_retry_or_dedupe(self):
        """把重复点击逻辑放进服务会掩盖 UI 任务键失效或重试平台请求。"""

        collector = FixedCollector(CommentInsightFailure("comment_access_denied"))

        first = self._sync(collector)
        second = self._sync(collector)

        self.assertEqual(first["errorCode"], "comment_access_denied")
        self.assertEqual(second["errorCode"], "comment_access_denied")
        self.assertEqual(collector.calls, 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM platform_comment_sync_runs WHERE status = 'failed'"
            ).fetchone()[0],
            2,
        )

    def test_unconfigured_ai_does_not_hide_or_rollback_comments(self):
        """未配置 AI 时，评论同步仍必须是成功并可立即读取。"""

        result = self._sync(
            FixedCollector(valid_batch()), ai_provider_factory=lambda: None
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["aiStatus"], "skipped")
        self.assertEqual(result["aiErrorCode"], "comment_ai_not_configured")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM platform_comments").fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM comment_insight_runs").fetchone()[0],
            1,
        )
        row = self.conn.execute(
            """
            SELECT status, errorCode, commentCount, inputFingerprint
            FROM comment_insight_runs
            """
        ).fetchone()
        self.assertEqual(tuple(row[:3]), ("skipped", "comment_ai_not_configured", 2))
        self.assertEqual(row["inputFingerprint"], current_fingerprint(self.conn))

    def test_ai_outcome_must_match_the_real_database_comment_snapshot(self):
        """调用方虚构的评论键、数量或指纹不能被保存为成功洞察。"""

        self._persist_existing()
        fake_key = "c" * 64
        fabricated = InsightResult(
            classifications=(CommentClassification(fake_key, ("追问",)),),
            candidates=(TopicCandidate("虚构选题", "没有数据库证据", (fake_key,)),),
            known_comment_keys=frozenset({fake_key}),
        )

        with self.assertRaises(CommentInsightFailure) as raised:
            service.record_comment_ai_outcome(
                12,
                "work-7",
                result=fabricated,
                error_code="",
                model_name="test-model",
                prompt_version="comment-insight-v1",
                schema_version=1,
                input_fingerprint=current_fingerprint(self.conn),
                comment_count=1,
                connect_factory=self.connect,
            )

        self.assertEqual(raised.exception.error_code, "comment_ai_evidence_invalid")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM comment_insight_runs WHERE status = 'success'"
            ).fetchone()[0],
            0,
        )

    def test_ai_outcome_rejects_stale_fingerprint_and_comment_count(self):
        """即使证据键真实，旧指纹或旧数量也不能写入当前评论快照。"""

        self._persist_existing()
        insight = InsightResult(
            classifications=(CommentClassification("a" * 64, ("追问",)),),
            candidates=(TopicCandidate("回答追问", "真实评论证据", ("a" * 64,)),),
            known_comment_keys=frozenset({"a" * 64}),
        )
        cases = (
            ("f" * 64, 1),
            (current_fingerprint(self.conn), 0),
        )

        for fingerprint, count in cases:
            with self.subTest(fingerprint=fingerprint, count=count):
                with self.assertRaises(CommentInsightFailure) as raised:
                    service.record_comment_ai_outcome(
                        12,
                        "work-7",
                        result=insight,
                        error_code="",
                        model_name="test-model",
                        prompt_version="comment-insight-v1",
                        schema_version=1,
                        input_fingerprint=fingerprint,
                        comment_count=count,
                        connect_factory=self.connect,
                    )
                self.assertEqual(
                    raised.exception.error_code, "comment_payload_invalid"
                )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM comment_insight_runs").fetchone()[0],
            0,
        )

    def test_all_ai_outcomes_hold_one_real_sqlite_snapshot_transaction(self):
        """文件数据库竞争写入不能插进评论快照校验与洞察写入之间。"""

        key_a = "a" * 64
        key_b = "b" * 64
        fingerprint = hashlib.sha256(
            (key_a + "\x1f为什么会这样？\x1e").encode("utf-8")
        ).hexdigest()
        insight = InsightResult(
            classifications=(CommentClassification(key_a, ("追问",)),),
            candidates=(TopicCandidate("回答追问", "真实评论证据", (key_a,)),),
            known_comment_keys=frozenset({key_a}),
        )
        cases = (
            ("success", insight, "", "test-model", ""),
            ("failed", None, "comment_ai_timeout", "test-model", "comment_ai_timeout"),
            (
                "skipped",
                None,
                "comment_ai_not_configured",
                "unconfigured",
                "comment_ai_not_configured",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            for expected_status, outcome, error_code, model_name, expected_error in cases:
                with self.subTest(status=expected_status):
                    path = f"{directory}/{expected_status}.sqlite3"
                    setup = sqlite3.connect(path)
                    setup.row_factory = sqlite3.Row
                    setup.execute("PRAGMA foreign_keys = ON")
                    setup.executescript(
                        """
                        CREATE TABLE user_info (
                            id INTEGER PRIMARY KEY,
                            type INTEGER NOT NULL,
                            filePath TEXT NOT NULL
                        );
                        CREATE TABLE platform_contents (
                            id INTEGER PRIMARY KEY,
                            accountId INTEGER NOT NULL,
                            platformType INTEGER NOT NULL,
                            contentId TEXT NOT NULL,
                            title TEXT NOT NULL DEFAULT '',
                            UNIQUE(accountId, platformType, contentId)
                        );
                        INSERT INTO user_info (id, type, filePath)
                        VALUES (12, 3, 'douyin-12.json');
                        INSERT INTO platform_contents
                            (accountId, platformType, contentId, title)
                        VALUES (12, 3, 'work-7', '作品七');
                        """
                    )
                    store.create_comment_schema(setup)
                    with setup:
                        store.persist_comment_batch(
                            setup,
                            12,
                            valid_batch(records=(record(),)),
                        )
                    setup.close()

                    transaction_states = []
                    competing_commits = []

                    def insert_competing_comment() -> None:
                        competitor = sqlite3.connect(path, timeout=0.02)
                        competitor.execute("PRAGMA foreign_keys = ON")
                        try:
                            with competitor:
                                competitor.execute(
                                    """
                                    INSERT INTO platform_comments
                                        (accountId, platformType, contentId,
                                         commentKey, body, likeCount, replyCount,
                                         commentedAt, firstSeenAt, lastSeenAt)
                                    VALUES (12, 3, 'work-7', ?, '竞争评论', 0, 0,
                                            ?, ?, ?)
                                    """,
                                    (key_b, OBSERVED, OBSERVED, OBSERVED),
                                )
                        except sqlite3.OperationalError as exc:
                            if "locked" not in str(exc).lower():
                                raise
                            competing_commits.append(False)
                        else:
                            competing_commits.append(True)
                        finally:
                            competitor.close()

                    class FetchHookCursor:
                        def __init__(self, cursor, connection) -> None:
                            self._cursor = cursor
                            self._connection = connection
                            self.description = cursor.description

                        def fetchall(self):
                            rows = self._cursor.fetchall()
                            self._cursor.close()
                            transaction_states.append(
                                self._connection.in_transaction
                            )
                            insert_competing_comment()
                            return rows

                    class HookConnection(sqlite3.Connection):
                        def execute(self, sql, parameters=()):
                            cursor = super().execute(sql, parameters)
                            if (
                                "FROM platform_comments" in sql
                                and "ORDER BY commentedAt DESC" in sql
                            ):
                                return FetchHookCursor(cursor, self)
                            return cursor

                    @contextmanager
                    def racing_connect():
                        conn = sqlite3.connect(
                            path,
                            timeout=0.1,
                            factory=HookConnection,
                        )
                        conn.row_factory = sqlite3.Row
                        conn.execute("PRAGMA foreign_keys = ON")
                        try:
                            with conn:
                                yield conn
                        finally:
                            conn.close()

                    @contextmanager
                    def plain_connect():
                        conn = sqlite3.connect(path, timeout=0.1)
                        conn.row_factory = sqlite3.Row
                        conn.execute("PRAGMA foreign_keys = ON")
                        try:
                            with conn:
                                yield conn
                        finally:
                            conn.close()

                    receipt = service.record_comment_ai_outcome(
                        12,
                        "work-7",
                        result=outcome,
                        error_code=error_code,
                        model_name=model_name,
                        prompt_version="comment-insight-v1",
                        schema_version=1,
                        input_fingerprint=fingerprint,
                        comment_count=1,
                        connect_factory=racing_connect,
                    )

                    self.assertEqual(
                        (transaction_states, competing_commits),
                        ([True], [False]),
                    )
                    self.assertEqual(receipt["status"], expected_status)
                    self.assertEqual(receipt["errorCode"], expected_error)

                    insert_competing_comment()
                    self.assertEqual(competing_commits, [False, True])
                    with plain_connect() as check:
                        row = check.execute(
                            """
                            SELECT status, errorCode, commentCount
                            FROM comment_insight_runs
                            ORDER BY id DESC LIMIT 1
                            """
                        ).fetchone()
                        count = check.execute(
                            "SELECT COUNT(*) FROM platform_comments"
                        ).fetchone()[0]
                    self.assertEqual(
                        tuple(row),
                        (expected_status, expected_error, 1),
                    )
                    self.assertEqual(count, 2)
                    panel = service.comment_panel_payload(
                        12,
                        "work-7",
                        connect_factory=plain_connect,
                    )
                    self.assertEqual(panel["aiStatus"], "failed")
                    self.assertEqual(
                        panel["aiErrorCode"], "comment_ai_response_invalid"
                    )
                    self.assertEqual(panel["candidates"], [])

    def test_new_comments_record_current_skipped_and_hide_stale_success(self):
        """未配置 AI 也要覆盖旧成功，否则新评论会展示旧分类和旧选题。"""

        self._sync(FixedCollector(valid_batch()), ai_provider_factory=lambda: None)
        insight = InsightResult(
            classifications=(CommentClassification("a" * 64, ("追问",)),),
            candidates=(TopicCandidate("旧选题", "旧评论证据", ("a" * 64,)),),
            known_comment_keys=frozenset({"a" * 64, "b" * 64}),
        )
        service.record_comment_ai_outcome(
            12,
            "work-7",
            result=insight,
            error_code="",
            model_name="test-model",
            prompt_version="comment-insight-v1",
            schema_version=1,
            input_fingerprint=current_fingerprint(self.conn),
            comment_count=2,
            connect_factory=self.connect,
        )
        new_batch = valid_batch(
            records=(record("c" * 64, body="这是一条新评论。"),),
        )

        result = self._sync(
            FixedCollector(new_batch), ai_provider_factory=lambda: None
        )
        panel = service.comment_panel_payload(
            12, "work-7", connect_factory=self.connect
        )

        self.assertEqual(result["aiStatus"], "skipped")
        self.assertEqual(panel["aiStatus"], "skipped")
        self.assertEqual(panel["aiErrorCode"], "comment_ai_not_configured")
        self.assertEqual(panel["candidates"], [])
        latest = self.conn.execute(
            """
            SELECT status, errorCode, commentCount, inputFingerprint
            FROM comment_insight_runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(tuple(latest[:3]), ("skipped", "comment_ai_not_configured", 3))
        self.assertEqual(latest["inputFingerprint"], current_fingerprint(self.conn))

    def test_panel_rejects_latest_insight_after_comment_snapshot_changes(self):
        """没有后续 AI 运行时，面板也不能把旧快照洞察套到新评论上。"""

        self._persist_existing()
        insight = InsightResult(
            classifications=(CommentClassification("a" * 64, ("追问",)),),
            candidates=(TopicCandidate("旧选题", "旧评论证据", ("a" * 64,)),),
            known_comment_keys=frozenset({"a" * 64}),
        )
        service.record_comment_ai_outcome(
            12,
            "work-7",
            result=insight,
            error_code="",
            model_name="test-model",
            prompt_version="comment-insight-v1",
            schema_version=1,
            input_fingerprint=current_fingerprint(self.conn),
            comment_count=1,
            connect_factory=self.connect,
        )
        with self.conn:
            store.persist_comment_batch(
                self.conn,
                12,
                valid_batch(
                    records=(record("b" * 64, body="后来出现的新评论。"),),
                ),
            )

        panel = service.comment_panel_payload(
            12, "work-7", connect_factory=self.connect
        )

        self.assertEqual(panel["aiStatus"], "failed")
        self.assertEqual(panel["aiErrorCode"], "comment_ai_response_invalid")
        self.assertEqual(panel["candidates"], [])
        self.assertTrue(all(item["labels"] == [] for item in panel["comments"]))

    def test_unexpected_ai_failure_is_fixed_and_does_not_leak_secret_text(self):
        """原始 AI 异常不得进入结果、进度或数据库，但评论仍然成功。"""

        secret = "raw-token-and-response-body"
        ai = FixedAi(RuntimeError(secret))
        progress = []

        result = self._sync(
            FixedCollector(valid_batch()),
            ai_provider_factory=lambda: ai,
            report=progress.append,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["aiStatus"], "failed")
        self.assertEqual(result["aiErrorCode"], "comment_ai_service_unavailable")
        serialized = repr((result, progress, list(self.conn.iterdump())))
        self.assertNotIn(secret, serialized)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM platform_comments").fetchone()[0],
            2,
        )

    def test_collector_cannot_promote_ai_error_to_comment_sync_error(self):
        """采集器越界抛出的 AI 错误码必须归一化，不能污染同步运行。"""

        result = self._sync(
            FixedCollector(CommentInsightFailure("comment_ai_timeout"))
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errorCode"], "comment_payload_invalid")
        run = self.conn.execute(
            "SELECT errorCode FROM platform_comment_sync_runs"
        ).fetchone()
        self.assertEqual(run[0], "comment_payload_invalid")

    def test_fixed_cancel_failure_is_recorded_as_cancelled(self):
        """固定取消码必须落 cancelled，不能伪装成普通 failed。"""

        failure = CommentInsightFailure("comment_sync_cancelled")
        failure.cleanup_receipt = CleanupReceipt(
            closed=True, alive_resource_count=0
        )

        result = self._sync(FixedCollector(failure))
        run = self.conn.execute(
            "SELECT status, errorCode FROM platform_comment_sync_runs"
        ).fetchone()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errorCode"], "comment_sync_cancelled")
        self.assertEqual(set(result), PUBLIC_RESULT_KEYS)
        self.assertEqual(tuple(run), ("cancelled", "comment_sync_cancelled"))

    def test_process_control_during_ai_outcome_persistence_is_not_swallowed(self):
        """本地洞察写入期间的进程中断不能被伪装成普通 AI 失败。"""

        for control_type in (KeyboardInterrupt, asyncio.CancelledError):
            with self.subTest(control=control_type.__name__):
                calls = 0

                @contextmanager
                def interrupting_connect():
                    nonlocal calls
                    calls += 1
                    if calls == 4:
                        raise control_type()
                    with self.conn:
                        yield self.conn

                with self.assertRaises(control_type):
                    service.sync_comments(
                        12,
                        "work-7",
                        collector_factory=lambda: FixedCollector(valid_batch()),
                        ai_provider_factory=lambda: (_ for _ in ()).throw(
                            CommentInsightFailure("comment_ai_timeout")
                        ),
                        connect_factory=interrupting_connect,
                    )

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM platform_comments").fetchone()[0],
            2,
        )

    def test_invalid_ai_metadata_records_fixed_failed_outcome(self):
        """损坏的 AI 元数据也要留下固定失败行，不能只在内存中消失。"""

        ai = FixedAi(CommentInsightFailure("comment_ai_timeout"))
        ai.model_name = ""

        result = self._sync(
            FixedCollector(valid_batch()), ai_provider_factory=lambda: ai
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["aiStatus"], "failed")
        self.assertEqual(result["aiErrorCode"], "comment_ai_response_invalid")
        row = self.conn.execute(
            "SELECT status, errorCode, modelName FROM comment_insight_runs"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("failed", "comment_ai_response_invalid", "unavailable"),
        )

    def test_progress_exposes_only_fixed_stage_and_message_pairs(self):
        """向 UI 透传采集器报告会泄漏计数、正文或平台内部字段。"""

        progress = []

        def try_leaking(_account, _content_id, _known, _limit, report):
            if report is not None:
                report({"body": "私密评论", "contentId": "work-7"})

        result = self._sync(
            FixedCollector(valid_batch(), on_collect=try_leaking),
            report=progress.append,
            ai_provider_factory=lambda: None,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            progress,
            [
                {"stage": "preparing", "message": "正在准备只读会话"},
                {"stage": "collecting", "message": "正在读取评论"},
                {"stage": "validating", "message": "正在校验评论"},
                {"stage": "persisting", "message": "正在保存评论"},
                {"stage": "insight", "message": "正在生成洞察"},
                {"stage": "completed", "message": "评论同步完成"},
            ],
        )
        self.assertTrue(all(set(event) == {"stage", "message"} for event in progress))
        self.assertNotIn("work-7", repr(progress))
        self.assertNotIn("私密评论", repr(progress))

    def test_incomplete_sub_limit_batch_is_failed_and_never_persisted(self):
        """Task 5 的空停止原因不能被服务误记成完整成功。"""

        incomplete = valid_batch(
            records=(record(),),
            stop_reason="",
            warning_code="",
        )

        result = self._sync(FixedCollector(incomplete))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errorCode"], "comment_payload_invalid")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM platform_comments").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM platform_comment_sync_runs WHERE status = 'failed'"
            ).fetchone()[0],
            1,
        )

    def test_hundred_comments_without_terminal_evidence_is_not_success(self):
        """达到数量上限不等于平台已结束；空终止证据不能被记作成功。"""

        hundred = tuple(
            record(f"{index:064x}", body=f"评论 {index}")
            for index in range(1, 101)
        )
        incomplete = valid_batch(
            records=hundred,
            stop_reason="",
            warning_code="",
        )

        result = self._sync(FixedCollector(incomplete))
        run = self.conn.execute(
            "SELECT status, errorCode FROM platform_comment_sync_runs"
        ).fetchone()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errorCode"], "comment_payload_invalid")
        self.assertEqual(tuple(run), ("failed", "comment_payload_invalid"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM platform_comments").fetchone()[0],
            0,
        )

    def test_hundred_comments_require_a_verified_terminal_reason(self):
        """100 条只有平台结束、命中旧评论或明确上限三种证据可成功。"""

        hundred = tuple(
            record(f"{index:064x}", body=f"评论 {index}")
            for index in range(1, 101)
        )
        cases = (
            ("platform_end", ""),
            ("known_comment", ""),
            ("limit_reached", "comment_limit_reached"),
        )

        for stop_reason, warning_code in cases:
            with self.subTest(stop_reason=stop_reason):
                result = self._sync(
                    FixedCollector(
                        valid_batch(
                            records=hundred,
                            stop_reason=stop_reason,
                            warning_code=warning_code,
                        )
                    )
                )
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["stopReason"], stop_reason)
                self.assertEqual(result["warningCode"], warning_code)

    def test_public_sync_result_has_only_fixed_counts_codes_and_cleanup(self):
        """同步结果不能夹带账号、作品、评论正文、运行 ID 或异常对象。"""

        result = self._sync(FixedCollector(valid_batch()))

        self.assertEqual(set(result), PUBLIC_RESULT_KEYS)
        self.assertEqual(result["acceptedCount"], 2)
        self.assertEqual(result["insertedCount"], 2)
        forbidden = (
            "accountId",
            "contentId",
            "commentKey",
            "runId",
            "为什么会这样",
        )
        self.assertTrue(all(value not in repr(result) for value in forbidden))

    def test_panel_validates_label_and_limit_and_projects_no_internal_ids(self):
        """标签或页数校验放松、内部键外露时，此测试应失败。"""

        self._sync(FixedCollector(valid_batch()))
        insight = InsightResult(
            classifications=(
                CommentClassification("a" * 64, ("追问",)),
                CommentClassification("b" * 64, ("真实经历", "认同")),
            ),
            candidates=(
                TopicCandidate("回答追问", "评论提出了问题", ("a" * 64,)),
            ),
            known_comment_keys=frozenset({"a" * 64, "b" * 64}),
        )
        receipt = service.record_comment_ai_outcome(
            12,
            "work-7",
            result=insight,
            error_code="",
            model_name="test-model",
            prompt_version="comment-insight-v1",
            schema_version=1,
            input_fingerprint=current_fingerprint(self.conn),
            comment_count=2,
            connect_factory=self.connect,
        )

        panel = service.comment_panel_payload(
            12,
            "work-7",
            label="追问",
            limit=1,
            connect_factory=self.connect,
        )

        self.assertEqual(receipt, {"status": "success", "errorCode": ""})
        self.assertEqual(panel["title"], "作品七")
        self.assertEqual(len(panel["comments"]), 1)
        self.assertEqual(panel["comments"][0]["body"], "为什么会这样？")
        self.assertEqual(panel["comments"][0]["labels"], ["追问"])
        self.assertEqual(panel["candidates"][0]["evidence"][0]["body"], "为什么会这样？")
        serialized = repr(panel)
        for forbidden in (
            "accountId",
            "contentId",
            "commentKey",
            "lastSyncRunId",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ):
            self.assertNotIn(forbidden, serialized)

        for bad_label in ("", "追问 ", "未知", None, ["追问"]):
            with self.subTest(label=bad_label):
                with self.assertRaises(CommentInsightFailure) as raised:
                    service.comment_panel_payload(
                        12,
                        "work-7",
                        label=bad_label,
                        connect_factory=self.connect,
                    )
                self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        for bad_limit in (0, 101, True, 1.5, "1"):
            with self.subTest(limit=bad_limit):
                with self.assertRaises(CommentInsightFailure) as raised:
                    service.comment_panel_payload(
                        12,
                        "work-7",
                        limit=bad_limit,
                        connect_factory=self.connect,
                    )
                self.assertEqual(raised.exception.error_code, "comment_payload_invalid")

    def test_malformed_stored_insight_is_projected_as_fixed_failure(self):
        """损坏 JSON 中的复杂标签不得把 TypeError 或原始载荷抛到界面。"""

        self._sync(FixedCollector(valid_batch()))
        raw_marker = "private-malformed-label"
        malformed = json.dumps(
            [
                {
                    "commentKey": "a" * 64,
                    "labels": [{"raw": raw_marker}],
                }
            ],
            ensure_ascii=False,
        )
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO comment_insight_runs
                    (accountId, platformType, contentId, status, errorCode,
                     providerType, modelName, promptVersion, schemaVersion,
                     inputFingerprint, commentCount, classificationsJson,
                     candidatesJson, startedAt, finishedAt)
                VALUES (12, 3, 'work-7', 'success', '', 'openai_compatible',
                        'test-model', 'comment-insight-v1', 1, ?, 2, ?, '[]', ?, ?)
                """,
                (current_fingerprint(self.conn), malformed, OBSERVED, OBSERVED),
            )

        panel = service.comment_panel_payload(
            12, "work-7", connect_factory=self.connect
        )

        self.assertEqual(panel["aiStatus"], "failed")
        self.assertEqual(panel["aiErrorCode"], "comment_ai_response_invalid")
        self.assertNotIn(raw_marker, repr(panel))

    def test_deep_stored_json_is_projected_as_fixed_failure(self):
        """小于大小上限的超深 JSON 也不能把 RecursionError 抛到界面。"""

        self._sync(FixedCollector(valid_batch()))
        deeply_nested = "[" * 10_000 + "0" + "]" * 10_000
        self.assertLess(len(deeply_nested.encode("utf-8")), 1_000_000)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO comment_insight_runs
                    (accountId, platformType, contentId, status, errorCode,
                     providerType, modelName, promptVersion, schemaVersion,
                     inputFingerprint, commentCount, classificationsJson,
                     candidatesJson, startedAt, finishedAt)
                VALUES (12, 3, 'work-7', 'success', '', 'openai_compatible',
                        'test-model', 'comment-insight-v1', 1, ?, 2, ?, '[]', ?, ?)
                """,
                (
                    current_fingerprint(self.conn),
                    deeply_nested,
                    OBSERVED,
                    OBSERVED,
                ),
            )

        panel = service.comment_panel_payload(
            12, "work-7", connect_factory=self.connect
        )

        self.assertEqual(panel["aiStatus"], "failed")
        self.assertEqual(panel["aiErrorCode"], "comment_ai_response_invalid")

    def test_record_and_panel_map_database_errors_to_fixed_failure(self):
        """数据库缺表、锁定或损坏不得把原始 OperationalError 透给调用方。"""

        marker = "private-database-path-token"

        @contextmanager
        def broken_connect():
            raise sqlite3.OperationalError(marker)
            yield

        calls = (
            lambda: service.record_comment_ai_outcome(
                12,
                "work-7",
                result=None,
                error_code="comment_ai_not_configured",
                model_name="unconfigured",
                prompt_version="comment-insight-v1",
                schema_version=1,
                input_fingerprint="f" * 64,
                comment_count=0,
                connect_factory=broken_connect,
            ),
            lambda: service.comment_panel_payload(
                12, "work-7", connect_factory=broken_connect
            ),
        )

        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(CommentInsightFailure) as raised:
                    call()
                self.assertEqual(
                    raised.exception.error_code, "comment_payload_invalid"
                )
                self.assertNotIn(marker, repr(raised.exception))

    def test_record_and_panel_preserve_process_control(self):
        """固定数据库边界不能吞掉取消、键盘中断或进程退出。"""

        for control_type in (
            asyncio.CancelledError,
            KeyboardInterrupt,
            SystemExit,
        ):
            @contextmanager
            def interrupted_connect():
                raise control_type()
                yield

            calls = (
                lambda: service.record_comment_ai_outcome(
                    12,
                    "work-7",
                    result=None,
                    error_code="comment_ai_not_configured",
                    model_name="unconfigured",
                    prompt_version="comment-insight-v1",
                    schema_version=1,
                    input_fingerprint="f" * 64,
                    comment_count=0,
                    connect_factory=interrupted_connect,
                ),
                lambda: service.comment_panel_payload(
                    12, "work-7", connect_factory=interrupted_connect
                ),
            )
            for call in calls:
                with self.subTest(control=control_type.__name__, call=call):
                    with self.assertRaises(control_type):
                        call()

    def test_record_comment_ai_outcome_rejects_uncontrolled_values(self):
        """洞察记录入口不得保存原始异常码、错误类型或半成功结果。"""

        cases = (
            {"result": None, "error_code": "raw-provider-error"},
            {"result": object(), "error_code": ""},
            {"result": None, "error_code": ""},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(CommentInsightFailure) as raised:
                    service.record_comment_ai_outcome(
                        12,
                        "work-7",
                        model_name="test-model",
                        prompt_version="comment-insight-v1",
                        schema_version=1,
                        input_fingerprint="f" * 64,
                        comment_count=2,
                        connect_factory=self.connect,
                        **values,
                    )
                self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM comment_insight_runs").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
