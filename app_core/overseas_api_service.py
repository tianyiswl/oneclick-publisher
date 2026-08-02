# -*- coding: utf-8 -*-
"""一键发海外平台官方 API 接口层。

本模块只调用已迁入 ``app_core.overseas`` 的官方 OAuth/API 能力，
不读取浏览器 Cookie，也不依赖蚁小二客户端。OAuth 令牌仅保存
在系统私密数据目录；任务、日志和数据库只保留非敏感账号引用与
平台回执。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .overseas import (
    AuthorizationRequest,
    CapabilityAvailability,
    EvidenceRecord,
    EvidenceStage,
    PublicationMode,
    UploadRequest,
)
from .overseas.meta import (
    FacebookCapability,
    InstagramCapability,
    MetaCredentialStore,
)
from .overseas.tiktok import TikTokCapability, TikTokCredentialStore
from .overseas.youtube import YouTubeCapability, YouTubeCredentialStore


OFFICIAL_API_PLATFORM_TYPES = frozenset({6, 7, 8, 9})
PLATFORM_NAMES = {
    6: "TikTok",
    7: "YouTube",
    8: "Instagram Reels",
    9: "Facebook Reels",
}


def _combined_caption(payload: dict[str, Any]) -> str:
    parts = [
        value
        for value in (
            str(payload.get("title") or "").strip(),
            str(payload.get("description") or "").strip(),
        )
        if value
    ]
    tags = [
        str(item).strip().lstrip("#")
        for item in payload.get("tags") or []
        if str(item).strip().lstrip("#")
    ]
    if tags:
        parts.append(" ".join(f"#{item}" for item in tags))
    return "\n\n".join(parts)


def _platform_field_errors(
    payload: dict[str, Any],
    platform_type: int,
) -> list[str]:
    """校验会被官方通道真实写入的平台字段。"""

    errors: list[str] = []
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "")
    if platform_type == 7:
        if len(title) > 100:
            errors.append("YouTube 标题不能超过 100 个字符")
        if len(description.encode("utf-8")) > 5000:
            errors.append("YouTube 描述不能超过 5000 字节")
    elif platform_type in {8, 9}:
        limit = 2200 if platform_type == 8 else 2048
        caption = _combined_caption(payload)
        if not caption:
            errors.append(f"{PLATFORM_NAMES[platform_type]} 合并文案不能为空")
        elif len(caption) > limit:
            errors.append(
                f"{PLATFORM_NAMES[platform_type]} 合并文案不能超过 "
                f"{limit} 个字符，一键发不会静默截断"
            )
    if platform_type == 9 and payload.get("aiGenerated") is True:
        errors.append(
            "Facebook Reels 官方发布端点尚未提供已验证的 "
            "AI 自声明字段；已阻止遗漏声明的发布"
        )
    return errors


def _run(coroutine):
    """在同步服务线程中执行官方适配器协程。"""

    return asyncio.run(coroutine)


def configure_official_client(platform_type: int, source: str | Path) -> dict[str, Any]:
    """导入开发者应用配置；不发起授权或上传。"""

    platform_type = int(platform_type)
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError("官方 OAuth 客户端配置文件不存在")
    if platform_type == 7:
        config = YouTubeCredentialStore().configure_client(source_path)
        return {
            "platformType": platform_type,
            "platform": PLATFORM_NAMES[platform_type],
            "configured": True,
            "clientIdSuffix": config.client_id[-8:],
        }
    if platform_type == 6:
        config = TikTokCredentialStore().configure_client(source_path)
        return {
            "platformType": platform_type,
            "platform": PLATFORM_NAMES[platform_type],
            "configured": True,
            "clientKeySuffix": config.client_key[-8:],
        }
    if platform_type in {8, 9}:
        config = MetaCredentialStore().configure_broker(source_path)
        return {
            "platformType": platform_type,
            "platform": "Meta",
            "configured": True,
            "brokerHost": config.broker_url.split("//", 1)[-1].split("/", 1)[0],
            "appIdSuffix": config.app_id[-6:],
        }
    raise ValueError("当前平台没有官方 API 配置入口")


def official_status(platform_type: int) -> dict[str, Any]:
    """读取脱敏配置与授权状态，不访问平台。"""

    platform_type = int(platform_type)
    if platform_type == 7:
        status = YouTubeCredentialStore().status()
    elif platform_type == 6:
        status = TikTokCredentialStore().status()
    elif platform_type in {8, 9}:
        status = MetaCredentialStore().status()
    else:
        raise ValueError("未知海外平台")
    # 桌面 UI 只需要状态与脱敏计数，不向界面、任务或日志
    # 暴露本机 OAuth 配置和令牌路径。
    safe_status = {
        key: value
        for key, value in status.items()
        if not str(key).lower().endswith("path")
    }
    return {
        "platformType": platform_type,
        "platform": PLATFORM_NAMES[platform_type],
        **safe_status,
    }


def validate_official_preflight_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """离线校验官方 API 任务；不刷新令牌、不上传、不访问平台。"""

    platform_type = int(payload.get("type") or 0)
    errors: list[str] = []
    if platform_type not in OFFICIAL_API_PLATFORM_TYPES:
        errors.append("不是已迁入的海外官方 API 平台")
    if str(payload.get("contentType") or "") != "video":
        errors.append("海外官方通道当前只接受视频")
    if str(payload.get("runtimeMode") or "preflight") != "preflight":
        errors.append("官方 API 预检必须保持 runtimeMode=preflight")
    if payload.get("debugDryRun") is not True:
        errors.append("官方 API 预检必须保持 debugDryRun=true")
    auth_modes = {
        str(item or "") for item in payload.get("accountAuthModes") or []
    }
    if auth_modes != {"official_api"}:
        errors.append("官方 API 预检不能混用浏览器会话账号")
    references = [
        str(item or "").strip()
        for item in payload.get("accountReferences") or []
        if str(item or "").strip()
    ]
    if len(references) != 1:
        errors.append("官方 API 预检每个平台必须精确选择一个账号")
    elif not validate_official_account(
        {
            "type": platform_type,
            "authMode": "official_api",
            "accountReference": references[0],
        }
    ):
        errors.append("当前官方授权与所选账号引用不一致或已过期")
    files = [Path(str(item)) for item in payload.get("fileList") or []]
    if not files:
        errors.append("未选择视频素材")
    elif any(not item.is_file() for item in files):
        errors.append("视频素材不存在")
    if not str(payload.get("title") or "").strip():
        errors.append("平台标题不能为空")
    errors.extend(_platform_field_errors(payload, platform_type))
    if payload.get("enableTimer"):
        try:
            _schedule(payload)
        except ValueError as exc:
            errors.append(str(exc))
        if platform_type in {6, 8, 9}:
            errors.append(
                f"{PLATFORM_NAMES.get(platform_type, '当前平台')} 官方通道尚未实现定时回读"
            )
    try:
        status = official_status(platform_type)
    except (FileNotFoundError, ValueError, PermissionError) as exc:
        status = {}
        errors.append(str(exc))
    if status and not status.get("clientConfigured") and not status.get(
        "brokerConfigured"
    ):
        errors.append("尚未导入当前平台的官方开发者配置")
    if status and (
        not status.get("tokenConfigured")
        or not status.get("accessTokenValid")
        or not status.get("scopeGranted")
    ):
        errors.append("当前平台官方授权不完整或已过期")
    return {
        "ok": not errors,
        "errors": errors,
        "platformType": platform_type,
        "accountReferences": references,
        "files": files,
        "status": status,
    }


def run_official_preflight_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """返回可区分的本地预检结果，明确没有上传或发布。"""

    checked = validate_official_preflight_payload(payload)
    if not checked["ok"]:
        raise ValueError("；".join(checked["errors"]))
    platform_type = int(checked["platformType"])
    return {
        "ok": True,
        "platformType": platform_type,
        "platform": PLATFORM_NAMES[platform_type],
        "externalCall": False,
        "uploaded": False,
        "published": False,
        "message": (
            "TikTok 官方收件箱本地预检通过；已验证账号引用、"
            "授权状态与视频。收件箱接口只传视频，标题、文案和发布"
            "仍需在 TikTok 内完成；未调用上传或发布接口"
            if platform_type == 6
            else f"{PLATFORM_NAMES[platform_type]} 官方 API 本地预检通过；"
            "已验证账号引用、授权状态、视频及平台字段，"
            "未调用平台上传或发布接口"
        ),
    }


def validate_official_inbox_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """校验 TikTok 官方收件箱上传，与公开发布严格分开。"""

    candidate = dict(payload)
    candidate["runtimeMode"] = "preflight"
    candidate["debugDryRun"] = True
    checked = validate_official_preflight_payload(candidate)
    errors = list(checked["errors"])
    if int(payload.get("type") or 0) != 6:
        errors.append("当前收件箱通道只适用于 TikTok")
    if str(payload.get("runtimeMode") or "") != "draft":
        errors.append("TikTok 收件箱上传必须使用 draft 模式")
    if payload.get("debugDryRun") is not False:
        errors.append("真实收件箱上传必须明确 debugDryRun=false")
    if payload.get("officialApiConfirmed") is not True:
        errors.append("未确认本次 TikTok 收件箱真实上传")
    if payload.get("enableTimer"):
        errors.append("TikTok 收件箱上传不支持定时")
    checked["errors"] = errors
    checked["ok"] = not errors
    return checked


def run_official_inbox_upload_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """上传至 TikTok 收件箱；不会代替用户在 TikTok 内发布。"""

    checked = validate_official_inbox_payload(payload)
    if not checked["ok"]:
        raise ValueError("；".join(checked["errors"]))
    reference = checked["accountReferences"][0]
    capability = TikTokCapability(availability=CapabilityAvailability.AVAILABLE)
    results = []
    for video_path in checked["files"]:
        request = UploadRequest(
            account_reference=reference,
            video_path=video_path,
            title=str(payload.get("title") or "").strip(),
            description=str(payload.get("description") or ""),
            tags=tuple(str(item).lstrip("#") for item in payload.get("tags") or []),
            mode=PublicationMode.DRAFT,
            ai_generated=bool(payload.get("aiGenerated", False)),
            user_confirmed_upload=True,
        )
        result = _run(capability.upload(request))
        results.append(result)
        if not result.accepted:
            break
    inbox_ready = bool(results) and all(
        any(
            record.stage == EvidenceStage.PUBLICATION and record.verified
            for record in item.evidence
        )
        for item in results
    )
    ok = inbox_ready and all(item.accepted for item in results)
    accepted_but_processing = (
        bool(results) and all(item.accepted for item in results) and not inbox_ready
    )
    return {
        "ok": ok,
        "platformType": 6,
        "platform": "TikTok",
        "uploadedToInbox": inbox_ready,
        "processing": accepted_but_processing,
        "published": False,
        "operationReferences": [
            item.operation_reference for item in results if item.operation_reference
        ],
        "message": (
            "TikTok 已回读收件箱上传状态；仍需用户在 TikTok 内检查并发布"
            if ok
            else "TikTok 已接收视频但仍在处理，尚未验证进入收件箱"
            if accepted_but_processing
            else "TikTok 未能验证收件箱上传状态"
        ),
    }


def authorize_official_account(platform_type: int, profile_name: str) -> dict[str, Any]:
    """从一键发发起官方 OAuth，回读账号后保存非敏感账号记录。"""

    from . import account_service

    platform_type = int(platform_type)
    profile_name = str(profile_name or "").strip()
    if not profile_name:
        raise ValueError("请先填写账号主体")

    if platform_type == 7:
        result = _run(
            YouTubeCapability().authorize(
                AuthorizationRequest(profile_name=profile_name, interactive=True)
            )
        )
        token = YouTubeCredentialStore().load_token()
        account_id = account_service.save_official_api_account(
            7,
            profile_name,
            result.account_reference,
            token.channel_title or result.account_reference,
        )
        return _authorization_payload(result.evidence, [account_id])

    if platform_type == 6:
        capability = TikTokCapability(
            availability=CapabilityAvailability.AVAILABLE
        )
        result = _run(
            capability.authorize(
                AuthorizationRequest(profile_name=profile_name, interactive=True)
            )
        )
        token = TikTokCredentialStore().load_token()
        account_id = account_service.save_official_api_account(
            6,
            profile_name,
            result.account_reference,
            token.display_name or result.account_reference,
        )
        return _authorization_payload(result.evidence, [account_id])

    if platform_type in {8, 9}:
        # Meta 一次授权同时回读 Facebook Page 与其绑定的
        # Instagram 专业账号，一键发将其保存为独立发布目标。
        capability = InstagramCapability(
            availability=CapabilityAvailability.AVAILABLE
        )
        result = _run(
            capability.authorize(
                AuthorizationRequest(profile_name=profile_name, interactive=True)
            )
        )
        token = MetaCredentialStore().load_token()
        account_ids: list[int] = []
        for asset in token.assets:
            account_ids.append(
                account_service.save_official_api_account(
                    9,
                    profile_name,
                    asset.page_id,
                    asset.page_name or asset.page_id,
                )
            )
            if asset.instagram_account_id:
                account_ids.append(
                    account_service.save_official_api_account(
                        8,
                        profile_name,
                        asset.instagram_account_id,
                        asset.instagram_username or asset.instagram_account_id,
                    )
                )
        if not account_ids:
            raise PermissionError("Meta 授权没有返回可用 Page 或 Instagram 专业账号")
        return _authorization_payload(result.evidence, account_ids)

    raise ValueError("当前平台没有官方 OAuth 授权入口")


def _authorization_payload(
    evidence: tuple[EvidenceRecord, ...],
    account_ids: list[int],
) -> dict[str, Any]:
    return {
        "ok": True,
        "accountIds": account_ids,
        "evidence": [
            {
                "stage": item.stage.value,
                "verified": item.verified,
                "message": item.message,
                "reference": item.reference,
            }
            for item in evidence
        ],
    }


def validate_official_account(account: dict[str, Any]) -> bool:
    """静默检查本机 OAuth 令牌和账号引用是否一致。"""

    platform_type = int(account.get("type") or 0)
    reference = str(account.get("accountReference") or "").strip()
    if not reference or platform_type not in OFFICIAL_API_PLATFORM_TYPES:
        return False
    if platform_type == 7:
        status = YouTubeCredentialStore().status()
        return bool(
            status.get("accessTokenValid")
            and status.get("scopeGranted")
            and status.get("channelId") == reference
        )
    if platform_type == 6:
        status = TikTokCredentialStore().status()
        return bool(
            status.get("accessTokenValid")
            and status.get("scopeGranted")
            and status.get("openId") == reference
        )
    store = MetaCredentialStore()
    status = store.status()
    if not status.get("accessTokenValid") or not status.get("scopeGranted"):
        return False
    try:
        store.page_asset(reference, instagram=platform_type == 8)
    except (FileNotFoundError, ValueError, PermissionError):
        return False
    return True


def _schedule(payload: dict[str, Any]) -> datetime | None:
    value = str(payload.get("scheduleTime") or "").strip()
    if not value:
        return None
    timezone_name = str(payload.get("scheduleTimezone") or "Asia/Shanghai").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"无法识别定时时区：{timezone_name}") from exc
    try:
        local = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone
        )
    except ValueError as exc:
        raise ValueError("定时发布时间必须为 YYYY-MM-DD HH:MM") from exc
    if local <= datetime.now(timezone):
        raise ValueError("定时发布必须晚于当前时间")
    return local


def validate_official_publish_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """只做字段与本地授权检查，不发起任何平台请求。"""

    platform_type = int(payload.get("type") or 0)
    errors: list[str] = []
    if platform_type not in OFFICIAL_API_PLATFORM_TYPES:
        errors.append("不是已迁入的海外官方 API 平台")
    if str(payload.get("contentType") or "") != "video":
        errors.append("海外官方通道当前只接受视频")
    if str(payload.get("runtimeMode") or "") != "publish":
        errors.append("官方 API 提交必须是明确的正式发布任务")
    if payload.get("debugDryRun") is not False:
        errors.append("正式提交必须明确 debugDryRun=false")
    if payload.get("officialApiConfirmed") is not True:
        errors.append("未获得本次官方 API 上传确认")
    auth_modes = {str(item or "") for item in payload.get("accountAuthModes") or []}
    if auth_modes != {"official_api"}:
        errors.append("正式官方 API 任务不能混用浏览器会话账号")
    references = [
        str(item or "").strip()
        for item in payload.get("accountReferences") or []
        if str(item or "").strip()
    ]
    if len(references) != 1:
        errors.append("当前官方通道每个任务必须精确选择一个账号")
    elif not validate_official_account(
        {
            "type": platform_type,
            "authMode": "official_api",
            "accountReference": references[0],
        }
    ):
        errors.append("当前官方授权与所选账号引用不一致或已过期")
    files = [Path(str(item)) for item in payload.get("fileList") or []]
    if not files:
        errors.append("未选择视频素材")
    elif any(not item.is_file() for item in files):
        errors.append("视频素材不存在")
    if not str(payload.get("title") or "").strip():
        errors.append("平台标题不能为空")
    errors.extend(_platform_field_errors(payload, platform_type))

    if payload.get("enableTimer"):
        try:
            _schedule(payload)
        except ValueError as exc:
            errors.append(str(exc))
        if platform_type in {6, 8, 9}:
            errors.append(f"{PLATFORM_NAMES.get(platform_type, '当前平台')} 官方通道尚未实现定时回读")

    if platform_type == 6:
        errors.append("TikTok Direct Post 尚未通过开发者应用审核，正式公开发布保持关闭")

    return {
        "ok": not errors,
        "errors": errors,
        "platformType": platform_type,
        "accountReferences": references,
        "files": files,
    }


def run_official_publish_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """执行用户已确认的官方 API 上传，并返回可区分平台回执。"""

    checked = validate_official_publish_payload(payload)
    if not checked["ok"]:
        raise ValueError("；".join(checked["errors"]))
    platform_type = int(checked["platformType"])
    reference = checked["accountReferences"][0]
    visibility = str(payload.get("visibility") or "public")
    scheduled_at = _schedule(payload) if payload.get("enableTimer") else None
    if platform_type == 7:
        mode = (
            PublicationMode.SCHEDULED
            if scheduled_at
            else {
                "public": PublicationMode.PUBLIC,
                "private": PublicationMode.PRIVATE,
                "unlisted": PublicationMode.UNLISTED,
            }.get(visibility, PublicationMode.PRIVATE)
        )
        capability = YouTubeCapability()
    elif platform_type == 8:
        mode = PublicationMode.PUBLIC
        capability = InstagramCapability(
            availability=CapabilityAvailability.AVAILABLE
        )
    elif platform_type == 9:
        mode = PublicationMode.PUBLIC
        capability = FacebookCapability(
            availability=CapabilityAvailability.AVAILABLE
        )
    else:
        raise ValueError("TikTok Direct Post 尚未开放")

    results = []
    for video_path in checked["files"]:
        request = UploadRequest(
            account_reference=reference,
            video_path=video_path,
            title=str(payload.get("title") or "").strip(),
            description=str(payload.get("description") or ""),
            tags=tuple(str(item).lstrip("#") for item in payload.get("tags") or []),
            thumbnail_path=(
                Path(str(payload.get("coverPath")))
                if payload.get("coverPath")
                else None
            ),
            mode=mode,
            scheduled_at=scheduled_at,
            made_for_kids=bool(payload.get("madeForKids", False)),
            ai_generated=bool(payload.get("aiGenerated", False)),
            notify_subscribers=bool(payload.get("notifySubscribers", True)),
            share_to_feed=bool(payload.get("shareToFeed", True)),
            user_confirmed_upload=True,
        )
        result = _run(capability.upload(request))
        results.append(result)
        if not result.accepted:
            break

    ok = bool(results) and all(item.accepted for item in results)
    evidence = [
        {
            "stage": record.stage.value,
            "verified": record.verified,
            "message": record.message,
            "reference": record.reference,
        }
        for result in results
        for record in result.evidence
    ]
    references = [item.operation_reference for item in results if item.operation_reference]
    return {
        "ok": ok,
        "platformType": platform_type,
        "platform": PLATFORM_NAMES[platform_type],
        "uploaded": bool(results),
        "published": bool(ok and mode == PublicationMode.PUBLIC),
        "scheduled": bool(ok and mode == PublicationMode.SCHEDULED),
        "scheduledAt": scheduled_at.isoformat() if scheduled_at and ok else None,
        "operationReferences": references,
        "evidence": evidence,
        "message": (
            f"{PLATFORM_NAMES[platform_type]} 官方接口已回读目标状态："
            + "、".join(references)
            if ok
            else (
                f"{PLATFORM_NAMES[platform_type]} 已返回远端引用 "
                + "、".join(references)
                + "，但封面或最终状态未全部回读通过；"
                "请先到平台核对，不要盲目重试"
                if references
                else f"{PLATFORM_NAMES[platform_type]} 官方接口未能验证目标状态"
            )
        ),
    }
