# -*- coding: utf-8 -*-
"""YouTube 浏览器正式发布的兼容入口。

首版只开放单账号、单视频、立即发布。最终按钮只能在桌面端确认后点击，
并且只有上传器返回平台成功证据时才允许任务记为成功。验证码、扫码、
两步验证或未知页面状态会切换到可见浏览器并安全停止等待用户处理。

TikTok 已迁移到 ``app_core.overseas_tiktok_publish`` 的专用受控服务，
不得再从本兼容入口或 ``HANDLERS`` 双路由。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from myUtils.postVideo import post_video_youtube
from utils.publish_observer import publish_context

from .paths import COOKIE_DIR


OVERSEAS_VIDEO_PUBLISH_CONFIRMED = "overseasVideoPublishConfirmed"
PLATFORM_NAMES = {7: "YouTube"}
HANDLERS = {7: post_video_youtube}
VISIBILITY_LABELS = {
    "public": "公开",
    "private": "私密",
    "unlisted": "不公开",
}


class OverseasVideoPublishError(RuntimeError):
    """TikTok/YouTube 正式发布无法在受控边界内继续。"""


def validate_overseas_video_publish_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """只校验本地文件、会话和字段边界，不启动浏览器。"""

    platform_type = int(payload.get("type") or 0)
    platform_name = (
        "TikTok"
        if platform_type == 6
        else PLATFORM_NAMES.get(platform_type, "海外平台")
    )
    errors: list[str] = []
    if platform_type not in {6, *PLATFORM_NAMES}:
        errors.append("当前载荷不是 TikTok 或 YouTube 正式发布目标")
    if str(payload.get("contentType") or "") != "video":
        errors.append(f"{platform_name} 正式发布当前只支持视频")
    if str(payload.get("runtimeMode") or "") != "publish":
        errors.append(f"{platform_name} 正式发布必须使用 publish 模式")
    if payload.get("debugDryRun") is not False:
        errors.append(f"{platform_name} 正式发布必须明确 debugDryRun=false")
    if payload.get(OVERSEAS_VIDEO_PUBLISH_CONFIRMED) is not True:
        errors.append(f"缺少 {platform_name} 正式发布确认")
    if payload.get("enableTimer") is True or payload.get("scheduleTime"):
        errors.append(f"{platform_name} 首版仅开放立即发布，暂不支持定时发布")

    account_files = [
        Path(str(item)).name
        for item in payload.get("accountList") or []
        if str(item).strip()
    ]
    if len(account_files) != 1:
        errors.append(f"{platform_name} 正式发布每次必须精确选择一个账号")
    elif not (COOKIE_DIR / account_files[0]).is_file():
        errors.append(f"一键发 {platform_name} 本地登录会话不存在，请重新登录")

    files = [Path(str(item)) for item in payload.get("fileList") or []]
    if len(files) != 1:
        errors.append(f"{platform_name} 首版每次必须精确选择一条视频")
    elif not files[0].is_file():
        errors.append(f"{platform_name} 视频文件不存在")

    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    tags = [str(item).strip().lstrip("#") for item in payload.get("tags") or [] if str(item).strip()]
    visibility = str(payload.get("visibility") or "public").strip().lower()
    if not title:
        errors.append(f"{platform_name} 视频标题不能为空")

    if platform_type == 6:
        caption_parts = [item for item in (title, description) if item]
        if tags:
            caption_parts.append(" ".join(f"#{tag}" for tag in tags))
        caption = "\n\n".join(caption_parts)
        if len(caption) > 2200:
            errors.append("TikTok 标题、文案与话题合计不能超过 2200 个字符")
        if visibility != "public":
            errors.append("TikTok 首版正式发布仅支持公开可见")
        if payload.get("aiGenerated") is True:
            errors.append("TikTok AI 内容声明尚未接入可靠回读，已安全停止")
        if str(payload.get("collectionName") or "").strip():
            errors.append("TikTok 合集尚未接入可靠回读，请取消合集后发布")
        cover_path = str(payload.get("coverPath") or "").strip()
        if cover_path:
            errors.append("TikTok 自定义封面尚未接入可靠回读，请取消封面后发布")
    elif platform_type == 7:
        if len(title) > 100:
            errors.append("YouTube 标题不能超过 100 个字符")
        if len(description.encode("utf-8")) > 5000:
            errors.append("YouTube 描述不能超过 5000 字节")
        if visibility not in VISIBILITY_LABELS:
            errors.append("YouTube 可见性必须为公开、私密或不公开")
        if payload.get("notifySubscribers") is False:
            errors.append(
                "YouTube 浏览器通道尚无法稳定回读“不通知订阅者”，请保持通知开启"
            )

    return {
        "ok": not errors,
        "errors": errors,
        "platformType": platform_type,
        "platform": platform_name,
        "accountList": account_files,
        "files": files,
        "title": title,
        "description": description,
        "tags": tags,
        "visibility": visibility,
    }


def run_overseas_video_publish_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """执行可见浏览器正式发布，并要求上传器返回平台成功证据。"""

    checked = validate_overseas_video_publish_payload(payload)
    if not checked["ok"]:
        raise OverseasVideoPublishError("；".join(checked["errors"]))
    platform_type = int(checked["platformType"])
    if platform_type == 6:
        raise OverseasVideoPublishError(
            "TikTok 已迁移到专用受控服务；旧入口没有平台成功回执"
        )
    handler = HANDLERS[platform_type]
    with publish_context(mode="publish", background_mode=False):
        results = handler(
            checked["title"],
            [str(path) for path in checked["files"]],
            checked["tags"],
            checked["accountList"],
            payload.get("category"),
            False,
            1,
            [],
            0,
            description=checked["description"],
            cover_path=payload.get("coverPath"),
            cover_paths=payload.get("coverPaths") or {},
            schedule_time=None,
            jitter_minutes=0,
            dry_run=False,
            dry_run_hold_browser=False,
            browser_publish_confirmed=True,
            visibility=checked["visibility"],
            collection_name=str(payload.get("collectionName") or ""),
            ai_generated=bool(payload.get("aiGenerated", False)),
            made_for_kids=bool(payload.get("madeForKids", False)),
            notify_subscribers=bool(payload.get("notifySubscribers", True)),
        )
    normalized = [item for item in (results or []) if isinstance(item, dict)]
    expected_status = (
        "saved"
        if platform_type == 7 and checked["visibility"] != "public"
        else "published"
    )

    def receipt_is_valid(item: dict[str, Any]) -> bool:
        evidence = str(item.get("evidence") or "")
        if not evidence:
            return False
        if platform_type == 7 and not evidence.startswith(
            ("platform_feedback:", "studio_content_list:")
        ):
            return False
        return item.get("status") == expected_status

    verified = bool(normalized) and all(
        receipt_is_valid(item) for item in normalized
    )
    if not verified:
        raise OverseasVideoPublishError(
            f"{checked['platform']} 最终按钮已处理，但没有得到可验证的平台成功回执"
        )
    references = [str(item.get("evidence") or "") for item in normalized]
    published = platform_type == 6 or checked["visibility"] == "public"
    saved = platform_type == 7
    if platform_type == 7:
        message = (
            f"YouTube 已从明确平台提示或 Content 页回读完成，视频可见性："
            f"{VISIBILITY_LABELS[checked['visibility']]}"
        )
    else:
        message = "TikTok 已回读公开发布成功"
    return {
        "ok": True,
        "platformType": platform_type,
        "platform": checked["platform"],
        "published": published,
        "saved": saved,
        "visibility": checked["visibility"],
        "operationReferences": references,
        "message": message,
    }
