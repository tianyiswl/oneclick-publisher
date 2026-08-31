# -*- coding: utf-8 -*-
"""Instagram 专业账号绑定的本地持久化边界。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .overseas_instagram_identity import (
    InstagramIdentity,
    InstagramIdentityError,
    parse_instagram_identity_payload,
)


def ensure_instagram_account_binding_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS instagram_account_bindings (
            accountId INTEGER PRIMARY KEY,
            instagramUserId TEXT NOT NULL UNIQUE,
            username TEXT NOT NULL,
            accountType TEXT NOT NULL
                CHECK(accountType IN ('business', 'creator')),
            linkedPageId TEXT NOT NULL,
            linkedPageName TEXT NOT NULL,
            observedAt TEXT NOT NULL,
            FOREIGN KEY(accountId) REFERENCES user_info(id) ON DELETE CASCADE
        )
        """
    )


def _row_dict(cursor: sqlite3.Cursor, row: object) -> dict[str, object] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = [str(item[0]) for item in cursor.description or ()]
    return dict(zip(columns, row))


def _validated_identity(identity: InstagramIdentity) -> InstagramIdentity:
    if not isinstance(identity, InstagramIdentity):
        raise InstagramIdentityError(
            "instagram_identity_unavailable",
            "Instagram 页面没有返回完整的专业账号身份，已停止保存。",
        )
    return parse_instagram_identity_payload(
        {
            "state": "ok",
            "instagramUserId": identity.user_id,
            "username": identity.username,
            "displayName": identity.display_name,
            "avatarUrl": "",
            "accountType": identity.account_type,
            "linkedPageId": identity.linked_page_id,
            "linkedPageName": identity.linked_page_name,
            "canManageContent": identity.can_manage_content,
        }
    )


