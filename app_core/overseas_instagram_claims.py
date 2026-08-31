# -*- coding: utf-8 -*-
"""Instagram 正式发布的一次性 Claim 与禁止重放状态机。"""

from __future__ import annotations

from datetime import datetime
import re
import sqlite3
from typing import Mapping
from urllib.parse import urlsplit

from .overseas_instagram_errors import InstagramPublishError
from .overseas_instagram_publish import InstagramPublishIntent


_SAFE_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ERROR_CODE = re.compile(r"instagram_[a-z0-9_]{1,80}\Z")
_SAFE_MEDIA_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def ensure_instagram_claim_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS instagram_controlled_publish_claims (
            taskId INTEGER PRIMARY KEY,
            preflightTaskId INTEGER NOT NULL,
            accountId INTEGER NOT NULL,
            instagramUserId TEXT NOT NULL,
            intentFingerprint TEXT NOT NULL,
            formSnapshotHash TEXT NOT NULL,
            baselineHash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'reserved',
                'final_action_claimed',
                'final_action_clicked',
                'platform_accepted',
                'ambiguous',
                'succeeded',
                'safe_failed',
                'confirmed_not_published'
            )),
            blocksReplay INTEGER NOT NULL DEFAULT 1
                CHECK(blocksReplay IN (0, 1)),
            finalActionTriggered INTEGER NOT NULL DEFAULT 0
                CHECK(finalActionTriggered IN (0, 1)),
            mediaId TEXT,
            url TEXT,
            publishedAt TEXT,
            scheduledAt TEXT,
            errorCode TEXT NOT NULL DEFAULT '',
            platformErrorText TEXT NOT NULL DEFAULT '',
            createdAt TEXT NOT NULL,
            claimedAt TEXT,
            clickedAt TEXT,
            observedAt TEXT,
            updatedAt TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_instagram_active_replay_claim
        ON instagram_controlled_publish_claims(
            instagramUserId, intentFingerprint
        )
        WHERE blocksReplay = 1
        """
    )


def _valid_time(value: object) -> str:
    if type(value) is not str:
        raise InstagramPublishError(
            "instagram_claim_invalid", "Instagram Claim 时间无效。"
        )
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InstagramPublishError(
            "instagram_claim_invalid", "Instagram Claim 时间无效。"
        ) from exc
    return value


def _valid_hash(value: object, *, error_code: str) -> str:
    if type(value) is not str or _SAFE_HASH.fullmatch(value) is None:
        raise InstagramPublishError(error_code, "Instagram Claim 哈希无效。")
    return value


def _row_dict(cursor: sqlite3.Cursor, row: object) -> dict[str, object] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = [str(item[0]) for item in cursor.description or ()]
    return dict(zip(columns, row))


def reserve_instagram_claim(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    preflight_task_id: int,
    intent: InstagramPublishIntent,
    preflight_receipt: Mapping[str, object],
    created_at: str,
) -> None:
    if (
        type(task_id) is not int
        or task_id <= 0
        or type(preflight_task_id) is not int
        or preflight_task_id <= 0
        or not isinstance(intent, InstagramPublishIntent)
        or not isinstance(preflight_receipt, Mapping)
    ):
        raise InstagramPublishError(
            "instagram_publish_authorization_invalid",
            "Instagram 正式任务缺少有效预检证据。",
        )
    created_at = _valid_time(created_at)
    for value in (
        intent.video_sha256,
        intent.cover_sha256,
        intent.caption_sha256,
        intent.intent_fingerprint,
    ):
        _valid_hash(value, error_code="instagram_publish_authorization_invalid")
    expected = {
        "phase": "platform_form_verified",
        "accountId": intent.account_id,
        "instagramUserId": intent.instagram_user_id,
        "videoSha256": intent.video_sha256,
        "coverSha256": intent.cover_sha256,
        "captionSha256": intent.caption_sha256,
        "finalActionTriggered": False,
    }
    if any(
        type(preflight_receipt.get(key)) is not type(value)
        or preflight_receipt.get(key) != value
        for key, value in expected.items()
    ):
        raise InstagramPublishError(
            "instagram_preflight_stale",
            "Instagram 预检证据与正式发布意图不一致。",
        )
    form_snapshot_hash = _valid_hash(
        preflight_receipt.get("formSnapshotHash"),
        error_code="instagram_preflight_stale",
    )
    baseline_hash = _valid_hash(
        preflight_receipt.get("baselineHash"),
        error_code="instagram_preflight_stale",
    )
    ensure_instagram_claim_schema(conn)
    try:
        conn.execute(
            """
            INSERT INTO instagram_controlled_publish_claims (
                taskId, preflightTaskId, accountId, instagramUserId,
                intentFingerprint, formSnapshotHash, baselineHash,
                state, blocksReplay, finalActionTriggered,
                createdAt, updatedAt
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', 1, 0, ?, ?)
            """,
            (
                task_id,
                preflight_task_id,
                intent.account_id,
                intent.instagram_user_id,
                intent.intent_fingerprint,
                form_snapshot_hash,
                baseline_hash,
                created_at,
                created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise InstagramPublishError(
            "instagram_duplicate_blocked",
            "同一 Instagram 发布意图已有任务或结果不明记录，禁止重放。",
        ) from exc


def claim_instagram_final_action(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    form_snapshot_hash: str,
    claimed_at: str,
) -> None:
    form_snapshot_hash = _valid_hash(
        form_snapshot_hash,
        error_code="instagram_preflight_stale",
    )
    claimed_at = _valid_time(claimed_at)
    ensure_instagram_claim_schema(conn)
    changed = conn.execute(
        """
        UPDATE instagram_controlled_publish_claims
        SET state = 'final_action_claimed', claimedAt = ?, updatedAt = ?
        WHERE taskId = ?
          AND state = 'reserved'
          AND finalActionTriggered = 0
          AND formSnapshotHash = ?
        """,
        (claimed_at, claimed_at, task_id, form_snapshot_hash),
    ).rowcount
    if changed != 1:
        raise InstagramPublishError(
            "instagram_preflight_stale",
            "Instagram 表单快照已变化，最终动作未执行。",
        )


def record_instagram_final_action_clicked(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    clicked_at: str,
) -> None:
    clicked_at = _valid_time(clicked_at)
    ensure_instagram_claim_schema(conn)
    changed = conn.execute(
        """
        UPDATE instagram_controlled_publish_claims
        SET state = 'final_action_clicked', finalActionTriggered = 1,
            clickedAt = ?, updatedAt = ?
        WHERE taskId = ?
          AND state = 'final_action_claimed'
          AND finalActionTriggered = 0
        """,
        (clicked_at, clicked_at, task_id),
    ).rowcount
    if changed == 1:
        return
    current = get_instagram_claim(conn, task_id)
    if int(current.get("finalActionTriggered") or 0) == 1:
        raise InstagramPublishError(
            "instagram_final_action_already_triggered",
            "Instagram 最终按钮已经触发，禁止再次点击。",
            outcome_ambiguous=str(current.get("state") or "") == "ambiguous",
        )
    raise InstagramPublishError(
        "instagram_claim_invalid",
        "Instagram Claim 尚未进入可点击状态。",
    )


def mark_instagram_outcome_unknown(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    error_code: str,
    platform_error_text: str,
    observed_at: str,
) -> None:
    observed_at = _valid_time(observed_at)
    if type(error_code) is not str or _SAFE_ERROR_CODE.fullmatch(error_code) is None:
        raise InstagramPublishError(
            "instagram_claim_invalid", "Instagram 结果错误码无效。"
        )
    if type(platform_error_text) is not str:
        raise InstagramPublishError(
            "instagram_claim_invalid", "Instagram 平台错误原文无效。"
        )
    public_error_text = " ".join(platform_error_text.split())
    if not public_error_text or len(public_error_text) > 512 or any(
        marker in public_error_text.casefold()
        for marker in ("cookie=", "access_token", "authorization:")
    ):
        raise InstagramPublishError(
            "instagram_claim_invalid", "Instagram 平台错误原文无效。"
        )
    ensure_instagram_claim_schema(conn)
    changed = conn.execute(
        """
        UPDATE instagram_controlled_publish_claims
        SET state = 'ambiguous', blocksReplay = 1,
            errorCode = ?, platformErrorText = ?,
            observedAt = ?, updatedAt = ?
        WHERE taskId = ?
          AND state IN ('final_action_clicked', 'platform_accepted')
          AND finalActionTriggered = 1
        """,
        (
            error_code,
            public_error_text,
            observed_at,
            observed_at,
            task_id,
        ),
    ).rowcount
    if changed != 1:
        raise InstagramPublishError(
            "instagram_claim_invalid",
            "Instagram 当前状态不能标记为结果不明。",
        )


def mark_instagram_readback_success(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    receipt: Mapping[str, object],
    observed_at: str,
) -> None:
    observed_at = _valid_time(observed_at)
    if not isinstance(receipt, Mapping):
        raise InstagramPublishError(
            "instagram_readback_not_unique", "Instagram 成功回读结构无效。"
        )
    phase = receipt.get("phase")
    media_id = receipt.get("mediaId")
    url = receipt.get("url")
    published_at = receipt.get("publishedAt")
    scheduled_at = receipt.get("scheduledAt")
    if (
        phase not in {
            "published_readback_confirmed",
            "scheduled_readback_confirmed",
        }
        or type(media_id) is not str
        or _SAFE_MEDIA_ID.fullmatch(media_id) is None
        or receipt.get("finalActionTriggered") is not True
        or receipt.get("blocksReplay") is not True
    ):
        raise InstagramPublishError(
            "instagram_readback_not_unique", "Instagram 成功回读证据不完整。"
        )
    if url is not None:
        if type(url) is not str:
            raise InstagramPublishError(
                "instagram_readback_not_unique", "Instagram 内容链接无效。"
            )
        parsed_url = urlsplit(url)
        host = (parsed_url.hostname or "").casefold().rstrip(".")
        if (
            parsed_url.scheme != "https"
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
            or host not in {"instagram.com", "www.instagram.com"}
            or not parsed_url.path.startswith(("/reel/", "/p/"))
        ):
            raise InstagramPublishError(
                "instagram_readback_not_unique", "Instagram 内容链接无效。"
            )
    if phase == "published_readback_confirmed":
        if url is None or type(published_at) is not str or scheduled_at is not None:
            raise InstagramPublishError(
                "instagram_readback_not_unique", "Instagram 发布时间回读无效。"
            )
        _valid_time(published_at)
    else:
        if published_at is not None or type(scheduled_at) is not str:
            raise InstagramPublishError(
                "instagram_readback_not_unique", "Instagram 定时时间回读无效。"
            )
        _valid_time(scheduled_at)
    ensure_instagram_claim_schema(conn)
    current = get_instagram_claim(conn, task_id)
    if receipt.get("instagramUserId") != current.get("instagramUserId"):
        raise InstagramPublishError(
            "instagram_identity_mismatch",
            "Instagram 成功回读主体与 Claim 不一致。",
        )
    changed = conn.execute(
        """
        UPDATE instagram_controlled_publish_claims
        SET state = 'succeeded', blocksReplay = 1,
            mediaId = ?, url = ?, publishedAt = ?, scheduledAt = ?,
            errorCode = '', platformErrorText = '',
            observedAt = ?, updatedAt = ?
        WHERE taskId = ?
          AND state IN ('final_action_clicked', 'platform_accepted', 'ambiguous')
          AND finalActionTriggered = 1
        """,
        (
            media_id,
            url,
            published_at,
            scheduled_at,
            observed_at,
            observed_at,
            task_id,
        ),
    ).rowcount
    if changed != 1:
        raise InstagramPublishError(
            "instagram_claim_invalid",
            "Instagram Claim 当前状态不能写入成功回执。",
        )


def get_instagram_claim(
    conn: sqlite3.Connection,
    task_id: int,
) -> dict[str, object]:
    ensure_instagram_claim_schema(conn)
    cursor = conn.execute(
        "SELECT * FROM instagram_controlled_publish_claims WHERE taskId = ?",
        (task_id,),
    )
    row = _row_dict(cursor, cursor.fetchone())
    if row is None:
        raise InstagramPublishError(
            "instagram_claim_invalid", "Instagram 正式任务缺少 Claim。"
        )
    return row
