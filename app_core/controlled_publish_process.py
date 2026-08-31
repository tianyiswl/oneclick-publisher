# -*- coding: utf-8 -*-
"""MCP 调用现有受控 CLI 的进程隔离适配器。"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

from .controlled_publish import ControlledPublishError
from .meta_browser_policy import (
    META_BROWSER_AUTOMATION_ACKNOWLEDGED,
    META_BROWSER_PUBLISH_CONFIRMED,
)
from .overseas_meta_page_identity import facebook_page_v1_enabled


ROOT_DIR = Path(__file__).resolve().parents[1]
_ACTIVE: dict[int, subprocess.Popen[str]] = {}
_ACTIVE_LOCK = threading.Lock()


def _authorized_preflight_payloads(
    preflight_task_id: int,
    authorization_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Load one successful immutable preflight without caller-supplied content."""

    if (
        type(preflight_task_id) is not int
        or preflight_task_id <= 0
        or not str(authorization_id or "").strip()
    ):
        raise ControlledPublishError(
            "controlled_authorization_required",
            "正式发布必须携带已完成预检和一次性本地授权",
        )
    from . import task_service

    preflight = task_service.get_task(preflight_task_id)
    if (
        not isinstance(preflight, dict)
        or str(preflight.get("mode") or "") != "oneclick_preflight"
    ):
        raise ControlledPublishError(
            "controlled_preflight_required",
            "正式发布缺少对应预检任务",
        )
    if str(preflight.get("status") or "") != "success":
        raise ControlledPublishError(
            "controlled_preflight_not_successful",
            "对应预检尚未全部成功",
        )
    try:
        raw_payloads = json.loads(str(preflight.get("payloadJson") or "[]"))
    except json.JSONDecodeError as exc:
        raise ControlledPublishError(
            "controlled_preflight_invalid",
            "预检任务快照不可读取",
        ) from exc
    if (
        not isinstance(raw_payloads, list)
        or not raw_payloads
        or not all(isinstance(item, dict) for item in raw_payloads)
    ):
        raise ControlledPublishError(
            "controlled_preflight_invalid",
            "预检任务快照不可读取",
        )
    return (
        preflight,
        [dict(item) for item in raw_payloads],
        str(authorization_id).strip(),
    )