def save_instagram_account_binding(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    identity: InstagramIdentity,
    observed_at: str,
) -> None:
    if type(account_id) is not int or account_id <= 0:
        raise InstagramIdentityError(
            "instagram_account_invalid",
            "Instagram 本地账号记录无效，已停止保存。",
        )
    try:
        if type(observed_at) is not str:
            raise ValueError
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise InstagramIdentityError(
            "instagram_identity_unavailable",
            "Instagram 身份回读时间无效，已停止保存。",
        ) from exc
    normalized = _validated_identity(identity)
    ensure_instagram_account_binding_schema(conn)
    cursor = conn.execute(
        "SELECT id, type, accountReference FROM user_info WHERE id = ?",
        (account_id,),
    )
    account = _row_dict(cursor, cursor.fetchone())
    if account is None or int(account.get("type") or 0) != 8:
        raise InstagramIdentityError(
            "instagram_account_invalid",
            "Instagram 本地账号记录无效，已停止保存。",
        )
    saved_reference = str(account.get("accountReference") or "").strip()
    if saved_reference and saved_reference != normalized.user_id:
        raise InstagramIdentityError(
            "instagram_identity_mismatch",
            "保存的 Instagram 主体与当前登录账号不一致。",
        )
    existing_cursor = conn.execute(
        "SELECT instagramUserId FROM instagram_account_bindings WHERE accountId = ?",
        (account_id,),
    )
    existing = _row_dict(existing_cursor, existing_cursor.fetchone())
    if existing and str(existing.get("instagramUserId") or "") != normalized.user_id:
        raise InstagramIdentityError(
            "instagram_identity_mismatch",
            "保存的 Instagram 主体与当前登录账号不一致。",
        )
    try:
        conn.execute(
            """
            INSERT INTO instagram_account_bindings
                (accountId, instagramUserId, username, accountType,
                 linkedPageId, linkedPageName, observedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(accountId) DO UPDATE SET
                username = excluded.username,
                accountType = excluded.accountType,
                linkedPageId = excluded.linkedPageId,
                linkedPageName = excluded.linkedPageName,
                observedAt = excluded.observedAt
            """,
            (
                account_id,
                normalized.user_id,
                normalized.username,
                normalized.account_type,
                normalized.linked_page_id,
                normalized.linked_page_name,
                observed_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise InstagramIdentityError(
            "instagram_identity_conflict",
            "这个 Instagram 主体已经绑定到另一个本地账号。",
        ) from exc
    conn.execute(
        """
        UPDATE user_info
        SET accountReference = ?, userName = ?, profileName = ?, status = 1
        WHERE id = ? AND type = 8
        """,
        (
            normalized.user_id,
            normalized.username,
            normalized.display_name,
            account_id,
        ),
    )


def load_instagram_account_binding(
    conn: sqlite3.Connection,
    account_id: int,
) -> InstagramIdentity:
    ensure_instagram_account_binding_schema(conn)
    cursor = conn.execute(
        """
        SELECT instagramUserId, username, accountType,
               linkedPageId, linkedPageName
        FROM instagram_account_bindings
        WHERE accountId = ?
        """,
        (account_id,),
    )
    row = _row_dict(cursor, cursor.fetchone())
    if row is None:
        raise InstagramIdentityError(
            "instagram_account_invalid",
            "Instagram 本地账号缺少稳定身份绑定。",
        )
    profile_cursor = conn.execute(
        "SELECT profileName FROM user_info WHERE id = ? AND type = 8",
        (account_id,),
    )
    profile = _row_dict(profile_cursor, profile_cursor.fetchone())
    if profile is None:
        raise InstagramIdentityError(
            "instagram_account_invalid",
            "Instagram 本地账号记录无效。",
        )
    return InstagramIdentity(
        user_id=str(row["instagramUserId"]),
        username=str(row["username"]),
        display_name=str(profile.get("profileName") or row["username"]),
        avatar_url="",
        account_type=str(row["accountType"]),
        linked_page_id=str(row["linkedPageId"]),
        linked_page_name=str(row["linkedPageName"]),
        can_manage_content=True,
    )


def _managed_file_name(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise InstagramIdentityError(
            "instagram_account_invalid",
            f"Instagram {label}文件名无效，已停止保存。",
        )
    normalized = value.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or Path(normalized).name != normalized
    ):
        raise InstagramIdentityError(
            "instagram_account_invalid",
            f"Instagram {label}文件名无效，已停止保存。",
        )
    return normalized


def save_instagram_browser_account(
    conn: sqlite3.Connection,
    *,
    storage_file_name: str,
    identity: InstagramIdentity,
    observed_at: str,
    avatar_file_name: str | None = None,
    record_id: int | None = None,
) -> int:
    """Atomically persist one browser session and its stable IG subject.

    The storage-state contents remain in the managed credential file.  This
    database boundary stores only its generated file name and public account
    identity fields.
    """

    storage_name = _managed_file_name(storage_file_name, label="会话")
    avatar_name = (
        _managed_file_name(avatar_file_name, label="头像")
        if avatar_file_name is not None
        else None
    )
    normalized = _validated_identity(identity)
    savepoint = "instagram_browser_account_save"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        ensure_instagram_account_binding_schema(conn)
        account_id = 0
        if record_id is not None:
            if type(record_id) is not int or record_id <= 0:
                raise InstagramIdentityError(
                    "instagram_account_invalid",
                    "Instagram 本地账号记录无效，已停止保存。",
                )
            account_cursor = conn.execute(
                "SELECT id,type,accountReference FROM user_info WHERE id = ?",
                (record_id,),
            )
            account = _row_dict(account_cursor, account_cursor.fetchone())
            if account is None or int(account.get("type") or 0) != 8:
                raise InstagramIdentityError(
                    "instagram_account_invalid",
                    "Instagram 本地账号记录无效，已停止保存。",
                )
            saved_reference = str(account.get("accountReference") or "").strip()
            if saved_reference and saved_reference != normalized.user_id:
                raise InstagramIdentityError(
                    "instagram_identity_mismatch",
                    "保存的 Instagram 主体与当前登录账号不一致。",
                )
            account_id = int(record_id)
        else:
            matches = conn.execute(
                """
                SELECT id FROM user_info
                WHERE type = 8 AND accountReference = ?
                ORDER BY id
                """,
                (normalized.user_id,),
            ).fetchall()
            if len(matches) > 1:
                raise InstagramIdentityError(
                    "instagram_identity_conflict",
                    "这个 Instagram 主体对应多个本地账号，已停止保存。",
                )
            if matches:
                account_id = int(matches[0][0])

        if account_id:
            conn.execute(
                """
                UPDATE user_info
                SET filePath = ?, userName = ?, status = 1, profileName = ?,
                    avatarPath = COALESCE(?, avatarPath),
                    avatarUpdatedAt = CASE
                        WHEN ? IS NOT NULL THEN ? ELSE avatarUpdatedAt END,
                    lastCheckedAt = ?, lastLoginAt = ?, authMode = 'browser'
                WHERE id = ? AND type = 8
                """,
                (
                    storage_name,
                    normalized.username,
                    normalized.display_name,
                    avatar_name,
                    avatar_name,
                    observed_at,
                    observed_at,
                    observed_at,
                    account_id,
                ),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO user_info (
                    type,filePath,userName,status,profileName,avatarPath,
                    avatarUpdatedAt,lastCheckedAt,lastLoginAt,authMode,
                    accountReference
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 'browser', ?)
                """,
                (
                    8,
                    storage_name,
                    normalized.username,
                    normalized.display_name,
                    avatar_name,
                    observed_at if avatar_name else None,
                    observed_at,
                    observed_at,
                    normalized.user_id,
                ),
            )
            account_id = int(cursor.lastrowid)

        save_instagram_account_binding(
            conn,
            account_id=account_id,
            identity=normalized,
            observed_at=observed_at,
        )
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    conn.execute(f"RELEASE {savepoint}")
    return account_id
