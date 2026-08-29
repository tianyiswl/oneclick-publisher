# -*- coding: utf-8 -*-
"""Facebook Page identity selection and injected-page readback contracts."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

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
        if int(await active_rows.count()) != 1:
            raise _identity_mismatch()
        active_page_id = normalize_facebook_page_id(
            await active_rows.nth(0).get_attribute("data-page-id")
        )
    except FacebookPagePublishError:
        raise
    except Exception as exc:
        raise _identity_mismatch() from exc
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
    pages = await discover_manageable_facebook_pages(page)
    resolve_facebook_page_selection(pages, expected)
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
