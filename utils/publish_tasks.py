# -*- coding: utf-8 -*-
"""发布任务记录与查询工具。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from conf import BASE_DIR


DB_PATH = Path(BASE_DIR / "db" / "database.db")
PLATFORM_NAME_MAP = {
    1: "小红书",
    2: "视频号",
    3: "抖音",
    4: "快手",
    5: "B站",
    6: "TikTok",
    7: "YouTube",
    8: "Instagram Reels",
    9: "Facebook Reels",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _as_bool(value: Any) -> bool:
    return value in (1, "1", True, "true", "True", "yes", "on")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """打开旧任务库连接，并在上下文退出时关闭 Windows 文件句柄。"""

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _is_douyin_commerce_batch_task(conn: sqlite3.Connection, task_id: int) -> bool:
    """识别由新批量执行器管理的任务，避免旧运行时写出伪成功。

    ``utils.publish_tasks`` 仍服务历史多平台发布，因此不能仅按抖音平台号
    判断。批量任务的内部单视频载荷会保留 ``batchWorkflow``，这是唯一可审计
    的任务边界；解析失败时按历史任务处理，避免把损坏的旧任务误判为批量。
    """

    row = conn.execute(
        "SELECT payloadJson FROM publish_tasks WHERE id = ?", (int(task_id),)
    ).fetchone()
    if not row:
        return False
    try:
        payloads = json.loads(str(row["payloadJson"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(payloads, list) and any(
        isinstance(payload, dict)
        and str(payload.get("batchWorkflow") or "").strip()
        == "douyin-commerce-batch"
        for payload in payloads
    )


def _reject_batch_success_writer(conn: sqlite3.Connection, task_id: int, operation: str) -> None:
    """旧通用成功写入器不得处理逐视频批量任务。"""

    if _is_douyin_commerce_batch_task(conn, task_id):
        raise ValueError(
            f"抖音带货批量任务不能通过{operation}写入成功；"
            "必须由批量执行器逐视频写入可信平台回执"
        )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_columns(conn: sqlite3.Connection, table: str, definitions: tuple[tuple[str, str], ...]) -> None:
    existing = _columns(conn, table)
    for name, definition in definitions:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _account_display(profile_name: str | None, user_name: str | None, remark: str | None, fallback: str | None = "") -> str:
    parts = [part for part in (profile_name, user_name, remark) if part]
    return " | ".join(parts) or str(fallback or "")


def initialize_publish_task_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
CREATE TABLE IF NOT EXISTS publish_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taskNo TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    dryRun INTEGER NOT NULL DEFAULT 0,
    platformCount INTEGER NOT NULL DEFAULT 0,
    itemCount INTEGER NOT NULL DEFAULT 0,
    successCount INTEGER NOT NULL DEFAULT 0,
    failedCount INTEGER NOT NULL DEFAULT 0,
    skippedCount INTEGER NOT NULL DEFAULT 0,
    payloadJson TEXT,
    lastError TEXT,
    createdAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    startedAt TEXT,
    finishedAt TEXT
)
"""
    )
    cursor.execute(
        """
CREATE TABLE IF NOT EXISTS publish_task_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taskId INTEGER NOT NULL,
    platformType INTEGER NOT NULL,
    platformName TEXT NOT NULL,
    accountFile TEXT,
    accountLabel TEXT,
    filePath TEXT,
    fileName TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    createdAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    startedAt TEXT,
    finishedAt TEXT,
    FOREIGN KEY(taskId) REFERENCES publish_tasks(id) ON DELETE CASCADE
)
"""
    )
    _add_columns(
        conn,
        "publish_tasks",
        (
            ("accountSummary", "TEXT"),
            ("platformSummary", "TEXT"),
        ),
    )
    _add_columns(
        conn,
        "publish_task_items",
        (
            ("profileName", "TEXT"),
            ("userName", "TEXT"),
            ("accountRemark", "TEXT"),
        ),
    )
    cursor.execute(
        """
CREATE TABLE IF NOT EXISTS publish_task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taskId INTEGER NOT NULL,
    itemId INTEGER,
    level TEXT NOT NULL DEFAULT 'info',
    eventType TEXT NOT NULL,
    message TEXT,
    detailJson TEXT,
    createdAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(taskId) REFERENCES publish_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(itemId) REFERENCES publish_task_items(id) ON DELETE CASCADE
)
"""
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_publish_tasks_created ON publish_tasks(createdAt DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_publish_task_items_task ON publish_task_items(taskId)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_publish_task_events_task ON publish_task_events(taskId, createdAt)")


def _lookup_account_meta(
    conn: sqlite3.Connection,
    account_refs: set[tuple[int, str]],
) -> dict[tuple[int, str], dict[str, str]]:
    if not account_refs:
        return {}
    account_files = {file_path for _, file_path in account_refs}
    placeholders = ",".join(["?"] * len(account_files))
    rows = conn.execute(
        f"""
        SELECT type, filePath, userName, profileName, remark
        FROM user_info
        WHERE filePath IN ({placeholders})
        """,
        tuple(account_files),
    ).fetchall()
    labels: dict[tuple[int, str], dict[str, str]] = {}
    for row in rows:
        labels[(int(row["type"]), row["filePath"])] = dict(row)
    return labels


def _flatten_payload_items(
    data_list: list[dict[str, Any]],
    account_meta: dict[tuple[int, str], dict[str, str]],
) -> list[dict[str, Any]]:
    items = []
    for data in data_list:
        try:
            platform_type = int(data.get("type"))
        except (TypeError, ValueError):
            continue
        platform_name = PLATFORM_NAME_MAP.get(platform_type, f"平台{platform_type}")
        file_list = data.get("fileList") or []
        account_list = data.get("accountList") or []
        for file_path in file_list:
            file_name = Path(str(file_path)).name
            for account_file in account_list:
                account_file = str(account_file)
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
                        "platformName": platform_name,
                        "accountFile": account_file,
                        "accountLabel": account_label,
                        "profileName": meta.get("profileName") or "",
                        "userName": meta.get("userName") or "",
                        "accountRemark": meta.get("remark") or "",
                        "filePath": str(file_path),
                        "fileName": file_name,
                    }
                )
    return items


