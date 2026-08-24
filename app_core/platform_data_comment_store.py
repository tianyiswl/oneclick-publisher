# -*- coding: utf-8 -*-
"""抖音匿名评论的本地表结构和事务内读写。"""

from __future__ import annotations

from datetime import datetime
import json
import re
import sqlite3

from .platform_data_comment_models import (
    ALLOWED_COMMENT_ERROR_CODES,
    CommentCollectionBatch,
    CommentInsightFailure,
    InsightResult,
)


_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _account_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise CommentInsightFailure("comment_payload_invalid")
    return value


def _content_id(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CommentInsightFailure("comment_payload_invalid")
    return value


def _source_mode(value: object) -> str:
    if type(value) is not str or value not in {"direct_session", "browser_signed"}:
        raise CommentInsightFailure("comment_payload_invalid")
    return value


def _error_code(value: object) -> str:
    if type(value) is not str or value not in ALLOWED_COMMENT_ERROR_CODES:
        raise CommentInsightFailure("comment_payload_invalid")
    return value


def _row_dict(cursor: sqlite3.Cursor, row: object) -> dict:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((column[0] for column in cursor.description or ()), row, strict=True))


def _owned_content(
    conn: sqlite3.Connection, account_id: int, content_id: str
) -> dict | None:
    cursor = conn.execute(
        """
        SELECT contents.contentId, contents.title
        FROM platform_contents AS contents
        JOIN user_info AS accounts ON accounts.id = contents.accountId
        WHERE contents.accountId = ?
          AND accounts.type = 3
          AND contents.platformType = 3
          AND contents.contentId = ?
        LIMIT 1
        """,
        (account_id, content_id),
    )
    row = cursor.fetchone()
    return None if row is None else _row_dict(cursor, row)


def _require_owned_content(
    conn: sqlite3.Connection, account_id: int, content_id: str
) -> None:
    account = conn.execute(
        "SELECT type FROM user_info WHERE id = ? LIMIT 1", (account_id,)
    ).fetchone()
    account_type = account[0] if account is not None else None
    if type(account_type) is not int or account_type != 3:
        raise CommentInsightFailure("comment_content_unavailable")
    if _owned_content(conn, account_id, content_id) is None:
        raise CommentInsightFailure("comment_content_unavailable")


