# -*- coding: utf-8 -*-
"""一键发桌面端发布任务服务。

恢复代码只作为能力参考；这里仅调度一键发自己的会话、任务表和执行器。
小红书图文/视频与公众号正式发表均使用一键发内置受控执行器；其他平台仍保持
既有安全边界。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from myUtils.postVideo import post_video_batch_draft_tabs

from . import (
    douyin_commerce_service,
    douyin_location_service,
    douyin_publish_executor,
    oneclick_capabilities,
    oneclick_preflight,
    overseas_browser_publish,
    overseas_preflight,
    task_service,
    wechat_publish_executor,
    wechat_publish_policy,
    xhs_publish_executor,
)
from .paths import VIDEO_DIR


_publish_lock = threading.Lock()
_active_threads: dict[int, threading.Thread] = {}


_PLATFORM_NAMES = {
    1: "小红书", 2: "视频号", 3: "抖音", 4: "快手", 5: "B站",
    6: "TikTok", 7: "YouTube", 8: "Instagram Reels", 9: "Facebook Reels",
    10: "公众号",
}


def _runtime_media_path(value: object) -> str:
    """把素材库中的安全文件名解析为运行时绝对路径。"""

    raw = str(value or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if path.is_file():
        return str(path.resolve())
    stored = VIDEO_DIR / path.name
    return str(stored.resolve()) if stored.is_file() else raw


def _validate_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not payloads:
        raise ValueError("没有可执行的预发布检查项")
    validated: list[dict[str, Any]] = []
    for raw in payloads:
        payload = dict(raw)
        if (
            str(payload.get("workflow") or "").strip() == "douyin-commerce-batch"
            or str(payload.get("batchWorkflow") or "").strip()
            == "douyin-commerce-batch"
        ):
            raise ValueError(
                "抖音带货批量任务必须使用批量执行器，不能进入通用发布服务"
            )
        payload["fileList"] = [
            _runtime_media_path(item)
            for item in payload.get("fileList") or []
        ]
        payload["coverPath"] = _runtime_media_path(payload.get("coverPath"))
        if isinstance(payload.get("coverPaths"), dict):
            payload["coverPaths"] = {
                str(ratio): _runtime_media_path(path)
                for ratio, path in payload["coverPaths"].items()
                if _runtime_media_path(path)
            }
        if isinstance(payload.get("imagePlacements"), list):
            payload["imagePlacements"] = [
                {
                    **dict(item),
                    "path": _runtime_media_path(dict(item).get("path")),
                }
                for item in payload["imagePlacements"]
                if isinstance(item, dict)
            ]
        runtime_mode = str(payload.get("runtimeMode") or "preflight")
        platform_type = int(payload.get("type") or 0)
        if platform_type == 3:
            location_keyword = " ".join(
                str(payload.get("locationKeyword") or "").split()
            )
            location = douyin_location_service.normalize_location_candidate(
                payload.get("locationPoi")
            )
            if location_keyword:
                if not location or location["name"] != location_keyword:
                    raise ValueError(
                        "抖音发布定位必须来自一键发官方地点候选，不能只传关键词"
                    )
                payload["locationKeyword"] = location["name"]
                payload["locationPoi"] = location
            else:
                payload["locationKeyword"] = ""
                payload["locationPoi"] = {}
            if str(payload.get("workflow") or "") == "douyin-commerce":
                try:
                    payload.update(
                        douyin_commerce_service.validate_douyin_commerce_payload(payload)
                    )
                except douyin_commerce_service.DouyinCommerceError as exc:
                    raise ValueError(str(exc)) from exc
        if runtime_mode == "preflight":
            if payload.get("debugDryRun") is not True:
                raise ValueError("预发布检查必须保持 debugDryRun=true")
        elif runtime_mode == "publish":
            if platform_type not in {1, 3, 8, 9, 10}:
                raise ValueError(
                    "当前正式发布执行器只开放小红书、抖音、公众号和 Meta 浏览器通道"
                )
            if payload.get("debugDryRun") is not False:
                raise ValueError("正式发布必须明确 debugDryRun=false")
            if platform_type == 3:
                # 抖音正式发布强制前台并规范化结构化 POI，写回任务载荷，
                # 让任务记录、执行器与页面实际行为保持一致。
                payload.update(
                    douyin_publish_executor.validate_douyin_publish_payload(payload)
                )
            elif platform_type in {8, 9}:
                checked = overseas_browser_publish.validate_meta_browser_publish_payload(
                    payload
                )
                if not checked["ok"]:
                    raise ValueError("；".join(checked["errors"]))
            elif platform_type == 1:
                if str(payload.get("contentType") or "") not in {"article", "video"}:
                    raise ValueError("小红书正式发布只支持图文或视频")
                if payload.get("enableTimer") is True and not payload.get("scheduleTime"):
                    raise ValueError("小红书已开启定时发布，但未设置发布时间")
            else:
                wechat_publish_policy.normalize_wechat_publish_preferences(payload)
        elif runtime_mode == "draft":
            if platform_type not in {2, 5}:
                raise ValueError(
                    "当前平台没有可验证的草稿保存通道；"
                    "目前仅视频号和B站支持"
                )
        else:
            raise ValueError("当前任务模式不支持")
        capability = oneclick_capabilities.validate_payload(
            {
                "platform": payload.get("type") and _PLATFORM_NAMES.get(int(payload["type"]), ""),
                "content_type": payload.get("contentType"),
                "title": payload.get("title"),
                "description": payload.get("description"),
                "fileList": payload.get("fileList") or [],
                "coverPath": payload.get("coverPath"),
            }
        )
        if not capability.get("valid"):
            raise ValueError("；".join(capability.get("errors") or ["发布内容未通过预检"]))
        missing = [str(path) for path in payload.get("fileList") or [] if not Path(str(path)).is_file()]
        if missing:
            raise ValueError("素材文件不存在，请重新选择素材")
        if (
            int(payload.get("type") or 0) in {3, 10}
            and str(payload.get("contentType") or "") == "text"
            and not Path(str(payload.get("coverPath") or "")).is_file()
        ):
            platform_name = _PLATFORM_NAMES[int(payload["type"])]
            raise ValueError(f"{platform_name}文字预检需要选择本地封面图片")
        validated.append(payload)
    return validated


def _run_preflight(task: dict, payloads: list[dict[str, Any]]) -> None:
    if not _publish_lock.acquire(blocking=False):
        task_service.mark_platform_result(
            task["id"], int(payloads[0]["type"]), ok=False,
            message="已有预检任务正在执行，请稍后重试",
            content_type=str(payloads[0].get("contentType") or ""),
        )
        _active_threads.pop(int(task["id"]), None)
        return
    try:
        task_service.mark_task_running(task["id"], "一键发开始执行真实预发布检查")
        for payload in payloads:
            platform_type = int(payload["type"])
            task_service.record_task_event(task["id"], "platform_started", f"开始检查{platform_type}号平台的素材上传与表单填写")
            try:
                if (
                    platform_type == 3
                    and str(payload.get("workflow") or "") == "douyin-commerce"
                ):
                    result = douyin_publish_executor.run_douyin_commerce_preflight_sync(
                        payload,
                        task_id=int(task["id"]),
                    )
                elif platform_type in {6, 7, 8, 9}:
                    result = overseas_preflight.run_overseas_preflight_sync(payload)
                else:
                    result = oneclick_preflight.run_preflight_sync(payload)
                task_service.mark_platform_result(
                    task["id"],
                    platform_type,
                    ok=bool(result.get("ok")),
                    message=str(result.get("message") or "预发布检查结束"),
                    content_type=str(payload.get("contentType") or ""),
                )
            except Exception as exc:
                task_service.mark_platform_result(
                    task["id"],
                    platform_type,
                    ok=False,
                    message=f"预检任务异常：{type(exc).__name__}：{exc}",
                    content_type=str(payload.get("contentType") or ""),
                )
    except Exception as exc:
        task_service.mark_platform_result(
            task["id"], int(payloads[0]["type"]), ok=False,
            message=f"预检任务异常：{type(exc).__name__}：{exc}",
            content_type=str(payloads[0].get("contentType") or ""),
        )
    finally:
        _publish_lock.release()
        _active_threads.pop(int(task["id"]), None)


def _run_publish(task: dict, payloads: list[dict[str, Any]]) -> None:
    """执行已获用户确认的小红书图文/视频或公众号正式提交。"""

    if not _publish_lock.acquire(blocking=False):
        task_service.mark_platform_result(
            task["id"],
            int(payloads[0]["type"]),
            ok=False,
            message="已有发布任务正在执行，请稍后重试",
            content_type=str(payloads[0].get("contentType") or ""),
            event_type="platform_publish",
        )
        _active_threads.pop(int(task["id"]), None)
        return
    try:
        task_service.mark_task_running(task["id"], "一键发开始执行受控正式发布")
        for payload in payloads:
            platform_type = int(payload["type"])
            platform_name = _PLATFORM_NAMES.get(platform_type, "当前平台")
            task_service.record_task_event(
                task["id"],
                "platform_publish_started",
                f"开始执行{platform_name}字段回读与正式提交",
            )
            try:
                if platform_type == 1:
                    result = xhs_publish_executor.run_xhs_publish_sync(
                        payload,
                        task_id=int(task["id"]),
                    )
                elif platform_type == 3:
                    result = douyin_publish_executor.run_douyin_publish_sync(
                        payload,
                        task_id=int(task["id"]),
                    )
                elif platform_type == 10:
                    result = wechat_publish_executor.run_wechat_publish_sync(
                        payload,
                        task_id=int(task["id"]),
                    )
                elif platform_type in {8, 9}:
                    result = overseas_browser_publish.run_meta_browser_publish_sync(
                        payload
                    )
                else:
                    raise ValueError(f"{platform_name}尚未接入受控正式发布执行器")
                task_service.mark_platform_result(
                    task["id"],
                    platform_type,
                    ok=bool(result.get("ok")),
                    message=str(
                        result.get("message")
                        or (
                            (
                                f"小红书定时发布成功：{result.get('scheduledAt')}"
                                if platform_type == 1 and result.get("scheduled")
                                else f"{platform_name}正式提交结束"
                            )
                        )
                    ),
                    content_type=str(payload.get("contentType") or ""),
                    event_type="platform_publish",
                )
            except Exception as exc:
                task_service.mark_platform_result(
                    task["id"],
                    platform_type,
                    ok=False,
                    message=f"正式发布异常：{type(exc).__name__}：{exc}",
                    content_type=str(payload.get("contentType") or ""),
                    event_type="platform_publish",
                )
    except Exception as exc:
        task_service.mark_platform_result(
            task["id"],
            int(payloads[0]["type"]),
            ok=False,
            message=f"正式发布异常：{type(exc).__name__}：{exc}",
            content_type=str(payloads[0].get("contentType") or ""),
            event_type="platform_publish",
        )
    finally:
        _publish_lock.release()
        _active_threads.pop(int(task["id"]), None)


def _run_draft(task: dict, payloads: list[dict[str, Any]]) -> None:
    """执行已接入并可回读的国内平台草稿保存。"""

    if not _publish_lock.acquire(blocking=False):
        task_service.mark_platform_result(
            task["id"],
            int(payloads[0]["type"]),
            ok=False,
            message="已有发布任务正在执行，请稍后重试",
            content_type=str(payloads[0].get("contentType") or ""),
            event_type="platform_draft",
        )
        _active_threads.pop(int(task["id"]), None)
        return
    try:
        task_service.mark_task_running(
            task["id"], "一键发开始执行受控平台草稿保存"
        )
        results = post_video_batch_draft_tabs(payloads)
        by_platform: dict[int, list[dict[str, Any]]] = {}
        for result in results or []:
            try:
                result_type = int(result.get("type") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            by_platform.setdefault(result_type, []).append(result)
        for payload in payloads:
            platform_type = int(payload["type"])
            platform_results = by_platform.get(platform_type, [])
            failed = [
                item for item in platform_results if item.get("ok") is False
            ]
            ok = bool(platform_results) and not failed
            message = (
                str(failed[0].get("message") or "平台草稿保存失败")
                if failed
                else "平台已返回可验证的草稿保存结果"
                if ok
                else "平台草稿执行器未返回可验证结果"
            )
            task_service.mark_platform_result(
                task["id"],
                platform_type,
                ok=ok,
                message=message,
                content_type=str(payload.get("contentType") or ""),
                event_type="platform_draft",
            )
    except Exception as exc:
        task_service.mark_platform_result(
            task["id"],
            int(payloads[0]["type"]),
            ok=False,
            message=f"平台草稿异常：{type(exc).__name__}：{exc}",
            content_type=str(payloads[0].get("contentType") or ""),
            event_type="platform_draft",
        )
    finally:
        _publish_lock.release()
        _active_threads.pop(int(task["id"]), None)


def start_desktop_publish(payloads: list[dict[str, Any]]) -> dict:
    """按载荷启动预检或已确认的公众号正式发布任务。"""

    prepared = _validate_payloads(payloads)
    runtime_mode = str(prepared[0].get("runtimeMode") or "preflight")
    if any(str(item.get("runtimeMode") or "preflight") != runtime_mode for item in prepared):
        raise ValueError("同一任务不能混合预检与正式发布")
    is_publish = runtime_mode == "publish"
    is_draft = runtime_mode == "draft"
    task = task_service.create_pending_task(
        prepared,
        mode=(
            "oneclick_publish"
            if is_publish
            else "oneclick_draft"
            if is_draft
            else "oneclick_preflight"
        ),
    )
    worker = threading.Thread(
        target=_run_publish if is_publish else _run_draft if is_draft else _run_preflight,
        args=(task, prepared),
        daemon=True,
        name=(
            f"oneclick-{'publish' if is_publish else 'draft' if is_draft else 'preflight'}-"
            f"{task['id']}"
        ),
    )
    _active_threads[int(task["id"])] = worker
    worker.start()
    return task


def is_task_running(task_id: int) -> bool:
    worker = _active_threads.get(int(task_id))
    return bool(worker and worker.is_alive())
