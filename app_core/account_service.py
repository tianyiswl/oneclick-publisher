# -*- coding: utf-8 -*-
"""账号数据服务。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from conf import DEBUG_SKIP_FINAL_PUBLISH

from .database import connect
from .overseas_meta_errors import FacebookPagePublishError
from .overseas_meta_page_identity import (
    FacebookPageIdentity,
    facebook_page_v1_enabled,
    normalize_facebook_page_id,
)
from .overseas_tiktok_identity import (
    TikTokIdentity,
    TikTokIdentityError,
    normalize_tiktok_handle,
    validate_identity_binding,
    validate_saved_tiktok_account,
)
from .paths import AVATAR_DIR, COOKIE_DIR


PLATFORMS = {
    1: "小红书",
    2: "视频号",
    3: "抖音",
    4: "快手",
    5: "B站",
    6: "TikTok",
    7: "YouTube",
    8: "Instagram Reels",
    9: "Facebook Reels",
    10: "公众号",
}
PLATFORM_ORDER = [3, 2, 5, 1, 4, 10, 6, 7, 8, 9]
LOGIN_PLATFORM_OPTIONS = [
    (3, "抖音"),
    (2, "视频号"),
    (5, "B站"),
    (1, "小红书"),
    (4, "快手"),
    (10, "公众号"),
    (6, "TikTok"),
    (7, "YouTube"),
    (8, "Instagram Reels"),
]
OVERSEAS_PLATFORM_TYPES = {6, 7, 8, 9}
AUTH_MODE_BROWSER = "browser"
AUTH_MODE_YOUTUBE_OAUTH = "youtube_oauth"
DRAFT_SUPPORTED_PLATFORM_TYPES = frozenset({2, 5})
DRAFT_UNSUPPORTED_PLATFORM_MESSAGES = {
    1: (
        "小红书当前无法正常保存并稳定回读平台草稿；"
        "请改用前台预发布检查"
    ),
    3: (
        "抖音当前仅提供自动化浏览器本地未发布缓存，"
        "不能验证为平台后台草稿；请改用前台预发布检查"
    ),
    4: (
        "快手当前仅提供自动化浏览器本地缓存（未发布的视频），"
        "不能验证为平台后台草稿；请改用前台预发布检查"
    ),
    6: "TikTok 浏览器通道不保存平台草稿；请使用预发布检查或可见浏览器正式发布。",
    7: "YouTube 浏览器通道不保存平台草稿；请使用预发布检查或可见浏览器正式发布。",
    8: "Instagram Reels 当前不开放平台草稿保存。",
    9: "Facebook Reels 当前不开放平台草稿保存。",
}
STATUS_TEXT = {2: "待检测", 1: "正常", 0: "异常"}
ACCOUNT_CHECK_TTL_MINUTES = 24 * 60
TENCENT_LOGIN_ESTIMATED_HOURS = 24
HEALTH_STATUS_TEXT = {
    "normal": "正常",
    "pending": "已登录待检测",
    "stale": "待检测",
    "abnormal": "异常",
}


def _ensure_demo_accounts() -> None:
    """仅在展示版补入不可执行的示例账号，方便验收发布适配界面。"""

    if not DEBUG_SKIP_FINAL_PUBLISH:
        return
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM user_info WHERE filePath = ? LIMIT 1",
            ("__oneclick_demo_wechat__.json",),
        ).fetchone()
        if exists:
            return
        conn.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, remark)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                10,
                "__oneclick_demo_wechat__.json",
                "一键发公众号（演示）",
                1,
                "一键发示例主体",
                "演示账号：未连接真实公众号，不可执行登录或发布",
            ),
        )


def _promote_confirmed_oneclick_sessions() -> None:
    """兼容早期“已登录待检测”记录。

    这些记录由用户完成官方页面登录后保存，属于已收到身份回执的登录成功，
    应标记为正常；发布资格仍由发布任务预检单独判断。
    """

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute(
            """
            UPDATE user_info
            SET status = 1,
                lastCheckedAt = COALESCE(lastCheckedAt, lastLoginAt, ?),
                remark = ''
            WHERE status = 2
              AND remark LIKE '一键发本地授权会话已保存%'
            """,
            (now,),
        )
        # 清理此前由系统自动写入的登录说明；备注只保留用户主动填写的内容。
        conn.execute(
            """
            UPDATE user_info
            SET remark = ''
            WHERE remark = '一键发已通过平台身份回执确认登录正常'
            """
        )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("T", " "))
    except ValueError:
        return None


def login_platform_type(platform_type: int) -> int:
    """Keep each explicit publishing target on its own login route."""

    return int(platform_type)


def login_platform_options() -> tuple[tuple[int, str], ...]:
    """Expose Facebook Page only for the process-local V1 opt-in."""

    options = list(LOGIN_PLATFORM_OPTIONS)
    if facebook_page_v1_enabled():
        options.append((9, "Facebook Page"))
    return tuple(options)


def check_is_fresh(
    last_checked_at: str | None,
    *,
    now: datetime | None = None,
    max_age_minutes: int = ACCOUNT_CHECK_TTL_MINUTES,
) -> bool:
    """判断登录检测结果是否仍在可信时限内。"""

    checked_at = _parse_datetime(last_checked_at)
    if checked_at is None:
        return False
    reference = now or datetime.now()
    return checked_at >= reference - timedelta(minutes=max_age_minutes)


def estimated_login_expiry(
    platform_type: int,
    last_login_at: str | None,
    *,
    now: datetime | None = None,
) -> dict:
    """返回平台登录态的预计失效提示，不替代真实登录检测。"""

    if int(platform_type) != 2:
        return {
            "estimatedExpiresAt": None,
            "estimatedExpiryStatus": "not_applicable",
            "estimatedExpiryText": "以实时检测为准",
        }
    logged_at = _parse_datetime(last_login_at)
    if logged_at is None:
        return {
            "estimatedExpiresAt": None,
            "estimatedExpiryStatus": "unknown",
            "estimatedExpiryText": "登录后计算",
        }
    expires_at = logged_at + timedelta(hours=TENCENT_LOGIN_ESTIMATED_HOURS)
    expired = expires_at <= (now or datetime.now())
    expires_text = expires_at.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "estimatedExpiresAt": expires_text,
        "estimatedExpiryStatus": "expired" if expired else "valid",
        "estimatedExpiryText": (
            f"{expires_text}（预计已到期）"
            if expired
            else f"{expires_text}（预计）"
        ),
    }


def _row_to_dict(row) -> dict:
    data = dict(row)
    raw_status = int(data.get("status") or 0)
    needs_page_rebind = (
        int(data.get("type") or 0) == 9
        and not str(data.get("accountReference") or "").strip()
    )
    needs_publish_scope_upgrade = (
        int(data.get("type") or 0) == 7
        and str(data.get("authMode") or AUTH_MODE_BROWSER)
        == AUTH_MODE_YOUTUBE_OAUTH
        and int(data.get("oauthScopeVersion") or 1) < 2
    )
    if needs_page_rebind:
        health_status = "abnormal"
    elif raw_status == 2:
        health_status = "pending"
    elif raw_status != 1:
        health_status = "abnormal"
    elif needs_publish_scope_upgrade:
        health_status = "pending"
    elif check_is_fresh(data.get("lastCheckedAt")):
        health_status = "normal"
    else:
        health_status = "stale"
    data["platformName"] = PLATFORMS.get(data.get("type"), f"平台{data.get('type')}")
    data["healthStatus"] = health_status
    data["isHealthy"] = health_status == "normal"
    data["needsPublishScopeUpgrade"] = needs_publish_scope_upgrade
    data["needsPageRebind"] = needs_page_rebind
    data["statusText"] = (
        "需要升级发布权限"
        if raw_status == 1 and needs_publish_scope_upgrade
        else HEALTH_STATUS_TEXT[health_status]
    )
    data["remark"] = data.get("remark") or ""
    data["profileName"] = data.get("profileName") or data.get("userName") or "未命名主体"
    data.update(
        estimated_login_expiry(
            int(data.get("type") or 0),
            data.get("lastLoginAt"),
        )
    )
    return data


def _list_accounts(*, include_youtube_oauth: bool) -> list[dict]:
    _ensure_demo_accounts()
    _promote_confirmed_oneclick_sessions()
    auth_modes = (
        (AUTH_MODE_BROWSER, AUTH_MODE_YOUTUBE_OAUTH)
        if include_youtube_oauth
        else (AUTH_MODE_BROWSER,)
    )
    placeholders = ", ".join("?" for _item in auth_modes)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, type, filePath, userName, status, profileName, avatarPath,
                   avatarUpdatedAt, remark, lastCheckedAt, lastLoginAt,
                   COALESCE(authMode, 'browser') AS authMode, accountReference,
                   COALESCE(oauthScopeVersion, 1) AS oauthScopeVersion
            FROM user_info
            WHERE COALESCE(authMode, 'browser') IN ({placeholders})
            ORDER BY profileName COLLATE NOCASE, type
            """,
            auth_modes,
        ).fetchall()
    # 演示账号只用于早期展示，不应混入用户的真实账号、发布目标或统计结果。
    return [
        _row_to_dict(row)
        for row in rows
        if not str(row["filePath"] or "").startswith("__oneclick_demo_")
    ]


