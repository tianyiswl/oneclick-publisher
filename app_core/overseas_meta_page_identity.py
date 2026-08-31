# -*- coding: utf-8 -*-
"""Facebook Page identity selection and injected-page readback contracts."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlparse

from .overseas_meta_errors import FacebookPagePublishError


FACEBOOK_PAGE_ACTIVATION_TIMEOUT_SECONDS = 15.0
FACEBOOK_PAGE_ACTIVATION_POLL_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class FacebookPageIdentity:
    page_id: str
    page_name: str
    avatar_url: str = ""
    can_manage_content: bool = False


def normalize_facebook_page_id(value: object) -> str:
    """Accept only a non-empty ASCII decimal Page ID, never a display name."""

    if type(value) not in {str, int} or type(value) is bool:
        raise FacebookPagePublishError(
            "facebook_page_identity_mismatch",
            "Facebook Page 身份无效，已停止操作。",
        )
    page_id = str(value or "").strip()
    if not page_id or not page_id.isascii() or not page_id.isdigit():
        raise FacebookPagePublishError(
            "facebook_page_identity_mismatch",
            "Facebook Page 身份无效，已停止操作。",
        )
    return page_id


def _page_not_found() -> FacebookPagePublishError:
    return FacebookPagePublishError(
        "facebook_page_not_found", "当前会话没有可管理的 Facebook Page。"
    )


def _permission_missing() -> FacebookPagePublishError:
    return FacebookPagePublishError(
        "facebook_page_content_permission_missing",
        "当前 Facebook Page 没有内容管理权限，已停止操作。",
    )


def _identity_mismatch() -> FacebookPagePublishError:
    return FacebookPagePublishError(
        "facebook_page_identity_mismatch",
        "保存的 Facebook Page 与当前页面不一致，已停止操作。",
    )


def _active_facebook_page_id_from_url(value: object) -> str:
    if type(value) is not str or not value.strip():
        return ""
    try:
        parsed = urlparse(value)
        if parsed.hostname not in {"business.facebook.com", "www.business.facebook.com"}:
            return ""
        values = parse_qs(parsed.query).get("asset_id", [])
        if len(values) != 1:
            return ""
        return normalize_facebook_page_id(values[0])
    except (TypeError, ValueError, FacebookPagePublishError):
        return ""


def _facebook_destination_page_name(
    value: object,
    *,
    platform_marker_present: bool = False,
) -> str:
    if type(value) is not str:
        return ""
    text = " ".join(value.replace("\u200b", " ").split()).strip()
    folded = text.casefold()
    marker = "facebook"
    index = folded.find(marker)
    if index >= 0:
        text = text[index + len(marker) :].strip(" \t\r\n\xb7·|-")
    elif not platform_marker_present:
        return ""
    for prefix in (
        "发布位置",
        "发布到",
        "publishing location",
        "publish to",
        "destination",
    ):
        if text.casefold().startswith(prefix.casefold()):
            text = text[len(prefix) :].strip(" \t\r\n:\uff1a\xb7·|-")
            break
    return text


async def _destination_has_facebook_marker(destination, *values: object) -> bool:
    if any(type(value) is str and "facebook" in value.casefold() for value in values):
        return True
    try:
        icons = destination.locator(
            'img[alt*="Facebook" i], [aria-label*="Facebook" i]'
        )
        return int(await icons.count()) > 0
    except Exception:
        return False


async def _live_composer_page_identity(page) -> FacebookPageIdentity | None:
    """Read the active Page from Meta's real composer without test-only attrs."""

    page_id = _active_facebook_page_id_from_url(getattr(page, "url", ""))
    if not page_id:
        return None
    try:
        destinations = page.locator('[role="combobox"]')
        count = int(await destinations.count())
    except Exception:
        return None

    page_name = ""
    for index in range(count):
        try:
            destination = destinations.nth(index)
            text = await destination.inner_text()
            aria_label = await destination.get_attribute("aria-label")
        except Exception:
            continue
        marker_present = await _destination_has_facebook_marker(
            destination,
            text,
            aria_label,
        )
        page_name = _facebook_destination_page_name(
            text,
            platform_marker_present=marker_present,
        )
        if not page_name:
            page_name = _facebook_destination_page_name(
                aria_label,
                platform_marker_present=marker_present,
            )
        if page_name:
            break
    if not page_name:
        return None

    has_content_control = False
    for selector in (
        'input[type="file"]',
        'button:has-text("Add photo/video")',
        'button:has-text("Add photos/videos")',
        'button:has-text("添加照片/视频")',
        '[role="button"]:has-text("添加照片/视频")',
        '[contenteditable="true"][role="textbox"]',
    ):
        try:
            controls = page.locator(selector)
            if int(await controls.count()) > 0:
                has_content_control = True
                break
        except Exception:
            continue
    if not has_content_control:
        return None

    return FacebookPageIdentity(
        page_id=page_id,
        page_name=page_name,
        can_manage_content=True,
    )


def resolve_facebook_page_selection(
    pages: Iterable[FacebookPageIdentity], selected_page_id: str | None = None
) -> FacebookPageIdentity:
    """Choose a manageable Page by stable ID only, never by name or position."""

    distinct: dict[str, FacebookPageIdentity] = {}
    for page in pages:
        page_id = normalize_facebook_page_id(page.page_id)
        existing = distinct.get(page_id)
        if existing is None:
            distinct[page_id] = page
        elif existing.can_manage_content != page.can_manage_content:
            raise _identity_mismatch()

    if not distinct:
        raise _page_not_found()

    expected = normalize_facebook_page_id(selected_page_id) if selected_page_id is not None else ""
    if expected:
        selected = distinct.get(expected)
        if selected is None:
            raise _identity_mismatch()
        if not selected.can_manage_content:
            raise _permission_missing()
        return selected

    manageable = [page for page in distinct.values() if page.can_manage_content]
    if not manageable:
        raise _permission_missing()
    if len(manageable) == 1:
        return manageable[0]
    raise FacebookPagePublishError(
        "facebook_page_selection_required",
        "发现多个可管理的 Facebook Page，请明确选择 Page。",
    )


