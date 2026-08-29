# -*- coding: utf-8 -*-
"""Stable, public TikTok account identity readback.

This module reads only TikTok's public username identity data.  A display name
or avatar is useful UI context, but neither is a stable account binding.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright

from .database import connect
from .overseas_tiktok_session_scope import (
    TikTokSessionScopeError,
    sanitize_tiktok_storage_state,
)
from .paths import COOKIE_DIR


_HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PROFILE_PATH_PATTERN = re.compile(r"^/@([^/]+)$")
_PROFILE_LINK_SELECTOR = 'a[href*="/@"]'
_PROFILE_NAVIGATION_SELECTOR = 'a[data-e2e="nav-profile"]'
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com"}
TIKTOK_IDENTITY_URL = "https://www.tiktok.com/"
_AUTH_ROUTE_TOKENS = ("login", "challenge", "verify", "captcha", "security")
_HOMEPAGE_CONTEXT_SETTLE_SECONDS = 75.0
_TIKTOK_IDENTITY_NAVIGATION_TIMEOUT_MS = 120_000
_SAVED_IDENTITY_MAX_CONTEXT_ATTEMPTS = 5
_PROFILE_NAVIGATION_MAX_ATTEMPTS = 200
_FEED_PROFILE_NAVIGATION_SETTLE_SECONDS = 90.0
_FEED_SHELL_PATHS = {"/foryou", "/following", "/explore"}
_APP_CONTEXT_USER_EVALUATOR = """
() => {
  const scripts = document.querySelectorAll('script#__UNIVERSAL_DATA_FOR_REHYDRATION__');
  if (scripts.length === 0) return { state: 'missing' };
  if (scripts.length !== 1) return { state: 'invalid' };
  try {
    const documentData = JSON.parse(scripts[0].textContent || '');
    const scope = documentData && documentData.__DEFAULT_SCOPE__;
    const appContext = scope && scope['webapp.app-context'];
    const user = appContext && appContext.user;
    if (!user || typeof user !== 'object') return { state: 'invalid' };
    const result = { state: 'ok' };
    if (Object.prototype.hasOwnProperty.call(user, 'uniqueId')) {
      if (typeof user.uniqueId === 'string') result.uniqueId = user.uniqueId;
    }
    if (Object.prototype.hasOwnProperty.call(user, 'unique_id')) {
      if (typeof user.unique_id === 'string') result.unique_id = user.unique_id;
    }
    return result;
  } catch (_error) {
    return { state: 'invalid' };
  }
}
"""


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


def _identity_invalid() -> TikTokIdentityError:
    return TikTokIdentityError(
        "tiktok_account_invalid", "TikTok 页面没有返回稳定账号标识"
    )


def parse_tiktok_app_context_handle(payload: object) -> str:
    """Validate only the two public username fields returned by page-side code."""

    if not isinstance(payload, dict):
        raise _identity_invalid()
    if payload.get("state") != "ok":
        raise _identity_invalid()

    handles: set[str] = set()
    for field in ("uniqueId", "unique_id"):
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str):
            continue
        handle = _valid_handle(value)
        if not handle:
            continue
        handles.add(handle)
    if len(handles) == 1:
        return next(iter(handles))
    if len(handles) > 1:
        raise TikTokIdentityError(
            "tiktok_account_identity_ambiguous",
            "TikTok 页面返回了多个冲突的账号标识",
        )
    raise _identity_invalid()


def _page_is_tiktok_auth_route(page) -> bool:
    """Classify an authentication route without retaining or reporting its URL."""

    try:
        parsed = urlsplit(str(getattr(page, "url", "")))
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return host in _TIKTOK_HOSTS and any(
        token in parsed.path.lower() for token in _AUTH_ROUTE_TOKENS
    )


def _is_tiktok_homepage_route(page) -> bool:
    """Return whether the page is the HTTPS TikTok homepage, ignoring query data."""

    try:
        parsed = urlsplit(str(getattr(page, "url", "")))
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme.lower() == "https"
        and host in _TIKTOK_HOSTS
        and parsed.path in {"", "/"}
    )


def _is_tiktok_feed_shell_route(page) -> bool:
    """Return whether TikTok redirected the signed-in user to a feed shell."""

    try:
        parsed = urlsplit(str(getattr(page, "url", "")))
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme.lower() == "https"
        and host in _TIKTOK_HOSTS
        and parsed.path.rstrip("/") in _FEED_SHELL_PATHS
    )


async def _homepage_app_context_handle(page) -> str:
    """Ask the page to return only its two public app-context username fields."""

    try:
        result = await page.evaluate(_APP_CONTEXT_USER_EVALUATOR)
    except Exception:
        return ""
    if not isinstance(result, dict):
        raise _identity_invalid()
    if result.get("state") == "missing":
        return ""
    return parse_tiktok_app_context_handle(result)


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


async def _homepage_profile_navigation_identity(
    page,
    *,
    poll_seconds: float,
) -> TikTokIdentity | None:
    """Read the signed-in handle through TikTok's unique Profile navigation.

    TikTok's current homepage no longer always exposes the app-context user.
    The one visible ``nav-profile`` control is account-scoped even when its
    href is an opaque route.  We therefore accept only one visible control,
    click it, and require the resulting TikTok profile URL to stabilize.
    Feed-author links never participate in this fallback.
    """

    try:
        controls = page.locator(_PROFILE_NAVIGATION_SELECTOR)
        if int(await controls.count()) != 1:
            return None
        control = controls.nth(0)
        if not await _visible(control):
            return None
        await _call_async(control.click)
    except Exception:
        return None

    interval = max(0.0, float(poll_seconds))
    previous_handle = ""
    for _ in range(_PROFILE_NAVIGATION_MAX_ATTEMPTS):
        if _page_is_tiktok_auth_route(page):
            raise _identity_invalid()
        handle = normalize_tiktok_handle(str(getattr(page, "url", "") or ""))
        if handle and handle == previous_handle:
            return TikTokIdentity(
                handle=handle,
                display_name="",
                profile_url=f"https://www.tiktok.com/@{handle}",
            )
        previous_handle = handle
        await asyncio.sleep(interval)
    raise _identity_invalid()


async def _feed_profile_navigation_identity(
    page,
    *,
    poll_seconds: float,
    settle_seconds: float = _FEED_PROFILE_NAVIGATION_SETTLE_SECONDS,
) -> TikTokIdentity | None:
    """Wait for the unique signed-in Profile control on a slow feed shell."""

    interval = max(0.0, float(poll_seconds))
    deadline = time.monotonic() + max(0.0, float(settle_seconds))
    attempts = 2 if interval <= 0.0 else int(settle_seconds / interval) + 2
    attempts = max(2, min(attempts, 2_000))
    for _ in range(attempts):
        identity = await _homepage_profile_navigation_identity(
            page,
            poll_seconds=interval,
        )
        if identity is not None:
            return identity
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        await asyncio.sleep(min(interval, remaining) if interval > 0.0 else 0.0)
    return None


async def read_tiktok_signed_in_navigation_identity(
    page,
    *,
    poll_seconds: float = 0.15,
    settle_seconds: float = _FEED_PROFILE_NAVIGATION_SETTLE_SECONDS,
) -> TikTokIdentity:
    """Read only the account-scoped Profile navigation, never page authors."""

    identity = await _feed_profile_navigation_identity(
        page,
        poll_seconds=poll_seconds,
        settle_seconds=settle_seconds,
    )
    if identity is None:
        raise _identity_invalid()
    return identity


async def read_tiktok_identity(
    page,
    *,
    poll_seconds: float = 0.15,
    max_attempts: int = 5,
    homepage_settle_seconds: float = _HOMEPAGE_CONTEXT_SETTLE_SECONDS,
) -> TikTokIdentity:
    """Read one stable TikTok public profile handle from a page.

    The HTTPS TikTok homepage uses only its fixed app-context username path;
    feed profile links never participate there. It polls that page-side
    extractor for a short bounded window because the script may appear before
    the document reports ``complete``. Legacy pages retain the narrow
    Studio-anchor fallback. Conflicting values always fail closed, rather than
    guessing which account is logged in.
    """

    attempts = max(2, int(max_attempts))
    interval = max(0.0, float(poll_seconds))
    settle_seconds = max(0.0, float(homepage_settle_seconds))
    homepage_deadline = time.monotonic() + settle_seconds
    previous_handle = ""
    completed_attempts = 0
    while True:
        if _page_is_tiktok_auth_route(page):
            raise _identity_invalid()
        is_homepage = _is_tiktok_homepage_route(page)
        is_feed_shell = _is_tiktok_feed_shell_route(page)
        if is_homepage and time.monotonic() >= homepage_deadline:
            break
        homepage_handle = (
            await _homepage_app_context_handle(page) if is_homepage else ""
        )
        if is_homepage and time.monotonic() >= homepage_deadline:
            break
        if homepage_handle:
            candidates = {homepage_handle: (None, False)}
        elif is_homepage:
            navigation_identity = await _homepage_profile_navigation_identity(
                page,
                poll_seconds=interval,
            )
            if navigation_identity is not None:
                return navigation_identity
            candidates = {}
        elif is_feed_shell:
            navigation_identity = await _feed_profile_navigation_identity(
                page,
                poll_seconds=interval,
            )
            if navigation_identity is not None:
                return navigation_identity
            raise _identity_invalid()
        else:
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
                    display_name=await _text(link) if visible and link is not None else "",
                    profile_url=f"https://www.tiktok.com/@{handle}",
                )
            previous_handle = handle
        else:
            previous_handle = ""
        completed_attempts += 1
        if not is_homepage:
            if completed_attempts >= attempts:
                break
            await asyncio.sleep(interval)
            continue
        if interval <= 0.0:
            if completed_attempts >= attempts:
                break
            await asyncio.sleep(0.0)
            continue
        remaining = homepage_deadline - time.monotonic()
        if remaining <= 0.0:
            break
        await asyncio.sleep(min(interval, remaining))

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


def _replace_saved_tiktok_state_file(
    state_file: Path,
    storage_state: Mapping[str, Any],
) -> None:
    """Atomically replace one existing TikTok-only state file."""

    temporary: Path | None = None
    try:
        if state_file.is_symlink() or state_file.parent.is_symlink():
            raise OSError("unsafe TikTok state path")
        sanitized = sanitize_tiktok_storage_state(storage_state)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=state_file.parent,
            prefix=f".{state_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(
                sanitized,
                output,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            output.flush()
            os.fsync(output.fileno())
        if os.name == "posix":
            temporary.chmod(0o600)
        os.replace(temporary, state_file)
        temporary = None
        if os.name == "posix":
            state_file.chmod(0o600)
    except (OSError, TikTokSessionScopeError, TypeError, ValueError) as exc:
        raise TikTokIdentityError(
            "tiktok_session_expired", "TikTok 登录已失效"
        ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


async def read_tiktok_public_profile(page, identity: TikTokIdentity):
    """Lazy bridge avoids a module cycle while keeping the reader patchable."""

    from .overseas_tiktok_profile import read_tiktok_public_profile as reader

    return await reader(page, identity)


def persist_tiktok_public_profile(account_id: int, profile) -> str:
    """Lazy bridge for the bound public-profile persistence step."""

    from .overseas_tiktok_profile import persist_tiktok_public_profile as persist

    return persist(account_id, profile)


async def _validate_saved_tiktok_account_async(
    account: Mapping[str, Any],
) -> TikTokIdentity:
    state_file = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if (
        state_file.is_symlink()
        or state_file.parent.is_symlink()
        or not state_file.is_file()
    ):
        raise TikTokIdentityError(
            "tiktok_session_missing", "TikTok 本地登录会话不存在"
        )

    playwright = None
    browser = None
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        for _ in range(_SAVED_IDENTITY_MAX_CONTEXT_ATTEMPTS):
            context = None
            page = None
            try:
                context = await browser.new_context(storage_state=str(state_file))
                page = await context.new_page()
                await page.goto(
                    TIKTOK_IDENTITY_URL,
                    wait_until="domcontentloaded",
                    timeout=_TIKTOK_IDENTITY_NAVIGATION_TIMEOUT_MS,
                )
                identity = await read_tiktok_identity(page)
            except TikTokIdentityError as exc:
                if exc.error_code != "tiktok_account_invalid":
                    raise
                continue
            except Exception:
                continue
            else:
                validate_identity_binding(
                    account,
                    identity,
                    allow_initial_bind=True,
                )
                public_profile = None
                try:
                    public_profile = await read_tiktok_public_profile(page, identity)
                except TikTokIdentityError as exc:
                    if exc.error_code == "tiktok_account_identity_mismatch":
                        raise
                if public_profile is not None:
                    identity = TikTokIdentity(
                        public_profile.handle,
                        public_profile.display_name,
                        f"https://www.tiktok.com/@{public_profile.handle}",
                    )
                refreshed_state = await context.storage_state()
                persist_tiktok_identity(
                    int(account.get("id") or 0),
                    identity,
                    allow_initial_bind=True,
                )
                if public_profile is not None:
                    try:
                        persist_tiktok_public_profile(
                            int(account.get("id") or 0),
                            public_profile,
                        )
                    except TikTokIdentityError as exc:
                        if exc.error_code in {
                            "tiktok_account_identity_mismatch",
                            "tiktok_account_invalid",
                        }:
                            raise
                _replace_saved_tiktok_state_file(state_file, refreshed_state)
                return identity
            finally:
                for resource in (page, context):
                    if resource is None:
                        continue
                    try:
                        await resource.close()
                    except Exception:
                        pass

        raise TikTokIdentityError(
            "tiktok_session_expired", "TikTok 登录已失效"
        )
    except TikTokIdentityError:
        raise
    except Exception as exc:
        raise TikTokIdentityError(
            "tiktok_session_expired", "TikTok 登录已失效"
        ) from exc
    finally:
        if browser is not None:
            try:
                await browser.close()
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
    "parse_tiktok_app_context_handle",
    "persist_tiktok_identity",
    "read_tiktok_identity",
    "TIKTOK_IDENTITY_URL",
    "validate_identity_binding",
    "validate_saved_tiktok_account",
]
