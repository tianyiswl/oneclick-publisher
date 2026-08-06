# -*- coding: utf-8 -*-
"""任务记录查询服务。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from .account_service import PLATFORMS
from .database import connect


CONTENT_TYPE_LABELS = {
    "video": "视频",
    "article": "图文",
    "text": "文字",
    "mixed": "混合类型",
}

WORKFLOW_LABELS = {
    "douyin-commerce": "抖音带货",
    "douyin-commerce-batch": "抖音带货批量",
}

_BATCH_READBACK_FIELDS = {
    "platformPostId",
    "postUrl",
    "publishedAt",
    "scheduleTime",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _account_display(profile_name: str | None, user_name: str | None, remark: str | None, fallback: str | None = "") -> str:
    parts = [part for part in (profile_name, user_name, remark) if part]
    return " | ".join(parts) or str(fallback or "")


def content_type_for_payloads(payloads: list[dict]) -> str:
    """返回统一任务类型；一个任务包含多类型时明确标记为 mixed。"""

    types = {
        str(payload.get("contentType") or "").strip()
        for payload in payloads
        if str(payload.get("contentType") or "").strip()
    }
    if len(types) == 1:
        return next(iter(types))
    return "mixed" if types else ""


def _payloads_from_json(payload_json: object) -> list[dict]:
    """安全读取历史载荷，不对历史任务做回写。"""

    try:
        payloads = json.loads(str(payload_json or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [dict(item) for item in payloads if isinstance(item, dict)] if isinstance(payloads, list) else []


def _batch_readback_projection(readback: object) -> dict[str, str]:
    """只保留可审计的非敏感平台回执字段。"""

    if not isinstance(readback, dict):
        return {}
    return {
        key: str(value).strip()
        for key, value in readback.items()
        if key in _BATCH_READBACK_FIELDS and isinstance(value, (str, int, float)) and str(value).strip()
    }


def _has_final_batch_receipt(event_type: str, readback: dict[str, str]) -> bool:
    """最终回执必须含平台可复核的身份或定时时间。"""

    if event_type == "platform_scheduled_receipt":
        return bool(readback.get("scheduleTime"))
    if event_type == "platform_publish_receipt":
        return bool(readback.get("platformPostId") or readback.get("postUrl"))
    return False


def content_type_from_payload_json(payload_json: object) -> str:
    """兼容历史任务，从已存参数推断类型，不改写原任务。"""

    return content_type_for_payloads(_payloads_from_json(payload_json))


def content_type_label(content_type: object) -> str:
    return CONTENT_TYPE_LABELS.get(str(content_type or ""), "未知类型")


def display_task_no(task_no: object) -> str:
    """以紧凑格式展示历史长任务号，不改变审计记录中的原始编号。"""

    value = str(task_no or "")
    legacy = re.fullmatch(r"PT\d{4}(\d{2})(\d{2})(\d{2})(\d{2})\d{2}-([0-9a-fA-F]{8})", value)
    if legacy:
        month, day, hour, minute, suffix = legacy.groups()
        return f"T{month}{day}{hour}{minute}-{suffix[:4].upper()}"
    return value


def workflow_from_payload_json(payload_json: object) -> str:
    """从任务载荷识别场景；混合场景明确标记而不猜测。"""

    payloads = _payloads_from_json(payload_json)
    batch_workflows = {
        str(payload.get("batchWorkflow") or "").strip()
        for payload in payloads
        if str(payload.get("batchWorkflow") or "").strip()
    }
    if len(batch_workflows) == 1:
        return next(iter(batch_workflows))
    workflows = {
        str(payload.get("workflow") or "").strip()
        for payload in payloads
        if str(payload.get("workflow") or "").strip()
    }
    if len(workflows) == 1:
        return next(iter(workflows))
    return "mixed" if workflows else ""


def workflow_label(workflow: object) -> str:
    value = str(workflow or "")
    if not value:
        return "常规发布"
    if value == "mixed":
        return "混合场景"
    return WORKFLOW_LABELS.get(value, "自定义场景")


def commerce_summary_from_payload_json(payload_json: object) -> str:
    """提取带货任务的地点、声明与发布方式，不保留会话敏感信息。"""

    payloads = _payloads_from_json(payload_json)
    is_batch = any(
        str(payload.get("batchWorkflow") or "") == "douyin-commerce-batch"
        for payload in payloads
    )
    batch_lines = []
    for payload in payloads:
        if str(payload.get("workflow") or "") != "douyin-commerce":
            continue
        poi = payload.get("locationPoi") if isinstance(payload.get("locationPoi"), dict) else {}
        location_name = str(poi.get("name") or payload.get("locationKeyword") or "").strip()
        location_address = str(poi.get("address") or "").strip()
        scope = str(payload.get("locationScope") or "").strip()
        declaration = str(payload.get("contentDeclaration") or "").strip()
        schedule = str(payload.get("scheduleTime") or "").strip()
        if is_batch:
            file_list = payload.get("fileList") if isinstance(payload.get("fileList"), list) else []
            file_name = Path(str(file_list[0])).name if file_list else "未命名视频"
            location = f"{location_name}（{location_address}）" if location_address else location_name
            batch_lines.extend(
                [
                    f"视频：{file_name}",
                    f"地点：{location}",
                    f"发布方式：北京时间定时 {schedule}" if payload.get("enableTimer") is True and schedule else "发布方式：立即发布",
                ]
            )
            continue
        fields = []
        if location_name:
            fields.append(f"地点：{location_name}{f'（{location_address}）' if location_address else ''}")
        if scope:
            scope_label = {"local": "本地", "domestic": "国内"}.get(scope, scope)
            fields.append(f"范围：{scope_label}")
        if declaration:
            fields.append(f"声明：{declaration}")
        if payload.get("enableTimer") is True and schedule:
            fields.append(f"定时：{schedule}（北京时间）")
        elif payload.get("enableTimer") is not True:
            fields.append("发布方式：立即发表")
        return "\n".join(fields)
    if is_batch:
        return "\n".join(batch_lines)
    return ""


def _attach_content_type(task: dict) -> dict:
    if not task.get("contentType"):
        task["contentType"] = content_type_from_payload_json(task.get("payloadJson"))
    task["contentTypeLabel"] = content_type_label(task.get("contentType"))
    task["workflow"] = workflow_from_payload_json(task.get("payloadJson"))
    task["workflowLabel"] = workflow_label(task.get("workflow"))
    task["commerceSummary"] = commerce_summary_from_payload_json(task.get("payloadJson"))
    task["taskTypeLabel"] = (
        f"{task['contentTypeLabel']} · {task['workflowLabel']}"
        if task.get("workflow")
        else task["contentTypeLabel"]
    )
    task["taskNoDisplay"] = display_task_no(task.get("taskNo"))
    return task


def list_tasks(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, taskNo, mode, title, status, dryRun, platformCount, itemCount,
                   successCount, failedCount, skippedCount, contentType, payloadJson, accountSummary,
                   platformSummary, createdAt, startedAt, finishedAt, lastError
            FROM publish_tasks
            ORDER BY createdAt DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        tasks = [_attach_content_type(dict(row)) for row in rows]
        for task in tasks:
            if task.get("accountSummary") and task.get("platformSummary"):
                continue
            items = conn.execute(
                """
                SELECT accountLabel, platformName, profileName, userName, accountRemark
                FROM publish_task_items
                WHERE taskId = ?
                ORDER BY id
                """,
                (task["id"],),
            ).fetchall()
            accounts = []
            platforms = []
            for item in items:
                account_display = _account_display(
                    item["profileName"],
                    item["userName"],
                    item["accountRemark"],
                    item["accountLabel"],
                )
                if account_display and account_display not in accounts:
                    accounts.append(account_display)
                if item["platformName"] and item["platformName"] not in platforms:
                    platforms.append(item["platformName"])
            task["accountSummary"] = task.get("accountSummary") or "；".join(accounts)
            task["platformSummary"] = task.get("platformSummary") or "、".join(platforms)
    return tasks


def task_stats(limit: int = 20) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN status IN ('failed', 'partial_failed') THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN createdAt LIKE ? THEN 1 ELSE 0 END) AS today
            FROM publish_tasks
            """,
            (f"{today}%",),
        ).fetchone()
        latest = conn.execute(
            """
            SELECT id, taskNo, title, status, contentType, platformSummary, accountSummary, createdAt
            FROM publish_tasks
            ORDER BY createdAt DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit or 20), 20)),),
        ).fetchall()
    return {
        "total": row["total"] or 0,
        "success": row["success"] or 0,
        "failed": row["failed"] or 0,
        "active": row["active"] or 0,
        "today": row["today"] or 0,
        "latest": [_attach_content_type(dict(item)) for item in latest],
    }


def get_task(task_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM publish_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        items = conn.execute("SELECT * FROM publish_task_items WHERE taskId = ? ORDER BY id", (task_id,)).fetchall()
        events = conn.execute("SELECT * FROM publish_task_events WHERE taskId = ? ORDER BY id", (task_id,)).fetchall()
    data = _attach_content_type(dict(row))
    data["items"] = [dict(item) for item in items]
    data["events"] = [dict(event) for event in events]
    return data


def delete_tasks(task_ids: list[int]) -> int:
    """删除已结束任务及其明细；正在执行的任务不允许删除。"""

    normalized_ids = list(dict.fromkeys(int(task_id) for task_id in task_ids if int(task_id) > 0))
    if not normalized_ids:
        return 0

    placeholders = ",".join("?" for _ in normalized_ids)
    with connect() as conn:
        active_rows = conn.execute(
            f"""
            SELECT taskNo, status
            FROM publish_tasks
            WHERE id IN ({placeholders}) AND status IN ('pending', 'running')
            ORDER BY id
            """,
            tuple(normalized_ids),
        ).fetchall()
        if active_rows:
            task_numbers = "、".join(str(row["taskNo"]) for row in active_rows)
            raise ValueError(f"等待执行或执行中的任务不能删除：{task_numbers}")

        conn.execute(
            f"DELETE FROM publish_task_events WHERE taskId IN ({placeholders})",
            tuple(normalized_ids),
        )
        conn.execute(
            f"DELETE FROM publish_task_items WHERE taskId IN ({placeholders})",
            tuple(normalized_ids),
        )
        cursor = conn.execute(
            f"DELETE FROM publish_tasks WHERE id IN ({placeholders})",
            tuple(normalized_ids),
        )
        conn.commit()
    return int(cursor.rowcount)


def create_pending_task(payloads: list[dict], mode: str = "desktop") -> dict:
    account_files = sorted({a for payload in payloads for a in payload.get("accountList", [])})
    with connect() as conn:
        account_meta = {}
        if account_files:
            placeholders = ",".join(["?"] * len(account_files))
            rows = conn.execute(
                f"SELECT type, filePath, userName, profileName, remark FROM user_info WHERE filePath IN ({placeholders})",
                tuple(account_files),
            ).fetchall()
            account_meta = {(int(row["type"]), row["filePath"]): dict(row) for row in rows}

        items = []
        for payload in payloads:
            platform_type = int(payload.get("type"))
            # 纯文字没有素材文件，但同样必须生成账号执行项，才能正确回填结果。
            for file_path in payload.get("fileList", []) or [""]:
                for account_file in payload.get("accountList", []):
                    meta = account_meta.get((platform_type, account_file), {})
                    account_label = _account_display(
                        meta.get("profileName"),
                        meta.get("userName"),
                        meta.get("remark"),
                        account_file,
                    )
                    items.append(
                        {
                            "platformType": platform_type,
                            "platformName": PLATFORMS.get(platform_type, f"平台{platform_type}"),
                            "contentType": str(payload.get("contentType") or ""),
                            "accountFile": account_file,
                            "accountLabel": account_label,
                            "profileName": meta.get("profileName") or "",
                            "userName": meta.get("userName") or "",
                            "accountRemark": meta.get("remark") or "",
                            "filePath": file_path,
                            "fileName": Path(file_path).name,
                        }
                    )
        content_type = content_type_for_payloads(payloads)
        task_no = ""
        for _ in range(8):
            candidate = f"T{datetime.now():%m%d%H%M}-{uuid.uuid4().hex[:4].upper()}"
            exists = conn.execute("SELECT 1 FROM publish_tasks WHERE taskNo = ?", (candidate,)).fetchone()
            if not exists:
                task_no = candidate
                break
        if not task_no:
            raise RuntimeError("无法生成唯一任务号，请重试")
        title = next((payload.get("title") or payload.get("biliTitle") for payload in payloads if payload.get("title") or payload.get("biliTitle")), "")
        account_summary = "；".join(sorted(set(item["accountLabel"] for item in items)))
        platform_summary = "、".join(sorted(set(item["platformName"] for item in items)))
        dry_run = 1 if all(payload.get("debugDryRun") for payload in payloads) else 0
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO publish_tasks (
                taskNo, mode, title, status, dryRun, platformCount, itemCount, contentType,
                payloadJson, accountSummary, platformSummary, createdAt
            )
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_no,
                mode,
                title,
                dry_run,
                len({item["platformType"] for item in items}),
                len(items),
                content_type,
                json.dumps(payloads, ensure_ascii=False),
                account_summary,
                platform_summary,
                _now(),
            ),
        )
        task_id = cursor.lastrowid
        for item in items:
            cursor.execute(
                """
                INSERT INTO publish_task_items (
                    taskId, platformType, platformName, accountFile, accountLabel,
                    profileName, userName, accountRemark, contentType, filePath, fileName, createdAt
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    item["platformType"],
                    item["platformName"],
                    item["accountFile"],
                    item["accountLabel"],
                    item["profileName"],
                    item["userName"],
                    item["accountRemark"],
                    item["contentType"],
                    item["filePath"],
                    item["fileName"],
                    _now(),
                ),
            )
        conn.execute(
            "INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt) VALUES (?, 'info', 'created', ?, ?)",
            (task_id, "桌面端已创建发布任务", _now()),
        )
        conn.commit()
    return {"id": task_id, "taskNo": task_no, "itemCount": len(items)}


