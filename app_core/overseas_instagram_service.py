# -*- coding: utf-8 -*-
"""Instagram UI、CLI 和本地接口共用的发布服务边界。

本模块当前只开放零平台写入的本地预检。真实表单填写和最终分享
必须在后续阶段通过同一服务的受控适配器开放，不会回退到旧 Meta
浏览器通道。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Mapping

from .overseas_instagram_account import load_instagram_account_binding
from .overseas_instagram_errors import (
    InstagramPublishError,
    project_instagram_receipt,
)
from .overseas_instagram_identity import (
    InstagramIdentity,
    InstagramIdentityError,
    parse_instagram_identity_payload,
)
from .overseas_instagram_publish import prepare_instagram_publish_intent


IdentityLoader = Callable[[int], InstagramIdentity]


def _load_saved_identity(account_id: int) -> InstagramIdentity:
    from .database import connect

    with connect() as conn:
        return load_instagram_account_binding(conn, account_id)


def _normalize_identity(identity: object) -> InstagramIdentity:
    if not isinstance(identity, InstagramIdentity):
        raise InstagramPublishError(
            "instagram_account_invalid",
            "Instagram 本地账号缺少稳定身份绑定。",
        )
    try:
        return parse_instagram_identity_payload(
            {
                "state": "ok",
                "instagramUserId": identity.user_id,
                "username": identity.username,
                "displayName": identity.display_name,
                # 本地预检不持久化或投影远程头像 URL。
                "avatarUrl": "",
                "accountType": identity.account_type,
                "linkedPageId": identity.linked_page_id,
                "linkedPageName": identity.linked_page_name,
                "canManageContent": identity.can_manage_content,
            }
        )
    except InstagramIdentityError as exc:
        raise InstagramPublishError(
            "instagram_account_invalid",
            "Instagram 本地账号缺少稳定专业账号绑定。",
        ) from exc


def run_instagram_local_preflight_sync(
    payload: Mapping[str, object],
    *,
    identity_loader: IdentityLoader | None = None,
) -> dict[str, object]:
    """校验内容、精确素材和已保存主体，不连接 Instagram。"""

    if not isinstance(payload, Mapping):
        raise InstagramPublishError(
            "instagram_payload_invalid",
            "Instagram 发布载荷必须是结构化对象。",
        )
    intent = prepare_instagram_publish_intent(payload)
    loader = identity_loader or _load_saved_identity
    try:
        identity = _normalize_identity(loader(intent.account_id))
    except InstagramPublishError:
        raise
    except Exception as exc:
        raise InstagramPublishError(
            "instagram_account_invalid",
            "Instagram 本地账号绑定无法读取。",
        ) from exc
    if identity.user_id != intent.instagram_user_id:
        raise InstagramPublishError(
            "instagram_identity_mismatch",
            "Instagram 内容主体与已保存账号不一致。",
        )
    receipt = project_instagram_receipt(
        {
            "phase": "local_preflight_passed",
            "accountId": intent.account_id,
            "instagramUserId": intent.instagram_user_id,
            "username": identity.username,
            "accountType": identity.account_type,
            "linkedPageId": identity.linked_page_id,
            "linkedPageName": identity.linked_page_name,
            "videoSha256": intent.video_sha256,
            "coverSha256": intent.cover_sha256,
            "captionSha256": intent.caption_sha256,
            "topics": list(intent.topics),
            "visibility": intent.visibility,
            "shareToFeed": intent.share_to_feed,
            "scheduleMode": intent.schedule_mode,
            "scheduledAt": intent.scheduled_at,
            "scheduleTimezone": intent.schedule_timezone,
            "platformWriteOccurred": False,
            "finalActionTriggered": False,
            "blocksReplay": False,
        }
    )
    return {
        "ok": True,
        "phase": "local_preflight_passed",
        "message": "Instagram 本地预检通过；未连接平台、未上传媒体。",
        "receipt": receipt,
    }