def create_publish_task(data_list: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    account_refs: set[tuple[int, str]] = set()
    for data in data_list:
        try:
            platform_type = int(data.get("type"))
        except (TypeError, ValueError):
            continue
        account_refs.update(
            (platform_type, str(account_file))
            for account_file in (data.get("accountList") or [])
        )
    with _connect() as conn:
        initialize_publish_task_schema(conn)
        account_meta = _lookup_account_meta(conn, account_refs)
        items = _flatten_payload_items(data_list, account_meta)
        platform_count = len({item["platformType"] for item in items})
        dry_run_values = [_as_bool(data.get("debugDryRun")) for data in data_list]
        dry_run = all(dry_run_values) if dry_run_values else False
        title = ""
        for data in data_list:
            title = (data.get("title") or data.get("biliTitle") or "").strip()
            if title:
                break

        account_summary = "；".join(sorted({item["accountLabel"] for item in items if item["accountLabel"]}))
        platform_summary = "、".join(sorted({item["platformName"] for item in items if item["platformName"]}))
        task_no = f"PT{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO publish_tasks (
                taskNo, mode, title, status, dryRun, platformCount, itemCount,
                payloadJson, accountSummary, platformSummary, createdAt
            )
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_no,
                mode,
                title,
                1 if dry_run else 0,
                platform_count,
                len(items),
                _json_dumps(data_list),
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
                    profileName, userName, accountRemark, filePath, fileName, createdAt
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    item["filePath"],
                    item["fileName"],
                    _now(),
                ),
            )
        cursor.execute(
            """
            INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt)
            VALUES (?, 'info', 'created', ?, ?)
            """,
            (task_id, f"发布任务已创建，共 {len(items)} 个执行项", _now()),
        )
        conn.commit()
        return {"id": task_id, "taskNo": task_no, "itemCount": len(items)}


def mark_task_running(task_id: int, message: str = "发布任务开始执行") -> None:
    with _connect() as conn:
        now = _now()
        conn.execute(
            "UPDATE publish_tasks SET status = 'running', startedAt = COALESCE(startedAt, ?) WHERE id = ?",
            (now, task_id),
        )
        conn.execute(
            """
            INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt)
            VALUES (?, 'info', 'running', ?, ?)
            """,
            (task_id, message, now),
        )
        conn.commit()


def record_task_event(
    task_id: int,
    event_type: str,
    message: str,
    level: str = "info",
    detail: Any | None = None,
    item_id: int | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO publish_task_events (taskId, itemId, level, eventType, message, detailJson, createdAt)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                item_id,
                level,
                event_type,
                message,
                _json_dumps(detail) if detail is not None else None,
                _now(),
            ),
        )
        conn.commit()


def mark_items(
    task_id: int,
    status: str,
    message: str | None = None,
    platform_type: int | None = None,
) -> None:
    with _connect() as conn:
        if status == "success":
            _reject_batch_success_writer(conn, task_id, "通用任务状态接口")
        now = _now()
        params: list[Any] = [status, message, now, task_id]
        where = "taskId = ?"
        if platform_type is not None:
            where += " AND platformType = ?"
            params.append(platform_type)
        conn.execute(
            f"""
            UPDATE publish_task_items
            SET status = ?,
                message = COALESCE(?, message),
                attempts = attempts + CASE WHEN ? IN ('success', 'failed') THEN 1 ELSE 0 END,
                startedAt = COALESCE(startedAt, ?),
                finishedAt = CASE WHEN ? IN ('success', 'failed', 'skipped') THEN ? ELSE finishedAt END
            WHERE {where}
            """,
            [status, message, status, now, status, now, *params[3:]],
        )
        conn.execute(
            """
            INSERT INTO publish_task_events (taskId, level, eventType, message, detailJson, createdAt)
            VALUES (?, ?, 'items_updated', ?, ?, ?)
            """,
            (
                task_id,
                "error" if status == "failed" else "info",
                message or f"执行项状态更新为 {status}",
                _json_dumps({"status": status, "platformType": platform_type}),
                now,
            ),
        )
        _refresh_task_summary(conn, task_id, fallback_error=message if status == "failed" else None)
        conn.commit()