def list_accounts() -> list[dict]:
    """Return browser-session rows that existing publishing can safely consume."""

    return _list_accounts(include_youtube_oauth=False)


def list_managed_accounts() -> list[dict]:
    """Return all account-management rows, including official YouTube OAuth."""

    return _list_accounts(include_youtube_oauth=True)


def _is_publishable_facebook_page_account(account: Mapping[str, Any]) -> bool:
    try:
        validate_saved_facebook_page_account(account)
    except FacebookPagePublishError:
        return False
    return True


def list_publishable_accounts() -> list[dict]:
    """Return browser accounts plus official YouTube OAuth publishing rows."""

    return [
        row
        for row in list_managed_accounts()
        if (
            str(row.get("authMode") or AUTH_MODE_BROWSER) == AUTH_MODE_BROWSER
            and (
                int(row.get("type") or 0) != 6
                or (
                    int(row.get("status") or 0) == 1
                    and bool(normalize_tiktok_handle(row.get("accountReference")))
                )
            )
            and (
                int(row.get("type") or 0) != 9
                or _is_publishable_facebook_page_account(row)
            )
        )
        or (
            int(row.get("type") or 0) == 7
            and str(row.get("authMode") or "") == AUTH_MODE_YOUTUBE_OAUTH
        )
    ]


def get_managed_account(account_id: int) -> dict | None:
    wanted = int(account_id)
    return next(
        (row for row in list_managed_accounts() if int(row["id"]) == wanted),
        None,
    )


def save_oneclick_authorized_account(
    platform_type: int,
    profile_name: str,
    storage_file_name: str,
    *,
    record_id: int | None = None,
    display_name: str | None = None,
) -> int:
    """保存平台身份回执已确认的一键发独立浏览器会话。"""

    platform_type = int(platform_type)
    if platform_type not in PLATFORMS:
        raise ValueError("未知平台")
    profile_name = str(profile_name or "").strip()
    storage_file_name = Path(str(storage_file_name or "")).name
    if not profile_name or not storage_file_name:
        raise ValueError("账号主体或会话文件不能为空")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_name = str(display_name or "").strip() or f"{PLATFORMS[platform_type]}账号"
    with connect() as conn:
        if record_id:
            existing = conn.execute("SELECT id FROM user_info WHERE id = ?", (int(record_id),)).fetchone()
            if not existing:
                raise ValueError("待更新账号不存在")
            conn.execute(
                """
                UPDATE user_info
                SET type = ?, filePath = ?, userName = CASE WHEN ? = '' THEN userName ELSE ? END,
                    status = 1, profileName = ?,
                    lastLoginAt = ?, lastCheckedAt = ?
                WHERE id = ?
                """,
                (
                    platform_type,
                    storage_file_name,
                    str(display_name or "").strip(),
                    user_name,
                    profile_name,
                    now,
                    now,
                    int(record_id),
                ),
            )
            return int(record_id)
        cursor = conn.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, remark, lastLoginAt, lastCheckedAt)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (platform_type, storage_file_name, user_name, profile_name, "", now, now),
        )
        return int(cursor.lastrowid)


