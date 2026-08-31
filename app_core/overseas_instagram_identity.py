# -*- coding: utf-8 -*-
"""Instagram 专业账号的非敏感稳定身份合同。"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._]{1,30}\Z")


@dataclass(frozen=True, slots=True)
class InstagramIdentity:
    user_id: str
    username: str
    display_name: str
    avatar_url: str
    account_type: str
    linked_page_id: str
    linked_page_name: str
    can_manage_content: bool


class InstagramIdentityError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(self.public_message)


def parse_instagram_identity_payload(
    payload: Mapping[str, object],
) -> InstagramIdentity:
    try:
        if payload.get("state") != "ok":
            raise ValueError
        user_id = normalize_instagram_user_id(payload.get("instagramUserId"))
        raw_username = payload.get("username")
        if type(raw_username) is not str:
            raise ValueError
        username = raw_username.strip().lstrip("@").casefold()
        if _USERNAME_PATTERN.fullmatch(username) is None:
            raise ValueError
        display_name = " ".join(str(payload.get("displayName") or "").split())
        linked_page_id = normalize_instagram_user_id(payload.get("linkedPageId"))
        linked_page_name = " ".join(
            str(payload.get("linkedPageName") or "").split()
        )
        account_type = str(payload.get("accountType") or "").strip().casefold()
        if (
            not display_name
            or not linked_page_name
            or account_type not in {"business", "creator"}
            or payload.get("canManageContent") is not True
        ):
            raise ValueError
        avatar_url = payload.get("avatarUrl")
        if type(avatar_url) is not str:
            avatar_url = ""
    except (AttributeError, TypeError, ValueError) as exc:
        raise InstagramIdentityError(
            "instagram_identity_unavailable",
            "Instagram 页面没有返回完整的专业账号身份，已停止保存。",
        ) from exc
    return InstagramIdentity(
        user_id=user_id,
        username=username,
        display_name=display_name,
        avatar_url=avatar_url,
        account_type=account_type,
        linked_page_id=linked_page_id,
        linked_page_name=linked_page_name,
        can_manage_content=True,
    )


def normalize_instagram_user_id(value: object) -> str:
    if type(value) is not str:
        raise ValueError("Instagram 主体 ID 必须是十进制字符串")
    normalized = value.strip()
    if not normalized or not normalized.isascii() or not normalized.isdigit():
        raise ValueError("Instagram 主体 ID 必须是十进制字符串")
    return normalized


def confirm_two_page_identity(
    first: InstagramIdentity,
    second: InstagramIdentity,
) -> InstagramIdentity:
    first_key = (
        first.user_id.strip(),
        first.username.strip().casefold(),
        first.account_type.strip().casefold(),
        first.linked_page_id.strip(),
        bool(first.can_manage_content),
    )
    second_key = (
        second.user_id.strip(),
        second.username.strip().casefold(),
        second.account_type.strip().casefold(),
        second.linked_page_id.strip(),
        bool(second.can_manage_content),
    )
    if first_key != second_key:
        raise InstagramIdentityError(
            "instagram_identity_mismatch",
            "两个 Instagram 页面返回的账号主体不一致，已停止保存。",
        )
    return first
