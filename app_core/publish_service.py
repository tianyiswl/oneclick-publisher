# -*- coding: utf-8 -*-
"""一键发桌面端预发布任务服务。

恢复代码只作为能力参考；这里仅调度一键发自己的会话、任务表和预检执行器。
正式发布、草稿保存均不由本服务执行。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from . import oneclick_capabilities, oneclick_preflight, task_service


_publish_lock = threading.Lock()
_active_threads: dict[int, threading.Thread] = {}


_PLATFORM_NAMES = {1: "小红书", 2: "视频号", 3: "抖音", 4: "快手", 5: "B站", 10: "公众号"}


def _validate_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not payloads:
        raise ValueError("没有可执行的预发布检查项")
    validated: list[dict[str, Any]] = []
    for raw in payloads:
        payload = dict(raw)
        if str(payload.get("runtimeMode") or "preflight") != "preflight" or not payload.get("debugDryRun", True):
            raise ValueError("一键发当前只允许预发布检查；正式发布必须由用户在平台页面人工确认")
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


def start_desktop_publish(payloads: list[dict[str, Any]]) -> dict:
    """启动只停在最终发布前的真实预检任务。"""

    prepared = _validate_payloads(payloads)
    task = task_service.create_pending_task(prepared, mode="oneclick_preflight")
    worker = threading.Thread(
        target=_run_preflight,
        args=(task, prepared),
        daemon=True,
        name=f"oneclick-preflight-{task['id']}",
    )
    _active_threads[int(task["id"])] = worker
    worker.start()
    return task


def is_task_running(task_id: int) -> bool:
    worker = _active_threads.get(int(task_id))
    return bool(worker and worker.is_alive())
