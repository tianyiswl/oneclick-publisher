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


ROOT_DIR = Path(__file__).resolve().parents[1]
_ACTIVE: dict[int, subprocess.Popen[str]] = {}
_ACTIVE_LOCK = threading.Lock()


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
    if mode not in {"preflight", "formal"}:
        raise ControlledPublishError(
            "silicon_evolution_mode_invalid", "自动直发模式无效"
        )
    action = "silicon-preflight" if mode == "preflight" else "silicon-formal"
    prepared = dict(request)
    prepared.pop("mode", None)
    prepared["mode"] = mode
    return _submit_action_in_process(
        action,
        prepared,
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