_TIKTOK_ACCOUNT_COMPARE_FIELDS = (
    "id",
    "type",
    "filePath",
    "userName",
    "status",
    "profileName",
    "remark",
    "lastCheckedAt",
    "lastLoginAt",
    "authMode",
    "accountReference",
)


def _invalid_tiktok_account(message: str) -> TikTokIdentityError:
    return TikTokIdentityError("tiktok_account_invalid", message)


def _normalize_tiktok_session_basename(value: object) -> str:
    raw = str(value or "").strip()
    basename = Path(raw).name
    if not raw or raw != basename or not basename.endswith(".json"):
        raise _invalid_tiktok_account("TikTok 会话文件名无效")
    return basename


def _reject_duplicate_tiktok_handle(conn, handle: str, *, exclude_id: int | None) -> None:
    rows = conn.execute(
        """
        SELECT id, accountReference
        FROM user_info
        WHERE type = 6
          AND accountReference IS NOT NULL
          AND TRIM(accountReference) != ''
        """
    ).fetchall()
    for row in rows:
        if exclude_id is not None and int(row["id"]) == int(exclude_id):
            continue
        if normalize_tiktok_handle(row["accountReference"]) == handle:
            raise _invalid_tiktok_account("TikTok 账号已经绑定")


def save_tiktok_browser_account(
    *,
    profile_name: str,
    storage_file_name: str,
    identity: TikTokIdentity,
    record_id: int | None = None,
    expected_account: Mapping[str, Any] | None = None,
) -> int:
    """Atomically bind one sanitized browser session to a verified TikTok handle."""

    normalized_profile = str(profile_name or "").strip()
    session_basename = _normalize_tiktok_session_basename(storage_file_name)
    handle = normalize_tiktok_handle(getattr(identity, "handle", ""))
    if not normalized_profile or not handle:
        raise _invalid_tiktok_account("TikTok 账号信息不完整")
    display_name = str(getattr(identity, "display_name", "") or "").strip()
    user_name = display_name or f"@{handle}"
    wanted_id = int(record_id) if record_id is not None else None
    snapshot = dict(expected_account) if expected_account is not None else None
    if (wanted_id is None) != (snapshot is None):
        raise _invalid_tiktok_account("TikTok 账号更新信息不完整")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        # Serialize duplicate checks and conditional updates inside one SQLite
        # transaction.  The verified public handle is the only identity saved.
        conn.execute("BEGIN IMMEDIATE")
        if wanted_id is None:
            validate_identity_binding(
                {"accountReference": ""},
                identity,
                allow_initial_bind=True,
            )
            _reject_duplicate_tiktok_handle(conn, handle, exclude_id=None)
            cursor = conn.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, remark,
                     lastLoginAt, lastCheckedAt, authMode, accountReference)
                VALUES (6, ?, ?, 1, ?, '', ?, ?, ?, ?)
                """,
                (
                    session_basename,
                    user_name,
                    normalized_profile,
                    now,
                    now,
                    AUTH_MODE_BROWSER,
                    handle,
                ),
            )
            account_id = int(cursor.lastrowid or 0)
            if account_id <= 0:
                raise _invalid_tiktok_account("TikTok 账号保存失败")
            return account_id

        if snapshot is None or any(
            field not in snapshot
            for field in ("id", "type", "filePath", "accountReference")
        ):
            raise _invalid_tiktok_account("TikTok 账号更新信息不完整")
        try:
            snapshot_id = int(snapshot["id"])
            snapshot_type = int(snapshot["type"])
        except (TypeError, ValueError):
            raise _invalid_tiktok_account("TikTok 账号更新信息无效") from None
        if snapshot_id != wanted_id or snapshot_type != 6:
            raise _invalid_tiktok_account("TikTok 账号更新信息无效")

        row = conn.execute(
            """
            SELECT id, type, filePath, userName, status, profileName,
                   COALESCE(remark, '') AS remark,
                   lastCheckedAt, lastLoginAt,
                   COALESCE(authMode, 'browser') AS authMode,
                   accountReference
            FROM user_info
            WHERE id = ?
            """,
            (wanted_id,),
        ).fetchone()
        if not row or int(row["type"] or 0) != 6:
            raise _invalid_tiktok_account("TikTok 账号记录不存在或类型不正确")

        current = dict(row)
        compared_fields = tuple(
            field
            for field in _TIKTOK_ACCOUNT_COMPARE_FIELDS
            if field in snapshot
        )
        if any(current.get(field) != snapshot.get(field) for field in compared_fields):
            raise _invalid_tiktok_account("TikTok 账号记录在保存期间已变更")

        validate_identity_binding(
            current,
            identity,
            allow_initial_bind=False,
        )
        _reject_duplicate_tiktok_handle(conn, handle, exclude_id=wanted_id)

        where_parts = [f"{field} IS ?" for field in compared_fields]
        where_values = [snapshot.get(field) for field in compared_fields]
        updated = conn.execute(
            f"""
            UPDATE user_info
            SET type = 6, filePath = ?, userName = ?, status = 1,
                profileName = ?, remark = '', lastLoginAt = ?,
                lastCheckedAt = ?, authMode = ?, accountReference = ?
            WHERE {' AND '.join(where_parts)}
            """,
            (
                session_basename,
                user_name,
                normalized_profile,
                now,
                now,
                AUTH_MODE_BROWSER,
                handle,
                *where_values,
            ),
        )
        if int(updated.rowcount or 0) != 1:
            raise _invalid_tiktok_account("TikTok 账号记录在保存期间已变更")
        return wanted_id


def _facebook_page_identity_mismatch(message: str) -> FacebookPagePublishError:
    return FacebookPagePublishError("facebook_page_identity_mismatch", message)


def _normalize_facebook_session_basename(value: object) -> str:
    raw = str(value or "").strip()
    basename = Path(raw).name
    if not raw or raw != basename or not basename.endswith(".json"):
        raise _facebook_page_identity_mismatch("Facebook Page 会话文件名无效。")
    return basename


def _normalize_optional_facebook_avatar_basename(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value or "").strip()
    basename = Path(raw).name
    if not raw or raw != basename:
        raise _facebook_page_identity_mismatch("Facebook Page 头像文件名无效。")
    return basename


def validate_saved_facebook_page_account(account: Mapping[str, Any]) -> str:
    """Return the stable Page ID only for a usable saved Facebook Page row."""

    if not isinstance(account, Mapping):
        raise _facebook_page_identity_mismatch("Facebook Page 账号记录无效。")
    try:
        platform_type = int(account.get("type"))
    except (TypeError, ValueError):
        raise _facebook_page_identity_mismatch("Facebook Page 账号记录无效。") from None
    if platform_type != 9:
        raise _facebook_page_identity_mismatch("Facebook Page 账号记录类型不正确。")
    if (
        int(account.get("status") or 0) != 1
        or str(account.get("authMode") or AUTH_MODE_BROWSER) != AUTH_MODE_BROWSER
        or not str(account.get("filePath") or "").strip()
    ):
        raise _facebook_page_identity_mismatch("Facebook Page 账号记录不可用。")
    try:
        return normalize_facebook_page_id(account.get("accountReference"))
    except FacebookPagePublishError:
        raise
    except Exception as exc:
        raise _facebook_page_identity_mismatch("Facebook Page 账号记录无效。") from exc


_FACEBOOK_PAGE_ACCOUNT_COMPARE_FIELDS = (
    "id",
    "type",
    "filePath",
    "userName",
    "status",
    "profileName",
    "avatarPath",
    "remark",
    "lastCheckedAt",
    "lastLoginAt",
    "authMode",
    "accountReference",
)


def save_facebook_page_browser_account(
    *,
    profile_name: str,
    storage_file_name: str,
    identity: FacebookPageIdentity,
    record_id: int | None = None,
    expected_account: Mapping[str, Any] | None = None,
    avatar_file_name: str | None = None,
) -> int:
    """Atomically upsert exactly one browser row for one stable Facebook Page."""

    normalized_profile = str(profile_name or "").strip()
    session_basename = _normalize_facebook_session_basename(storage_file_name)
    avatar_basename = _normalize_optional_facebook_avatar_basename(avatar_file_name)
    try:
        page_id = normalize_facebook_page_id(getattr(identity, "page_id", None))
    except FacebookPagePublishError:
        raise
    page_name = str(getattr(identity, "page_name", "") or "").strip()
    if not normalized_profile or not page_name:
        raise _facebook_page_identity_mismatch("Facebook Page 账号信息不完整。")

    wanted_id = int(record_id) if record_id is not None else None
    snapshot = dict(expected_account) if expected_account is not None else None
    if snapshot is not None:
        try:
            snapshot_type = int(snapshot.get("type"))
        except (TypeError, ValueError):
            raise _facebook_page_identity_mismatch("Facebook Page 账号更新信息无效。") from None
        if snapshot_type != 9:
            raise _facebook_page_identity_mismatch("Facebook Page 账号更新信息无效。")
        snapshot_id = snapshot.get("id")
        if snapshot_id is not None:
            try:
                snapshot_id = int(snapshot_id)
            except (TypeError, ValueError):
                raise _facebook_page_identity_mismatch("Facebook Page 账号更新信息无效。") from None
            if wanted_id is None:
                wanted_id = snapshot_id
            elif snapshot_id != wanted_id:
                raise _facebook_page_identity_mismatch("Facebook Page 账号更新信息无效。")
        snapshot_reference = str(snapshot.get("accountReference") or "").strip()
        if snapshot_reference:
            if normalize_facebook_page_id(snapshot_reference) != page_id:
                raise _facebook_page_identity_mismatch(
                    "保存的 Facebook Page 与当前页面不一致，已停止操作。"
                )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if wanted_id is None:
            existing = conn.execute(
                """
                SELECT id, type, filePath, userName, status, profileName,
                       avatarPath, COALESCE(remark, '') AS remark,
                       lastCheckedAt, lastLoginAt,
                       COALESCE(authMode, 'browser') AS authMode,
                       accountReference
                FROM user_info
                WHERE type = 9 AND accountReference = ?
                """,
                (page_id,),
            ).fetchone()
        else:
            existing = conn.execute(
                """
                SELECT id, type, filePath, userName, status, profileName,
                       avatarPath, COALESCE(remark, '') AS remark,
                       lastCheckedAt, lastLoginAt,
                       COALESCE(authMode, 'browser') AS authMode,
                       accountReference
                FROM user_info
                WHERE id = ?
                """,
                (wanted_id,),
            ).fetchone()

        if existing is None:
            if wanted_id is not None:
                raise _facebook_page_identity_mismatch(
                    "Facebook Page 账号记录不存在或类型不正确。"
                )
            cursor = conn.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, avatarPath,
                     avatarUpdatedAt, remark, lastLoginAt, lastCheckedAt,
                     authMode, accountReference)
                VALUES (9, ?, ?, 1, ?, ?, ?, '', ?, ?, ?, ?)
                """,
                (
                    session_basename,
                    page_name,
                    normalized_profile,
                    avatar_basename,
                    now if avatar_basename else None,
                    now,
                    now,
                    AUTH_MODE_BROWSER,
                    page_id,
                ),
            )
            account_id = int(cursor.lastrowid or 0)
            if account_id <= 0:
                raise _facebook_page_identity_mismatch("Facebook Page 账号保存失败。")
            return account_id

        current = dict(existing)
        if int(current.get("type") or 0) != 9:
            raise _facebook_page_identity_mismatch(
                "Facebook Page 账号记录不存在或类型不正确。"
            )
        current_reference = str(current.get("accountReference") or "").strip()
        if current_reference and normalize_facebook_page_id(current_reference) != page_id:
            raise _facebook_page_identity_mismatch(
                "保存的 Facebook Page 与当前页面不一致，已停止操作。"
            )
        if snapshot is not None:
            compared_fields = tuple(
                field
                for field in _FACEBOOK_PAGE_ACCOUNT_COMPARE_FIELDS
                if field in snapshot
            )
            if any(current.get(field) != snapshot.get(field) for field in compared_fields):
                raise _facebook_page_identity_mismatch(
                    "Facebook Page 账号记录在保存期间已变更。"
                )

        duplicate = conn.execute(
            "SELECT id FROM user_info WHERE type = 9 AND accountReference = ? AND id != ?",
            (page_id, int(current["id"])),
        ).fetchone()
        if duplicate:
            raise _facebook_page_identity_mismatch("Facebook Page 已经绑定其他账号记录。")
        conn.execute(
            """
            UPDATE user_info
            SET type = 9, filePath = ?, userName = ?, status = 1,
                profileName = ?, avatarPath = COALESCE(?, avatarPath),
                avatarUpdatedAt = CASE WHEN ? IS NULL THEN avatarUpdatedAt ELSE ? END,
                lastLoginAt = ?, lastCheckedAt = ?, authMode = ?,
                accountReference = ?
            WHERE id = ?
            """,
            (
                session_basename,
                page_name,
                normalized_profile,
                avatar_basename,
                avatar_basename,
                now,
                now,
                now,
                AUTH_MODE_BROWSER,
                page_id,
                int(current["id"]),
            ),
        )
        return int(current["id"])