def mark_platform_results(task_id: int, results: list[dict[str, Any]], default_message: str | None = None) -> None:
    with _connect() as conn:
        _reject_batch_success_writer(conn, task_id, "通用平台结果接口")

    by_platform: dict[int, list[dict[str, Any]]] = {}
    for result in results or []:
        try:
            platform_type = int(result.get("type"))
        except (TypeError, ValueError):
            continue
        by_platform.setdefault(platform_type, []).append(result)

    if not by_platform:
        mark_items(task_id, "success", default_message or "发布流程已完成")
        return

    for platform_type, platform_results in by_platform.items():
        failed = [item for item in platform_results if item.get("ok") is False]
        if failed:
            message = failed[0].get("message") or default_message or "平台发布失败"
            mark_items(task_id, "failed", message, platform_type=platform_type)
        else:
            mark_items(task_id, "success", default_message or "平台发布流程已完成", platform_type=platform_type)

    with _connect() as conn:
        unresolved = conn.execute(
            """
            SELECT DISTINCT platformType
            FROM publish_task_items
            WHERE taskId = ? AND status IN ('pending', 'running')
            """,
            (task_id,),
        ).fetchall()
        for row in unresolved:
            mark_items(task_id, "skipped", "同平台前序执行失败，后续执行项已跳过", platform_type=row["platformType"])


def fail_task(task_id: int, message: str) -> None:
    mark_items(task_id, "failed", message)


def complete_task(task_id: int, message: str = "发布任务已完成") -> None:
    with _connect() as conn:
        _reject_batch_success_writer(conn, task_id, "完成任务接口")
    mark_items(task_id, "success", message)


def _refresh_task_summary(conn: sqlite3.Connection, task_id: int, fallback_error: str | None = None) -> None:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS itemCount,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS successCount,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failedCount,
            SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skippedCount,
            SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS activeCount
        FROM publish_task_items
        WHERE taskId = ?
        """,
        (task_id,),
    ).fetchone()
    item_count = row["itemCount"] or 0
    success_count = row["successCount"] or 0
    failed_count = row["failedCount"] or 0
    skipped_count = row["skippedCount"] or 0
    active_count = row["activeCount"] or 0

    if active_count:
        status = "running"
        finished_at = None
    elif failed_count and success_count:
        status = "partial_failed"
        finished_at = _now()
    elif failed_count:
        status = "failed"
        finished_at = _now()
    elif skipped_count and not success_count:
        status = "failed"
        finished_at = _now()
    else:
        status = "success"
        finished_at = _now()

    conn.execute(
        """
        UPDATE publish_tasks
        SET status = ?,
            itemCount = ?,
            successCount = ?,
            failedCount = ?,
            skippedCount = ?,
            lastError = CASE WHEN ? IS NOT NULL THEN ? ELSE lastError END,
            finishedAt = CASE WHEN ? IS NOT NULL THEN ? ELSE finishedAt END
        WHERE id = ?
        """,
        (
            status,
            item_count,
            success_count,
            failed_count,
            skipped_count,
            fallback_error,
            fallback_error,
            finished_at,
            finished_at,
            task_id,
        ),
    )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def list_publish_tasks(limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 100))
    with _connect() as conn:
        initialize_publish_task_schema(conn)
        rows = conn.execute(
            """
            SELECT id, taskNo, mode, title, status, dryRun, platformCount, itemCount,
                   successCount, failedCount, skippedCount, accountSummary, platformSummary,
                   lastError, createdAt, startedAt, finishedAt
            FROM publish_tasks
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]


def get_publish_task(task_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        initialize_publish_task_schema(conn)
        task = conn.execute(
            """
            SELECT id, taskNo, mode, title, status, dryRun, platformCount, itemCount,
                   successCount, failedCount, skippedCount, payloadJson,
                   accountSummary, platformSummary, lastError,
                   createdAt, startedAt, finishedAt
            FROM publish_tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
        if not task:
            return None
        items = conn.execute(
            """
            SELECT id, platformType, platformName, accountFile, accountLabel,
                   profileName, userName, accountRemark, filePath, fileName,
                   status, message, attempts, createdAt, startedAt, finishedAt
            FROM publish_task_items
            WHERE taskId = ?
            ORDER BY id ASC
            """,
            (task_id,),
        ).fetchall()
        events = conn.execute(
            """
            SELECT id, itemId, level, eventType, message, detailJson, createdAt
            FROM publish_task_events
            WHERE taskId = ?
            ORDER BY id ASC
            """,
            (task_id,),
        ).fetchall()
        result = _row_to_dict(task)
        result["items"] = [_row_to_dict(row) for row in items]
        result["events"] = [_row_to_dict(row) for row in events]
        return result