def create_comment_schema(conn: sqlite3.Connection) -> None:
    """创建评论专用表和读取索引；调用方负责包住迁移事务。"""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_comment_sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            accountId INTEGER NOT NULL,
            platformType INTEGER NOT NULL CHECK(platformType = 3),
            contentId TEXT NOT NULL,
            sourceMode TEXT NOT NULL CHECK(sourceMode IN ('direct_session', 'browser_signed')),
            status TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed', 'cancelled')),
            errorCode TEXT NOT NULL DEFAULT '',
            acceptedCount INTEGER NOT NULL DEFAULT 0 CHECK(acceptedCount >= 0),
            insertedCount INTEGER NOT NULL DEFAULT 0 CHECK(insertedCount >= 0),
            updatedCount INTEGER NOT NULL DEFAULT 0 CHECK(updatedCount >= 0),
            rejectedCount INTEGER NOT NULL DEFAULT 0 CHECK(rejectedCount >= 0),
            pageCount INTEGER NOT NULL DEFAULT 0 CHECK(pageCount >= 0),
            stopReason TEXT NOT NULL DEFAULT '' CHECK(stopReason IN ('', 'known_comment', 'limit_reached', 'platform_end')),
            startedAt TEXT NOT NULL,
            finishedAt TEXT,
            FOREIGN KEY(accountId) REFERENCES user_info(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            accountId INTEGER NOT NULL,
            platformType INTEGER NOT NULL CHECK(platformType = 3),
            contentId TEXT NOT NULL,
            commentKey TEXT NOT NULL,
            body TEXT NOT NULL,
            likeCount INTEGER NOT NULL CHECK(likeCount >= 0),
            replyCount INTEGER NOT NULL CHECK(replyCount >= 0),
            commentedAt TEXT NOT NULL,
            firstSeenAt TEXT NOT NULL,
            lastSeenAt TEXT NOT NULL,
            lastSyncRunId INTEGER,
            UNIQUE(accountId, platformType, contentId, commentKey),
            FOREIGN KEY(accountId) REFERENCES user_info(id) ON DELETE CASCADE,
            FOREIGN KEY(lastSyncRunId) REFERENCES platform_comment_sync_runs(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comment_insight_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            accountId INTEGER NOT NULL,
            platformType INTEGER NOT NULL CHECK(platformType = 3),
            contentId TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed', 'skipped')),
            errorCode TEXT NOT NULL DEFAULT '',
            providerType TEXT NOT NULL CHECK(providerType = 'openai_compatible'),
            modelName TEXT NOT NULL,
            promptVersion TEXT NOT NULL,
            schemaVersion INTEGER NOT NULL,
            inputFingerprint TEXT NOT NULL,
            commentCount INTEGER NOT NULL CHECK(commentCount >= 0),
            classificationsJson TEXT NOT NULL,
            candidatesJson TEXT NOT NULL,
            startedAt TEXT NOT NULL,
            finishedAt TEXT,
            FOREIGN KEY(accountId) REFERENCES user_info(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_platform_comment_sync_runs_account_content
        ON platform_comment_sync_runs(accountId, contentId, id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_platform_comments_latest_page
        ON platform_comments(accountId, contentId, commentedAt DESC, id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_comment_insight_runs_latest
        ON comment_insight_runs(accountId, contentId, id DESC)
        """
    )


def content_for_comment_sync(
    conn: sqlite3.Connection, account_id: int, content_id: str
) -> dict | None:
    """返回当前抖音账号拥有的稳定作品定位符及展示标题。"""

    account_id = _account_id(account_id)
    content_id = _content_id(content_id)
    return _owned_content(conn, account_id, content_id)


def known_comment_keys(
    conn: sqlite3.Connection, account_id: int, content_id: str
) -> frozenset[str]:
    """返回当前账号、作品下已经保存的匿名键。"""

    account_id = _account_id(account_id)
    content_id = _content_id(content_id)
    return frozenset(
        row[0]
        for row in conn.execute(
            """
            SELECT commentKey FROM platform_comments
            WHERE accountId = ? AND platformType = 3 AND contentId = ?
            """,
            (account_id, content_id),
        )
    )


def _create_running_run(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    content_id: str,
    source_mode: str,
    started_at: str,
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO platform_comment_sync_runs
                (accountId, platformType, contentId, sourceMode, status, startedAt)
            VALUES (?, 3, ?, ?, 'running', ?)
            """,
            (account_id, content_id, source_mode, started_at),
        ).lastrowid
    )


def _finalize_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    error_code: str,
    accepted_count: int,
    inserted_count: int,
    updated_count: int,
    rejected_count: int,
    page_count: int,
    stop_reason: str,
    finished_at: str,
) -> None:
    cursor = conn.execute(
        """
        UPDATE platform_comment_sync_runs
        SET status = ?, errorCode = ?, acceptedCount = ?, insertedCount = ?,
            updatedCount = ?, rejectedCount = ?, pageCount = ?, stopReason = ?,
            finishedAt = ?
        WHERE id = ? AND status = 'running'
        """,
        (
            status,
            error_code,
            accepted_count,
            inserted_count,
            updated_count,
            rejected_count,
            page_count,
            stop_reason,
            finished_at,
            run_id,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("comment sync run finalization failed")


def _upsert_comment(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    account_id: int,
    record,
) -> bool:
    exists = conn.execute(
        """
        SELECT 1 FROM platform_comments
        WHERE accountId = ? AND platformType = 3 AND contentId = ? AND commentKey = ?
        """,
        (account_id, record.content_id, record.comment_key),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO platform_comments
            (accountId, platformType, contentId, commentKey, body, likeCount,
             replyCount, commentedAt, firstSeenAt, lastSeenAt, lastSyncRunId)
        VALUES (?, 3, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accountId, platformType, contentId, commentKey) DO UPDATE SET
            body = excluded.body,
            likeCount = excluded.likeCount,
            replyCount = excluded.replyCount,
            commentedAt = excluded.commentedAt,
            lastSeenAt = excluded.lastSeenAt,
            lastSyncRunId = excluded.lastSyncRunId
        """,
        (
            account_id,
            record.content_id,
            record.comment_key,
            record.body,
            record.like_count,
            record.reply_count,
            record.commented_at,
            record.observed_at,
            record.observed_at,
            run_id,
        ),
    )
    return exists is None


def persist_comment_batch(
    conn: sqlite3.Connection, account_id: int, batch: CommentCollectionBatch
) -> dict:
    """在调用方事务中把一批评论和成功运行一起落库，绝不自行提交。"""

    account_id = _account_id(account_id)
    if type(batch) is not CommentCollectionBatch:
        raise CommentInsightFailure("comment_payload_invalid")
    _require_owned_content(conn, account_id, batch.content_id)
    run_id = _create_running_run(
        conn,
        account_id=account_id,
        content_id=batch.content_id,
        source_mode=batch.source_mode,
        started_at=batch.platform_observed_at,
    )
    inserted_count = 0
    updated_count = 0
    for record in batch.comments:
        if _upsert_comment(
            conn, run_id=run_id, account_id=account_id, record=record
        ):
            inserted_count += 1
        else:
            updated_count += 1
    _finalize_run(
        conn,
        run_id=run_id,
        status="success",
        error_code="",
        accepted_count=batch.accepted_count,
        inserted_count=inserted_count,
        updated_count=updated_count,
        rejected_count=batch.rejected_count,
        page_count=batch.page_count,
        stop_reason=batch.stop_reason,
        finished_at=batch.platform_observed_at,
    )
    return {
        "id": run_id,
        "status": "success",
        "errorCode": "",
        "acceptedCount": batch.accepted_count,
        "insertedCount": inserted_count,
        "updatedCount": updated_count,
        "rejectedCount": batch.rejected_count,
        "pageCount": batch.page_count,
        "stopReason": batch.stop_reason,
    }


def record_failed_comment_sync(
    conn: sqlite3.Connection,
    account_id: int,
    content_id: str,
    source_mode: str,
    error_code: str,
    *,
    cancelled: bool = False,
) -> dict:
    """记录没有写入评论的失败或取消运行，保留已保存历史。"""

    account_id = _account_id(account_id)
    content_id = _content_id(content_id)
    source_mode = _source_mode(source_mode)
    error_code = _error_code(error_code)
    if type(cancelled) is not bool:
        raise CommentInsightFailure("comment_payload_invalid")
    _require_owned_content(conn, account_id, content_id)
    timestamp = _now()
    run_id = _create_running_run(
        conn,
        account_id=account_id,
        content_id=content_id,
        source_mode=source_mode,
        started_at=timestamp,
    )
    status = "cancelled" if cancelled else "failed"
    _finalize_run(
        conn,
        run_id=run_id,
        status=status,
        error_code=error_code,
        accepted_count=0,
        inserted_count=0,
        updated_count=0,
        rejected_count=0,
        page_count=0,
        stop_reason="",
        finished_at=timestamp,
    )
    return {"id": run_id, "status": status, "errorCode": error_code}


def list_comment_rows(
    conn: sqlite3.Connection,
    account_id: int,
    content_id: str,
    *,
    limit: int = 100,
) -> list[dict]:
    """读取最新评论页，只返回匿名评论字段。"""

    account_id = _account_id(account_id)
    content_id = _content_id(content_id)
    if type(limit) is not int or limit <= 0 or limit > 100:
        raise CommentInsightFailure("comment_payload_invalid")
    cursor = conn.execute(
        """
        SELECT id, contentId, commentKey, body, likeCount, replyCount, commentedAt,
               firstSeenAt, lastSeenAt, lastSyncRunId
        FROM platform_comments
        WHERE accountId = ? AND platformType = 3 AND contentId = ?
        ORDER BY commentedAt DESC, id DESC
        LIMIT ?
        """,
        (account_id, content_id, limit),
    )
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _insight_json(result: InsightResult | None) -> tuple[str, str]:
    if result is None:
        return "[]", "[]"
    classifications = [
        {"commentKey": item.comment_key, "labels": list(item.labels)}
        for item in result.classifications
    ]
    candidates = [
        {
            "title": item.title,
            "reason": item.reason,
            "evidenceKeys": list(item.evidence_keys),
        }
        for item in result.candidates
    ]
    return (
        json.dumps(classifications, ensure_ascii=False, separators=(",", ":")),
        json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
    )


def persist_insight_result(
    conn: sqlite3.Connection,
    account_id: int,
    content_id: str,
    *,
    provider_type: str,
    model_name: str,
    prompt_version: str,
    schema_version: int,
    input_fingerprint: str,
    comment_count: int,
    result: InsightResult | None,
    error_code: str = "",
) -> dict:
    """保存已验证的洞察结果或固定失败码，不触碰评论表。"""

    account_id = _account_id(account_id)
    content_id = _content_id(content_id)
    _require_owned_content(conn, account_id, content_id)
    if provider_type != "openai_compatible":
        raise CommentInsightFailure("comment_payload_invalid")
    if type(model_name) is not str or not model_name or model_name != model_name.strip():
        raise CommentInsightFailure("comment_payload_invalid")
    if type(prompt_version) is not str or not prompt_version or prompt_version != prompt_version.strip():
        raise CommentInsightFailure("comment_payload_invalid")
    if type(schema_version) is not int or schema_version <= 0:
        raise CommentInsightFailure("comment_payload_invalid")
    if type(input_fingerprint) is not str or _FINGERPRINT_RE.fullmatch(input_fingerprint) is None:
        raise CommentInsightFailure("comment_payload_invalid")
    if type(comment_count) is not int or comment_count < 0:
        raise CommentInsightFailure("comment_payload_invalid")
    if result is not None and type(result) is not InsightResult:
        raise CommentInsightFailure("comment_payload_invalid")
    if type(error_code) is not str:
        raise CommentInsightFailure("comment_payload_invalid")
    if result is None:
        error_code = _error_code(error_code)
        status = "failed"
    elif error_code:
        raise CommentInsightFailure("comment_payload_invalid")
    else:
        status = "success"
    classifications_json, candidates_json = _insight_json(result)
    timestamp = _now()
    run_id = int(
        conn.execute(
            """
            INSERT INTO comment_insight_runs
                (accountId, platformType, contentId, status, errorCode, providerType,
                 modelName, promptVersion, schemaVersion, inputFingerprint,
                 commentCount, classificationsJson, candidatesJson, startedAt, finishedAt)
            VALUES (?, 3, ?, ?, ?, 'openai_compatible', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                content_id,
                status,
                error_code,
                model_name,
                prompt_version,
                schema_version,
                input_fingerprint,
                comment_count,
                classifications_json,
                candidates_json,
                timestamp,
                timestamp,
            ),
        ).lastrowid
    )
    return {"id": run_id, "status": status, "errorCode": error_code}


def latest_insight(
    conn: sqlite3.Connection, account_id: int, content_id: str
) -> dict | None:
    """读取当前账号作品最近一次洞察运行。"""

    account_id = _account_id(account_id)
    content_id = _content_id(content_id)
    cursor = conn.execute(
        """
        SELECT id, contentId, status, errorCode, providerType, modelName,
               promptVersion, schemaVersion, inputFingerprint, commentCount,
               classificationsJson, candidatesJson, startedAt, finishedAt
        FROM comment_insight_runs
        WHERE accountId = ? AND platformType = 3 AND contentId = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (account_id, content_id),
    )
    row = cursor.fetchone()
    return None if row is None else _row_dict(cursor, row)
