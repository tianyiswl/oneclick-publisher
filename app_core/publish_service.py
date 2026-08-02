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

from . import (
    douyin_location_service,
    oneclick_capabilities,
    oneclick_preflight,
    task_service,
    wechat_publish_executor,
    wechat_publish_policy,
    xhs_publish_executor,
)
from .paths import VIDEO_DIR


_publish_lock = threading.Lock()
_active_threads: dict[int, threading.Thread] = {}


_PLATFORM_NAMES = {1: "小红书", 2: "视频号", 3: "抖音", 4: "快手", 5: "B站", 10: "公众号"}


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
        if runtime_mode == "preflight":
            if payload.get("debugDryRun") is not True:
                raise ValueError("预发布检查必须保持 debugDryRun=true")
        elif runtime_mode == "publish":
            if platform_type not in {1, 10}:
                raise ValueError("当前正式发布执行器只开放小红书和公众号")
            if payload.get("debugDryRun") is not False:
                raise ValueError("正式发布必须明确 debugDryRun=false")
            if platform_type == 1:
                if str(payload.get("contentType") or "") not in {"article", "video"}:
                    raise ValueError("小红书正式发布只支持图文或视频")
                if payload.get("enableTimer") is True and not payload.get("scheduleTime"):
                    raise ValueError("小红书已开启定时发布，但未设置发布时间")
            else:
                wechat_publish_policy.normalize_wechat_publish_preferences(payload)
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
            if platform_type == 1:
                result = xhs_publish_executor.run_xhs_publish_sync(
                    payload,
                    task_id=int(task["id"]),
                )
            else:
                result = wechat_publish_executor.run_wechat_publish_sync(
                    payload,
                    task_id=int(task["id"]),
                )
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
            int(payloads[0]["type"]),
            ok=False,
            message=f"正式发布异常：{type(exc).__name__}：{exc}",
            content_type=str(payloads[0].get("contentType") or ""),
            event_type="platform_publish",
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
    task = task_service.create_pending_task(
        prepared,
        mode="oneclick_publish" if is_publish else "oneclick_preflight",
    )
    worker = threading.Thread(
        target=_run_publish if is_publish else _run_preflight,
        args=(task, prepared),
        daemon=True,
        name=f"oneclick-{'publish' if is_publish else 'preflight'}-{task['id']}",
    )
    _active_threads[int(task["id"])] = worker
    worker.start()
    return task


def is_task_running(task_id: int) -> bool:
    worker = _active_threads.get(int(task_id))
    return bool(worker and worker.is_alive())
