# -*- coding: utf-8 -*-
"""账号数据服务。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from conf import DEBUG_SKIP_FINAL_PUBLISH

from .database import connect
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
    (8, "Instagram / Facebook（Meta）"),
]
OVERSEAS_PLATFORM_TYPES = {6, 7, 8, 9}
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
    6: "TikTok 浏览器通道仅支持预发布检查，不会冒充平台草稿。",
    7: "YouTube 浏览器通道仅支持预发布检查，不会冒充平台草稿。",
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
    """Instagram 与 Facebook 共用一次 Meta Business Suite 登录。"""
    return 8 if int(platform_type) == 9 else int(platform_type)


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
    if raw_status == 2:
        health_status = "pending"
    elif raw_status != 1:
        health_status = "abnormal"
    elif check_is_fresh(data.get("lastCheckedAt")):
        health_status = "normal"
    else:
        health_status = "stale"
    data["platformName"] = PLATFORMS.get(data.get("type"), f"平台{data.get('type')}")
    data["healthStatus"] = health_status
    data["isHealthy"] = health_status == "normal"
    data["statusText"] = HEALTH_STATUS_TEXT[health_status]
    data["remark"] = data.get("remark") or ""
    data["profileName"] = data.get("profileName") or data.get("userName") or "未命名主体"
    data.update(
        estimated_login_expiry(
            int(data.get("type") or 0),
            data.get("lastLoginAt"),
        )
    )
    return data


def list_accounts() -> list[dict]:
    _ensure_demo_accounts()
    _promote_confirmed_oneclick_sessions()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, type, filePath, userName, status, profileName, avatarPath,
                   avatarUpdatedAt, remark, lastCheckedAt, lastLoginAt
            FROM user_info
            WHERE COALESCE(authMode, 'browser') = 'browser'
            ORDER BY profileName COLLATE NOCASE, type
            """
        ).fetchall()
    # 演示账号只用于早期展示，不应混入用户的真实账号、发布目标或统计结果。
    return [
        _row_to_dict(row)
        for row in rows
        if not str(row["filePath"] or "").startswith("__oneclick_demo_")
    ]


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


def group_accounts() -> list[dict]:
    grouped: dict[str, dict] = {}
    for account in list_accounts():
        profile = account["profileName"]
        if profile not in grouped:
            grouped[profile] = {"profileName": profile, "accounts": {}}
        grouped[profile]["accounts"][account["type"]] = account
    return list(grouped.values())


def list_profiles() -> list[str]:
    return sorted(
        {
            row["profileName"]
            for row in list_accounts()
            if row.get("profileName")
            and not str(row.get("filePath") or "").startswith("__oneclick_demo_")
        }
    )


def account_stats() -> dict:
    accounts = list_accounts()
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
        for account in list_accounts()
        if account.get("healthStatus") == "stale"
        and (not wanted or int(account["id"]) in wanted)
    ]


def delete_account(account_id: int) -> None:
    with connect() as conn:
        row = conn.execute("SELECT filePath, avatarPath FROM user_info WHERE id = ?", (account_id,)).fetchone()
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
    from .oneclick_authorization import verify_saved_session
    if int(invalid_status) not in {0, 2}:
        raise ValueError("无效登录态只能标记为异常或待检测")
    accounts = list_accounts()
    wanted = {int(item) for item in account_ids or []}
    selected = [row for row in accounts if not wanted or row["id"] in wanted]
    failures: list[str] = []

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
            valid = verify_saved_session(row)
        except Exception as exc:
            valid = False
            failures.append(f"{row['platformName']}：检测失败（{type(exc).__name__}）。")
        else:
            if not valid:
                failures.append(f"{row['platformName']}：未确认当前登录状态，请重新登录。")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with connect() as conn:
            conn.execute(
                "UPDATE user_info SET status = ?, lastCheckedAt = ? WHERE id = ?",
                (1 if valid else int(invalid_status), now, int(row["id"])),
            )
        report({**base_event, "phase": "checked", "valid": valid})
    refreshed_map = {row["id"]: row for row in list_accounts()}
    checked = [refreshed_map.get(row["id"], row) for row in selected]
    return {
        "failures": failures,
        "checked": checked,
        "normal": [row for row in checked if row.get("status") == 1],
        "abnormal": [row for row in checked if row.get("status") == 0],
        "pending": [row for row in checked if row.get("status") == 2],
        "interventionRequired": [
            row for row in checked if row.get("status") == 0
        ],
    }


def refresh_account_avatar(account_id: int) -> dict:
    run_async_capture_account_avatar(int(account_id))
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, type, filePath, userName, status, profileName, avatarPath,
                   avatarUpdatedAt, remark, lastCheckedAt, lastLoginAt
            FROM user_info
            WHERE id = ?
            """,
            (account_id,),
        ).fetchone()
    return _row_to_dict(row) if row else {}


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
