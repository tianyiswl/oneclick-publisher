# -*- coding: utf-8 -*-
"""海外平台视频预发布检查。

本模块只调用已恢复的蚁小二 TikTok、YouTube 和 Meta 浏览器执行器。
它强制 ``debugDryRun=true`` 并停在最终按钮前，不会点击最终发布动作。
YouTube 在上传后可能由平台保留私密内容，必须在结果中明确标记。
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

    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    tags = [
        str(item).strip().lstrip("#")
        for item in payload.get("tags") or []
        if str(item).strip().lstrip("#")
    ]
    caption_parts = [part for part in (title, description) if part]
    if tags:
        caption_parts.append(" ".join(f"#{item}" for item in tags))
    caption = "\n\n".join(caption_parts)
    if platform_type == 7:
        if len(title) > 100:
            errors.append("YouTube 标题不能超过 100 个字符")
        if len(description.encode("utf-8")) > 5000:
            errors.append("YouTube 描述不能超过 5000 字节")
        if payload.get("notifySubscribers") is False:
            errors.append(
                "YouTube 浏览器预检尚无法稳定回读“不通知订阅者”；"
                "请保持默认通知"
            )
    elif platform_type in {6, 8, 9}:
        limit = 2048 if platform_type == 9 else 2200
        if not caption:
            errors.append(f"{platform_name} 必须填写标题、正文或话题")
        elif len(caption) > limit:
            errors.append(
                f"{platform_name} 合并文案不能超过 {limit} 个字符，"
                "一键发不会静默截断"
            )
    if platform_type == 8 and payload.get("shareToFeed") is False:
        errors.append(
            "Instagram 浏览器预检尚无法稳定回读“仅 Reels”；"
            "请保持同时分享到动态"
        )
    if platform_type in {8, 9} and payload.get("aiGenerated") is True:
        errors.append(
            f"{platform_name} 浏览器执行器尚未可靠回读 AI 声明；"
            "为避免遗漏合规字段已安全停止"
        )

    files = [Path(str(item)) for item in payload.get("fileList") or []]
    if not files:
        errors.append("海外视频预检缺少视频素材")
    else:
        missing_files = [path.name for path in files if not path.is_file()]
        if missing_files:
            errors.append("视频素材不存在：" + "、".join(missing_files))
    if platform_type == 7 and len(files) != 1:
        errors.append("YouTube 预检每次必须精确选择一条视频")

    account_files = [Path(str(item)).name for item in payload.get("accountList") or []]
    if not account_files:
        errors.append("海外视频预检缺少目标账号")
    else:
        missing_accounts = [
            name for name in account_files if not (COOKIE_DIR / name).is_file()
        ]
        if missing_accounts:
            errors.append("一键发本地登录会话不存在，请重新登录")
    if platform_type == 7 and len(account_files) != 1:
        errors.append("YouTube 预检每次必须精确选择一个账号")

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
        results = handler(
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
            visibility=str(payload.get("visibility") or "private"),
            collection_name=str(payload.get("collectionName") or ""),
            ai_generated=bool(payload.get("aiGenerated", False)),
            made_for_kids=bool(payload.get("madeForKids", False)),
            notify_subscribers=bool(payload.get("notifySubscribers", True)),
            share_to_feed=bool(payload.get("shareToFeed", True)),
        )
    if platform_type == 7:
        raw_results = [results] if isinstance(results, dict) else list(results or [])
        receipts = [item for item in raw_results if isinstance(item, dict)]
        required_fields = {"video", "title", "audience", "visibility"}
        if str(payload.get("description") or "").strip():
            required_fields.add("description")
        if any(str(item).strip() for item in payload.get("tags") or []):
            required_fields.add("tags")
        if str(payload.get("coverPath") or "").strip() or any(
            str(item).strip()
            for item in (payload.get("coverPaths") or {}).values()
        ):
            required_fields.add("thumbnail")
        if str(payload.get("collectionName") or "").strip():
            required_fields.add("playlist")
        if payload.get("aiGenerated") is True:
            required_fields.add("aiGenerated")

        receipt = receipts[0] if len(receipts) == 1 else {}
        verified_fields = {
            str(item) for item in receipt.get("verifiedFields") or [] if str(item)
        }
        requested_visibility = str(payload.get("visibility") or "private").lower()
        receipt_is_valid = (
            receipt.get("status") == "preflight_ready"
            and bool(receipt.get("evidence"))
            and receipt.get("platformMutation") == "private_upload"
            and str(receipt.get("visibility") or "").lower()
            == requested_visibility
            and required_fields.issubset(verified_fields)
        )
        if not receipt_is_valid:
            missing = sorted(required_fields - verified_fields)
            detail = "、".join(missing) if missing else "平台预检回执"
            raise OverseasPreflightError(
                f"YouTube 未取得完整逐字段回读：{detail}；未执行最终 SAVE/Publish"
            )
        return {
            "type": platform_type,
            "ok": True,
            "evidence": str(receipt["evidence"]),
            "verifiedFields": sorted(verified_fields),
            "visibility": requested_visibility,
            "platformMutation": "private_upload",
            "message": (
                "YouTube 已上传测试视频并完成逐字段回读，停在最终 SAVE/Publish 前；"
                "YouTube 可能把未完成的上传保留为私密内容，本次未公开发布。"
            ),
        }
    return {
        "type": platform_type,
        "ok": True,
        "message": (
            f"{checked['platform']} 恢复通道已完成视频上传与文案填写检查，"
            "并停在最终发布按钮前；未保存草稿、未发布。"
        ),
    }
