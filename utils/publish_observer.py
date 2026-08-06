# -*- coding: utf-8 -*-
"""发布流程事件观察器。

上传器只关心“发生了什么步骤”，不直接依赖数据库表结构。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from utils.publish_tasks import record_task_event


_PUBLISH_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("publish_context", default={})


def get_publish_context() -> dict[str, Any]:
    return dict(_PUBLISH_CONTEXT.get() or {})


@contextmanager
def publish_context(**values):
    current = get_publish_context()
    merged = {**current, **{key: value for key, value in values.items() if value is not None}}
    token = _PUBLISH_CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _PUBLISH_CONTEXT.reset(token)


def publish_event(event_type: str, message: str, level: str = "info", **detail) -> None:
    context = get_publish_context()
    task_id = context.get("task_id")
    if not task_id:
        return
    payload = {**context, **detail}
    try:
        record_task_event(
            int(task_id),
            event_type,
            message,
            level=level,
            detail=payload,
            item_id=context.get("item_id"),
        )
    except Exception as exc:
        print(f"记录发布事件失败：{exc}")


@contextmanager
def publish_step(step: str, message: str, **detail):
    publish_event(f"{step}_start", f"{message}开始", **detail)
    try:
        yield
    except Exception as exc:
        publish_event(f"{step}_failed", f"{message}失败：{exc}", level="error", error=str(exc), **detail)
        raise
    else:
        publish_event(f"{step}_success", f"{message}完成", **detail)
