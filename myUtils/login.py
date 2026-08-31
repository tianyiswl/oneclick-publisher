import asyncio
import base64
import os
from datetime import datetime
from io import BytesIO

from playwright.async_api import async_playwright
from PIL import Image

from app_core.database import open_connection
from app_core import account_service
from app_core.managed_artifact_cleanup import (
    unlink_managed_artifact_if_unreferenced,
)
from app_core.overseas_meta_errors import FacebookPagePublishError
from app_core.overseas_meta_page_identity import (
    FacebookPageIdentity,
    activate_saved_facebook_page,
    discover_manageable_facebook_pages,
    resolve_facebook_page_selection,
)
from app_core.overseas_instagram_account import save_instagram_browser_account
from app_core.overseas_instagram_browser_identity import (
    read_confirmed_instagram_identity,
)
from app_core.overseas_instagram_identity import (
    InstagramIdentity,
    InstagramIdentityError,
)
from app_core.overseas_tiktok_identity import (
    TikTokIdentityError,
    persist_tiktok_identity,
    read_tiktok_identity,
    validate_identity_binding,
)
from myUtils.auth import check_cookie
from myUtils.avatar import capture_identity_from_page
from utils.base_social_media import (
    launch_chromium_with_codecs,
    reveal_page_window,
    save_context_storage_state,
    set_init_script,
)
import uuid
from pathlib import Path
from conf import BASE_DIR


TIKTOK_UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
TIKTOK_QR_LOGIN_URL = "https://www.tiktok.com/login/qrcode?lang=en"
TIKTOK_QR_RATE_LIMIT_MARKERS = (
    "访问太频繁",
    "maximum number of attempts reached",
)
DOUYIN_CREATOR_HOME_URL = "https://creator.douyin.com/"
DOUYIN_UPLOAD_URL = (
    "https://creator.douyin.com/creator-micro/content/upload"
)
DOUYIN_AUTH_COOKIE_NAMES = frozenset(
    (
        "sessionid",
        "sessionid_ss",
        "sid_guard",
        "sid_tt",
        "uid_tt",
        "uid_tt_ss",
    )
)
FACEBOOK_PAGE_DISCOVERY_TIMEOUT_SECONDS = 30.0
FACEBOOK_PAGE_DISCOVERY_POLL_INTERVAL_SECONDS = 0.5


async def launch_login_browser(playwright, background_mode=False, **options):
    options.pop("executable_path", None)
    requested_headless = options.pop("headless", False)
    options.pop("args", None)
    headless = bool(background_mode or requested_headless)
    browser = await launch_chromium_with_codecs(
        playwright,
        headless=headless,
        executable_path=None,
        hide_until_ready=False,
    )
    return browser


async def new_login_context(browser):
    return await browser.new_context(
        no_viewport=True,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )


async def wait_for_login_or_cancel(url_changed_event, cancel_event=None, timeout=200):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"

        remaining = deadline - loop.time()
        if remaining <= 0:
            return "timeout"

        try:
            await asyncio.wait_for(url_changed_event.wait(), timeout=min(0.5, remaining))
            return "logged_in"
        except asyncio.TimeoutError:
            continue


def _has_douyin_authenticated_cookie(cookies) -> bool:
    """只把抖音域下的强登录凭证视为扫码完成，避免二维码刷新造成误判。"""

    for cookie in cookies or ():
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        name = str(cookie.get("name") or "").lower()
        value = str(cookie.get("value") or "")
        if (
            (domain == "douyin.com" or domain.endswith(".douyin.com"))
            and name in DOUYIN_AUTH_COOKIE_NAMES
            and bool(value)
        ):
            return True
    return False


async def _douyin_backend_ready(page) -> bool:
    """兼容平台只跳转后台、但登录 Cookie 名发生变化的情况。"""

    url = (page.url or "").lower()
    if "/creator-micro/" not in url:
        return False
    text = await _body_text(page)
    return not any(
        marker in text
        for marker in (
            "手机号登录",
            "扫码登录",
            "验证码登录",
            "登录后可使用",
        )
    )


async def _wait_for_douyin_login(
    page,
    context,
    cancel_event=None,
    timeout: float = 200,
    poll_interval: float = 0.5,
) -> str:
    """同时观察登录 Cookie 与后台页面，兼容扫码后主页面不跳转的新版流程。"""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        try:
            if _has_douyin_authenticated_cookie(await context.cookies()):
                return "logged_in"
        except Exception:
            pass
        if await _douyin_backend_ready(page):
            return "logged_in"
        await asyncio.sleep(min(poll_interval, max(0, deadline - loop.time())))
    return "timeout"


async def close_login_resources(page=None, context=None, browser=None):
    for resource in (page, context, browser):
        if resource is None:
            continue
        try:
            await resource.close()
        except Exception:
            pass


async def finish_cancelled_login(status_queue, page=None, context=None, browser=None):
    print("登录流程已取消")
    status_queue.put("CANCELLED")
    await close_login_resources(page, context, browser)


async def capture_login_identity(page, platform_type, avatar_key):
    try:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(1200)
        return await capture_identity_from_page(page, f"account_{avatar_key}.png", platform_type)
    except Exception as e:
        print(f"[login] capture identity failed platform={platform_type}: {e}")
        return None, None