def save_youtube_oauth_account(
    *,
    profile_name: str,
    credential_reference: str,
    channel_id: str,
    display_name: str | None,
    record_id: int | None = None,
    oauth_scope_version: int = 1,
) -> int:
    """Persist public YouTube identity without storing OAuth tokens in SQLite."""

    profile_name = str(profile_name or "").strip()
    credential_reference = str(credential_reference or "").strip()
    channel_id = str(channel_id or "").strip()
    oauth_scope_version = int(oauth_scope_version)
    user_name = str(display_name or "").strip() or "YouTube 频道"
    if (
        not profile_name
        or not credential_reference.startswith("youtube-oauth:")
        or not channel_id
        or oauth_scope_version not in {1, 2}
    ):
        raise ValueError("YouTube OAuth 账号信息不完整")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        if record_id is not None:
            existing = conn.execute(
                """
                SELECT id, type, COALESCE(authMode, 'browser') AS authMode
                FROM user_info WHERE id = ?
                """,
                (int(record_id),),
            ).fetchone()
            if (
                not existing
                or int(existing["type"]) != 7
                or str(existing["authMode"]) != AUTH_MODE_YOUTUBE_OAUTH
            ):
                raise ValueError("待更新的 YouTube OAuth 账号不存在")
            conn.execute(
                """
                UPDATE user_info
                SET type = 7, filePath = ?, userName = ?, status = 1,
                    profileName = ?, remark = '', lastLoginAt = ?,
                    lastCheckedAt = ?, authMode = ?, accountReference = ?,
                    oauthScopeVersion = ?
                WHERE id = ?
                """,
                (
                    credential_reference,
                    user_name,
                    profile_name,
                    now,
                    now,
                    AUTH_MODE_YOUTUBE_OAUTH,
                    channel_id,
                    oauth_scope_version,
                    int(record_id),
                ),
            )
            return int(record_id)
        cursor = conn.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, remark,
                 lastLoginAt, lastCheckedAt, authMode, accountReference,
                 oauthScopeVersion)
            VALUES (7, ?, ?, 1, ?, '', ?, ?, ?, ?, ?)
            """,
            (
                credential_reference,
                user_name,
                profile_name,
                now,
                now,
                AUTH_MODE_YOUTUBE_OAUTH,
                channel_id,
                oauth_scope_version,
            ),
        )
        return int(cursor.lastrowid)


def group_accounts() -> list[dict]:
    grouped: dict[str, dict] = {}
    for account in list_managed_accounts():
        profile = account["profileName"]
        if profile not in grouped:
            grouped[profile] = {"profileName": profile, "accounts": {}}
        grouped[profile]["accounts"][account["type"]] = account
    return list(grouped.values())


def list_profiles() -> list[str]:
    return sorted(
        {
            row["profileName"]
            for row in list_managed_accounts()
            if row.get("profileName")
            and not str(row.get("filePath") or "").startswith("__oneclick_demo_")
        }
    )


def account_stats() -> dict:
    accounts = list_managed_accounts()
    by_platform = defaultdict(lambda: {"total": 0, "normal": 0, "abnormal": 0})
    for account in accounts:
        item = by_platform[account["type"]]
        item["platformName"] = account["platformName"]
        item["total"] += 1
        if account.get("healthStatus") == "normal":
            item["normal"] += 1
        else:
            item["abnormal"] += 1
        if account.get("healthStatus") == "stale":
            item["stale"] = item.get("stale", 0) + 1
    return {
        "total": len(accounts),
        "normal": sum(1 for item in accounts if item.get("healthStatus") == "normal"),
        "abnormal": sum(1 for item in accounts if item.get("healthStatus") != "normal"),
        "stale": sum(1 for item in accounts if item.get("healthStatus") == "stale"),
        "profiles": len({item["profileName"] for item in accounts}),
        "platforms": [by_platform[key] for key in PLATFORM_ORDER if key in by_platform],
    }


def update_remark(account_id: int, remark: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE user_info SET remark = ? WHERE id = ?", (remark.strip(), account_id))
        conn.commit()


def update_status(account_id: int, status: int) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute("UPDATE user_info SET status = ?, lastCheckedAt = ? WHERE id = ?", (status, now, account_id))
        conn.commit()


def accounts_requiring_check(account_ids: Iterable[int] | None = None) -> list[int]:
    """返回超过 24 小时未检测、但尚未确认失效的账号。"""

    wanted = {int(item) for item in account_ids or []}
    return [
        int(account["id"])
        for account in list_managed_accounts()
        if account.get("healthStatus") == "stale"
        and (not wanted or int(account["id"]) in wanted)
    ]


def delete_account(account_id: int) -> None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT filePath, avatarPath, COALESCE(authMode, 'browser') AS authMode
            FROM user_info WHERE id = ?
            """,
            (account_id,),
        ).fetchone()
        if row and str(row["authMode"]) == AUTH_MODE_YOUTUBE_OAUTH:
            from .overseas_youtube_credentials import KeyringOAuthCredentialStore

            KeyringOAuthCredentialStore().delete_refresh_token(
                str(row["filePath"] or "")
            )
        conn.execute("DELETE FROM user_info WHERE id = ?", (account_id,))
        remaining_file_refs = 0
        remaining_avatar_refs = 0
        if row and row["filePath"]:
            remaining_file_refs = conn.execute(
                "SELECT COUNT(*) FROM user_info WHERE filePath = ?",
                (row["filePath"],),
            ).fetchone()[0]
        if row and row["avatarPath"]:
            remaining_avatar_refs = conn.execute(
                "SELECT COUNT(*) FROM user_info WHERE avatarPath = ?",
                (row["avatarPath"],),
            ).fetchone()[0]
        conn.commit()
    if not row:
        return
    if str(row["authMode"]) == AUTH_MODE_YOUTUBE_OAUTH:
        if row["avatarPath"] and not remaining_avatar_refs:
            avatar_path = AVATAR_DIR / Path(row["avatarPath"]).name
            if avatar_path.exists():
                avatar_path.unlink()
        return
    candidates = (
        (COOKIE_DIR, row["filePath"], remaining_file_refs),
        (AVATAR_DIR, row["avatarPath"], remaining_avatar_refs),
    )
    for base, value, remaining_refs in candidates:
        if remaining_refs:
            continue
        if value:
            path = base / Path(value).name
            if path.exists():
                path.unlink()


