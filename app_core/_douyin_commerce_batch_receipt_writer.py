# -*- coding: utf-8 -*-
"""抖音带货批量执行器专用的最终平台回执写入器。

该模块不是任务服务的公开接口。未来批量执行器只可在完成平台最终回读后
导入并调用这里的写入函数；普通调用方继续使用 ``mark_batch_item_result``，
且该公开路径永远没有写入成功状态的能力。
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from .database import connect


_FINAL_EVENTS = {"platform_publish_receipt", "platform_scheduled_receipt"}
_RECEIPT_FIELDS = {
    "platformPostId",
    "postUrl",
    "publishedAt",
    "scheduleTime",
    "timezone",
}
_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_BEIJING_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _validated_receipt(
    event_type: object,
    readback: object,
    timezone: object,
) -> tuple[str, dict[str, str]]:
    """在写库前重新验证最终事件、全部字段和北京时间。"""

    if type(event_type) is not str or event_type not in _FINAL_EVENTS:
        raise ValueError("批量最终平台回执类型无效")
    if type(timezone) is not str or timezone != _SHANGHAI_TIMEZONE:
        raise ValueError("批量最终平台回执必须使用 Asia/Shanghai")
    if type(readback) is not dict:
        raise ValueError("批量最终平台回执必须是原始字段字典")

    unknown_fields = set(readback) - _RECEIPT_FIELDS
    if unknown_fields:
        raise ValueError("批量最终平台回执包含未允许字段")

    safe_readback: dict[str, str] = {}
    for key, value in readback.items():
        if type(key) is not str or type(value) is not str or not value.strip():
            raise ValueError(f"批量最终平台回执字段 {key} 必须是非空字符串")
        safe_readback[key] = value.strip()

    receipt_timezone = safe_readback.get("timezone")
    if receipt_timezone != _SHANGHAI_TIMEZONE:
        raise ValueError("批量最终平台回执字段 timezone 必须是 Asia/Shanghai")

    def _validate_beijing_datetime(field_name: str) -> None:
        value = safe_readback.get(field_name, "")
        if not _BEIJING_DATETIME_RE.fullmatch(value):
            raise ValueError(f"批量最终平台回执缺少有效的北京时间 {field_name}")
        try:
            datetime.strptime(value, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ValueError(
                f"批量最终平台回执缺少有效的北京时间 {field_name}"
            ) from exc

    normalized_event = str(event_type)
    if normalized_event == "platform_scheduled_receipt":
        _validate_beijing_datetime("scheduleTime")
    else:
        if not (safe_readback.get("platformPostId") or safe_readback.get("postUrl")):
            raise ValueError("批量发布回执缺少 platformPostId 或 postUrl")
        _validate_beijing_datetime("publishedAt")

    return normalized_event, safe_readback


def _write_final_batch_receipt(
    task_id: int,
    item_id: int,
    *,
    event_type: str,
    readback: object,
    timezone: str,
    message: str,
) -> None:
    """验证并原子写入批量执行器取得的最终平台回执。

    该函数故意保持模块私有；生产代码只能经
    ``douyin_commerce_batch_executor.write_verified_platform_result`` 调用。
    """

    normalized_event, safe_readback = _validated_receipt(
        event_type, readback, timezone
    )
    now = _now()
    with connect() as conn:
        item = conn.execute(
            """
            SELECT id
            FROM publish_task_items
            WHERE id = ? AND taskId = ? AND batchItemIndex IS NOT NULL
            """,
            (int(item_id), int(task_id)),
        ).fetchone()
        if not item:
            raise ValueError("批量视频条目不属于该任务")

        conn.execute(
            """
            UPDATE publish_task_items
            SET status = 'success', message = ?, attempts = attempts + 1,
                startedAt = COALESCE(startedAt, ?), finishedAt = ?
            WHERE id = ?
            """,
            (str(message), now, now, int(item_id)),
        )
        summary = conn.execute(
            """
            SELECT SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS active
            FROM publish_task_items WHERE taskId = ?
            """,
            (int(task_id),),
        ).fetchone()
        success, failed, active = (
            int(summary[key] or 0) for key in ("success", "failed", "active")
        )
        task_status = (
            "running"
            if active
            else "partial_failed"
            if failed and success
            else "failed"
            if failed
            else "success"
        )
        conn.execute(
            """
            UPDATE publish_tasks
            SET status = ?, successCount = ?, failedCount = ?, skippedCount = 0,
                startedAt = COALESCE(startedAt, ?),
                finishedAt = CASE WHEN ? = 0 THEN ? ELSE finishedAt END
            WHERE id = ?
            """,
            (task_status, success, failed, now, active, now, int(task_id)),
        )
        conn.execute(
            """
            INSERT INTO publish_task_events (
                taskId, itemId, level, eventType, message, detailJson, createdAt
            )
            VALUES (?, ?, 'info', ?, ?, ?, ?)
            """,
            (
                int(task_id),
                int(item_id),
                normalized_event,
                str(message),
                json.dumps(safe_readback, ensure_ascii=False),
                now,
            ),
        )
