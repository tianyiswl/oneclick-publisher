# -*- coding: utf-8 -*-
"""Stable, public TikTok account identity readback.

This module reads public profile links only.  A display name or avatar is
useful UI context, but neither is a stable account binding.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright

from .database import connect
from .paths import COOKIE_DIR


_HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PROFILE_PATH_PATTERN = re.compile(r"^/@([^/]+)$")
_PROFILE_LINK_SELECTOR = 'a[href*="/@"]'
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com"}
_TIKTOK_STUDIO_URL = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
_AUTH_ROUTE_TOKENS = ("login", "challenge", "verify", "captcha", "security")


@dataclass(frozen=True, slots=True)
class TikTokIdentity:
    handle: str
    display_name: str
    profile_url: str


class TikTokIdentityError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(message)


def _valid_handle(value: str) -> str:
    candidate = value.strip().lstrip("@").lower()
    if not candidate or not _HANDLE_PATTERN.fullmatch(candidate):
        return ""
    return candidate


def normalize_tiktok_handle(value: object) -> str:
    """Return a lowercase TikTok handle from a handle or profile URL.

    Values that are not a plain handle, a ``/@handle`` path, or a TikTok
    profile URL normalize to the empty string.  No page/session data is
    accepted or persisted here.
    """

    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            raw = value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    else:
        raw = str(value)
    raw = raw.strip()
    if not raw:
        return ""

    if raw.startswith("https://") or raw.startswith("http://"):
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in _TIKTOK_HOSTS:
            return ""
        path = parsed.path.rstrip("/")
        match = _PROFILE_PATH_PATTERN.fullmatch(path)
        return _valid_handle(match.group(1)) if match else ""

    if raw.startswith("/@"):
        return _valid_handle(raw[2:].rstrip("/"))
    return _valid_handle(raw)


async def _call_async(method, *args, **kwargs):
    """Call a Playwright-like method while accepting minimal fake locators."""

    try:
        return await method(*args, **kwargs)
    except TypeError:
        if kwargs:
            return await method(*args)
        raise


async def _visible(locator) -> bool:
    try:
        return bool(await _call_async(locator.is_visible, timeout=1200))
    except Exception:
        return False


async def _attribute(locator, name: str) -> str | None:
    try:
        value = await _call_async(locator.get_attribute, name)
    except Exception:
        return None
    return value if isinstance(value, str) else None


async def _text(locator) -> str:
    try:
        value = await _call_async(locator.inner_text, timeout=1200)
    except Exception:
        return ""
    return " ".join(str(value or "").split())


def _is_tiktok_studio_route(page) -> bool:
    """Allow only an authenticated-looking Studio workspace route.

    The URL is classified locally and never retained or reported.  This is a
    narrow allowance for the Studio account shell, not proof of authentication.
    """

    try:
        parsed = urlsplit(str(getattr(page, "url", "")))
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host not in _TIKTOK_HOSTS:
        return False
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if len(segments) < 2 or segments[0] != "tiktokstudio":
        return False
    return not any(
        token in segment.lower()
        for segment in segments[1:]
        for token in _AUTH_ROUTE_TOKENS
    )


def _store_profile_candidate(
    candidates: dict[str, tuple[object, bool]],
    handle: str,
    link: object,
    *,
    visible: bool,
) -> None:
    """Keep one link per handle, preferring a visible link for UI context."""

    existing = candidates.get(handle)
    if existing is None or (visible and not existing[1]):
        candidates[handle] = (link, visible)


async def _profile_candidates(
    page,
) -> tuple[dict[str, tuple[object, bool]], dict[str, tuple[object, bool]]]:
    """Read profile-link handles once, without retaining page content."""

    try:
        links = page.locator(_PROFILE_LINK_SELECTOR)
        count = int(await links.count())
    except Exception as exc:
        raise TikTokIdentityError(
            "tiktok_account_invalid", "TikTok 页面没有返回稳定账号标识"
        ) from exc

    all_candidates: dict[str, tuple[object, bool]] = {}
    visible_candidates: dict[str, tuple[object, bool]] = {}
    for index in range(count):
        try:
            link = links.nth(index)
        except Exception:
            continue
        handle = normalize_tiktok_handle(await _attribute(link, "href"))
        if not handle:
            continue
        visible = await _visible(link)
        _store_profile_candidate(all_candidates, handle, link, visible=visible)
        if visible:
            _store_profile_candidate(visible_candidates, handle, link, visible=True)
    return all_candidates, visible_candidates


async def read_tiktok_identity(
    page,
    *,
    poll_seconds: float = 0.15,
    max_attempts: int = 5,
) -> TikTokIdentity:
    """Read one stable TikTok public profile handle from a page.

    Visible links are usable on all routes.  The dedicated TikTok Studio
    account shell may expose its sole public link in responsive/closed markup,
    so only that route may use one hidden link.  Conflicting handles always
    fail closed, rather than guessing which account is logged in.
    """

    attempts = max(2, int(max_attempts))
    interval = max(0.0, float(poll_seconds))
    previous_handle = ""
    for index in range(attempts):
        all_candidates, visible_candidates = await _profile_candidates(page)
        if len(all_candidates) > 1:
            raise TikTokIdentityError(
                "tiktok_account_identity_ambiguous",
                "TikTok 页面返回了多个冲突的账号标识",
            )
        candidates = (
            all_candidates if _is_tiktok_studio_route(page) else visible_candidates
        )
        if len(candidates) == 1:
            handle, (link, visible) = next(iter(candidates.items()))
            if handle == previous_handle:
                return TikTokIdentity(
                    handle=handle,
                    display_name=await _text(link) if visible else "",
                    profile_url=f"https://www.tiktok.com/@{handle}",
                )
            previous_handle = handle
        else:
            previous_handle = ""
        if index + 1 < attempts:
            await asyncio.sleep(interval)

    raise TikTokIdentityError(
        "tiktok_account_invalid", "TikTok 页面没有返回稳定账号标识"
    )


def validate_identity_binding(
    account: Mapping[str, Any],
    identity: TikTokIdentity,
    *,
    allow_initial_bind: bool,
) -> str:
    """Require the currently read handle to match the saved account binding."""

    actual = normalize_tiktok_handle(getattr(identity, "handle", ""))
    if not actual:
        raise TikTokIdentityError(
            "tiktok_account_invalid", "TikTok 页面没有返回稳定账号标识"
        )

    raw_reference = account.get("accountReference")
    if raw_reference is None:
        reference_text = ""
    elif isinstance(raw_reference, bytes):
        try:
            reference_text = raw_reference.decode("utf-8")
        except UnicodeDecodeError:
            reference_text = str(raw_reference)
    else:
        reference_text = str(raw_reference)
    expected = normalize_tiktok_handle(raw_reference)
    if not reference_text.strip() and allow_initial_bind:
        return actual
    if not expected or expected != actual:
        raise TikTokIdentityError(
            "tiktok_account_identity_mismatch", "当前 TikTok 登录主体与已保存账号不一致"
        )
    return actual


def persist_tiktok_identity(
    account_id: int,
    identity: TikTokIdentity,
    *,
    allow_initial_bind: bool,
) -> None:
    """Persist only a verified public handle on one TikTok browser row."""

    with connect() as conn:
        row = conn.execute(
            "SELECT id, type, accountReference FROM user_info WHERE id = ?",
            (int(account_id),),
        ).fetchone()
        if not row or int(row["type"] or 0) != 6:
            raise TikTokIdentityError(
                "tiktok_account_invalid", "TikTok 账号记录不存在或类型不正确"
            )
        handle = validate_identity_binding(
            dict(row),
            identity,
            allow_initial_bind=allow_initial_bind,
        )
        original_reference = row["accountReference"]
        if original_reference is None or not str(original_reference).strip():
            updated = conn.execute(
                """
                UPDATE user_info
                SET accountReference = ?, status = 1
                WHERE id = ? AND type = 6
                  AND (accountReference IS NULL OR TRIM(accountReference) = '')
                """,
                (handle, int(account_id)),
            )
        else:
            updated = conn.execute(
                """
                UPDATE user_info
                SET accountReference = ?
                WHERE id = ? AND type = 6 AND accountReference = ?
                """,
                (handle, int(account_id), original_reference),
            )
        if int(updated.rowcount or 0) == 1:
            return

        current = conn.execute(
            "SELECT id, type, accountReference FROM user_info WHERE id = ?",
            (int(account_id),),
        ).fetchone()
        if not current or int(current["type"] or 0) != 6:
            raise TikTokIdentityError(
                "tiktok_account_invalid", "TikTok 账号记录已变更"
            )
        validate_identity_binding(
            dict(current),
            identity,
            allow_initial_bind=False,
        )
        raise TikTokIdentityError(
            "tiktok_account_invalid", "TikTok 账号绑定在保存期间已变更"
        )


async def _validate_saved_tiktok_account_async(
    account: Mapping[str, Any],
) -> TikTokIdentity:
    state_file = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not state_file.is_file():
        raise TikTokIdentityError(
            "tiktok_session_missing", "TikTok 本地登录会话不存在"
        )

    playwright = None
    browser = None
    context = None
    page = None
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(state_file))
        page = await context.new_page()
        await page.goto(
            _TIKTOK_STUDIO_URL,
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        try:
            identity = await read_tiktok_identity(page)
        except TikTokIdentityError as exc:
            if exc.error_code == "tiktok_account_invalid":
                raise TikTokIdentityError(
                    "tiktok_session_expired", "TikTok 登录已失效"
                ) from exc
            raise
        persist_tiktok_identity(
            int(account.get("id") or 0),
            identity,
            allow_initial_bind=True,
        )
        return identity
    except TikTokIdentityError:
        raise
    except Exception as exc:
        raise TikTokIdentityError(
            "tiktok_session_expired", "TikTok 登录已失效"
        ) from exc
    finally:
        for resource in (page, context, browser):
            if resource is None:
                continue
            try:
                await resource.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass


def validate_saved_tiktok_account(account: Mapping[str, Any]) -> TikTokIdentity:
    """Silently verify one saved isolated session against its public handle."""

    import asyncio

    return asyncio.run(_validate_saved_tiktok_account_async(dict(account)))


__all__ = [
    "TikTokIdentity",
    "TikTokIdentityError",
    "normalize_tiktok_handle",
    "persist_tiktok_identity",
    "read_tiktok_identity",
    "validate_identity_binding",
    "validate_saved_tiktok_account",
]