def validate_accounts(
    account_ids: Iterable[int] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    *,
    invalid_status: int = 0,
) -> dict:
    """静默复核登录态；仅返回需用户介入的账号，不自行弹浏览器。"""
    from conf import YOUTUBE_OAUTH_CLIENT_ID
    from .oneclick_authorization import verify_saved_session
    from .overseas_youtube_login import validate_saved_youtube_oauth_account
    if int(invalid_status) not in {0, 2}:
        raise ValueError("无效登录态只能标记为异常或待检测")
    accounts = list_managed_accounts()
    wanted = {int(item) for item in account_ids or []}
    selected = [row for row in accounts if not wanted or row["id"] in wanted]
    failures: list[str] = []
    auth_issues: dict[int, str] = {}

    def report(event: dict) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(event)
        except Exception:
            # 进度显示失败不得中断真实账号检测。
            pass

    for index, row in enumerate(selected, start=1):
        base_event = {
            "current": index,
            "total": len(selected),
            "platformName": row.get("platformName") or PLATFORMS.get(row["type"], "未知平台"),
            "profileName": row.get("profileName") or "未命名主体",
            "userName": row.get("userName") or "",
        }
        report({**base_event, "phase": "checking"})
        try:
            if (
                str(row.get("authMode") or AUTH_MODE_BROWSER)
                == AUTH_MODE_YOUTUBE_OAUTH
            ):
                identity = validate_saved_youtube_oauth_account(
                    row,
                    client_id=YOUTUBE_OAUTH_CLIENT_ID,
                )
                valid = identity.channel_id == str(row.get("accountReference") or "")
            elif (
                int(row.get("type") or 0) == 6
                and str(row.get("authMode") or AUTH_MODE_BROWSER)
                == AUTH_MODE_BROWSER
            ):
                validate_saved_tiktok_account(row)
                valid = True
            else:
                valid = verify_saved_session(row)
        except TikTokIdentityError as exc:
            valid = False
            error_code = str(exc.error_code or "tiktok_account_invalid")
            if not error_code.startswith("tiktok_"):
                error_code = "tiktok_account_invalid"
            auth_issues[int(row["id"])] = error_code
            if error_code == "tiktok_account_identity_mismatch":
                failures.append(
                    "TikTok：当前主体与已保存账号不一致，已保留原绑定。"
                )
            elif error_code == "tiktok_session_missing":
                failures.append("TikTok：本地登录会话不存在，请重新登录。")
            else:
                failures.append("TikTok：登录已失效，请重新登录。")
        except FacebookPagePublishError as exc:
            valid = False
            error_code = str(
                exc.error_code or "facebook_page_identity_mismatch"
            )
            if error_code not in {
                "facebook_page_identity_mismatch",
                "facebook_page_content_permission_missing",
                "facebook_page_not_found",
            }:
                error_code = "facebook_page_identity_mismatch"
            auth_issues[int(row["id"])] = error_code
            failures.append(
                "Facebook Page：保存的 Page 无法精确回读，请重新绑定。"
            )
        except Exception as exc:
            valid = False
            reason = str(exc)
            if (
                str(row.get("authMode") or AUTH_MODE_BROWSER)
                == AUTH_MODE_YOUTUBE_OAUTH
                and reason in {
                    "channel_identity_mismatch",
                    "youtube_channel_identity_mismatch",
                }
            ):
                auth_issues[int(row["id"])] = (
                    "youtube_channel_identity_mismatch"
                )
                failures.append(
                    "YouTube：频道身份不一致，已停止并保留原账号绑定。"
                )
            elif (
                str(row.get("authMode") or AUTH_MODE_BROWSER)
                == AUTH_MODE_YOUTUBE_OAUTH
                and reason
                in {
                    "credential_unavailable",
                    "authorization_invalid",
                    "channel_identity_unavailable",
                }
            ):
                auth_issues[int(row["id"])] = "youtube_authorization_invalid"
                failures.append("YouTube：官方授权已失效，请重新授权。")
            else:
                failures.append(
                    f"{row['platformName']}：检测失败（{type(exc).__name__}）。"
                )
        else:
            valid = bool(valid)
            if not valid:
                failures.append(f"{row['platformName']}：未确认当前登录状态，请重新登录。")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        failed_status = (
            0
            if int(row.get("type") or 0) == 9
            and int(row["id"]) in auth_issues
            else int(invalid_status)
        )
        with connect() as conn:
            conn.execute(
                "UPDATE user_info SET status = ?, lastCheckedAt = ? WHERE id = ?",
                (1 if valid else failed_status, now, int(row["id"])),
            )
        report({**base_event, "phase": "checked", "valid": valid})
    refreshed_map = {row["id"]: row for row in list_managed_accounts()}
    checked = [refreshed_map.get(row["id"], row) for row in selected]
    for checked_row in checked:
        issue_code = auth_issues.get(int(checked_row.get("id") or 0))
        if issue_code:
            checked_row["authIssueCode"] = issue_code
    return {
        "failures": failures,
        "accounts": checked,
        "authIssues": auth_issues,
        "checked": checked,
        "normal": [row for row in checked if row.get("status") == 1],
        "abnormal": [row for row in checked if row.get("status") == 0],
        "pending": [row for row in checked if row.get("status") == 2],
        "interventionRequired": [
            row for row in checked if row.get("status") == 0
        ],
    }