def save_login_account(platform_type, cookie_file, profile_name, update_mode=False, record_id=None, avatar_path=None, display_name=None):
    if int(platform_type) == 6 and update_mode:
        raise TikTokIdentityError(
            "tiktok_account_invalid",
            "TikTok 账号更新必须使用已验证身份的条件写入",
        )
    user_name = display_name or profile_name
    saved_account_id = int(record_id) if update_mode and record_id else None
    initial_status = 0 if int(platform_type) == 6 and not update_mode else 1
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_path = Path(BASE_DIR / "db" / "database.db")
    with open_connection(db_path) as conn:
        cursor = conn.cursor()
        if update_mode and record_id:
            cursor.execute(
                """
                UPDATE user_info
                SET type = ?,
                    filePath = ?,
                    userName = ?,
                    status = ?,
                    profileName = ?,
                    avatarPath = COALESCE(?, avatarPath),
                    avatarUpdatedAt = CASE WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP ELSE avatarUpdatedAt END,
                    lastCheckedAt = ?,
                    lastLoginAt = ?
                WHERE id = ?
                """,
                (
                    platform_type,
                    cookie_file,
                    user_name,
                    initial_status,
                    profile_name,
                    avatar_path,
                    avatar_path,
                    checked_at,
                    checked_at,
                    int(record_id),
                ),
            )
        elif avatar_path:
            cursor.execute(
                """
                INSERT INTO user_info (
                    type, filePath, userName, status, profileName, avatarPath,
                    avatarUpdatedAt, lastCheckedAt, lastLoginAt
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
                """,
                (
                    platform_type,
                    cookie_file,
                    user_name,
                    initial_status,
                    profile_name,
                    avatar_path,
                    checked_at,
                    checked_at,
                ),
            )
            saved_account_id = cursor.lastrowid
        else:
            cursor.execute(
                """
                INSERT INTO user_info (
                    type, filePath, userName, status, profileName, lastCheckedAt,
                    lastLoginAt
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform_type,
                    cookie_file,
                    user_name,
                    initial_status,
                    profile_name,
                    checked_at,
                    checked_at,
                ),
            )
            saved_account_id = cursor.lastrowid
        conn.commit()
        print("[OK] 用户状态已记录")
    return saved_account_id


def _save_tiktok_update_if_unchanged(
    previous_account: dict,
    *,
    cookie_file: str,
    profile_name: str,
    avatar_path: str | None,
    display_name: str | None,
    account_reference: str,
) -> int:
    """Replace one TikTok login only while every overwritten field is unchanged."""

    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_path = Path(BASE_DIR / "db" / "database.db")
    with open_connection(db_path) as conn:
        updated = conn.execute(
            """
            UPDATE user_info
            SET type = 6,
                filePath = ?,
                userName = ?,
                status = 1,
                profileName = ?,
                avatarPath = COALESCE(?, avatarPath),
                avatarUpdatedAt = CASE
                    WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP
                    ELSE avatarUpdatedAt
                END,
                lastCheckedAt = ?,
                lastLoginAt = ?,
                accountReference = ?
            WHERE id = ?
              AND type IS ?
              AND filePath IS ?
              AND userName IS ?
              AND status IS ?
              AND profileName IS ?
              AND avatarPath IS ?
              AND avatarUpdatedAt IS ?
              AND lastCheckedAt IS ?
              AND lastLoginAt IS ?
              AND accountReference IS ?
            """,
            (
                cookie_file,
                display_name or profile_name,
                profile_name,
                avatar_path,
                avatar_path,
                checked_at,
                checked_at,
                account_reference,
                int(previous_account["id"]),
                previous_account.get("type"),
                previous_account.get("filePath"),
                previous_account.get("userName"),
                previous_account.get("status"),
                previous_account.get("profileName"),
                previous_account.get("avatarPath"),
                previous_account.get("avatarUpdatedAt"),
                previous_account.get("lastCheckedAt"),
                previous_account.get("lastLoginAt"),
                previous_account.get("accountReference"),
            ),
        )
        if int(updated.rowcount or 0) != 1:
            raise TikTokIdentityError(
                "tiktok_account_invalid",
                "TikTok 账号在登录保存期间已变更",
            )
        conn.commit()
    return int(previous_account["id"])


def _discard_failed_tiktok_login(
    account_id: int | None,
    *,
    update_mode: bool,
    cookie_path: Path,
    avatar_path: str | None = None,
    previous_account: dict | None = None,
    account_write_attempted: bool = True,
) -> None:
    """Remove only artifacts created by the failed TikTok login attempt."""

    if not account_write_attempted:
        cookie_path.unlink(missing_ok=True)
        if avatar_path:
            (Path(BASE_DIR / "avatars") / Path(avatar_path).name).unlink(
                missing_ok=True
            )
        return

    cookie_unreferenced = False
    avatar_unreferenced = not avatar_path
    try:
        db_path = Path(BASE_DIR / "db" / "database.db")
        with open_connection(db_path) as conn:
            if not update_mode and account_id:
                conn.execute(
                    """
                    DELETE FROM user_info
                    WHERE id = ? AND type = 6 AND filePath = ?
                      AND status = 0
                      AND (accountReference IS NULL OR TRIM(accountReference) = '')
                    """,
                    (int(account_id), cookie_path.name),
                )
            elif not update_mode:
                conn.execute(
                    """
                    DELETE FROM user_info
                    WHERE type = 6 AND filePath = ? AND status = 0
                      AND (accountReference IS NULL OR TRIM(accountReference) = '')
                    """,
                    (cookie_path.name,),
                )
            conn.commit()
            cookie_unreferenced = not conn.execute(
                "SELECT 1 FROM user_info WHERE type = 6 AND filePath = ? LIMIT 1",
                (cookie_path.name,),
            ).fetchone()
            if avatar_path:
                avatar_unreferenced = not conn.execute(
                    "SELECT 1 FROM user_info WHERE type = 6 AND avatarPath = ? LIMIT 1",
                    (Path(avatar_path).name,),
                ).fetchone()
    except Exception:
        # 无法确认数据库补偿时保留候选文件，避免留下断链账号行。
        return

    if cookie_unreferenced:
        cookie_path.unlink(missing_ok=True)
    if avatar_path and avatar_unreferenced:
        (Path(BASE_DIR / "avatars") / Path(avatar_path).name).unlink(missing_ok=True)


def _load_tiktok_account_snapshot(record_id: int | None) -> dict:
    if not record_id:
        raise TikTokIdentityError(
            "tiktok_account_invalid", "待更新的 TikTok 账号不存在"
        )
    db_path = Path(BASE_DIR / "db" / "database.db")
    with open_connection(db_path, row_factory=True) as conn:
        row = conn.execute(
            "SELECT * FROM user_info WHERE id = ? AND type = 6",
            (int(record_id),),
        ).fetchone()
    if not row:
        raise TikTokIdentityError(
            "tiktok_account_invalid", "待更新的 TikTok 账号不存在"
        )
    return dict(row)


def _remove_replaced_tiktok_artifacts(
    previous_account: dict,
    *,
    current_cookie: str,
    current_avatar: str | None,
) -> None:
    """Best-effort removal after the replacement row has committed."""

    old_cookie = Path(str(previous_account.get("filePath") or "")).name
    old_avatar = Path(str(previous_account.get("avatarPath") or "")).name
    try:
        db_path = Path(BASE_DIR / "db" / "database.db")
        with open_connection(db_path) as conn:
            cookie_referenced = bool(
                old_cookie
                and conn.execute(
                    "SELECT 1 FROM user_info WHERE filePath = ? LIMIT 1",
                    (old_cookie,),
                ).fetchone()
            )
            avatar_referenced = bool(
                old_avatar
                and conn.execute(
                    "SELECT 1 FROM user_info WHERE avatarPath = ? LIMIT 1",
                    (old_avatar,),
                ).fetchone()
            )
    except Exception:
        return

    if old_cookie and old_cookie != Path(current_cookie).name and not cookie_referenced:
        try:
            (Path(BASE_DIR / "cookiesFile") / old_cookie).unlink(missing_ok=True)
        except OSError:
            pass
    if (
        current_avatar
        and old_avatar
        and old_avatar != Path(current_avatar).name
        and not avatar_referenced
    ):
        try:
            (Path(BASE_DIR / "avatars") / old_avatar).unlink(missing_ok=True)
        except OSError:
            pass


def _remove_replaced_facebook_page_session(
    previous_account: dict,
    *,
    current_cookie: str,
) -> None:
    """Best-effort removal of one committed Page login's unreferenced old state."""

    old_value = previous_account.get("filePath")
    if not old_value:
        return
    try:
        db_path = Path(BASE_DIR / "db" / "database.db")
        with open_connection(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            unlink_managed_artifact_if_unreferenced(
                conn,
                raw_target=old_value,
                managed_dir=Path(BASE_DIR / "cookiesFile"),
                reference_column="filePath",
            )
    except Exception:
        # The new account row has already committed; cleanup cannot invalidate it.
        return


def save_meta_login_accounts(cookie_file, profile_name, update_mode=False, record_id=None, avatar_path=None, display_name=None):
    """Persist only the explicitly selected Instagram target."""
    user_name = display_name or profile_name
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_path = Path(BASE_DIR / "db" / "database.db")
    saved_ids = []
    with open_connection(db_path, row_factory=True) as conn:
        old_profile = profile_name
        if update_mode and record_id:
            source = conn.execute(
                "SELECT profileName FROM user_info WHERE id = ?",
                (int(record_id),),
            ).fetchone()
            if source and source["profileName"]:
                old_profile = source["profileName"]

        rows = conn.execute(
            """
            SELECT id, type FROM user_info
            WHERE type = 8 AND (profileName = ? OR profileName = ?)
            ORDER BY id
            """,
            (old_profile, profile_name),
        ).fetchall()
        rows_by_type = {int(row["type"]): int(row["id"]) for row in rows}

        for platform_type in (8,):
            existing_id = rows_by_type.get(platform_type)
            if existing_id:
                conn.execute(
                    """
                    UPDATE user_info
                    SET filePath = ?, userName = ?, status = 1, profileName = ?,
                        avatarPath = COALESCE(?, avatarPath),
                        avatarUpdatedAt = CASE WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP ELSE avatarUpdatedAt END,
                        lastCheckedAt = ?,
                        lastLoginAt = ?
                    WHERE id = ?
                    """,
                    (
                        cookie_file,
                        user_name,
                        profile_name,
                        avatar_path,
                        avatar_path,
                        checked_at,
                        checked_at,
                        existing_id,
                    ),
                )
                saved_ids.append(existing_id)
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO user_info (
                        type, filePath, userName, status, profileName, avatarPath,
                        avatarUpdatedAt, lastCheckedAt, lastLoginAt
                    )
                    VALUES (
                        ?, ?, ?, 1, ?, ?,
                        CASE WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP ELSE NULL END,
                        ?, ?
                    )
                    """,
                    (
                        platform_type,
                        cookie_file,
                        user_name,
                        profile_name,
                        avatar_path,
                        avatar_path,
                        checked_at,
                        checked_at,
                    ),
                )
                saved_ids.append(int(cursor.lastrowid))
        conn.commit()
    print("[OK] Meta 登录状态已记录为 Instagram")
    return saved_ids


def save_confirmed_instagram_account(
    cookie_file: str,
    *,
    identity: InstagramIdentity,
    avatar_path: str | None = None,
    record_id: int | None = None,
) -> int:
    """Persist one already-confirmed IG subject and its managed session file."""

    db_path = Path(BASE_DIR / "db" / "database.db")
    observed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with open_connection(db_path) as conn:
        account_id = save_instagram_browser_account(
            conn,
            storage_file_name=Path(cookie_file).name,
            identity=identity,
            observed_at=observed_at,
            avatar_file_name=Path(avatar_path).name if avatar_path else None,
            record_id=record_id,
        )
        conn.commit()
    print("[OK] Instagram 专业账号稳定主体已记录")
    return account_id


async def select_facebook_page_for_login(
    page,
    *,
    selection_callback=None,
    expected_page_id: str | None = None,
) -> FacebookPageIdentity:
    """Select and read back one exact manageable Page before saving anything."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + FACEBOOK_PAGE_DISCOVERY_TIMEOUT_SECONDS
    pages: tuple[FacebookPageIdentity, ...] = ()
    while True:
        try:
            pages = await discover_manageable_facebook_pages(page)
        except FacebookPagePublishError as exc:
            if exc.error_code != "facebook_page_not_found":
                raise
            pages = ()
        if pages or loop.time() >= deadline:
            break
        await asyncio.sleep(FACEBOOK_PAGE_DISCOVERY_POLL_INTERVAL_SECONDS)
    if expected_page_id:
        selected = resolve_facebook_page_selection(pages, expected_page_id)
    else:
        try:
            selected = resolve_facebook_page_selection(pages)
        except FacebookPagePublishError as exc:
            if exc.error_code != "facebook_page_selection_required":
                raise
            if selection_callback is None:
                raise
            chosen = await selection_callback(tuple(pages))
            chosen_page_id = (
                chosen.page_id
                if isinstance(chosen, FacebookPageIdentity)
                else chosen
            )
            selected = resolve_facebook_page_selection(pages, chosen_page_id)
    return await activate_saved_facebook_page(page, selected.page_id)


def _mark_failed_facebook_page_update(
    *,
    update_mode: bool,
    record_id: int | None,
    expected_account,
    error: FacebookPagePublishError,
) -> None:
    """Invalidate only the original Page row after an identity/permission failure."""

    if not update_mode or record_id is None or not expected_account:
        return
    if error.error_code not in {
        "facebook_page_identity_mismatch",
        "facebook_page_content_permission_missing",
        "facebook_page_not_found",
    }:
        return
    try:
        expected_id = int(expected_account.get("id"))
        expected_type = int(expected_account.get("type"))
    except (AttributeError, TypeError, ValueError):
        return
    if expected_id != int(record_id) or expected_type != 9:
        return
    account_service.update_status(expected_id, 0)


async def _visible(page, selector: str) -> bool:
    try:
        locator = page.locator(selector).first
        return bool(await locator.count() and await locator.is_visible())
    except Exception:
        return False


async def _body_text(page) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return ""


async def _browser_login_state(page, platform_type: int) -> str:
    url = (page.url or "").lower()
    text = await _body_text(page)
    if platform_type == 6:
        if "/login" in url or any(marker in text for marker in ("log in to tiktok", "sign up for tiktok")):
            return "pending"
        for selector in (
            'input[type="file"]',
            'button:has-text("Select video")',
            'button:has-text("Select file")',
        ):
            if await _visible(page, selector):
                return "ready"
        return "pending"

    if platform_type == 7:
        if "accounts.google.com" in url or "/signin" in url:
            return "pending"
        if "/channel/" in url or await _visible(page, "ytcp-app"):
            return "ready"
        return "pending"

    denied_markers = (
        "无法用这个账户访问 meta business suite",
        "can't access meta business suite",
        "cannot access meta business suite",
        "you don't have access to meta business suite",
    )
    if any(marker in text for marker in denied_markers):
        return "denied"
    if "facebook.com/login" in url or "checkpoint" in url:
        return "pending"
    composer_ready = any(
        marker in text
        for marker in (
            "create post",
            "create reel",
            "add video",
            "add photo/video",
            "add photos/videos",
            "创建帖子",
            "创建 reel",
            "发帖",
            "添加视频",
            "添加照片/视频",
        )
    ) or await _visible(page, '[contenteditable="true"][role="textbox"]')
    destination = "facebook" if int(platform_type) == 9 else "instagram"
    return "ready" if composer_ready and destination in text else "pending"


async def _wait_for_browser_login(page, platform_type: int, cancel_event=None, timeout: int = 600) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        state = await _browser_login_state(page, platform_type)
        if state in {"ready", "denied"}:
            return state
        await asyncio.sleep(1)
    return "timeout"


def _tiktok_qr_is_ready(src: str | None, spinner_visible: bool) -> bool:
    value = (src or "").lower()
    return bool(
        value
        and not spinner_visible
        and "c4c40812758dc8175106" not in value
    )


def _is_tiktok_qr_login_url(url: str | None) -> bool:
    return "/login/qrcode" in (url or "").lower()


def _tiktok_qr_rate_limited(errors: list[str]) -> bool:
    message = "\n".join(errors).lower()
    return any(marker in message for marker in TIKTOK_QR_RATE_LIMIT_MARKERS)


async def _tiktok_qr_source(page, timeout: int = 25) -> tuple[str | None, str | None]:
    errors = []

    def collect_console_error(message):
        if message.type == "error":
            errors.append(message.text)

    page.on("console", collect_console_error)
    if not _is_tiktok_qr_login_url(page.url):
        switch = page.get_by_text("使用二维码登录", exact=True)
        if not await switch.count():
            switch = page.get_by_text("Use QR code", exact=True)
        if not await switch.count():
            await page.goto(
                TIKTOK_QR_LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
        else:
            await switch.first.click(timeout=10000)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _tiktok_qr_rate_limited(errors):
            return None, "TikTok 已达到二维码尝试次数上限，平台尚未解除限制"
        image = page.locator(
            '[data-e2e*="qr" i] img, img[class*="ImgCode"]'
        ).first
        try:
            if await image.count() and await image.is_visible():
                src = await image.get_attribute("src")
                spinner = page.locator(
                    '[data-e2e*="qr" i] svg[class*="SvgContainer"], '
                    'svg[class*="SvgContainer"]'
                ).first
                spinner_visible = bool(
                    await spinner.count() and await spinner.is_visible()
                )
                if _tiktok_qr_is_ready(src, spinner_visible):
                    return await _locator_png_data_url(image), None
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None, "二维码加载超时"


async def _wait_for_tiktok_qr_login(page, cancel_event=None, timeout: int = 200) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        if "/login" not in (page.url or "").lower():
            try:
                await page.goto(
                    TIKTOK_UPLOAD_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception:
                pass
            remaining = max(1, int(deadline - loop.time()))
            return await _wait_for_browser_login(
                page,
                6,
                cancel_event,
                timeout=remaining,
            )
        await asyncio.sleep(0.5)
    return "timeout"


async def _browser_cookie_gen(
    platform_type,
    profile_name,
    status_queue,
    update_mode=False,
    record_id=None,
    cancel_event=None,
    background_mode=False,
    selection_callback=None,
    expected_account=None,
):
    config = {
        6: (
            TIKTOK_QR_LOGIN_URL if background_mode else TIKTOK_UPLOAD_URL,
            "请使用 TikTok App 扫描二维码并在手机端确认登录。",
        ),
        7: (
            "https://studio.youtube.com/",
            "请在打开的窗口完成 Google / YouTube 登录；进入频道后台后会自动保存。",
        ),
        8: (
            "https://business.facebook.com/latest/composer/",
            "请在打开的窗口登录 Meta，并确认 Instagram 发布入口可用。",
        ),
        9: (
            "https://business.facebook.com/latest/composer/",
            "请在打开的窗口登录 Meta，并确认要绑定的 Facebook Page。",
        ),
    }
    url, instruction = config[int(platform_type)]
    async with async_playwright() as playwright:
        browser = await launch_login_browser(
            playwright,
            headless=False,
            background_mode=background_mode,
        )
        context = await new_login_context(browser)
        context = await set_init_script(context)
        page = await context.new_page()
        cookie_path = None
        avatar_path = None
        account_id = None
        tiktok_previous_account = None
        tiktok_login_succeeded = False
        tiktok_account_write_attempted = False
        facebook_page_login_succeeded = False
        instagram_login_succeeded = False
        instagram_identity = None
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if platform_type == 6 and background_mode:
                qr_source, qr_error = await _tiktok_qr_source(page)
                if not qr_source:
                    if qr_error and "尝试次数上限" in qr_error:
                        status_queue.put(
                            "ERROR: TikTok 已限制当前设备的二维码生成次数，"
                            "平台未公布解除时间。请停止重复点击“开始登录”；"
                            "如需立即登录，取消勾选“后台运行”后使用手机号、"
                            "邮箱或用户名登录，不要选择 Google 登录。"
                        )
                    else:
                        status_queue.put(
                            "ERROR: TikTok 二维码暂时无法生成："
                            f"{qr_error or '未知错误'}。请稍后重试；如需立即登录，"
                            "取消勾选“后台运行”后使用手机号、邮箱或用户名登录，"
                            "不要选择 Google 登录。"
                        )
                    status_queue.put("500")
                    return None
                status_queue.put(qr_source)
            elif not background_mode:
                await reveal_page_window(page)
                if platform_type == 6:
                    instruction = (
                        "请在浏览器中使用 TikTok 二维码、手机号、邮箱或用户名登录；"
                        "Google 会拦截自动化浏览器，请勿选择 Google 登录。"
                    )
            status_queue.put(instruction)
            if platform_type == 6 and background_mode:
                wait_result = await _wait_for_tiktok_qr_login(page, cancel_event)
            else:
                wait_result = await _wait_for_browser_login(page, int(platform_type), cancel_event)
            if wait_result == "cancelled":
                status_queue.put("CANCELLED")
                return None
            if wait_result == "denied":
                if platform_type == 9:
                    status_queue.put("ERROR:facebook_page_business_access_denied")
                else:
                    status_queue.put(
                        "ERROR: Meta Business Suite 尚不可用。请先建立 Facebook Page，并把 Instagram 切换为专业账号后连接到该 Page。"
                    )
                status_queue.put("500")
                return None
            if wait_result != "ready":
                status_queue.put("ERROR: 等待平台登录超时，未保存任何登录数据。")
                status_queue.put("500")
                return None

            if platform_type == 8:
                try:
                    instagram_identity = await read_confirmed_instagram_identity(page)
                except InstagramIdentityError as exc:
                    status_queue.put(f"ERROR:{exc.error_code}")
                    status_queue.put("500")
                    return None
                if cancel_event is not None and cancel_event.is_set():
                    status_queue.put("CANCELLED")
                    return None
                status_queue.put(
                    "Instagram 专业账号主体、账号类型和关联 Page 已完成双页面回读。"
                )

            facebook_identity = None
            if platform_type == 9:
                expected_page_id = str(
                    (expected_account or {}).get("accountReference") or ""
                ).strip()
                try:
                    facebook_identity = await select_facebook_page_for_login(
                        page,
                        selection_callback=selection_callback,
                        expected_page_id=expected_page_id or None,
                    )
                except asyncio.CancelledError:
                    status_queue.put("CANCELLED")
                    return None
                except FacebookPagePublishError as exc:
                    _mark_failed_facebook_page_update(
                        update_mode=bool(update_mode),
                        record_id=record_id,
                        expected_account=expected_account,
                        error=exc,
                    )
                    status_queue.put(f"ERROR:{exc.error_code}")
                    status_queue.put("500")
                    return None
                if cancel_event is not None and cancel_event.is_set():
                    status_queue.put("CANCELLED")
                    return None

            status_queue.put("SCAN_CONFIRMED")
            identity = uuid.uuid4()
            cookie_file = f"{identity}.json"
            cookie_path = Path(BASE_DIR / "cookiesFile" / cookie_file)
            await save_context_storage_state(
                context,
                cookie_path,
                include_indexed_db=platform_type in (7, 8, 9),
            )
            os.chmod(cookie_path, 0o600)

            if platform_type == 9:
                try:
                    checked_identity = await check_cookie(
                        9,
                        cookie_file,
                        account_reference=facebook_identity.page_id,
                    )
                    if not isinstance(checked_identity, FacebookPageIdentity):
                        raise FacebookPagePublishError(
                            "facebook_page_identity_mismatch",
                            "保存的 Facebook Page 无法精确回读，已停止操作。",
                        )
                    resolve_facebook_page_selection(
                        (checked_identity,),
                        facebook_identity.page_id,
                    )
                except FacebookPagePublishError as exc:
                    _mark_failed_facebook_page_update(
                        update_mode=bool(update_mode),
                        record_id=record_id,
                        expected_account=expected_account,
                        error=exc,
                    )
                    cookie_path.unlink(missing_ok=True)
                    status_queue.put(f"ERROR:{exc.error_code}")
                    status_queue.put("500")
                    return None
                checks_ok = True
            elif platform_type == 8:
                checks_ok = True
            else:
                checks_ok = bool(await check_cookie(platform_type, cookie_file))
            if not checks_ok:
                cookie_path.unlink(missing_ok=True)
                status_queue.put("ERROR: 登录页面已完成，但平台发布权限检查未通过。")
                status_queue.put("500")
                return None

            tiktok_identity = None
            if platform_type == 6:
                try:
                    tiktok_identity = await read_tiktok_identity(page)
                    if update_mode:
                        tiktok_previous_account = _load_tiktok_account_snapshot(
                            record_id
                        )
                        validate_identity_binding(
                            tiktok_previous_account,
                            tiktok_identity,
                            allow_initial_bind=True,
                        )
                except TikTokIdentityError as exc:
                    cookie_path.unlink(missing_ok=True)
                    status_queue.put(
                        f"ERROR: TikTok 账号身份核对失败（{exc.error_code}）。"
                    )
                    status_queue.put("500")
                    return None

            if platform_type == 9:
                avatar_path, display_name = None, facebook_identity.page_name
            else:
                avatar_path, display_name = await capture_login_identity(
                    page,
                    platform_type,
                    identity,
                )
            if platform_type == 8:
                if instagram_identity is None:
                    raise InstagramIdentityError(
                        "instagram_identity_unavailable",
                        "Instagram 页面没有返回稳定主体，已停止保存。",
                    )
                try:
                    account_id = save_confirmed_instagram_account(
                        cookie_file,
                        identity=instagram_identity,
                        avatar_path=avatar_path,
                        record_id=record_id if update_mode else None,
                    )
                except InstagramIdentityError as exc:
                    status_queue.put(f"ERROR:{exc.error_code}")
                    status_queue.put("500")
                    return None
                instagram_login_succeeded = True
                status_queue.put(f"ACCOUNT_ID:{account_id}")
            elif platform_type == 9:
                if cancel_event is not None and cancel_event.is_set():
                    cookie_path.unlink(missing_ok=True)
                    status_queue.put("CANCELLED")
                    return None
                try:
                    account_id = account_service.save_facebook_page_browser_account(
                        profile_name=profile_name,
                        storage_file_name=cookie_file,
                        identity=facebook_identity,
                        record_id=record_id if update_mode else None,
                        expected_account=expected_account if update_mode else None,
                    )
                except FacebookPagePublishError as exc:
                    _mark_failed_facebook_page_update(
                        update_mode=bool(update_mode),
                        record_id=record_id,
                        expected_account=expected_account,
                        error=exc,
                    )
                    cookie_path.unlink(missing_ok=True)
                    status_queue.put(f"ERROR:{exc.error_code}")
                    status_queue.put("500")
                    return None
                if update_mode and expected_account:
                    _remove_replaced_facebook_page_session(
                        dict(expected_account),
                        current_cookie=cookie_file,
                    )
                facebook_page_login_succeeded = True
                status_queue.put(f"ACCOUNT_ID:{account_id}")
            else:
                account_id = None
                try:
                    tiktok_account_write_attempted = platform_type == 6
                    if platform_type == 6 and update_mode:
                        if tiktok_previous_account is None or tiktok_identity is None:
                            raise TikTokIdentityError(
                                "tiktok_account_invalid",
                                "TikTok 账号记录未能安全更新",
                            )
                        account_id = _save_tiktok_update_if_unchanged(
                            tiktok_previous_account,
                            cookie_file=cookie_file,
                            profile_name=profile_name,
                            avatar_path=avatar_path,
                            display_name=display_name,
                            account_reference=tiktok_identity.handle,
                        )
                    else:
                        account_id = save_login_account(
                            platform_type,
                            cookie_file,
                            profile_name,
                            update_mode,
                            record_id,
                            avatar_path,
                            display_name,
                        )
                    if platform_type == 6:
                        if not account_id or tiktok_identity is None:
                            raise TikTokIdentityError(
                                "tiktok_account_invalid",
                                "TikTok 账号记录未能安全保存",
                            )
                        if not update_mode:
                            persist_tiktok_identity(
                                account_id,
                                tiktok_identity,
                                allow_initial_bind=True,
                            )
                        tiktok_login_succeeded = True
                        if update_mode and tiktok_previous_account:
                            _remove_replaced_tiktok_artifacts(
                                tiktok_previous_account,
                                current_cookie=cookie_file,
                                current_avatar=avatar_path,
                            )
                except TikTokIdentityError as exc:
                    if platform_type != 6:
                        raise
                    status_queue.put(
                        f"ERROR: TikTok 账号身份绑定失败（{exc.error_code}）。"
                    )
                    status_queue.put("500")
                    return None
                if account_id:
                    status_queue.put(f"ACCOUNT_ID:{account_id}")
            status_queue.put("200")
            return cookie_file
        except Exception:
            if platform_type != 6:
                raise
            status_queue.put(
                "ERROR: TikTok 账号保存失败（tiktok_account_invalid）。"
            )
            status_queue.put("500")
            return None
        finally:
            try:
                if (
                    platform_type == 9
                    and cookie_path is not None
                    and not facebook_page_login_succeeded
                ):
                    cookie_path.unlink(missing_ok=True)
                if (
                    platform_type == 8
                    and cookie_path is not None
                    and not instagram_login_succeeded
                ):
                    cookie_path.unlink(missing_ok=True)
                    if avatar_path:
                        try:
                            (
                                Path(BASE_DIR / "avatars")
                                / Path(str(avatar_path)).name
                            ).unlink(missing_ok=True)
                        except OSError:
                            pass
                if (
                    platform_type == 6
                    and cookie_path is not None
                    and not tiktok_login_succeeded
                ):
                    try:
                        _discard_failed_tiktok_login(
                            account_id,
                            update_mode=bool(update_mode),
                            cookie_path=cookie_path,
                            avatar_path=avatar_path,
                            previous_account=tiktok_previous_account,
                            account_write_attempted=tiktok_account_write_attempted,
                        )
                    except Exception:
                        # 登录主错误保持稳定；仍继续关闭浏览器资源。
                        pass
            finally:
                await close_login_resources(page, context, browser)


async def tiktok_cookie_gen(id, status_queue, update_mode=False, record_id=None, cancel_event=None, background_mode=False):
    return await _browser_cookie_gen(6, id, status_queue, update_mode, record_id, cancel_event, background_mode)


async def youtube_cookie_gen(id, status_queue, update_mode=False, record_id=None, cancel_event=None, background_mode=False):
    return await _browser_cookie_gen(7, id, status_queue, update_mode, record_id, cancel_event, background_mode)


async def meta_cookie_gen(id, status_queue, update_mode=False, record_id=None, cancel_event=None, background_mode=False):
    return await _browser_cookie_gen(8, id, status_queue, update_mode, record_id, cancel_event, background_mode)


async def facebook_page_cookie_gen(
    id,
    status_queue,
    update_mode=False,
    record_id=None,
    cancel_event=None,
    background_mode=False,
    *,
    selection_callback=None,
    expected_account=None,
):
    return await _browser_cookie_gen(
        9,
        id,
        status_queue,
        update_mode,
        record_id,
        cancel_event,
        background_mode,
        selection_callback=selection_callback,
        expected_account=expected_account,
    )

# 抖音登录
async def douyin_cookie_gen(
    id,
    status_queue,
    update_mode=False,
    record_id=None,
    cancel_event=None,
    background_mode=False,
):
    async with async_playwright() as playwright:
        options = {
            'headless': False
        }
        browser = await launch_login_browser(playwright, background_mode=background_mode, **options)
        context = await new_login_context(browser)
        context = await set_init_script(context)
        page = await context.new_page()
        cookie_path = None
        try:
            await page.goto(
                DOUYIN_CREATOR_HOME_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            img_locator = page.get_by_role("img", name="二维码")
            src = await img_locator.get_attribute("src")
            print("[OK] 图片地址:", src)
            status_queue.put(src)

            wait_result = await _wait_for_douyin_login(
                page,
                context,
                cancel_event,
            )
            if wait_result == "cancelled":
                status_queue.put("CANCELLED")
                return None
            if wait_result != "logged_in":
                status_queue.put(
                    "ERROR: 等待抖音扫码确认超时，未保存任何登录数据。"
                )
                status_queue.put("500")
                return None

            status_queue.put("SCAN_CONFIRMED")
            await page.wait_for_timeout(1500)
            uuid_v1 = uuid.uuid1()
            cookie_file = f"{uuid_v1}.json"
            cookie_path = Path(BASE_DIR / "cookiesFile" / cookie_file)
            print(f"UUID v1: {uuid_v1}")
            await save_context_storage_state(context, cookie_path)
            result = await check_cookie(3, cookie_file)
            if not result:
                try:
                    await page.goto(
                        DOUYIN_UPLOAD_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await page.wait_for_timeout(2500)
                    await save_context_storage_state(context, cookie_path)
                    result = await check_cookie(3, cookie_file)
                except Exception:
                    result = False
            if not result:
                cookie_path.unlink(missing_ok=True)
                status_queue.put(
                    "ERROR: 扫码已确认，但抖音发布权限检查未通过，请重新登录。"
                )
                status_queue.put("500")
                return None

            avatar_path, display_name = await capture_login_identity(
                page,
                3,
                uuid_v1,
            )
            account_id = save_login_account(
                3,
                cookie_file,
                id,
                update_mode,
                record_id,
                avatar_path,
                display_name,
            )
            if account_id:
                status_queue.put(f"ACCOUNT_ID:{account_id}")
            status_queue.put("200")
            return cookie_file
        finally:
            await close_login_resources(page, context, browser)


def _tencent_qr_candidate_score(src: str | None, box: dict | None) -> float:
    """筛掉头像和图标，仅保留尺寸合理的方形二维码候选。"""

    if not box:
        return 0
    width = float(box.get("width") or 0)
    height = float(box.get("height") or 0)
    if min(width, height) < 120 or max(width, height) > 420:
        return 0
    if abs(width - height) > max(width, height) * 0.08:
        return 0
    source = (src or "").lower()
    if any(word in source for word in ("avatar", "logo", "icon", "default", "head")):
        return 0
    keyword_bonus = (
        1_000_000
        if any(word in source for word in ("qrcode", "qr_code", "qr-code", "loginqr"))
        else 0
    )
    return keyword_bonus + width * height


def _qr_candidate_area(box: dict | None) -> float:
    if not box:
        return 0
    return float(box.get("width") or 0) * float(box.get("height") or 0)


async def _locator_png_data_url(locator) -> str | None:
    try:
        data = await locator.screenshot(type="png")
    except Exception:
        return None
    if not data:
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _dominant_qr_span(counts: list[int], minimum_count: int) -> tuple[int, int] | None:
    """返回投影中最宽的连续二维码主体，忽略两侧孤立装饰线。"""

    runs = []
    start = None
    for index, count in enumerate([*counts, 0]):
        if count >= minimum_count and start is None:
            start = index
        elif count < minimum_count and start is not None:
            runs.append((start, index))
            start = None
    if not runs:
        return None
    return max(runs, key=lambda item: item[1] - item[0])


def _trim_tencent_qr_decorations(png_data: bytes) -> bytes:
    """裁掉视频号二维码截图外围的角标和竖线，同时保留安全白边。"""

    try:
        source = Image.open(BytesIO(png_data)).convert("RGBA")
    except Exception:
        return png_data
    width, height = source.size
    if min(width, height) < 80:
        return png_data

    pixels = source.load()
    column_counts = [0] * width
    row_counts = [0] * height
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            if alpha < 64:
                continue
            luminance = red * 0.299 + green * 0.587 + blue * 0.114
            if luminance < 110:
                column_counts[x] += 1
                row_counts[y] += 1

    x_span = _dominant_qr_span(column_counts, max(3, round(height * 0.035)))
    y_span = _dominant_qr_span(row_counts, max(3, round(width * 0.035)))
    if not x_span or not y_span:
        return png_data

    body_width = x_span[1] - x_span[0]
    body_height = y_span[1] - y_span[0]
    if (
        body_width < width * 0.45
        or body_height < height * 0.45
        or abs(body_width - body_height) > max(body_width, body_height) * 0.15
    ):
        return png_data

    padding = max(4, round(max(body_width, body_height) * 0.045))
    left = max(0, x_span[0] - padding)
    top = max(0, y_span[0] - padding)
    right = min(width, x_span[1] + padding)
    bottom = min(height, y_span[1] + padding)
    if (left, top, right, bottom) == (0, 0, width, height):
        return png_data

    cropped = source.crop((left, top, right, bottom))
    side = max(cropped.size)
    cleaned = Image.new("RGBA", (side, side), "white")
    cleaned.alpha_composite(
        cropped,
        ((side - cropped.width) // 2, (side - cropped.height) // 2),
    )
    output = BytesIO()
    cleaned.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


async def _tencent_locator_png_data_url(locator) -> str | None:
    try:
        data = await locator.screenshot(type="png")
    except Exception:
        return None
    if not data:
        return None
    cleaned = _trim_tencent_qr_decorations(data)
    return "data:image/png;base64," + base64.b64encode(cleaned).decode("ascii")


async def _qr_visual_source(locator) -> str:
    """组合可视元素的二维码特征，供备用候选排序。"""

    values = []
    for attribute in ("src", "class", "alt"):
        try:
            value = await locator.get_attribute(attribute)
        except Exception:
            value = None
        if value:
            values.append(value)
    return " ".join(values)


async def _smallest_qr_visual_descendant(locator):
    """从二维码容器中取最内层图像，避免把周边页面一起截入。"""

    candidates = []
    descendants = locator.locator("img:visible, canvas:visible, svg:visible")
    for index in range(min(await descendants.count(), 8)):
        descendant = descendants.nth(index)
        try:
            box = await descendant.bounding_box()
        except Exception:
            continue
        source = await _qr_visual_source(descendant)
        if _tencent_qr_candidate_score(source or "qrcode", box):
            candidates.append((_qr_candidate_area(box), descendant))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


async def _tencent_qr_source(page, *, switch_to_qr: bool = True) -> str | None:
    """优先切换到二维码登录，并截取真正的二维码画面。"""

    await page.locator("iframe").first.wait_for(state="visible", timeout=60000)
    scopes = [frame for frame in page.frames if frame is not page.main_frame]
    scopes.append(page.main_frame)

    if switch_to_qr:
        for scope in scopes:
            try:
                normal_login = scope.locator(".js_switchToNormal:visible").first
                if await normal_login.count():
                    await normal_login.click()
                    await page.wait_for_timeout(900)
                    break
            except Exception:
                pass
            for text in (
                "使用其他头像、昵称或账号",
                "扫码登录",
                "使用微信扫码",
                "二维码登录",
            ):
                try:
                    switch = scope.get_by_text(text, exact=False).first
                    if await switch.count() and await switch.is_visible():
                        await switch.click()
                        await page.wait_for_timeout(700)
                        break
                except Exception:
                    continue

    for scope in scopes:
        qr_images = scope.locator(
            "img.js_qrcode_img:visible, img.web_qrcode_img:visible, "
            'img[alt*="二维码"]:visible, img[alt*="QR" i]:visible, '
            'canvas[class*="qrcode" i]:visible, svg[class*="qrcode" i]:visible'
        )
        for index in range(await qr_images.count()):
            qr_image = qr_images.nth(index)
            try:
                box = await qr_image.bounding_box()
            except Exception:
                continue
            if not _tencent_qr_candidate_score("qrcode", box):
                continue
            data_url = await _tencent_locator_png_data_url(qr_image)
            if data_url:
                return data_url

    visual_candidates = []
    container_candidates = []
    for scope in scopes:
        visuals = scope.locator("img, canvas, svg")
        for index in range(min(await visuals.count(), 40)):
            visual = visuals.nth(index)
            try:
                if not await visual.is_visible():
                    continue
                box = await visual.bounding_box()
            except Exception:
                continue
            source = await _qr_visual_source(visual)
            score = _tencent_qr_candidate_score(source, box)
            if score:
                visual_candidates.append((score, _qr_candidate_area(box), visual))

        for selector in (
            '[class*="qrcode" i]',
            '[class*="qr-code" i]',
            '[class*="qr_code" i]',
        ):
            locators = scope.locator(selector)
            for index in range(min(await locators.count(), 8)):
                locator = locators.nth(index)
                try:
                    if not await locator.is_visible():
                        continue
                    box = await locator.bounding_box()
                except Exception:
                    continue
                score = _tencent_qr_candidate_score("qrcode", box)
                if score:
                    container_candidates.append((_qr_candidate_area(box), locator))

    keyword_visuals = [item for item in visual_candidates if item[0] >= 1_000_000]
    generic_visuals = [item for item in visual_candidates if item[0] < 1_000_000]
    for _score, _area, locator in sorted(keyword_visuals, key=lambda item: item[1]):
        data_url = await _tencent_locator_png_data_url(locator)
        if data_url:
            return data_url

    for _area, container in sorted(container_candidates, key=lambda item: item[0]):
        locator = await _smallest_qr_visual_descendant(container) or container
        data_url = await _tencent_locator_png_data_url(locator)
        if data_url:
            return data_url

    for _score, _area, locator in sorted(generic_visuals, key=lambda item: item[1]):
        data_url = await _tencent_locator_png_data_url(locator)
        if data_url:
            return data_url
    return None


async def _bilibili_qr_source(page) -> str | None:
    """精确截取 B 站登录二维码，不回退到页面第一张图片。"""

    selectors = (
        '.login-scan__qrcode img[alt="Scan me!"]',
        ".login-scan__qrcode img",
        'img[alt="Scan me!"]',
        'img[src*="qrcode" i]',
        'img[class*="qrcode" i]',
        'canvas[class*="qrcode" i]',
    )
    for _attempt in range(30):
        for selector in selectors:
            locators = page.locator(selector)
            for index in range(min(await locators.count(), 4)):
                locator = locators.nth(index)
                try:
                    if not await locator.is_visible():
                        continue
                    box = await locator.bounding_box()
                except Exception:
                    continue
                if not _tencent_qr_candidate_score("qrcode", box):
                    continue
                data_url = await _locator_png_data_url(locator)
                if data_url:
                    return data_url
        await page.wait_for_timeout(350)
    return None


TENCENT_BACKEND_URL = "https://channels.weixin.qq.com/platform/post/create"
TENCENT_READY_MARKERS = ("内容管理", "动态管理", "数据中心", "创作中心", "发表视频")


def _is_tencent_oauth_callback_url(url: str | None) -> bool:
    value = (url or "").lower()
    return "channels.weixin.qq.com/platform/oauth-callback" in value


def _is_tencent_backend_state(url: str | None, text: str, has_login_frame: bool) -> bool:
    value = (url or "").lower()
    return (
        "/platform" in value
        and "oauth-callback" not in value
        and not has_login_frame
        and any(marker in text for marker in TENCENT_READY_MARKERS)
    )


async def _tencent_page_ready(page) -> bool:
    has_login_frame = False
    try:
        login_frames = page.locator(
            'iframe[src*="open.weixin.qq.com"], '
            'iframe[src*="qrconnect"], '
            'iframe[src*="login"]'
        )
        for index in range(await login_frames.count()):
            if await login_frames.nth(index).is_visible():
                has_login_frame = True
                break
    except Exception:
        pass
    return _is_tencent_backend_state(
        page.url,
        await _body_text(page),
        has_login_frame,
    )


async def _tencent_authorization_signal(page) -> bool:
    for frame in page.frames:
        if _is_tencent_oauth_callback_url(frame.url):
            return True
        if frame is page.main_frame:
            continue
        try:
            text = (
                await frame.locator("body").inner_text(timeout=600)
            ).lower()
        except Exception:
            continue
        if any(marker in text for marker in ("登录成功", "已确认")):
            return True
    return False


async def _wait_for_tencent_backend(
    page,
    cancel_event=None,
    timeout=240,
    oauth_callback_event=None,
) -> str:
    """等待授权回调；真正的登录有效性在保存会话后单独验证。"""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        if await _tencent_page_ready(page):
            return "logged_in"

        callback_seen = bool(
            oauth_callback_event is not None and oauth_callback_event.is_set()
        )
        if callback_seen or await _tencent_authorization_signal(page):
            return "logged_in"
        await asyncio.sleep(0.5)
    return "timeout"


# 视频号登录
async def get_tencent_cookie(id,status_queue, update_mode=False, record_id=None, cancel_event=None, background_mode=False):
    async with async_playwright() as playwright:
        options = {
            'args': [
                '--lang en-GB'
            ],
            'headless': False,  # Set headless option here
        }
        # Make sure to run headed.
        browser = await launch_login_browser(playwright, background_mode=background_mode, **options)
        # Setup context however you like.
        context = await new_login_context(browser)
        # Pause the page, and start recording manually.
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            oauth_callback_event = asyncio.Event()

            def observe_tencent_frame(frame):
                if _is_tencent_oauth_callback_url(frame.url):
                    oauth_callback_event.set()

            page.on("framenavigated", observe_tencent_frame)
            await page.goto("https://channels.weixin.qq.com", wait_until="domcontentloaded", timeout=60000)
            src = await _tencent_qr_source(page, switch_to_qr=True)
            if not src:
                status_queue.put("ERROR: 未找到视频号登录二维码，请稍后重试。")
                status_queue.put("500")
                return None
            status_queue.put(src)

            wait_result = await _wait_for_tencent_backend(
                page,
                cancel_event,
                oauth_callback_event=oauth_callback_event,
            )
            if wait_result == "cancelled":
                status_queue.put("CANCELLED")
                return None
            if wait_result != "logged_in":
                status_queue.put("ERROR: 等待视频号后台登录超时，未保存登录数据。")
                status_queue.put("500")
                return None

            status_queue.put('SCAN_CONFIRMED')
            await page.wait_for_timeout(1800)
            uuid_v1 = uuid.uuid1()
            cookie_file = f"{uuid_v1}.json"
            cookie_path = Path(BASE_DIR / "cookiesFile" / cookie_file)
            print(f"UUID v1: {uuid_v1}")
            await save_context_storage_state(
                context,
                cookie_path,
                include_indexed_db=True,
            )
            result = await check_cookie(2, cookie_file)
            if not result:
                try:
                    await page.goto(
                        TENCENT_BACKEND_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await page.wait_for_timeout(2500)
                    await save_context_storage_state(
                        context,
                        cookie_path,
                        include_indexed_db=True,
                    )
                    result = await check_cookie(2, cookie_file)
                except Exception:
                    result = False
                if not result:
                    cookie_path.unlink(missing_ok=True)
                    status_queue.put("ERROR: 视频号网页授权未完成，请使用二维码重新扫码。")
                    status_queue.put("500")
                    return None
            avatar_path, display_name = await capture_login_identity(page, 2, uuid_v1)
            account_id = save_login_account(2, cookie_file, id, update_mode, record_id, avatar_path, display_name)
            if account_id:
                status_queue.put(f"ACCOUNT_ID:{account_id}")
            status_queue.put("200")
            return cookie_file
        finally:
            await close_login_resources(page, context, browser)

# 快手登录
async def get_ks_cookie(id,status_queue, update_mode=False, record_id=None, cancel_event=None, background_mode=False):
    url_changed_event = asyncio.Event()
    async def on_url_change():
        # 检查是否是主框架的变化
        if page.url != original_url:
            url_changed_event.set()
    async with async_playwright() as playwright:
        options = {
            'args': [
                '--lang en-GB'
            ],
            'headless': False,  # Set headless option here
        }
        # Make sure to run headed.
        browser = await launch_login_browser(playwright, background_mode=background_mode, **options)
        # Setup context however you like.
        context = await new_login_context(browser)
        context = await set_init_script(context)
        # Pause the page, and start recording manually.
        page = await context.new_page()
        await page.goto("https://cp.kuaishou.com")

        # 定位并点击“立即登录”按钮（类型为 link）
        await page.get_by_role("link", name="立即登录").click()
        await page.get_by_text("扫码登录").click()
        img_locator = page.get_by_role("img", name="qrcode")
        # 获取 src 属性值
        src = await img_locator.get_attribute("src")
        original_url = page.url
        print("[OK] 图片地址:", src)
        status_queue.put(src)
        # 监听页面的 'framenavigated' 事件，只关注主框架的变化
        page.on('framenavigated',
                lambda frame: asyncio.create_task(on_url_change()) if frame == page.main_frame else None)

        wait_result = await wait_for_login_or_cancel(url_changed_event, cancel_event)
        if wait_result == "logged_in":
            print("监听页面跳转成功")
        elif wait_result == "cancelled":
            await finish_cancelled_login(status_queue, page, context, browser)
            return None
        else:
            status_queue.put("500")
            print("监听页面跳转超时")
            await page.close()
            await context.close()
            await browser.close()
            return None
        status_queue.put('SCAN_CONFIRMED')
        uuid_v1 = uuid.uuid1()
        cookie_file = f"{uuid_v1}.json"
        print(f"UUID v1: {uuid_v1}")
        await save_context_storage_state(context, Path(BASE_DIR / "cookiesFile" / cookie_file))
        result = await check_cookie(4, cookie_file)
        if not result:
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None
        avatar_path, display_name = await capture_login_identity(page, 4, uuid_v1)
        await page.close()
        await context.close()
        await browser.close()
        account_id = save_login_account(4, cookie_file, id, update_mode, record_id, avatar_path, display_name)
        if account_id:
            status_queue.put(f"ACCOUNT_ID:{account_id}")
        status_queue.put("200")

# 小红书登录
async def xiaohongshu_cookie_gen(id,status_queue, update_mode=False, record_id=None, cancel_event=None, background_mode=False):
    url_changed_event = asyncio.Event()

    async def on_url_change():
        # 检查是否是主框架的变化
        if page.url != original_url:
            url_changed_event.set()

    async with async_playwright() as playwright:
        options = {
            'args': [
                '--lang en-GB'
            ],
            'headless': False,  # Set headless option here
        }
        # Make sure to run headed.
        browser = await launch_login_browser(playwright, background_mode=background_mode, **options)
        # Setup context however you like.
        context = await new_login_context(browser)
        context = await set_init_script(context)
        # Pause the page, and start recording manually.
        page = await context.new_page()
        await page.goto("https://creator.xiaohongshu.com/")
        await page.locator('img.css-wemwzq').click()

        img_locator = page.get_by_role("img").nth(2)
        # 获取 src 属性值
        src = await img_locator.get_attribute("src")
        original_url = page.url
        print("[OK] 图片地址:", src)
        status_queue.put(src)
        # 监听页面的 'framenavigated' 事件，只关注主框架的变化
        page.on('framenavigated',
                lambda frame: asyncio.create_task(on_url_change()) if frame == page.main_frame else None)

        wait_result = await wait_for_login_or_cancel(url_changed_event, cancel_event)
        if wait_result == "logged_in":
            print("监听页面跳转成功")
        elif wait_result == "cancelled":
            await finish_cancelled_login(status_queue, page, context, browser)
            return None
        else:
            status_queue.put("500")
            print("监听页面跳转超时")
            await page.close()
            await context.close()
            await browser.close()
            return None
        status_queue.put('SCAN_CONFIRMED')
        uuid_v1 = uuid.uuid1()
        cookie_file = f"{uuid_v1}.json"
        print(f"UUID v1: {uuid_v1}")
        await save_context_storage_state(context, Path(BASE_DIR / "cookiesFile" / cookie_file))
        result = await check_cookie(1, cookie_file)
        if not result:
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None
        avatar_path, display_name = await capture_login_identity(page, 1, uuid_v1)
        await page.close()
        await context.close()
        await browser.close()
        account_id = save_login_account(1, cookie_file, id, update_mode, record_id, avatar_path, display_name)
        if account_id:
            status_queue.put(f"ACCOUNT_ID:{account_id}")
        status_queue.put("200")

# a = asyncio.run(xiaohongshu_cookie_gen(4,None))
# print(a)

# B站登录
async def bilibili_cookie_gen(id, status_queue, update_mode=False, record_id=None, cancel_event=None, background_mode=False):
    from utils.log import bilibili_logger

    bilibili_logger.info(f"[bilibili_login] 开始B站登录流程，用户ID: {id}")

    url_changed_event = asyncio.Event()

    async def on_url_change():
        bilibili_logger.info(f"[bilibili_login] 页面URL变化: {page.url}")
        if page.url != original_url:
            bilibili_logger.info(f"[bilibili_login] 检测到URL变化，原始URL: {original_url}, 新URL: {page.url}")
            url_changed_event.set()

    async with async_playwright() as playwright:
        options = {
            'headless': False
        }
        bilibili_logger.info("[bilibili_login] 启动浏览器...")
        browser = await launch_login_browser(playwright, background_mode=background_mode, **options)
        context = await new_login_context(browser)
        context = await set_init_script(context)
        page = await context.new_page()

        bilibili_logger.info("[bilibili_login] 导航到B站上传页面...")
        await page.goto("https://member.bilibili.com/platform/upload/video/frame")
        original_url = page.url
        bilibili_logger.info(f"[bilibili_login] 初始页面URL: {original_url}")

        # 精确抓取二维码，避免选到登录页的其他图片。
        bilibili_logger.info("[bilibili_login] 尝试查找二维码...")
        try:
            qr_source = await _bilibili_qr_source(page)
            if qr_source:
                bilibili_logger.info("[bilibili_login] 已精确截取二维码")
                status_queue.put(qr_source)
            else:
                bilibili_logger.error("[bilibili_login] 未找到可用的登录二维码")
                status_queue.put("ERROR: B站登录二维码获取失败，请稍后重试。")
                status_queue.put("500")
                await page.close()
                await context.close()
                await browser.close()
                return None
        except Exception as e:
            bilibili_logger.error(f"[bilibili_login] 查找二维码时出错: {e}")
            status_queue.put(f"ERROR: B站登录二维码获取失败：{e}")
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None

        bilibili_logger.info("[bilibili_login] 等待用户扫码登录...")
        page.on('framenavigated',
                lambda frame: asyncio.create_task(on_url_change()) if frame == page.main_frame else None)
        wait_result = await wait_for_login_or_cancel(url_changed_event, cancel_event)
        if wait_result == "logged_in":
            bilibili_logger.info("[bilibili_login] 检测到页面跳转，登录可能成功")
        elif wait_result == "cancelled":
            bilibili_logger.info("[bilibili_login] 登录流程已取消")
            await finish_cancelled_login(status_queue, page, context, browser)
            return None
        else:
            bilibili_logger.error("[bilibili_login] 登录超时（200秒）")
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None

        bilibili_logger.info("[bilibili_login] 保存登录状态...")
        status_queue.put('SCAN_CONFIRMED')
        uuid_v1 = uuid.uuid1()
        cookie_file = f"{uuid_v1}.json"
        await save_context_storage_state(context, Path(BASE_DIR / "cookiesFile" / cookie_file))
        bilibili_logger.info(f"[bilibili_login] 登录状态已保存到: {cookie_file}")

        bilibili_logger.info("[bilibili_login] 验证cookie有效性...")
        result = await check_cookie(5, cookie_file)
        bilibili_logger.info(f"[bilibili_login] cookie验证结果: {result}")

        if not result:
            bilibili_logger.error("[bilibili_login] cookie验证失败，登录失败")
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None

        avatar_path, display_name = await capture_login_identity(page, 5, uuid_v1)
        bilibili_logger.info("[bilibili_login] 关闭浏览器...")
        await page.close()
        await context.close()
        await browser.close()

        bilibili_logger.info("[bilibili_login] 保存用户信息到数据库...")
        account_id = save_login_account(5, cookie_file, id, update_mode, record_id, avatar_path, display_name)
        if account_id:
            status_queue.put(f"ACCOUNT_ID:{account_id}")
        bilibili_logger.info("[OK] [bilibili_login] 用户状态已记录到数据库")

        bilibili_logger.info("[bilibili_login] B站登录流程完成，返回成功状态")
        status_queue.put("200")
