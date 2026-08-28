# -*- coding: utf-8 -*-
"""Stable, public TikTok account identity readback.

This module deliberately reads only a visible public profile link.  A display
name or avatar is useful UI context, but neither is a stable account binding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit


_HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PROFILE_PATH_PATTERN = re.compile(r"^/@([^/]+)$")
_PROFILE_LINK_SELECTOR = 'a[href*="/@"]'
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com"}


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


async def read_tiktok_identity(page) -> TikTokIdentity:
    """Read one stable, visible TikTok profile handle from a page.

    The page must expose one or more Playwright-compatible profile-link
    locators.  Invisible links are ignored.  Duplicate links for the same
    handle are harmless; conflicting visible handles are rejected instead of
    guessing which account is logged in.
    """

    try:
        links = page.locator(_PROFILE_LINK_SELECTOR)
        count = int(await links.count())
    except Exception as exc:
        raise TikTokIdentityError(
            "tiktok_account_invalid", "TikTok 页面没有返回稳定账号标识"
        ) from exc

    candidates: dict[str, tuple[object, str]] = {}
    for index in range(count):
        try:
            link = links.nth(index)
        except Exception:
            continue
        if not await _visible(link):
            continue
        handle = normalize_tiktok_handle(await _attribute(link, "href"))
        if not handle:
            continue
        candidates.setdefault(handle, (link, ""))

    if not candidates:
        raise TikTokIdentityError(
            "tiktok_account_invalid", "TikTok 页面没有返回稳定账号标识"
        )
    if len(candidates) > 1:
        raise TikTokIdentityError(
            "tiktok_account_identity_ambiguous", "TikTok 页面返回了多个冲突的账号标识"
        )

    handle, (link, _) = next(iter(candidates.items()))
    display_name = await _text(link)
    return TikTokIdentity(
        handle=handle,
        display_name=display_name,
        profile_url=f"https://www.tiktok.com/@{handle}",
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

    expected = normalize_tiktok_handle(account.get("accountReference"))
    if not expected and allow_initial_bind:
        return actual
    if not expected or expected != actual:
        raise TikTokIdentityError(
            "tiktok_account_identity_mismatch", "当前 TikTok 登录主体与已保存账号不一致"
        )
    return actual


__all__ = [
    "TikTokIdentity",
    "TikTokIdentityError",
    "normalize_tiktok_handle",
    "read_tiktok_identity",
    "validate_identity_binding",
]