def refresh_account_avatar(account_id: int) -> dict:
    account_id = int(account_id)
    account = get_managed_account(account_id)
    if not account:
        return {}
    if str(account.get("authMode") or AUTH_MODE_BROWSER) == AUTH_MODE_YOUTUBE_OAUTH:
        from .overseas_youtube_profile import refresh_youtube_oauth_profile

        profile = refresh_youtube_oauth_profile(account, avatar_dir=AVATAR_DIR)
        _save_youtube_public_profile(
            account_id,
            profile.get("displayName"),
            profile.get("avatarFileName"),
        )
    else:
        run_async_capture_account_avatar(account_id)
    return get_managed_account(account_id) or {}


def _save_youtube_public_profile(
    account_id: int,
    display_name: object,
    avatar_file_name: object,
) -> None:
    """Persist only locally derived public profile fields for one OAuth row."""

    name = str(display_name or "").strip()
    avatar = Path(str(avatar_file_name or "")).name
    if not name and not avatar:
        return
    updates: list[str] = []
    params: list[object] = []
    if name:
        updates.append("userName = ?")
        params.append(name)
    if avatar:
        updates.append("avatarPath = ?")
        params.append(avatar)
    updates.append("avatarUpdatedAt = ?")
    params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    params.extend((int(account_id), AUTH_MODE_YOUTUBE_OAUTH))
    with connect() as conn:
        conn.execute(
            f"""
            UPDATE user_info SET {', '.join(updates)}
            WHERE id = ? AND COALESCE(authMode, 'browser') = ?
            """,
            params,
        )
        conn.commit()