def _formal_payloads_from_preflight(
    payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert only runtime fields; content and target identity stay frozen."""

    prepared: list[dict[str, Any]] = []
    for stored in payloads:
        payload = dict(stored)
        platform_type = int(payload.get("type") or 0)
        payload["runtimeMode"] = "publish"
        payload["debugDryRun"] = False
        payload["debugDryRunHoldBrowser"] = False
        if platform_type == 7 and payload.get("youtubeOfficialApi") is True:
            payload["backgroundMode"] = True
        elif platform_type in {6, 7, 8, 9}:
            payload["backgroundMode"] = False
        if platform_type == 6 or (
            platform_type == 7
            and payload.get("youtubeOfficialApi") is not True
        ):
            payload["overseasVideoPublishConfirmed"] = True
        if platform_type == 8:
            # Instagram V1 不再使用旧 Meta 布尔确认。正式动作将
            # 由平台表单预检快照和数据库 claim 绑定。
            payload.pop(META_BROWSER_PUBLISH_CONFIRMED, None)
            payload.pop(META_BROWSER_AUTOMATION_ACKNOWLEDGED, None)
            payload.pop("overseasVideoPublishConfirmed", None)
            payload["instagramExecutionIntent"] = "formal_public"
        if platform_type == 9:
            # Page authorization is the database claim; legacy Meta booleans
            # are neither trusted nor persisted on the formal task.
            payload.pop(META_BROWSER_PUBLISH_CONFIRMED, None)
            payload.pop(META_BROWSER_AUTOMATION_ACKNOWLEDGED, None)
            payload.pop("overseasVideoPublishConfirmed", None)
        prepared.append(payload)
    return prepared


def submit_authorized_preflight_task(
    preflight_task_id: int,
    authorization_id: str,
) -> dict[str, Any]:
    """Submit a frozen preflight using only its ID and one-time authorization."""

    _preflight, stored_payloads, normalized_authorization = (
        _authorized_preflight_payloads(preflight_task_id, authorization_id)
    )
    payloads = _formal_payloads_from_preflight(stored_payloads)
    platform_types = [int(item.get("type") or 0) for item in payloads]
    from . import controlled_publish, publish_service, task_service

    if 8 in platform_types:
        if len(payloads) != 1 or platform_types != [8]:
            raise ControlledPublishError(
                "instagram_target_invalid",
                "Instagram V1 一次只支持一个专业账号和一个 Reel。",
            )
        raise ControlledPublishError(
            "instagram_platform_preflight_required",
            "Instagram 本地预检不是平台表单预检，不能据此消费正式发布授权。",
        )
    if 9 in platform_types:
        if not facebook_page_v1_enabled():
            raise ControlledPublishError(
                "facebook_page_feature_disabled",
                "Facebook Page 发布功能尚未开启。",
            )
        if len(payloads) != 1 or platform_types != [9]:
            raise ControlledPublishError(
                "facebook_unsupported_publish_setting",
                "Facebook Page 首版一次只支持一个 Page 和一个视频。",
            )
        task = controlled_publish._create_claimed_facebook_page_task(
            payloads,
            preflight_task_id=preflight_task_id,
            authorization_id=normalized_authorization,
        )
    elif 6 in platform_types:
        if len(payloads) != 1 or platform_types != [6]:
            raise ControlledPublishError(
                "tiktok_target_invalid",
                "TikTok 受控正式任务一次只支持一个账号和视频",
            )
        task = controlled_publish._create_claimed_tiktok_task(
            payloads,
            mode="formal",
            preflight_task_id=preflight_task_id,
            authorization_id=normalized_authorization,
        )
        publish_service.start_controlled_tiktok_publish(int(task["id"]))
    else:
        recent = task_service.list_tasks(limit=500)
        detailed = [
            task_service.get_task(int(row.get("id") or 0)) or dict(row)
            for row in recent
            if int(row.get("id") or 0) > 0
        ]
        existing = controlled_publish._find_blocking_formal_scope_task(
            detailed,
            payloads,
        )
        if existing:
            event_types = {
                str(event.get("eventType") or "")
                for event in existing.get("events") or []
                if isinstance(event, Mapping)
            }
            ambiguous = (
                str(existing.get("status") or "") != "success"
                and bool(
                    event_types.intersection(
                        {
                            "wechat_final_submit_clicked",
                            "tiktok_final_action_triggered",
                            "tiktok_publish_outcome_ambiguous",
                        }
                    )
                )
            )
            existing_task_id = int(existing.get("id") or 0)
            message = (
                "同一账号、内容与排期已点击最终发布，"
                "结果需要先做只读核对，已阻止自动重发；"
                if ambiguous
                else "同一账号、内容与排期已有正式成功回执，"
                "已阻止重复发布；"
            )
            raise ControlledPublishError(
                (
                    "controlled_publish_outcome_ambiguous"
                    if ambiguous
                    else "controlled_already_published"
                ),
                f"{message}taskId={existing_task_id}",
            )
        from .database import connect

        with connect() as conn:
            controlled_publish.consume_authorization(
                conn,
                normalized_authorization,
                preflight_task_id,
                payloads,
            )
        task = publish_service.start_desktop_publish(payloads)
    stored = task_service.get_task(int(task["id"])) or task
    return controlled_publish.project_task(stored)


def _command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(ROOT_DIR / "desktop_native_app.py")]


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    for stream in (process.stdin, process.stdout):
        if stream is not None and not stream.closed:
            stream.close()


def _reap(task_id: int, process: subprocess.Popen[str]) -> None:
    try:
        if process.stdout is not None:
            for _line in process.stdout:
                # 最终状态已写入数据库，MCP 通过 taskId 读取；
                # 这里只持续排空管道，避免子进程被 stdout 反压卡住。
                pass
        process.wait()
    finally:
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        with _ACTIVE_LOCK:
            _ACTIVE.pop(task_id, None)


def submit_request_in_process(
    request: Mapping[str, Any],
    *,
    startup_timeout_seconds: float = 20,
) -> dict[str, Any]:
    """启动现有 CLI，立即返回初始 taskId。

    正式任务的验证窗口继续由 CLI 子进程处理；MCP 服务端
    只保留任务号和本机项目档案，不接收验证内容。
    """

    command = _command() + [
        "--controlled-publish-action",
        "create",
        "--controlled-publish-request",
        "-",
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT_DIR,
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    if process.stdin is None or process.stdout is None:
        _stop_process(process)
        raise ControlledPublishError(
            "controlled_process_stdio_unavailable", "受控发布子进程管道不可用"
        )
    process.stdin.write(
        json.dumps(dict(request), ensure_ascii=False, separators=(",", ":"))
    )
    process.stdin.close()

    first_line: queue.Queue[str] = queue.Queue(maxsize=1)

    def read_first_line() -> None:
        first_line.put(process.stdout.readline())

    threading.Thread(target=read_first_line, daemon=True).start()
    try:
        line = first_line.get(timeout=max(1.0, float(startup_timeout_seconds)))
    except queue.Empty as exc:
        _stop_process(process)
        raise ControlledPublishError(
            "controlled_process_start_timeout", "受控发布子进程未在限时内返回 taskId"
        ) from exc
    if not line:
        return_code = process.wait(timeout=3) if process.poll() is not None else None
        _stop_process(process)
        raise ControlledPublishError(
            "controlled_process_start_failed",
            f"受控发布子进程未返回 JSON（退出码 {return_code}）",
        )
    try:
        initial = json.loads(line)
    except json.JSONDecodeError as exc:
        _stop_process(process)
        raise ControlledPublishError(
            "controlled_process_protocol_invalid", "受控发布子进程返回了无效 JSON"
        ) from exc
    if not isinstance(initial, dict):
        _stop_process(process)
        raise ControlledPublishError(
            "controlled_process_protocol_invalid", "受控发布子进程返回值不是对象"
        )
    task_id = int(initial.get("taskId") or 0)
    if task_id <= 0:
        _stop_process(process)
        raise ControlledPublishError(
            str(initial.get("errorCode") or "controlled_process_start_failed"),
            str(initial.get("errorText") or "受控发布子进程未创建任务"),
        )
    with _ACTIVE_LOCK:
        _ACTIVE[task_id] = process
    threading.Thread(target=_reap, args=(task_id, process), daemon=True).start()
    return initial


def submit_silicon_evolution_request_in_process(
    request: Mapping[str, Any],
    *,
    startup_timeout_seconds: float = 20,
) -> dict[str, Any]:
    """在独立进程中启动 V1.2 冻结包的公众号任务。"""

    mode = str(request.get("mode") or "")
    if mode not in {"preflight", "formal", "direct"}:
        raise ControlledPublishError(
            "silicon_evolution_mode_invalid", "自动直发模式无效"
        )
    action = (
        "silicon-preflight"
        if mode == "preflight"
        else "silicon-direct"
        if mode == "direct"
        else "silicon-formal"
    )
    prepared = dict(request)
    prepared.pop("mode", None)
    prepared["mode"] = mode
    return _submit_action_in_process(
        action,
        prepared,
        startup_timeout_seconds=startup_timeout_seconds,
    )


def submit_douyin_graphic_matrix_request_in_process(
    request: Mapping[str, Any],
    *,
    startup_timeout_seconds: float = 20,
) -> dict[str, Any]:
    """在独立 CLI 进程中启动图文矩阵本地检查或正式任务。"""

    return _submit_action_in_process(
        "matrix",
        request,
        startup_timeout_seconds=startup_timeout_seconds,
    )


def _submit_action_in_process(
    action: str,
    request: Mapping[str, Any],
    *,
    startup_timeout_seconds: float,
) -> dict[str, Any]:
    """复用现有子进程协议启动一个受控动作。"""

    command = _command() + [
        "--controlled-publish-action",
        action,
        "--controlled-publish-request",
        "-",
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT_DIR,
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    if process.stdin is None or process.stdout is None:
        _stop_process(process)
        raise ControlledPublishError(
            "controlled_process_stdio_unavailable", "受控发布子进程管道不可用"
        )
    process.stdin.write(json.dumps(dict(request), ensure_ascii=False, separators=(",", ":")))
    process.stdin.close()
    first_line: queue.Queue[str] = queue.Queue(maxsize=1)
    threading.Thread(
        target=lambda: first_line.put(process.stdout.readline()), daemon=True
    ).start()
    try:
        line = first_line.get(timeout=max(1.0, float(startup_timeout_seconds)))
    except queue.Empty as exc:
        _stop_process(process)
        raise ControlledPublishError(
            "controlled_process_start_timeout", "受控发布子进程未在限时内返回 taskId"
        ) from exc
    if not line:
        _stop_process(process)
        raise ControlledPublishError(
            "controlled_process_start_failed", "受控发布子进程未返回 JSON"
        )
    try:
        initial = json.loads(line)
    except json.JSONDecodeError as exc:
        _stop_process(process)
        raise ControlledPublishError(
            "controlled_process_protocol_invalid", "受控发布子进程返回了无效 JSON"
        ) from exc
    task_id = int(initial.get("taskId") or 0) if isinstance(initial, dict) else 0
    if task_id <= 0:
        _stop_process(process)
        raise ControlledPublishError(
            str(initial.get("errorCode") or "controlled_process_start_failed"),
            str(initial.get("errorText") or "受控发布子进程未创建任务"),
        )
    with _ACTIVE_LOCK:
        _ACTIVE[task_id] = process
    threading.Thread(target=_reap, args=(task_id, process), daemon=True).start()
    return initial