def create_douyin_batch_task(batch: dict, mode: str = "oneclick_publish") -> dict:
    """为批量信封中的每条视频创建独立、可审计的发布项。"""

    from .douyin_commerce_batch_service import item_publish_payload

    items = batch.get("items") if isinstance(batch, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("抖音带货批量任务至少需要一条视频")
    payloads = [item_publish_payload(batch, item) for item in items]
    task = create_pending_task(payloads, mode=mode)
    now = _now()
    with connect() as conn:
        task_items = conn.execute(
            "SELECT id FROM publish_task_items WHERE taskId = ? ORDER BY id", (task["id"],)
        ).fetchall()
        for index, (task_item, payload) in enumerate(zip(task_items, payloads), start=1):
            poi = payload.get("locationPoi") if isinstance(payload.get("locationPoi"), dict) else {}
            location_name = str(poi.get("name") or payload.get("locationKeyword") or "").strip()
            location_address = str(poi.get("address") or "").strip()
            location_summary = f"{location_name}（{location_address}）" if location_address else location_name
            schedule_summary = (
                f"北京时间定时 {payload['scheduleTime']}"
                if payload.get("enableTimer") is True and payload.get("scheduleTime")
                else "立即发布"
            )
            conn.execute(
                """
                UPDATE publish_task_items
                SET batchItemIndex = ?, locationSummary = ?, scheduleSummary = ?
                WHERE id = ?
                """,
                (index, location_summary, schedule_summary, task_item["id"]),
            )
        conn.execute(
            "INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt) VALUES (?, 'info', 'batch_created', ?, ?)",
            (task["id"], "已创建抖音带货批量逐视频任务", now),
        )
        conn.commit()
    return task


def mark_task_running(task_id: int, message: str) -> None:
    """标记一键发本地预检开始执行。"""

    now = _now()
    with connect() as conn:
        conn.execute(
            "UPDATE publish_tasks SET status = 'running', startedAt = COALESCE(startedAt, ?) WHERE id = ?",
            (now, int(task_id)),
        )
        conn.execute(
            "INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt) VALUES (?, 'info', 'running', ?, ?)",
            (int(task_id), message, now),
        )
        conn.commit()


def record_task_event(task_id: int, event_type: str, message: str, *, level: str = "info") -> None:
    """追加不含会话凭据的平台预检事件。"""

    with connect() as conn:
        conn.execute(
            "INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt) VALUES (?, ?, ?, ?, ?)",
            (int(task_id), level, event_type, message, _now()),
        )
        conn.commit()


def mark_platform_result(
    task_id: int,
    platform_type: int,
    *,
    ok: bool,
    message: str,
    content_type: str | None = None,
    event_type: str = "platform_preflight",
) -> None:
    """按平台与内容类型回填结果；事件类型必须准确表达预检或正式提交。"""

    now = _now()
    status = "success" if ok else "failed"
    with connect() as conn:
        if content_type:
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = ?, message = ?, attempts = attempts + 1,
                    startedAt = COALESCE(startedAt, ?), finishedAt = ?
                WHERE taskId = ? AND platformType = ? AND contentType = ?
                """,
                (status, message, now, now, int(task_id), int(platform_type), str(content_type)),
            )
        else:
            # 兼容旧调用与历史任务。新预检调用都会传入 content_type。
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = ?, message = ?, attempts = attempts + 1,
                    startedAt = COALESCE(startedAt, ?), finishedAt = ?
                WHERE taskId = ? AND platformType = ?
                """,
                (status, message, now, now, int(task_id), int(platform_type)),
            )
        summary = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS active
            FROM publish_task_items WHERE taskId = ?
            """,
            (int(task_id),),
        ).fetchone()
        total = int(summary["total"] or 0)
        success = int(summary["success"] or 0)
        failed = int(summary["failed"] or 0)
        active = int(summary["active"] or 0)
        task_status = "running" if active else "partial_failed" if failed and success else "failed" if failed else "success"
        conn.execute(
            """
            UPDATE publish_tasks
            SET status = ?, successCount = ?, failedCount = ?, skippedCount = 0,
                lastError = CASE
                    WHEN ? THEN ?
                    WHEN ? = 0 THEN NULL
                    ELSE lastError
                END,
                finishedAt = CASE WHEN ? = 0 THEN ? ELSE finishedAt END
            WHERE id = ?
            """,
            (
                task_status,
                success,
                failed,
                1 if not ok else 0,
                message,
                failed,
                active,
                now,
                int(task_id),
            ),
        )
        conn.execute(
            "INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt) VALUES (?, ?, ?, ?, ?)",
            (int(task_id), "info" if ok else "error", str(event_type), message, now),
        )
        conn.commit()


def mark_batch_item_result(
    task_id: int,
    item_id: int,
    *,
    ok: bool,
    message: str,
    event_type: str,
    readback: dict | None,
) -> None:
    """按视频条目写入平台事件；只有最终平台回执可标记成功。"""

    now = _now()
    safe_readback = _batch_readback_projection(readback)
    final_receipts = {"platform_publish_receipt", "platform_scheduled_receipt"}
    requested_status = (
        "failed"
        if not ok
        else "success"
        if event_type in final_receipts and _has_final_batch_receipt(event_type, safe_readback)
        else "running"
    )
    with connect() as conn:
        item = conn.execute(
            "SELECT id, status FROM publish_task_items WHERE id = ? AND taskId = ?", (int(item_id), int(task_id))
        ).fetchone()
        if not item:
            raise ValueError("批量视频条目不属于该任务")
        # 已确认的平台成功是终态；后续编辑/预检事件只补充审计，不得回退。
        status = "success" if item["status"] == "success" else requested_status
        conn.execute(
            """
            UPDATE publish_task_items
            SET status = ?, message = ?, attempts = attempts + 1,
                startedAt = COALESCE(startedAt, ?),
                finishedAt = CASE WHEN ? IN ('success', 'failed') THEN ? ELSE finishedAt END
            WHERE id = ?
            """,
            (status, message, now, status, now, int(item_id)),
        )
        summary = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS active
            FROM publish_task_items WHERE taskId = ?
            """,
            (int(task_id),),
        ).fetchone()
        success, failed, active = (int(summary[key] or 0) for key in ("success", "failed", "active"))
        task_status = "running" if active else "partial_failed" if failed and success else "failed" if failed else "success"
        conn.execute(
            """
            UPDATE publish_tasks
            SET status = ?, successCount = ?, failedCount = ?, skippedCount = 0,
                startedAt = COALESCE(startedAt, ?),
                lastError = CASE WHEN ? THEN ? ELSE lastError END,
                finishedAt = CASE WHEN ? = 0 THEN ? ELSE finishedAt END
            WHERE id = ?
            """,
            (task_status, success, failed, now, 1 if not ok else 0, message, active, now, int(task_id)),
        )
        conn.execute(
            """
            INSERT INTO publish_task_events (taskId, itemId, level, eventType, message, detailJson, createdAt)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(task_id),
                int(item_id),
                "info" if ok else "error",
                str(event_type),
                message,
                json.dumps(safe_readback, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