_ACCOUNT_AVATAR_SELECTORS = {
    1: (".user_avatar", ".user-info img", "img[alt*='头像']"),
    2: (".finder-info img.avatar", ".account-info img.avatar", "img[alt*='视频号头像']"),
    3: ("#header-avatar [class*='avatar']", "#header-avatar"),
    4: (".user-info-dpd img", ".user-info img"),
    5: (".cc-header .custom-lazy-img", ".header .custom-lazy-img"),
    6: ('[data-e2e*="avatar" i] img', 'img[alt*="avatar" i]'),
    7: ("#avatar-btn img", "yt-img-shadow#avatar img"),
    8: ('img[alt*="profile picture" i]', '[aria-label*="profile" i] img'),
    9: ('img[alt*="profile picture" i]', '[aria-label*="profile" i] img'),
    10: (".weui-desktop-account__avatar img", ".account_info img", "img[alt*='头像']"),
}

_ACCOUNT_NAME_SELECTORS = {
    1: (".user-info .name-box", ".user-info .name", ".user_name"),
    2: (".finder-nickname", ".account-info .name"),
    # 抖音昵称与头像是相邻但独立的节点；#header-avatar 自身不含昵称。
    # 动态 class 的稳定前缀 `name-` 位于当前用户信息块，首项即账号昵称。
    3: ('div[class^="name-"]', "#header-avatar"),
    4: (".user-info-name", ".user-info-dpd .user-info-name"),
    # B站创作中心首页的 .name 大量用于数据指标（如“弹幕”），不能用作
    # 账号昵称回退。B站昵称统一由官方 nav 身份接口读取，见 _detect_display_name。
    5: (),
    6: ('[data-e2e*="nickname" i]', '[data-e2e*="username" i]'),
    7: ("#channel-title", "ytcp-entity-page-header-view-model #text"),
    8: ('[aria-label*="profile" i]', '[data-pagelet*="Profile" i]'),
    9: ('[aria-label*="profile" i]', '[data-pagelet*="Profile" i]'),
    10: (
        ".acount_box-nickname",
        ".weui-desktop_name",
        ".weui-desktop-account__name",
        ".account_info .name",
        "#js_name",
    ),
}


def _is_display_name(value: object) -> bool:
    text = " ".join(str(value or "").split())
    if len(text) < 2 or len(text) > 40:
        return False
    blocked = (
        "首页", "发布", "内容管理", "活动管理", "数据中心", "账号管理", "素材管理", "创作中心",
        "创作者中心", "消息", "通知", "设置", "退出", "登录", "上传", "平台",
        "服务平台", "个人中心", "帮助", "公众号后台",
    )
    return not any(word in text for word in blocked)


