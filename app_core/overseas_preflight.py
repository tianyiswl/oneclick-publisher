# -*- coding: utf-8 -*-
"""海外平台视频预发布检查。

本模块只调用已恢复的蚁小二 TikTok、YouTube 和 Meta 浏览器执行器。
它强制 ``debugDryRun=true`` 并停在最终按钮前，不会保存草稿或发布。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from myUtils.postVideo import (
    post_video_facebook,
    post_video_instagram,
    post_video_tiktok,
    post_video_youtube,
)
from utils.publish_observer import publish_context

from .paths import COOKIE_DIR


OVERSEAS_VIDEO_PLATFORM_TYPES = frozenset({6, 7, 8, 9})
PLATFORM_NAMES = {
    6: "TikTok",
    7: "YouTube",
    8: "Instagram Reels",
    9: "Facebook Reels",
}
PREFLIGHT_HANDLERS: dict[int, Callable[..., Any]] = {
    6: post_video_tiktok,
    7: post_video_youtube,
    8: post_video_instagram,
    9: post_video_facebook,
}


class OverseasPreflightError(RuntimeError):
    """海外视频预检无法在安全边界内继续。"""


def validate_overseas_preflight_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """只校验本地字段和文件，不启动浏览器。"""

    platform_type = int(payload.get("type") or 0)
    platform_name = PLATFORM_NAMES.get(platform_type, f"平台{platform_type}")
    errors: list[str] = []
    if platform_type not in OVERSEAS_VIDEO_PLATFORM_TYPES:
        errors.append("当前载荷不属于已恢复的海外视频平台")
    if str(payload.get("runtimeMode") or "preflight") != "preflight":
        errors.append("海外浏览器执行器当前只允许预发布检查")
    if payload.get("debugDryRun") is not True:
        errors.append("海外预发布检查必须保持 debugDryRun=true")
    if str(payload.get("contentType") or "") != "video":
        errors.append(f"{platform_name} 恢复执行器当前只验收了视频通道")
    if payload.get("enableTimer"):
        errors.append(f"{platform_name} 浏览器预检尚未回读定时时间，请先关闭定时")

    files = [Path(str(item)) for item in payload.get("fileList") or []]
    if not files:
        errors.append("海外视频预检缺少视频素材")
    else:
        missing_files = [path.name for path in files if not path.is_file()]
        if missing_files:
            errors.append("视频素材不存在：" + "、".join(missing_files))

    account_files = [Path(str(item)).name for item in payload.get("accountList") or []]
    if not account_files:
        errors.append("海外视频预检缺少目标账号")
    else:
        missing_accounts = [
            name for name in account_files if not (COOKIE_DIR / name).is_file()
        ]
        if missing_accounts:
            errors.append("一键发本地登录会话不存在，请重新登录")

    return {
        "ok": not errors,
        "type": platform_type,
        "platform": platform_name,
        "errors": errors,
        "fileList": [str(path) for path in files],
        "accountList": account_files,
    }


def run_overseas_preflight_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """执行恢复的视频上传与字段填写，最终按钮始终锁定。"""

    checked = validate_overseas_preflight_payload(payload)
    if not checked["ok"]:
        raise OverseasPreflightError("；".join(checked["errors"]))

    platform_type = int(checked["type"])
    handler = PREFLIGHT_HANDLERS[platform_type]
    background_mode = bool(payload.get("backgroundMode", True))
    with publish_context(mode="preflight", background_mode=background_mode):
        handler(
            str(payload.get("title") or ""),
            checked["fileList"],
            list(payload.get("tags") or []),
            checked["accountList"],
            payload.get("category"),
            False,
            1,
            [],
            0,
            description=str(payload.get("description") or ""),
            cover_path=payload.get("coverPath"),
            cover_paths=payload.get("coverPaths") or {},
            schedule_time=None,
            jitter_minutes=0,
            dry_run=True,
            dry_run_hold_browser=bool(
                payload.get("debugDryRunHoldBrowser", not background_mode)
            ),
        )
    return {
        "type": platform_type,
        "ok": True,
        "message": (
            f"{checked['platform']} 恢复通道已完成视频上传与文案填写检查，"
            "并停在最终发布按钮前；未保存草稿、未发布。"
        ),
    }
