# -*- coding: utf-8 -*-
"""Instagram/Facebook 浏览器正式发布的受控入口。

该通道复用恢复的 Meta Business Suite 执行器，但必须在一键发
桌面端完成两个独立确认，并且只在 Meta 返回明确成功证据时
才记为成功。验证码、二次验证、风控或未知页面状态会在可见
浏览器中安全停止。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from myUtils.postVideo import post_video_facebook, post_video_instagram
from utils.publish_observer import publish_context

from .overseas.meta.browser_policy import (
    META_BROWSER_AUTOMATION_ACKNOWLEDGED,
    META_BROWSER_PLATFORM_TYPES,
    META_BROWSER_PUBLISH_CONFIRMED,
    browser_publish_confirmation_valid,
)
from .paths import COOKIE_DIR
from .wechat_publish_policy import local_timezone_name


PLATFORM_NAMES = {8: "Instagram Reels", 9: "Facebook Reels"}
HANDLERS = {8: post_video_instagram, 9: post_video_facebook}


class OverseasBrowserPublishError(RuntimeError):
    """Meta 浏览器发布无法在受控边界内继续。"""


def _schedule_time(payload: dict[str, Any]) -> str | None:
    value = str(payload.get("scheduleTime") or "").strip()
    enabled = bool(payload.get("enableTimer"))
    if not enabled:
        return None
    if not value:
        raise ValueError("已开启 Meta 定时发布，但未设置发布时间")
    timezone_name = str(payload.get("scheduleTimezone") or "").strip()
    if timezone_name and timezone_name != local_timezone_name():
        raise ValueError(
            "Meta 浏览器定时只能使用当前系统时区，请重新选择时间"
        )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("Meta 定时时间必须为 YYYY-MM-DD HH:MM") from exc
    if parsed <= datetime.now():
        raise ValueError("Meta 定时发布必须晚于当前时间")
    return value


def validate_meta_browser_publish_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """只校验本地文件、会话和一次性确认，不启动浏览器。"""

    platform_type = int(payload.get("type") or 0)
    errors: list[str] = []
    if platform_type not in META_BROWSER_PLATFORM_TYPES:
        errors.append("当前载荷不是 Meta 浏览器发布目标")
    if str(payload.get("contentType") or "") != "video":
        errors.append("Meta 浏览器通道当前只支持 Reels 视频")
    if str(payload.get("runtimeMode") or "") != "publish":
        errors.append("Meta 浏览器正式通道必须使用 publish 模式")
    if payload.get("debugDryRun") is not False:
        errors.append("Meta 正式发布必须明确 debugDryRun=false")
    if not browser_publish_confirmation_valid(payload):
        errors.append("缺少 Meta 浏览器正式发布的两项独立确认")
    if set(payload.get("accountAuthModes") or []) != {"browser"}:
        errors.append("Meta 浏览器通道不能混用官方 API 账号")
    account_files = [
        Path(str(item)).name
        for item in payload.get("accountList") or []
        if str(item).strip()
    ]
    if len(account_files) != 1:
        errors.append("Meta 浏览器正式发布每次必须精确选择一个账号")
    elif not (COOKIE_DIR / account_files[0]).is_file():
        errors.append("一键发 Meta 本地会话不存在，请重新登录")
    files = [Path(str(item)) for item in payload.get("fileList") or []]
    if not files:
        errors.append("未选择 Meta Reels 视频")
    elif any(not item.is_file() for item in files):
        errors.append("Meta Reels 视频文件不存在")
    if not str(payload.get("title") or "").strip():
        errors.append("Meta Reels 标题不能为空")
    if str(payload.get("visibility") or "public") != "public":
        errors.append("Meta Reels 浏览器正式通道当前只允许公开发布")
    schedule = None
    try:
        schedule = _schedule_time(payload)
    except ValueError as exc:
        errors.append(str(exc))
    return {
        "ok": not errors,
        "errors": errors,
        "platformType": platform_type,
        "accountList": account_files,
        "files": files,
        "scheduleTime": schedule,
    }

def run_meta_browser_publish_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """执行可见 Meta 浏览器发布，必须回读平台成功证据。"""

    checked = validate_meta_browser_publish_payload(payload)
    if not checked["ok"]:
        raise OverseasBrowserPublishError("；".join(checked["errors"]))
    platform_type = int(checked["platformType"])
    handler = HANDLERS[platform_type]
    with publish_context(mode="publish", background_mode=False):
        results = handler(
            str(payload.get("title") or ""),
            [str(path) for path in checked["files"]],
            list(payload.get("tags") or []),
            checked["accountList"],
            payload.get("category"),
            bool(checked["scheduleTime"]),
            1,
            [checked["scheduleTime"][-5:]] if checked["scheduleTime"] else [],
            0,
            description=str(payload.get("description") or ""),
            cover_path=payload.get("coverPath"),
            cover_paths=payload.get("coverPaths") or {},
            schedule_time=checked["scheduleTime"],
            jitter_minutes=0,
            dry_run=False,
            dry_run_hold_browser=False,
            publish_confirmed=bool(payload[META_BROWSER_PUBLISH_CONFIRMED]),
            automation_acknowledged=bool(
                payload[META_BROWSER_AUTOMATION_ACKNOWLEDGED]
            ),
        )
    normalized = [item for item in (results or []) if isinstance(item, dict)]
    expected_status = "scheduled" if checked["scheduleTime"] else "published"
    verified = bool(normalized) and all(
        item.get("status") == expected_status and item.get("evidence")
        for item in normalized
    )
    if not verified:
        raise OverseasBrowserPublishError(
            f"{PLATFORM_NAMES[platform_type]} 最终按钮已处理，但没有得到可验证的平台成功回执"
        )
    references = [str(item.get("evidence") or "") for item in normalized]
    return {
        "ok": True,
        "platformType": platform_type,
        "platform": PLATFORM_NAMES[platform_type],
        "scheduled": bool(checked["scheduleTime"]),
        "scheduledAt": checked["scheduleTime"],
        "published": not bool(checked["scheduleTime"]),
        "operationReferences": references,
        "message": (
            f"{PLATFORM_NAMES[platform_type]} 已回读定时成功：{checked['scheduleTime']}"
            if checked["scheduleTime"]
            else f"{PLATFORM_NAMES[platform_type]} 已回读公开发布成功"
        ),
    }