async def _detect_display_name(page, platform_type: int) -> str | None:
    """从已登录官方后台提取昵称，失败时宁可保留旧名称也不猜测。"""

    if int(platform_type) == 3:
        # 抖音创作者中心的动态 ``name-*`` 节点不只用于当前账号。
        # 页面异步渲染时会命中“在线客服”等功能入口；而登录流程已经监听
        # 官方 ``media/user/info`` 身份回执。因此抖音昵称只能来自该回执，
        # 绝不以页面文字兜底或覆盖已保存的官方昵称。
        return None

    if int(platform_type) == 5:
        # B站创作中心的页面结构没有稳定的账号昵称节点；`[class*=name]`
        # 会命中播放量、弹幕等数据卡。只读取当前官方会话的 nav 身份接口，
        # 不将返回原文写入磁盘，也不把无法确认的结果当作账号名称。
        try:
            response = await page.evaluate(
                """async () => {
                    const response = await fetch(
                      'https://api.bilibili.com/x/web-interface/nav',
                      { credentials: 'include' }
                    );
                    const body = await response.json();
                    return { code: body?.code, uname: body?.data?.uname || '' };
                }"""
            )
            if isinstance(response, dict) and response.get("code") == 0:
                value = " ".join(str(response.get("uname") or "").split())
                return value if _is_display_name(value) else None
        except Exception:
            pass
        # B站身份接口不可用时宁可等待/保留旧名，绝不能退化到页面指标。
        return None

    for selector in _ACCOUNT_NAME_SELECTORS.get(int(platform_type), ()):
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible(timeout=1200):
                value = " ".join((await locator.inner_text(timeout=1200)).split())
                if _is_display_name(value):
                    return value
        except Exception:
            continue
    value = await page.evaluate(
        """
        () => {
          const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const blocked = ['首页','发布','内容管理','数据中心','账号管理','素材管理',
            '创作中心','消息','通知','设置','退出','登录','上传','平台','服务平台','个人中心'];
          const candidates = Array.from(document.querySelectorAll('span, div, a, p')).map(node => {
            const text = normalize(node.innerText || node.textContent);
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            const meta = [node.className || '', node.id || '', node.parentElement?.className || ''].join(' ').toLowerCase();
            const visible = rect.width >= 8 && rect.height >= 8 && rect.width <= 320 && rect.height <= 80 &&
              rect.bottom > 0 && rect.right > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            if (!visible || text.length < 2 || text.length > 40 || blocked.some(word => text.includes(word))) return null;
            let score = 0;
            if (/nick|nickname|user-name|username|display-name|account-name|profile-name|author|creator/.test(meta)) score += 90;
            if (/user|account|profile|author|creator|name/.test(meta)) score += 35;
            if (rect.top < 220) score += 24;
            if (rect.left > innerWidth * .45) score += 14;
            if (/^[\\u4e00-\\u9fa5A-Za-z0-9_.·-]{2,32}$/.test(text)) score += 16;
            if (node.children.length > 2) score -= 30;
            return {text, score};
          }).filter(Boolean).sort((a, b) => b.score - a.score);
          return candidates.length && candidates[0].score >= 50 ? candidates[0].text : null;
        }
        """
    )
    return " ".join(str(value or "").split()) if _is_display_name(value) else None


async def _first_visible_avatar(page, platform_type: int):
    """先使用常见平台选择器；页面改版后再用通用特征兜底。"""

    for selector in _ACCOUNT_AVATAR_SELECTORS.get(int(platform_type), ()):
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible(timeout=1200):
                return locator
        except Exception:
            continue
    found = await page.evaluate(
        """
        () => {
          const nodes = Array.from(document.querySelectorAll('img, [style*="background-image"]'));
          const ranked = nodes.map((node, index) => {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            const meta = [node.alt || '', node.className || '', node.id || '',
              node.parentElement?.className || ''].join(' ').toLowerCase();
            const visible = rect.width >= 24 && rect.height >= 24 && rect.width <= 180 &&
              rect.height <= 180 && rect.bottom > 0 && rect.right > 0 &&
              style.display !== 'none' && style.visibility !== 'hidden';
            if (!visible) return null;
            let score = 0;
            if (/avatar|head|user|profile|face|account|portrait/.test(meta)) score += 90;
            if (Math.abs(rect.width - rect.height) <= 12) score += 25;
            if (rect.top < 190 || rect.left > innerWidth * .55) score += 18;
            if (/qrcode|qr|logo|icon|banner|cover/.test(meta)) score -= 80;
            return {index, score};
          }).filter(Boolean).sort((a, b) => b.score - a.score);
          if (!ranked.length || ranked[0].score < 35) return false;
          nodes.forEach(node => node.removeAttribute('data-oneclick-avatar'));
          nodes[ranked[0].index].setAttribute('data-oneclick-avatar', '1');
          return true;
        }
        """
    )
    return page.locator('[data-oneclick-avatar="1"]').first if found else None


async def capture_account_identity_from_page(account_id: int, page, platform_type: int) -> tuple[str | None, str | None]:
    """从已完成登录的官方页面保存头像与昵称。

    授权流程已经停留在平台的身份页时应优先使用本函数，避免再启动一个
    浏览器而错过异步渲染的头像。头像抓取失败不影响已确认的登录会话。
    """

    await page.wait_for_timeout(1_200)
    avatar = await _first_visible_avatar(page, int(platform_type))
    display_name = await _detect_display_name(page, int(platform_type))
    avatar_name: str | None = None
    if avatar is not None:
        avatar_name = f"oneclick_account_{int(account_id)}.png"
        await avatar.screenshot(path=str(AVATAR_DIR / avatar_name))

    if avatar_name or display_name:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates: list[str] = []
        params: list[object] = []
        if avatar_name:
            updates.append("avatarPath = ?")
            params.append(avatar_name)
        if display_name:
            updates.append("userName = ?")
            params.append(display_name)
        updates.append("avatarUpdatedAt = ?")
        params.append(now)
        params.append(int(account_id))
        with connect() as conn:
            conn.execute(
                f"UPDATE user_info SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
    return avatar_name, display_name


def run_async_capture_account_avatar(account_id: int) -> tuple[str | None, str | None]:
    """使用一键发自身会话刷新头像，不再调用旧客户端模块。"""

    import asyncio

    from playwright.async_api import async_playwright
    from .oneclick_authorization import authorization_plan

    async def _capture() -> tuple[str | None, str | None]:
        with connect() as conn:
            row = conn.execute(
                "SELECT id, type, filePath FROM user_info WHERE id = ?",
                (account_id,),
            ).fetchone()
        if not row:
            raise RuntimeError("账号不存在")
        cookie_file = COOKIE_DIR / Path(row["filePath"]).name
        if not cookie_file.exists():
            raise RuntimeError("账号登录文件不存在，请重新登录")
        plan = authorization_plan(int(row["type"]), "账号信息刷新")
        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=False)
        context = None
        page = None
        try:
            context = await browser.new_context(storage_state=str(cookie_file))
            page = await context.new_page()
            await page.goto(plan.login_url, wait_until="domcontentloaded", timeout=45_000)
            # 创作后台首屏通常先渲染框架，再异步加载账号信息；等待完整身份区出现。
            await page.wait_for_timeout(3500)
            avatar_name, display_name = await capture_account_identity_from_page(
                account_id, page, int(row["type"])
            )
            if avatar_name is None:
                raise RuntimeError("未在当前官方后台找到可用头像，请稍后重试")
            return avatar_name, display_name
        finally:
            for resource in (page, context, browser):
                if resource:
                    try:
                        await resource.close()
                    except Exception:
                        pass
            try:
                await p.stop()
            except Exception:
                pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_capture())
    finally:
        loop.close()
