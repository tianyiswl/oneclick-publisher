# -*- coding: utf-8 -*-
"""Read-only Instagram professional identity probes for Meta Business Suite.

Only derived public account fields are retained.  Response bodies, cookies,
tokens and raw HTML are never returned or logged by this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlparse

from .overseas_instagram_identity import (
    InstagramIdentity,
    InstagramIdentityError,
    confirm_two_page_identity,
    parse_instagram_identity_payload,
)


INSTAGRAM_COMPOSER_URL = "https://business.facebook.com/latest/composer/"
INSTAGRAM_CONTENT_URL = "https://business.facebook.com/latest/content"
INSTAGRAM_IDENTITY_TIMEOUT_SECONDS = 45.0
INSTAGRAM_IDENTITY_POLL_SECONDS = 0.25

_INSTAGRAM_RELATION_KEYS = {
    "instagram_account",
    "instagram_business_account",
    "instagram_professional_account",
    "connected_instagram_account",
    "linked_instagram_account",
}
_PAGE_RELATION_KEYS = {
    "page",
    "facebook_page",
    "connected_facebook_page",
    "linked_page",
}


def _text(value: object) -> str:
    return " ".join(value.split()) if type(value) is str else ""


def _decimal_text(value: object) -> str:
    if type(value) is int and value > 0:
        return str(value)
    if type(value) is str:
        candidate = value.strip()
        if candidate.isascii() and candidate.isdigit():
            return candidate
    return ""


def _first_text(node: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _text(node.get(key))
        if value:
            return value
    return ""


def _first_decimal(node: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _decimal_text(node.get(key))
        if value:
            return value
    return ""


def _account_type(node: Mapping[str, object]) -> str:
    for key in (
        "account_type",
        "professional_account_type",
        "instagram_account_type",
        "profile_type",
    ):
        value = _text(node.get(key)).casefold().replace("-", "_")
        if "business" in value:
            return "business"
        if "creator" in value:
            return "creator"
    if node.get("is_business_account") is True:
        return "business"
    if node.get("is_creator_account") is True:
        return "creator"
    return ""


def _can_manage(node: Mapping[str, object]) -> bool:
    for key in (
        "can_manage_content",
        "can_publish",
        "can_create_content",
        "has_content_permission",
    ):
        if node.get(key) is True:
            return True
    tasks = node.get("tasks") or node.get("permissions")
    if isinstance(tasks, (list, tuple, set)):
        normalized = {str(item).strip().casefold() for item in tasks}
        return bool(
            normalized
            & {
                "create_content",
                "manage_content",
                "content",
                "instagram_content_publish",
            }
        )
    return False


def _page_fields(
    node: Mapping[str, object],
    parent: Mapping[str, object] | None,
) -> tuple[str, str, bool]:
    for key in _PAGE_RELATION_KEYS:
        candidate = node.get(key)
        if isinstance(candidate, Mapping):
            page_id = _first_decimal(candidate, ("id", "page_id"))
            page_name = _first_text(candidate, ("name", "page_name"))
            if page_id and page_name:
                return page_id, page_name, _can_manage(node) or _can_manage(candidate)
    if parent is not None:
        page_id = _first_decimal(parent, ("id", "page_id", "facebook_page_id"))
        page_name = _first_text(parent, ("name", "page_name", "facebook_page_name"))
        if page_id and page_name:
            return page_id, page_name, _can_manage(node) or _can_manage(parent)
    return "", "", False


def _identity_from_node(
    node: Mapping[str, object],
    parent: Mapping[str, object] | None,
) -> InstagramIdentity | None:
    user_id = _first_decimal(
        node,
        ("instagram_user_id", "instagram_account_id", "ig_user_id", "id"),
    )
    username = _first_text(node, ("username", "user_name", "instagram_username"))
    display_name = _first_text(node, ("full_name", "display_name", "name"))
    account_type = _account_type(node)
    linked_page_id, linked_page_name, can_manage = _page_fields(node, parent)
    if not can_manage:
        can_manage = _can_manage(node)
    if not all(
        (
            user_id,
            username,
            display_name,
            account_type,
            linked_page_id,
            linked_page_name,
            can_manage,
        )
    ):
        return None
    avatar_url = _first_text(
        node,
        (
            "profile_picture_url",
            "profile_pic_url_hd",
            "profile_pic_url",
            "avatar_url",
        ),
    )
    try:
        return parse_instagram_identity_payload(
            {
                "state": "ok",
                "instagramUserId": user_id,
                "username": username,
                "displayName": display_name,
                "avatarUrl": avatar_url,
                "accountType": account_type,
                "linkedPageId": linked_page_id,
                "linkedPageName": linked_page_name,
                "canManageContent": True,
            }
        )
    except InstagramIdentityError:
        return None


def extract_instagram_identities(payload: object) -> tuple[InstagramIdentity, ...]:
    """Project official Meta JSON into complete, non-sensitive identities."""

    discovered: dict[
        tuple[str, str, str, str, bool], InstagramIdentity
    ] = {}

    def walk(
        value: object,
        *,
        parent: Mapping[str, object] | None = None,
        relation: str = "",
    ) -> None:
        if isinstance(value, Mapping):
            relation_key = relation.casefold()
            instagram_shaped = (
                relation_key in _INSTAGRAM_RELATION_KEYS
                or "instagram" in relation_key
                or any(key in value for key in ("instagram_user_id", "ig_user_id"))
            )
            if instagram_shaped:
                identity = _identity_from_node(value, parent)
                if identity is not None:
                    key = (
                        identity.user_id,
                        identity.username,
                        identity.account_type,
                        identity.linked_page_id,
                        identity.can_manage_content,
                    )
                    discovered.setdefault(key, identity)
            for key, child in value.items():
                walk(child, parent=value, relation=str(key))
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, parent=parent, relation=relation)

    walk(payload)
    return tuple(discovered[key] for key in sorted(discovered))


def instagram_management_url(identity: InstagramIdentity) -> str:
    try:
        normalized = parse_instagram_identity_payload(
            {
                "state": "ok",
                "instagramUserId": identity.user_id,
                "username": identity.username,
                "displayName": identity.display_name,
                "avatarUrl": identity.avatar_url,
                "accountType": identity.account_type,
                "linkedPageId": identity.linked_page_id,
                "linkedPageName": identity.linked_page_name,
                "canManageContent": identity.can_manage_content,
            }
        )
    except (AttributeError, InstagramIdentityError) as exc:
        raise InstagramIdentityError(
            "instagram_identity_unavailable",
            "Instagram 专业账号身份无效，无法打开后台。",
        ) from exc
    return f"{INSTAGRAM_CONTENT_URL}?{urlencode({'asset_id': normalized.linked_page_id})}"


class InstagramIdentityCollector:
    """Consume official JSON responses without retaining their raw bodies."""

    def __init__(self) -> None:
        self._identities: dict[
            tuple[str, str, str, str, bool], InstagramIdentity
        ] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def add_payload(self, payload: object) -> None:
        for identity in extract_instagram_identities(payload):
            key = (
                identity.user_id,
                identity.username,
                identity.account_type,
                identity.linked_page_id,
                identity.can_manage_content,
            )
            self._identities.setdefault(key, identity)

    async def _consume_response(self, response: Any) -> None:
        try:
            parsed = urlparse(str(getattr(response, "url", "") or ""))
            hostname = (parsed.hostname or "").casefold()
            if not (
                hostname == "facebook.com"
                or hostname.endswith(".facebook.com")
                or hostname == "instagram.com"
                or hostname.endswith(".instagram.com")
            ):
                return
            payload = await response.json()
        except Exception:
            return
        self.add_payload(payload)

    def attach(self, page: Any) -> None:
        def observe(response: Any) -> None:
            task = asyncio.create_task(self._consume_response(response))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        page.on("response", observe)

    def resolved(self) -> InstagramIdentity | None:
        identities = tuple(self._identities.values())
        if len(identities) > 1:
            raise InstagramIdentityError(
                "instagram_identity_mismatch",
                "当前 Meta 页面返回了多个 Instagram 主体，已停止保存。",
            )
        return identities[0] if identities else None


async def _identity_from_dom(page: Any) -> InstagramIdentity | None:
    """Conservative fallback for explicit Meta identity attributes only."""

    try:
        rows = page.locator(
            "[data-instagram-user-id], [data-instagram-account-id], [data-ig-user-id]"
        )
        count = int(await rows.count())
    except Exception:
        return None
    identities: list[InstagramIdentity] = []
    for index in range(count):
        row = rows.nth(index)
        try:
            values: dict[str, object] = {"state": "ok"}
            for public_key, attributes in {
                "instagramUserId": (
                    "data-instagram-user-id",
                    "data-instagram-account-id",
                    "data-ig-user-id",
                ),
                "username": ("data-instagram-username", "data-username"),
                "displayName": ("data-display-name", "data-instagram-name"),
                "avatarUrl": ("data-avatar-url",),
                "accountType": ("data-account-type",),
                "linkedPageId": ("data-linked-page-id", "data-page-id"),
                "linkedPageName": ("data-linked-page-name", "data-page-name"),
                "canManageContent": ("data-can-manage-content",),
            }.items():
                for attribute in attributes:
                    candidate = await row.get_attribute(attribute)
                    if candidate:
                        values[public_key] = (
                            candidate.casefold() == "true"
                            if public_key == "canManageContent"
                            else candidate
                        )
                        break
            identities.append(parse_instagram_identity_payload(values))
        except (AttributeError, InstagramIdentityError):
            continue
    distinct = {identity.user_id: identity for identity in identities}
    if len(distinct) > 1:
        raise InstagramIdentityError(
            "instagram_identity_mismatch",
            "当前 Meta 页面返回了多个 Instagram 主体，已停止保存。",
        )
    return next(iter(distinct.values()), None)


async def navigate_and_read_instagram_identity(
    page: Any,
    url: str,
    *,
    timeout_seconds: float = INSTAGRAM_IDENTITY_TIMEOUT_SECONDS,
    poll_seconds: float = INSTAGRAM_IDENTITY_POLL_SECONDS,
) -> InstagramIdentity:
    collector = InstagramIdentityCollector()
    collector.attach(page)
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.001, float(timeout_seconds))
    while loop.time() < deadline:
        identity = collector.resolved() or await _identity_from_dom(page)
        if identity is not None:
            return identity
        await asyncio.sleep(
            min(max(0.01, float(poll_seconds)), max(0.0, deadline - loop.time()))
        )
    raise InstagramIdentityError(
        "instagram_identity_unavailable",
        "Meta Business Suite 没有返回完整 Instagram 专业账号身份，已停止保存。",
    )


IdentityReader = Callable[[Any, str], Awaitable[InstagramIdentity]]


async def read_confirmed_instagram_identity(
    page: Any,
    *,
    identity_reader: IdentityReader = navigate_and_read_instagram_identity,
) -> InstagramIdentity:
    """Confirm the same IG subject on composer and scoped content manager."""

    composer = await identity_reader(page, INSTAGRAM_COMPOSER_URL)
    content = await identity_reader(page, instagram_management_url(composer))
    return confirm_two_page_identity(composer, content)