async def discover_manageable_facebook_pages(page) -> tuple[FacebookPageIdentity, ...]:
    """Read only the Page identity fields exposed by the current Meta page."""

    try:
        rows = page.locator("[data-page-id]")
        count = int(await rows.count())
    except Exception as exc:
        raise _page_not_found() from exc

    discovered: list[FacebookPageIdentity] = []
    for index in range(count):
        try:
            row = rows.nth(index)
            page_id = normalize_facebook_page_id(await row.get_attribute("data-page-id"))
            page_name = await row.get_attribute("data-page-name")
            if type(page_name) is not str or not page_name.strip():
                page_name = await row.inner_text()
            if type(page_name) is not str or not page_name.strip():
                raise _identity_mismatch()
            avatar_url = await row.get_attribute("data-avatar-url")
            permission = await row.get_attribute("data-can-manage-content")
        except FacebookPagePublishError:
            raise
        except Exception as exc:
            raise _identity_mismatch() from exc
        discovered.append(
            FacebookPageIdentity(
                page_id=page_id,
                page_name=page_name.strip(),
                avatar_url=avatar_url if type(avatar_url) is str else "",
                can_manage_content=type(permission) is str
                and permission.casefold() == "true",
            )
        )
    if not discovered:
        live_identity = await _live_composer_page_identity(page)
        if live_identity is not None:
            discovered.append(live_identity)
    return tuple(discovered)


async def validate_facebook_page_binding(
    page, account: Mapping[str, Any]
) -> FacebookPageIdentity:
    """Confirm a saved accountReference is still the current manageable Page."""

    try:
        expected_page_id = normalize_facebook_page_id(account.get("accountReference"))
    except AttributeError as exc:
        raise _identity_mismatch() from exc
    pages = await discover_manageable_facebook_pages(page)
    selected = resolve_facebook_page_selection(pages, expected_page_id)
    try:
        active_rows = page.locator('[data-page-id][data-page-active="true"]')
        active_count = int(await active_rows.count())
        if active_count > 1:
            raise _identity_mismatch()
        active_page_id = (
            normalize_facebook_page_id(
                await active_rows.nth(0).get_attribute("data-page-id")
            )
            if active_count == 1
            else ""
        )
    except FacebookPagePublishError:
        raise
    except Exception as exc:
        raise _identity_mismatch() from exc
    url_page_id = _active_facebook_page_id_from_url(getattr(page, "url", ""))
    if active_page_id and url_page_id and active_page_id != url_page_id:
        raise _identity_mismatch()
    active_page_id = active_page_id or url_page_id
    if active_page_id != expected_page_id:
        raise _identity_mismatch()
    return selected


async def activate_saved_facebook_page(
    page,
    expected_page_id: str,
    *,
    timeout_seconds: float = FACEBOOK_PAGE_ACTIVATION_TIMEOUT_SECONDS,
    poll_interval_seconds: float = FACEBOOK_PAGE_ACTIVATION_POLL_INTERVAL_SECONDS,
) -> FacebookPageIdentity:
    """Switch to one explicit Page and prove the active Page ID after the switch."""

    expected = normalize_facebook_page_id(expected_page_id)

    async def wait_for_selected_page() -> FacebookPageIdentity:
        while True:
            try:
                pages = await discover_manageable_facebook_pages(page)
                return resolve_facebook_page_selection(pages, expected)
            except FacebookPagePublishError as exc:
                if exc.error_code != "facebook_page_not_found":
                    raise
            await asyncio.sleep(max(0.0, float(poll_interval_seconds)))

    try:
        selected = await asyncio.wait_for(
            wait_for_selected_page(),
            timeout=max(0.001, float(timeout_seconds)),
        )
    except TimeoutError as exc:
        raise _page_not_found() from exc
    active_page_id = _active_facebook_page_id_from_url(getattr(page, "url", ""))
    if active_page_id:
        if active_page_id != expected:
            raise _identity_mismatch()
        return selected
    try:
        targets = page.locator(f'[data-page-id="{expected}"]')
        if int(await targets.count()) != 1:
            raise _identity_mismatch()
        await targets.nth(0).click()
    except FacebookPagePublishError:
        raise
    except Exception as exc:
        raise _identity_mismatch() from exc

    async def wait_for_exact_readback() -> FacebookPageIdentity:
        while True:
            try:
                return await validate_facebook_page_binding(
                    page,
                    {"accountReference": expected},
                )
            except FacebookPagePublishError as exc:
                if exc.error_code == "facebook_page_content_permission_missing":
                    raise
            await asyncio.sleep(max(0.0, float(poll_interval_seconds)))

    try:
        return await asyncio.wait_for(
            wait_for_exact_readback(),
            timeout=max(0.001, float(timeout_seconds)),
        )
    except TimeoutError as exc:
        raise _identity_mismatch() from exc
    except FacebookPagePublishError:
        raise
    except (TypeError, ValueError) as exc:
        raise _identity_mismatch() from exc


def facebook_page_v1_enabled(environ: Mapping[str, str] = os.environ) -> bool:
    """The process-local opt-in cannot be overridden by request payloads."""

    return environ.get("ONECLICK_ENABLE_FACEBOOK_PAGE_V1") == "1"
