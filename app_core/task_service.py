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


def content_type_from_payload_json(payload_json: object) -> str:
    """兼容历史任务，从已存参数推断类型，不改写原任务。"""

    try:
        payloads = json.loads(str(payload_json or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    return content_type_for_payloads(payloads if isinstance(payloads, list) else [])


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


def _attach_content_type(task: dict) -> dict:
    if not task.get("contentType"):
        task["contentType"] = content_type_from_payload_json(task.get("payloadJson"))
    task["contentTypeLabel"] = content_type_label(task.get("contentType"))
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
