# -*- coding: utf-8 -*-
"""任务记录查询服务。"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from .account_service import PLATFORMS
from .database import connect
from .overseas_meta_errors import project_facebook_page_receipt
from .overseas_tiktok_errors import TikTokPublishError
from .tiktok_schedule_contract import tiktok_irreversible_evidence_sql


PAUSE_REASON_USER_REQUEST = "user_request"
PAUSE_REASON_WAITING_LOGIN = "waiting_login"
PAUSE_REASON_WAITING_VERIFICATION = "waiting_verification"
PAUSE_REASON_RECEIPT_AMBIGUOUS = "receipt_ambiguous"
PAUSE_REASON_AUTO_FAILURE = "auto_failure"
PAUSE_REASON_CLIENT_SHUTDOWN = "client_shutdown"
PAUSE_REASON_CLEANUP_INCOMPLETE = "cleanup_incomplete"
DOUYIN_BATCH_PAUSE_REASONS = frozenset(
    {
        PAUSE_REASON_USER_REQUEST,
        PAUSE_REASON_WAITING_LOGIN,
        PAUSE_REASON_WAITING_VERIFICATION,
        PAUSE_REASON_RECEIPT_AMBIGUOUS,
        PAUSE_REASON_AUTO_FAILURE,
        PAUSE_REASON_CLIENT_SHUTDOWN,
        PAUSE_REASON_CLEANUP_INCOMPLETE,
    }
)


CONTENT_TYPE_LABELS = {
    "video": "视频",
    "article": "图文",
    "text": "文字",
    "mixed": "混合类型",
}

WORKFLOW_LABELS = {
    "douyin-commerce": "抖音带货",
    "douyin-commerce-batch": "抖音带货批量",
    "douyin-graphic-matrix": "抖音图文矩阵",
}

_BATCH_READBACK_FIELDS = {
    "platformPostId",
    "postUrl",
    "publishedAt",
    "scheduleTime",
    "timezone",
    "cooldownStartedAt",
    "cooldownSeconds",
}
_MATRIX_RECEIPT_FIELDS = frozenset(
    {
        "platformPostId",
        "postUrl",
        "publishedAt",
        "scheduledAt",
        "scheduleTime",
        "timezone",
    }
)
_YOUTUBE_RECEIPT_FIELDS = frozenset(
    {
        "videoId",
        "studioUrl",
        "watchUrl",
        "visibility",
        "scheduledAt",
        "processingStatus",
        "thumbnailApplied",
        "platformMutation",
    }
)
_TIKTOK_RECEIPT_FIELDS = frozenset(
    {
        "accountId",
        "visibility",
        "scheduleMode",
        "scheduledAt",
        "scheduleTimezone",
        "platformWriteOccurred",
        "finalActionTriggered",
        "platformAccepted",
        "scheduledReadbackConfirmed",
        "contentId",
        "contentUrl",
        "publishedAt",
        "topicEntities",
        "phase",
    }
)
_TIKTOK_RECEIPT_PHASES = frozenset(
    {
        "local_preflight_passed",
        "platform_form_verified",
        "failed_before_final_action",
        "final_action_triggered",
        "platform_accepted",
        "published_readback_confirmed",
        "scheduled_accepted",
        "scheduled_readback_confirmed",
        "ambiguous",
    }
)
_MATRIX_TASK_MODES = frozenset(
    {
        "oneclick_matrix_local_check",
        "oneclick_matrix_preflight",
        "oneclick_matrix_publish",
    }
)
_LOCATION_DIAGNOSTIC_ERROR_CODES = frozenset({
    "publish_location_candidate_missing",
    "publish_location_candidate_ambiguous",
    "publish_location_click_failed",
    "publish_location_readback_mismatch",
    "publish_location_cleanup_incomplete",
    "publish_location_commission_mismatch",
    "publish_location_not_found_after_all_pages",
    "publish_location_load_more_limit",
    "publish_location_load_more_failed",
    "publish_location_action_timeout",
    "publish_location_click_limit",
    "publish_location_candidate_limit",
})
_LOCATION_DIAGNOSTIC_STAGES = frozenset({
    "search",
    "load_more",
    "readback",
    "all_pages",
})
_LOCATION_DIAGNOSTIC_SCOPES = frozenset({"local", "domestic"})
_LOCATION_DIAGNOSTIC_COUNT_LIMITS = {
    "loadMoreClicks": (0, 10),
    "candidateCount": (0, 100),
    "candidateLimit": (1, 100),
    "clickLimit": (1, 10),
    "operationTimeoutSeconds": (1, 30),
}

_FACEBOOK_PUBLIC_PHASES = frozenset(
    {
        "local_validation_passed",
        "waiting_user_verification",
        "platform_form_verified",
        "final_action_claimed",
        "final_action_clicked",
        "platform_accepted",
        "published_readback_confirmed",
        "failed",
        "confirmed_not_published",
        "ambiguous",
    }
)
_FACEBOOK_PHASE_STATUS = {
    "local_validation_passed": "running",
    "waiting_user_verification": "waiting_user_verification",
    "platform_form_verified": "running",
    "final_action_claimed": "running",
    "final_action_clicked": "running",
    "platform_accepted": "running",
    "published_readback_confirmed": "success",
    "failed": "failed",
    "confirmed_not_published": "failed",
    "ambiguous": "failed",
}
_FACEBOOK_PHASE_EVENT = {
    "local_validation_passed": "facebook_local_validation_passed",
    "waiting_user_verification": "facebook_waiting_user_verification",
    "platform_form_verified": "facebook_platform_form_verified",
    "final_action_claimed": "facebook_final_action_claimed",
    "final_action_clicked": "facebook_final_action_clicked",
    "platform_accepted": "facebook_platform_accepted",
    "published_readback_confirmed": "facebook_publish_readback_confirmed",
    "failed": "facebook_publish_failed",
    "confirmed_not_published": "facebook_confirmed_not_published",
    "ambiguous": "facebook_publish_outcome_ambiguous",
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


def _batch_readback_projection(readback: object) -> dict[str, str | int]:
    """只保留可审计的非敏感平台回执字段。"""

    if not isinstance(readback, dict):
        return {}
    projected: dict[str, str | int] = {
        key: str(value).strip()
        for key, value in readback.items()
        if key in _BATCH_READBACK_FIELDS and isinstance(value, str) and value.strip()
    }
    error_code = readback.get("errorCode")
    if type(error_code) is str and error_code in _LOCATION_DIAGNOSTIC_ERROR_CODES:
        projected["errorCode"] = error_code

    stage = readback.get("stage")
    if type(stage) is str and stage in _LOCATION_DIAGNOSTIC_STAGES:
        projected["stage"] = stage

    scope = readback.get("scope")
    if type(scope) is str and scope in _LOCATION_DIAGNOSTIC_SCOPES:
        projected["scope"] = scope

    keyword = readback.get("keyword")
    if type(keyword) is str:
        normalized_keyword = " ".join(keyword.split())
        lowered_keyword = normalized_keyword.casefold()
        if (
            normalized_keyword == keyword
            and 0 < len(keyword) <= 200
            and not any(
                fragment in lowered_keyword
                for fragment in (
                    "cookie=",
                    "data-private-",
                    "document.",
                    "file:",
                )
            )
            and not any(
                character in keyword for character in ("<", ">", "/", "\\")
            )
        ):
            projected["keyword"] = keyword

    for key, (minimum, maximum) in _LOCATION_DIAGNOSTIC_COUNT_LIMITS.items():
        value = readback.get(key)
        if type(value) is int and minimum <= value <= maximum:
            projected[key] = value
    return projected


def _youtube_receipt_projection(receipt: object) -> dict[str, object]:
    """只保留可用于精确视频核对的非敏感字段。"""

    if not isinstance(receipt, dict):
        return {}
    projected: dict[str, object] = {}
    for key in _YOUTUBE_RECEIPT_FIELDS:
        value = receipt.get(key)
        if key == "thumbnailApplied":
            if type(value) is bool:
                projected[key] = value
            continue
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized or len(normalized) > 500:
            continue
        projected[key] = normalized

    video_id = str(projected.get("videoId") or "")
    if video_id and not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", video_id):
        projected.pop("videoId", None)
        video_id = ""
    if "studioUrl" in projected and (
        not video_id
        or projected["studioUrl"]
        != f"https://studio.youtube.com/video/{video_id}/edit"
    ):
        projected.pop("studioUrl", None)
    if "watchUrl" in projected and (
        not video_id
        or projected["watchUrl"]
        != f"https://www.youtube.com/watch?v={video_id}"
    ):
        projected.pop("watchUrl", None)
    if projected.get("visibility") not in {
        "private",
        "unlisted",
        "public",
        "scheduled_public",
    }:
        projected.pop("visibility", None)
    return projected


def _tiktok_receipt_projection(receipt: object) -> dict[str, object]:
    """Apply the Task 4 value boundary to the shared TikTok receipt."""

    if not isinstance(receipt, Mapping):
        return {}
    # Task 4 already owns the strict scalar/URL/time safety contract. Reuse its
    # public error receipt projection, then add only the Task 7 topic list.
    projected = dict(
        TikTokPublishError(
            "tiktok_receipt_projection",
            "TikTok receipt projection",
            receipt=receipt,
        ).receipt
    )
    projected.pop("mode", None)

    topics = receipt.get("topicEntities")
    if type(topics) is list and len(topics) <= 50:
        safe_topics = []
        seen_topics: set[str] = set()
        for topic in topics:
            if (
                type(topic) is not str
                or not (1 <= len(topic) <= 100)
                or any(character.isspace() or character in "#@/\\<>" for character in topic)
            ):
                break
            identity = topic.casefold()
            if identity in seen_topics:
                break
            seen_topics.add(identity)
            safe_topics.append(topic)
        else:
            projected["topicEntities"] = safe_topics

    phase = receipt.get("phase")
    if type(phase) is str and phase in _TIKTOK_RECEIPT_PHASES:
        projected["phase"] = phase
    if projected.get("scheduleMode") == "platform_native":
        # A platform-native schedule is accepted for future publication; even
        # a syntactically valid upstream timestamp cannot turn that receipt
        # into evidence that the content is already public.
        projected["publishedAt"] = None
    return {
        key: projected[key]
        for key in _TIKTOK_RECEIPT_FIELDS
        if key in projected
    }


def _stable_error_code(value: object) -> str:
    code = str(value or "").strip()
    return code if re.fullmatch(r"[a-z][a-z0-9_]{2,80}", code) else ""


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
        revision_source_id = row["revisionSourceTaskId"]
        revision_source = (
            conn.execute(
                "SELECT taskNo FROM publish_tasks WHERE id = ?",
                (revision_source_id,),
            ).fetchone()
            if type(revision_source_id) is int and revision_source_id > 0
            else None
        )
    data = _attach_content_type(dict(row))
    data["revisionSourceTaskNo"] = (
        str(revision_source["taskNo"] or "") if revision_source else ""
    )
    data["items"] = [dict(item) for item in items]
    data["events"] = [dict(event) for event in events]
    return data


def _latest_sms_cooldown_event(task_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM publish_task_events WHERE taskId = ? AND eventType = 'douyin_sms_cooldown_started' ORDER BY id DESC LIMIT 1",
            (int(task_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def _require_utc_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("verification_cooldown_state_invalid")
    try:
        if value.utcoffset() != timedelta(0):
            raise ValueError("verification_cooldown_state_invalid")
    except (TypeError, ValueError, OverflowError):
        raise ValueError("verification_cooldown_state_invalid") from None
    return value


def load_douyin_sms_cooldown_remaining(
    task_id: int, *, now_utc: datetime
) -> float:
    now_utc = _require_utc_datetime(now_utc)
    task = get_task(int(task_id))
    if not isinstance(task, dict):
        raise ValueError("verification_cooldown_state_invalid")
    event = _latest_sms_cooldown_event(int(task_id))
    source_id = task.get("resumeSourceTaskId")
    if event is None and type(source_id) is int and source_id > 0:
        event = _latest_sms_cooldown_event(source_id)
    if event is None:
        return 0.0
    try:
        detail = json.loads(str(event["detailJson"]))
        if not isinstance(detail, dict):
            raise ValueError
        if detail.get("cooldownSeconds") != "60":
            raise ValueError
        started = datetime.fromisoformat(detail["cooldownStartedAt"])
        started = _require_utc_datetime(started)
        elapsed = (
            now_utc.astimezone(ZoneInfo("UTC"))
            - started.astimezone(ZoneInfo("UTC"))
        ).total_seconds()
        if elapsed < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("verification_cooldown_state_invalid") from None
    return max(0.0, 60.0 - elapsed)


def delete_tasks(task_ids: list[int]) -> int:
    """删除已结束任务及其明细；正在执行的任务不允许删除。"""

    normalized_ids = list(dict.fromkeys(int(task_id) for task_id in task_ids if int(task_id) > 0))
    if not normalized_ids:
        return 0

    placeholders = ",".join("?" for _ in normalized_ids)
    with connect() as conn:
        revision_descendant = conn.execute(
            f"""
            SELECT 1
            FROM publish_tasks
            WHERE revisionSourceTaskId IN ({placeholders})
            LIMIT 1
            """,
            tuple(normalized_ids),
        ).fetchone()
        if revision_descendant:
            raise ValueError("已有修订后代的来源任务不能删除")
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

        irreversible = tiktok_irreversible_evidence_sql("task.id")
        protected_tiktok = conn.execute(
            f"""
            SELECT 1
            FROM publish_tasks AS task
            WHERE task.id IN ({placeholders})
              AND task.mode = 'oneclick_publish'
              AND EXISTS (
                  SELECT 1 FROM publish_task_items AS item
                  WHERE item.taskId = task.id AND item.platformType = 6
              )
              AND (
                  task.status = 'success'
                  OR {irreversible}
              )
            LIMIT 1
            """,
            tuple(normalized_ids),
        ).fetchone()
        if protected_tiktok:
            raise ValueError(
                "TikTok 已发布或最终动作后的任务不能删除，必须保留防重复证据"
            )

        claim_table = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'tiktok_controlled_execution_claims'
            """
        ).fetchone()
        if claim_table:
            conn.execute(
                f"""
                DELETE FROM tiktok_controlled_execution_claims
                WHERE taskId IN ({placeholders})
                """,
                tuple(normalized_ids),
            )

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


def _insert_pending_task(
    conn,
    payloads: list[dict],
    mode: str = "desktop",
    *,
    resume_source_task_id: int | None = None,
    revision_source_task_id: int | None = None,
) -> dict:
    project_ids = {
        str(payload.get("contentProjectId") or "").strip().lower()
        for payload in payloads
    }
    project_identity: tuple[str, str] | None = None
    if project_ids != {""}:
        if (
            len(project_ids) != 1
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,63}", next(iter(project_ids)))
            or mode
            not in {
                "oneclick_preflight",
                "oneclick_platform_form_check",
                "oneclick_publish",
            }
        ):
            raise ValueError("内容项目任务归属无效")
        if mode != "oneclick_platform_form_check":
            project_identity = (
                next(iter(project_ids)),
                "formal" if mode == "oneclick_publish" else "preflight",
            )
    account_files = sorted({a for payload in payloads for a in payload.get("accountList", [])})
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
        payload_account_files = list(payload.get("accountList", []))
        payload_account_ids = payload.get("accountIds")
        # 纯文字没有素材文件，但同样必须生成账号执行项，才能正确回填结果。
        for file_path in payload.get("fileList", []) or [""]:
            for account_index, account_file in enumerate(payload_account_files):
                meta = account_meta.get((platform_type, account_file), {})
                account_id = (
                    payload_account_ids[account_index]
                    if isinstance(payload_account_ids, list)
                    and account_index < len(payload_account_ids)
                    and type(payload_account_ids[account_index]) is int
                    and payload_account_ids[account_index] > 0
                    else None
                )
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
                        "accountId": account_id,
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
            payloadJson, accountSummary, platformSummary, resumeSourceTaskId,
            revisionSourceTaskId, createdAt
        )
        VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            resume_source_task_id,
            revision_source_task_id,
            _now(),
        ),
    )
    task_id = cursor.lastrowid
    if project_identity is not None:
        conn.execute(
            """
            INSERT INTO content_project_task_links
                (projectId, taskId, phase, createdAt)
            VALUES (?, ?, ?, ?)
            """,
            (project_identity[0], task_id, project_identity[1], _now()),
        )
    for item in items:
        cursor.execute(
            """
            INSERT INTO publish_task_items (
                taskId, platformType, platformName, accountId,
                accountFile, accountLabel,
                profileName, userName, accountRemark, contentType, filePath, fileName, createdAt
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                item["platformType"],
                item["platformName"],
                item["accountId"],
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
    return {"id": task_id, "taskNo": task_no, "itemCount": len(items)}


def create_pending_task(
    payloads: list[dict],
    mode: str = "desktop",
    *,
    resume_source_task_id: int | None = None,
    revision_source_task_id: int | None = None,
) -> dict:
    with connect() as conn:
        task = _insert_pending_task(
            conn,
            payloads,
            mode=mode,
            resume_source_task_id=resume_source_task_id,
            revision_source_task_id=revision_source_task_id,
        )
        conn.commit()
    return task


def _unique_task_no(conn) -> str:
    for _ in range(8):
        candidate = f"T{datetime.now():%m%d%H%M}-{uuid.uuid4().hex[:4].upper()}"
        if not conn.execute(
            "SELECT 1 FROM publish_tasks WHERE taskNo = ?", (candidate,)
        ).fetchone():
            return candidate
    raise RuntimeError("无法生成唯一任务号，请重试")


def create_douyin_graphic_matrix_task(
    matrix: dict,
    *,
    mode: str,
    revision_source_task_id: int | None = None,
) -> dict:
    """创建每个账号一条明细的抖音图文矩阵任务。"""

    from .douyin_graphic_matrix_service import WORKFLOW, matrix_scope_fingerprint

    if mode not in _MATRIX_TASK_MODES:
        raise ValueError("抖音图文矩阵任务模式无效")
    if str(matrix.get("workflow") or "") != WORKFLOW:
        raise ValueError("抖音图文矩阵任务快照无效")
    targets = matrix.get("targets")
    content = matrix.get("content")
    if not isinstance(targets, list) or not targets or not isinstance(content, dict):
        raise ValueError("抖音图文矩阵任务快照无效")
    images = content.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError("抖音图文矩阵任务缺少图片")
    snapshot_hash = matrix_scope_fingerprint(matrix)
    matrix_snapshot = json.loads(json.dumps(matrix, ensure_ascii=False))
    matrix_snapshot["scopeFingerprint"] = snapshot_hash
    common = content.get("common") if isinstance(content.get("common"), dict) else {}
    title = str(common.get("title") or "抖音图文矩阵")
    now = _now()
    with connect() as conn:
        task_no = _unique_task_no(conn)
        cursor = conn.execute(
            """
            INSERT INTO publish_tasks (
                taskNo, mode, title, status, dryRun, platformCount, itemCount,
                contentType, payloadJson, accountSummary, platformSummary,
                revisionSourceTaskId, createdAt
            ) VALUES (?, ?, ?, 'pending', ?, 1, ?, 'article', ?, ?, '抖音', ?, ?)
            """,
            (
                task_no,
                mode,
                title,
                0 if mode == "oneclick_matrix_publish" else 1,
                len(targets),
                json.dumps([matrix_snapshot], ensure_ascii=False),
                "；".join(str(row.get("accountLabel") or "") for row in targets),
                revision_source_task_id,
                now,
            ),
        )
        task_id = int(cursor.lastrowid)
        first_image = str(images[0])
        for target in targets:
            conn.execute(
                """
                INSERT INTO publish_task_items (
                    taskId, platformType, platformName, accountId, accountLabel,
                    contentType, filePath, fileName, batchItemIndex,
                    scheduleSummary, authorizationSnapshotHash, createdAt
                ) VALUES (?, 3, '抖音', ?, ?, 'article', ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    int(target.get("accountId") or 0),
                    str(target.get("accountLabel") or ""),
                    first_image,
                    Path(first_image).name,
                    int(target.get("itemIndex") or 0),
                    str(target.get("scheduleTime") or ""),
                    snapshot_hash,
                    now,
                ),
            )
        conn.execute(
            """
            INSERT INTO publish_task_events
                (taskId, level, eventType, message, createdAt)
            VALUES (?, 'info', 'matrix_created', ?, ?)
            """,
            (task_id, "已创建抖音图文矩阵逐账号任务", now),
        )
        conn.commit()
    return {"id": task_id, "taskNo": task_no, "itemCount": len(targets)}


def matrix_item_for_index(task_id: int, item_index: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM publish_task_items
            WHERE taskId = ? AND batchItemIndex = ?
            LIMIT 1
            """,
            (int(task_id), int(item_index)),
        ).fetchone()
    if row is None:
        raise ValueError("抖音图文矩阵账号明细不存在")
    return dict(row)


def _matrix_parent_counts(conn, task_id: int) -> tuple[str, int, int, int]:
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS active
        FROM publish_task_items WHERE taskId = ?
        """,
        (int(task_id),),
    ).fetchone()
    success = int(row["success"] or 0)
    failed = int(row["failed"] or 0)
    active = int(row["active"] or 0)
    if active:
        status = "running"
    elif success and failed:
        status = "partial_failed"
    elif failed:
        status = "failed"
    else:
        status = "success"
    return status, success, failed, active


def start_matrix_item(task_id: int, item_id: int) -> None:
    now = _now()
    with connect() as conn:
        updated = conn.execute(
            """
            UPDATE publish_task_items
            SET status = 'running', attempts = attempts + 1,
                startedAt = COALESCE(startedAt, ?), message = ''
            WHERE id = ? AND taskId = ? AND status = 'pending'
            """,
            (now, int(item_id), int(task_id)),
        )
        if updated.rowcount != 1:
            raise ValueError("抖音图文矩阵账号明细不能开始执行")
        conn.execute(
            """
            UPDATE publish_tasks
            SET status = 'running', startedAt = COALESCE(startedAt, ?),
                workerPid = ?, workerHeartbeatAt = ?
            WHERE id = ? AND mode IN (?, ?, ?)
            """,
            (
                now,
                os.getpid(),
                now,
                int(task_id),
                *_MATRIX_TASK_MODES,
            ),
        )
        conn.commit()


def _matrix_receipt(receipt: object) -> dict[str, str]:
    if not isinstance(receipt, Mapping):
        return {}
    return {
        key: str(value).strip()
        for key, value in receipt.items()
        if key in _MATRIX_RECEIPT_FIELDS and str(value or "").strip()
    }


def finish_matrix_item(
    task_id: int,
    item_id: int,
    *,
    ok: bool,
    message: str,
    error_code: str = "",
    receipt: Mapping[str, object] | None = None,
) -> None:
    projected = _matrix_receipt(receipt)
    now = _now()
    status = "success" if ok else "failed"
    safe_error = "" if ok else str(error_code or "douyin_graphic_matrix_item_failed")
    receipt_json = (
        json.dumps(projected, ensure_ascii=False, sort_keys=True) if projected else ""
    )
    with connect() as conn:
        updated = conn.execute(
            """
            UPDATE publish_task_items
            SET status = ?, message = ?, errorCode = ?, receiptJson = ?,
                platformPostId = ?, postUrl = ?, publishedAt = ?, finishedAt = ?
            WHERE id = ? AND taskId = ? AND status = 'running'
            """,
            (
                status,
                str(message or ""),
                safe_error,
                receipt_json,
                projected.get("platformPostId", ""),
                projected.get("postUrl", ""),
                projected.get("publishedAt", ""),
                now,
                int(item_id),
                int(task_id),
            ),
        )
        if updated.rowcount != 1:
            existing = conn.execute(
                "SELECT status FROM publish_task_items WHERE id = ? AND taskId = ?",
                (int(item_id), int(task_id)),
            ).fetchone()
            if existing is not None and existing["status"] == "success":
                return
            raise ValueError("抖音图文矩阵账号明细无法写入终态")
        parent_status, success, failed, active = _matrix_parent_counts(conn, int(task_id))
        conn.execute(
            """
            UPDATE publish_tasks
            SET status = ?, successCount = ?, failedCount = ?,
                lastError = ?, workerHeartbeatAt = ?,
                finishedAt = CASE WHEN ? = 0 THEN ? ELSE NULL END
            WHERE id = ?
            """,
            (
                parent_status,
                success,
                failed,
                "" if ok else str(message or ""),
                now,
                active,
                now,
                int(task_id),
            ),
        )
        conn.execute(
            """
            INSERT INTO publish_task_events
                (taskId, itemId, level, eventType, message, detailJson, createdAt)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(task_id),
                int(item_id),
                "info" if ok else "error",
                "matrix_item_success" if ok else "matrix_item_failed",
                str(message or ""),
                receipt_json,
                now,
            ),
        )
        conn.commit()


def close_matrix_parent(task_id: int) -> None:
    now = _now()
    with connect() as conn:
        status, success, failed, active = _matrix_parent_counts(conn, int(task_id))
        if active:
            raise ValueError("抖音图文矩阵仍有账号没有结束")
        conn.execute(
            """
            UPDATE publish_tasks
            SET status = ?, successCount = ?, failedCount = ?,
                finishedAt = COALESCE(finishedAt, ?), workerHeartbeatAt = ?
            WHERE id = ?
            """,
            (status, success, failed, now, now, int(task_id)),
        )
        conn.commit()


def pause_douyin_graphic_matrix(
    task_id: int, *, current_item_id: int | None = None
) -> None:
    """只在下一次最终提交前暂停矩阵，尚未提交的当前账号回到 pending。"""

    now = _now()
    with connect() as conn:
        source = conn.execute(
            "SELECT mode, status FROM publish_tasks WHERE id = ?",
            (int(task_id),),
        ).fetchone()
        if (
            source is None
            or str(source["mode"] or "") not in _MATRIX_TASK_MODES
            or str(source["status"] or "") not in {"pending", "running"}
        ):
            raise ValueError("抖音图文矩阵当前不能暂停")
        if current_item_id is not None:
            updated = conn.execute(
                """
                UPDATE publish_task_items
                SET status = 'pending', message = '用户已暂停，当前账号未执行最终提交',
                    errorCode = '', finishedAt = NULL
                WHERE id = ? AND taskId = ? AND status = 'running'
                """,
                (int(current_item_id), int(task_id)),
            )
            if updated.rowcount != 1:
                raise ValueError("抖音图文矩阵当前账号无法安全回退")
        conn.execute(
            """
            UPDATE publish_tasks
            SET status = 'paused', pauseReasonCode = ?, workerHeartbeatAt = ?
            WHERE id = ?
            """,
            (PAUSE_REASON_USER_REQUEST, now, int(task_id)),
        )
        conn.execute(
            """
            INSERT INTO publish_task_events
                (taskId, itemId, level, eventType, message, createdAt)
            VALUES (?, ?, 'warning', 'matrix_paused', '已按用户要求暂停，未开始后续账号', ?)
            """,
            (int(task_id), current_item_id, now),
        )
        conn.commit()


def prepare_douyin_graphic_matrix_retry(task_id: int) -> dict[str, object]:
    """生成仅包含失败或未开始账号的新本地检查快照。"""

    source = get_task(int(task_id))
    if not source or str(source.get("mode") or "") not in _MATRIX_TASK_MODES:
        raise ValueError("来源不是抖音图文矩阵任务")
    if str(source.get("status") or "") in {"pending", "running"}:
        raise ValueError("抖音图文矩阵仍在执行，不能创建重试")
    try:
        rows = json.loads(str(source.get("payloadJson") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("抖音图文矩阵快照无法读取") from exc
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
    ):
        raise ValueError("抖音图文矩阵快照无法读取")
    retry_indexes = [
        int(item.get("batchItemIndex") or 0)
        for item in source.get("items") or []
        if isinstance(item, dict)
        and str(item.get("status") or "") in {"failed", "pending"}
        and int(item.get("batchItemIndex") or 0) > 0
    ]
    if not retry_indexes:
        raise ValueError("抖音图文矩阵没有可重试的账号")
    from .douyin_graphic_matrix_service import retry_matrix

    matrix = retry_matrix(rows[0], retry_indexes)
    matrix["runtimeMode"] = "local_check"
    return {
        "sourceTaskId": int(task_id),
        "sourceTaskNo": str(source.get("taskNo") or ""),
        "itemIndexes": retry_indexes,
        "matrix": matrix,
    }


def create_douyin_graphic_matrix_retry(task_id: int) -> dict[str, object]:
    prepared = prepare_douyin_graphic_matrix_retry(int(task_id))
    with connect() as conn:
        duplicate = conn.execute(
            "SELECT id FROM publish_tasks WHERE revisionSourceTaskId = ? LIMIT 1",
            (int(task_id),),
        ).fetchone()
    if duplicate is not None:
        raise ValueError("该抖音图文矩阵任务已创建过重试")
    return create_douyin_graphic_matrix_task(
        dict(prepared["matrix"]),
        mode="oneclick_matrix_local_check",
        revision_source_task_id=int(task_id),
    )


def project_task_link(task_id: int) -> dict | None:
    """读取发布任务的非敏感内容项目归属。"""

    if type(task_id) is not int or task_id <= 0:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT projectId, taskId, phase
            FROM content_project_task_links
            WHERE taskId = ?
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _require_linkable_revision_source(
    conn,
    source_task_id: object,
    requested_item_indexes: list[int],
) -> None:
    """在子任务写事务内重新确认修订来源。"""

    if type(source_task_id) is not int or source_task_id <= 0:
        raise ValueError("修订来源任务已变化，请重新返回修改")
    source = conn.execute(
        """
        SELECT status, pauseReasonCode, payloadJson
        FROM publish_tasks
        WHERE id = ?
        """,
        (source_task_id,),
    ).fetchone()
    source_items = conn.execute(
        "SELECT batchItemIndex, status FROM publish_task_items WHERE taskId = ? ORDER BY id",
        (source_task_id,),
    ).fetchall()
    statuses = [row["status"] for row in source_items]
    if (
        source is None
        or workflow_from_payload_json(source["payloadJson"])
        != "douyin-commerce-batch"
        or not (
            source["status"] in {"failed", "partial_failed"}
            or (
                source["status"] == "paused"
                and source["pauseReasonCode"] == PAUSE_REASON_USER_REQUEST
            )
        )
        or not statuses
        or not any(status in {"failed", "pending"} for status in statuses)
        or any(status not in {"success", "failed", "pending"} for status in statuses)
    ):
        raise ValueError("修订来源任务已变化，请重新返回修改")
    indexed_statuses: dict[int, list[str]] = {}
    for position, row in enumerate(source_items, start=1):
        stored_index = row["batchItemIndex"]
        item_index = (
            stored_index
            if type(stored_index) is int and stored_index > 0
            else position
        )
        indexed_statuses.setdefault(item_index, []).append(row["status"])
    if any(
        len(indexed_statuses.get(item_index, [])) != 1
        or indexed_statuses[item_index][0] not in {"failed", "pending"}
        for item_index in requested_item_indexes
    ):
        raise ValueError("修订来源任务已变化，请重新返回修改")


def create_douyin_batch_task(
    batch: dict,
    mode: str = "oneclick_publish",
    *,
    schedule_now=None,
    resume_source_task_id: int | None = None,
    revision_source_task_id: int | None = None,
    batch_item_indexes: list[int] | None = None,
) -> dict:
    """为批量信封中的每条视频创建独立、可审计的发布项。"""

    from .douyin_commerce_batch_service import (
        item_publish_payload,
        prepare_batch_for_execution,
    )

    # 新批量入口必须始终经过完整契约和同一受控时钟，以避免创建任务时遗漏
    # enableTimer/scheduleTime。保留下面的窄兼容分支，仅用于历史任务服务已
    # 规范化的内部快照（它们没有批次信封字段，但每条已有显式开关）。
    if str(batch.get("workflow") or "").strip() == "douyin-commerce-batch":
        prepared_batch = prepare_batch_for_execution(batch, now=schedule_now)
    else:
        prepared_batch = dict(batch)
        raw_items = prepared_batch.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("抖音带货批量任务至少需要一条视频")
        for index, raw_item in enumerate(raw_items, start=1):
            if not isinstance(raw_item, dict) or type(raw_item.get("enableTimer")) is not bool:
                raise ValueError(f"第 {index} 条批量视频缺少明确的 enableTimer")
            if raw_item["enableTimer"] is True and not str(raw_item.get("scheduleTime") or "").strip():
                raise ValueError(f"第 {index} 条批量视频缺少明确的 scheduleTime")
    items = prepared_batch.get("items") if isinstance(prepared_batch, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("抖音带货批量任务至少需要一条视频")
    if batch_item_indexes is None:
        resolved_item_indexes = list(range(1, len(items) + 1))
    else:
        resolved_item_indexes = list(batch_item_indexes)
        if len(resolved_item_indexes) != len(items):
            raise ValueError("抖音带货续发视频序号数量不匹配")
        if (
            any(type(index) is not int for index in resolved_item_indexes)
            or any(index <= 0 for index in resolved_item_indexes)
            or len(set(resolved_item_indexes)) != len(resolved_item_indexes)
        ):
            raise ValueError("抖音带货续发视频序号必须为互异正整数")
    payloads = [item_publish_payload(prepared_batch, item) for item in items]
    now = _now()
    with connect() as conn:
        if revision_source_task_id is not None:
            _require_linkable_revision_source(
                conn,
                revision_source_task_id,
                resolved_item_indexes,
            )
        task = _insert_pending_task(
            conn,
            payloads,
            mode=mode,
            resume_source_task_id=resume_source_task_id,
            revision_source_task_id=revision_source_task_id,
        )
        task_items = conn.execute(
            "SELECT id FROM publish_task_items WHERE taskId = ? ORDER BY id",
            (task["id"],),
        ).fetchall()
        if len(task_items) != len(payloads):
            raise RuntimeError("批量任务条目数量无法确认")
        for index, (task_item, payload) in enumerate(zip(task_items, payloads)):
            poi = payload.get("locationPoi") if isinstance(payload.get("locationPoi"), dict) else {}
            location_name = str(poi.get("name") or payload.get("locationKeyword") or "").strip()
            location_address = str(poi.get("address") or "").strip()
            commission_suffix = {
                "commission": "【返佣】",
                "no_commission": "【无佣】",
            }.get(poi.get("observedCommissionType"), "")
            location_summary = (
                f"{location_name}{commission_suffix}（{location_address}）"
                if location_address
                else f"{location_name}{commission_suffix}"
            )
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
                (resolved_item_indexes[index], location_summary, schedule_summary, task_item["id"]),
            )
        conn.execute(
            "INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt) VALUES (?, 'info', 'batch_created', ?, ?)",
            (task["id"], "已创建抖音带货批量逐视频任务", now),
        )
        conn.commit()
    return task


def _revision_blocked(
    task_id: int,
    reason: str,
    source: dict | None = None,
) -> dict[str, object]:
    """返回不带原始载荷或本地路径的固定修订拒绝结果。"""

    return {
        "revisionAllowed": False,
        "blockedReason": reason,
        "sourceTaskId": int(task_id),
        "sourceTaskNo": str((source or {}).get("taskNo") or ""),
        "revisionItemIndexes": [],
        "successfulMediaKeys": [],
    }


def build_douyin_batch_media_key(media_id: object, media_path: object) -> str:
    """构造服务层与 UI 共用的稳定媒体身份，不改写合法文件名。"""

    if type(media_id) is int and media_id > 0:
        return f"media:{media_id}"
    path = str(media_path or "").strip()
    return f"path:{Path(path).resolve(strict=False)}" if path else ""


def _batch_media_key(payload: dict) -> str:
    """使用稳定媒体身份，兼容没有 mediaId 的历史任务。"""

    file_list = payload.get("fileList")
    media_path = (
        file_list[0] if isinstance(file_list, list) and file_list else ""
    )
    return build_douyin_batch_media_key(payload.get("mediaId"), media_path)


def _revision_schedule_interval(payloads: list[dict]) -> int:
    """从已保存的逐条时间恢复间隔；单条任务使用最小有效值。"""

    parsed: list[datetime] = []
    for payload in payloads:
        try:
            parsed.append(
                datetime.strptime(
                    str(payload.get("scheduleTime") or "").strip(),
                    "%Y-%m-%d %H:%M",
                )
            )
        except ValueError:
            return 1
    intervals = [
        int((later - earlier).total_seconds() // 60)
        for earlier, later in zip(parsed, parsed[1:])
        if later > earlier
    ]
    return intervals[0] if intervals else 1


def _revision_ancestor_successful_media_keys(
    source: dict, successful_media_keys: list[str]
) -> list[str] | None:
    """汇总整条修订祖先链；任何断链、环或损坏快照都按失败处理。"""

    source_id = source.get("id")
    if type(source_id) is not int or source_id <= 0:
        return None
    visited = {source_id}
    ancestor_id = source.get("revisionSourceTaskId")
    result = list(successful_media_keys)
    while ancestor_id is not None:
        if (
            type(ancestor_id) is not int
            or ancestor_id <= 0
            or ancestor_id in visited
        ):
            return None
        visited.add(ancestor_id)
        ancestor = get_task(ancestor_id)
        if not ancestor or ancestor.get("workflow") != "douyin-commerce-batch":
            return None
        if ancestor.get("status") == "paused":
            if ancestor.get("pauseReasonCode") != PAUSE_REASON_USER_REQUEST:
                return None
        elif ancestor.get("status") not in {"failed", "partial_failed"}:
            return None
        try:
            raw_payloads = json.loads(ancestor.get("payloadJson") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        items = ancestor.get("items")
        if (
            not isinstance(raw_payloads, list)
            or not raw_payloads
            or any(not isinstance(payload, dict) for payload in raw_payloads)
            or not isinstance(items, list)
            or len(items) != len(raw_payloads)
            or any(
                not isinstance(item, dict)
                or item.get("status") not in {"success", "failed", "pending"}
                for item in items
            )
        ):
            return None
        for item, payload in zip(items, raw_payloads):
            if item.get("status") != "success":
                continue
            try:
                media_key = _batch_media_key(payload)
            except (OSError, RuntimeError, ValueError):
                return None
            if not media_key:
                return None
            if media_key not in result:
                result.append(media_key)
        ancestor_id = ancestor.get("revisionSourceTaskId")
    return result


def prepare_douyin_batch_revision(task_id: int) -> dict[str, object]:
    """从来源任务纯读生成失败或未开始视频的可编辑快照。"""

    source = get_task(int(task_id))
    if not source:
        return _revision_blocked(int(task_id), "任务记录不存在或已被删除")
    if source["workflow"] != "douyin-commerce-batch":
        return _revision_blocked(
            int(task_id), "仅支持抖音带货批量任务返回修改", source
        )
    if source["status"] == "paused":
        if source.get("pauseReasonCode") != PAUSE_REASON_USER_REQUEST:
            return _revision_blocked(
                int(task_id), "当前暂停原因不能返回修改", source
            )
    elif source["status"] not in {"failed", "partial_failed"}:
        return _revision_blocked(
            int(task_id), "当前任务尚未结束或没有明确失败结果", source
        )
    source_items = source.get("items")
    if isinstance(source_items, list) and any(
        item.get("status") == "running"
        for item in source_items
        if isinstance(item, dict)
    ):
        return _revision_blocked(
            int(task_id), "当前任务仍有视频正在处理", source
        )
    try:
        raw_payloads = json.loads(source.get("payloadJson") or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return _revision_blocked(int(task_id), "来源任务的批次快照无法读取", source)
    items = source.get("items")
    if (
        not isinstance(raw_payloads, list)
        or not raw_payloads
        or not isinstance(items, list)
        or len(items) != len(raw_payloads)
    ):
        return _revision_blocked(int(task_id), "来源任务的批次快照无法读取", source)
    if any(not isinstance(payload, dict) for payload in raw_payloads):
        return _revision_blocked(int(task_id), "来源任务的批次快照无法读取", source)
    payloads = [dict(payload) for payload in raw_payloads]

    allowed_statuses = {"success", "failed", "pending"}
    if any(
        not isinstance(item, dict) or item.get("status") not in allowed_statuses
        for item in items
    ):
        return _revision_blocked(int(task_id), "来源任务的视频状态无法确认", source)
    revision_snapshots = [
        (position, item, payload)
        for position, (item, payload) in enumerate(zip(items, payloads), start=1)
        if item.get("status") in {"failed", "pending"}
    ]
    if not revision_snapshots:
        return _revision_blocked(int(task_id), "来源任务没有失败或未开始的视频", source)

    successful_media_keys: list[str] = []
    for item, payload in zip(items, payloads):
        if item.get("status") == "success":
            try:
                media_key = _batch_media_key(payload)
            except (OSError, RuntimeError, ValueError):
                return _revision_blocked(
                    int(task_id), "来源任务的成功视频身份无法确认", source
                )
            if not media_key:
                return _revision_blocked(
                    int(task_id), "来源任务的成功视频身份无法确认", source
                )
            if media_key not in successful_media_keys:
                successful_media_keys.append(media_key)
    successful_media_keys = _revision_ancestor_successful_media_keys(
        source, successful_media_keys
    )
    if successful_media_keys is None:
        return _revision_blocked(
            int(task_id), "来源任务的修改链无法确认", source
        )

    revision_payloads = [payload for _, _, payload in revision_snapshots]
    account_files = revision_payloads[0].get("accountList")
    if not isinstance(account_files, list) or len(account_files) != 1:
        return _revision_blocked(int(task_id), "来源任务的批次快照无法读取", source)
    account_file = str(account_files[0] or "").strip()
    if not account_file or any(
        payload.get("accountList") != [account_file] for payload in revision_payloads
    ):
        return _revision_blocked(int(task_id), "来源任务的批次快照无法读取", source)
    with connect() as conn:
        account = conn.execute(
            "SELECT id FROM user_info WHERE type = 3 AND filePath = ? LIMIT 1",
            (account_file,),
        ).fetchone()
    if not account:
        return _revision_blocked(int(task_id), "来源任务的抖音账号当前不可用", source)

    scheduled_flags = [payload.get("enableTimer") is True for payload in revision_payloads]
    if any(flag != scheduled_flags[0] for flag in scheduled_flags):
        return _revision_blocked(int(task_id), "来源任务的批次快照无法读取", source)
    scheduled = scheduled_flags[0]
    draft_items: list[dict[str, object]] = []
    revision_indexes: list[int] = []
    for position, item, payload in revision_snapshots:
        if payload.get("batchWorkflow") != "douyin-commerce-batch":
            return _revision_blocked(int(task_id), "来源任务的批次快照无法读取", source)
        file_list = payload.get("fileList")
        media_path = str(
            file_list[0]
            if isinstance(file_list, list) and len(file_list) == 1
            else ""
        ).strip()
        if not media_path or not Path(media_path).is_file():
            return _revision_blocked(int(task_id), "来源任务包含不可用的本地媒体", source)
        location = payload.get("locationPoi")
        if not isinstance(location, dict) or not location:
            return _revision_blocked(int(task_id), "来源任务的地点快照无法读取", source)
        batch_index = item.get("batchItemIndex")
        revision_indexes.append(
            batch_index
            if type(batch_index) is int and batch_index > 0
            else position
        )
        draft_items.append(
            {
                "mediaId": payload.get("mediaId"),
                "mediaPath": media_path,
                "locationPresetId": str(location.get("id") or "").strip(),
                "locationPreset": dict(location),
                "enableTimer": scheduled,
                "scheduleTimeOverride": (
                    str(payload.get("scheduleTime") or "").strip() if scheduled else ""
                ),
            }
        )

    first_payload = revision_payloads[0]
    last_payload = revision_payloads[-1]
    first_schedule_time = str(first_payload.get("scheduleTime") or "").strip()
    draft = {
        "accountId": int(account["id"]),
        "accountFile": account_file,
        "shared": {
            "title": first_payload.get("title"),
            "description": first_payload.get("description"),
            "tags": first_payload.get("tags"),
            "selectedMusic": first_payload.get("selectedMusic"),
            "contentDeclaration": first_payload.get("contentDeclaration"),
        },
        "lastLocationSearch": {
            "scope": last_payload.get("locationScope"),
            "keyword": last_payload.get("locationSearchKeyword"),
            "commissionFilter": last_payload.get("locationCommissionFilter"),
        },
        "publishMode": "interval-schedule" if scheduled else "immediate",
        "schedule": {
            "timezone": "Asia/Shanghai",
            "startTime": first_schedule_time if scheduled else "",
            "intervalMinutes": (
                _revision_schedule_interval(revision_payloads) if scheduled else 0
            ),
        },
        "items": draft_items,
    }
    try:
        from .douyin_commerce_batch_draft_service import normalize_batch_draft

        draft = normalize_batch_draft(draft)
    except Exception:
        return _revision_blocked(int(task_id), "来源任务的批次快照无法读取", source)
    return {
        "revisionAllowed": True,
        "blockedReason": "",
        "sourceTaskId": int(task_id),
        "sourceTaskNo": str(source.get("taskNo") or ""),
        "revisionItemIndexes": revision_indexes,
        "successfulMediaKeys": successful_media_keys,
        "draft": draft,
    }


def _resume_blocked(
    task_id: int,
    reason: str,
    *,
    source_task: dict | None = None,
    pending_count: int = 0,
) -> dict[str, object]:
    """返回不带平台副作用的续发拒绝结果。"""

    return {
        "resumeAllowed": False,
        "blockedReason": reason,
        "pendingCount": int(pending_count),
        "sourceTaskId": int(task_id),
        "sourceTaskNo": str((source_task or {}).get("taskNo") or ""),
        "itemIndexes": [],
    }


def _load_douyin_batch_resume_source(task_id: int) -> tuple[dict, list[tuple[dict, dict]]]:
    """读取来源任务及其按原批次序号关联的逐视频快照。"""

    source = get_task(task_id)
    if not source:
        raise ValueError("任务记录不存在或已被删除")
    if source.get("workflow") != "douyin-commerce-batch":
        raise ValueError("仅支持抖音带货批量任务继续发布")
    if source.get("status") != "paused":
        raise ValueError("仅已暂停的抖音带货批量任务可以继续发布")
    if source.get("pauseReasonCode") not in {
        PAUSE_REASON_USER_REQUEST,
        PAUSE_REASON_CLIENT_SHUTDOWN,
    }:
        raise ValueError("仅支持用户主动暂停或客户端退出的批次继续发布")
    try:
        payloads = json.loads(source.get("payloadJson") or "")
    except json.JSONDecodeError as exc:
        raise ValueError("来源批次保存的逐视频数据无法读取") from exc
    if not isinstance(payloads, list) or not payloads:
        raise ValueError("来源批次缺少逐视频保存数据")
    items = source.get("items")
    if not isinstance(items, list) or len(items) != len(payloads):
        raise ValueError("来源批次的视频明细与保存数据不一致")

    snapshots: list[tuple[dict, dict]] = []
    for position, (item, payload) in enumerate(zip(items, payloads), start=1):
        if not isinstance(item, dict) or not isinstance(payload, dict):
            raise ValueError("来源批次的视频快照格式无效")
        batch_index = item.get("batchItemIndex")
        if not isinstance(batch_index, int) or batch_index <= 0:
            batch_index = position
            item = {**item, "batchItemIndex": batch_index}
        snapshots.append((item, payload))
    return source, snapshots


def _build_douyin_batch_from_pending_payloads(
    pending_snapshots: list[tuple[dict, dict]],
    *,
    now: datetime,
) -> tuple[dict, list[int]]:
    """将待续发的逐视频快照重建为受现有契约校验的批次信封。"""

    from .douyin_commerce_batch_service import prepare_batch_for_execution
    from .douyin_music_service import validate_favorite_music_mode

    if not pending_snapshots:
        raise ValueError("来源批次没有未开始的视频")
    first_payload = pending_snapshots[0][1]
    account_files = first_payload.get("accountList")
    if not isinstance(account_files, list) or len(account_files) != 1:
        raise ValueError("来源批次账号信息不完整")
    account_file = str(account_files[0] or "").strip()
    if not account_file:
        raise ValueError("来源批次账号信息不完整")
    with connect() as conn:
        account = conn.execute(
            """
            SELECT 1
            FROM user_info
            WHERE type = 3 AND status = 1 AND filePath = ?
            LIMIT 1
            """,
            (account_file,),
        ).fetchone()
    if not account:
        raise ValueError("来源批次的抖音账号当前不可用，请重新登录后新建批次")

    scheduled = first_payload.get("enableTimer") is True
    items: list[dict] = []
    item_indexes: list[int] = []
    for source_item, payload in pending_snapshots:
        if payload.get("batchWorkflow") != "douyin-commerce-batch":
            raise ValueError("来源批次保存的数据不属于抖音带货批量任务")
        if payload.get("accountList") != [account_file]:
            raise ValueError("来源批次待续发视频的账号不一致")
        file_list = payload.get("fileList")
        if not isinstance(file_list, list) or len(file_list) != 1:
            raise ValueError("来源批次视频文件信息不完整")
        media_path = str(file_list[0] or "").strip()
        if not media_path or not Path(media_path).is_file():
            raise ValueError(f"原批次第 {source_item['batchItemIndex']} 条视频素材不存在")
        location = payload.get("locationPoi")
        if not isinstance(location, dict) or not str(location.get("name") or "").strip():
            raise ValueError(f"原批次第 {source_item['batchItemIndex']} 条视频地点信息不完整")
        if not payload.get("selectedMusic"):
            raise ValueError(f"原批次第 {source_item['batchItemIndex']} 条视频收藏音乐信息不完整")
        if not str(payload.get("contentDeclaration") or "").strip():
            raise ValueError(f"原批次第 {source_item['batchItemIndex']} 条视频自主声明信息不完整")
        try:
            validate_favorite_music_mode(payload.get("musicMode"))
        except Exception as exc:
            raise ValueError(f"原批次第 {source_item['batchItemIndex']} 条视频收藏音乐模式无效") from exc
        item_scheduled = payload.get("enableTimer") is True
        if item_scheduled != scheduled:
            raise ValueError("来源批次待续发视频的发布方式不一致")
        schedule_time = str(payload.get("scheduleTime") or "").strip() if item_scheduled else ""
        if item_scheduled and not schedule_time:
            raise ValueError(f"原批次第 {source_item['batchItemIndex']} 条视频缺少原定时时间")
        location_preset = {
            "poiId": location.get("poiId"),
            "name": location.get("name"),
            "address": location.get("address"),
            "distance": location.get("distance"),
            "scope": payload.get("locationScope") or location.get("scope"),
            "searchKeyword": payload.get("locationSearchKeyword")
            or location.get("searchKeyword"),
            "commissionFilter": payload.get("locationCommissionFilter"),
            "observedCommissionType": location.get("observedCommissionType"),
            "productCount": location.get("productCount"),
            "commissionProductCount": location.get("commissionProductCount"),
        }
        items.append(
            {
                "mediaPath": media_path,
                "locationPreset": location_preset,
                "scheduleTimeOverride": schedule_time,
            }
        )
        item_indexes.append(int(source_item["batchItemIndex"]))

    publish_mode = "interval-schedule" if scheduled else "immediate"
    batch = {
        "type": 3,
        "workflow": "douyin-commerce-batch",
        "commerceMode": "local-group-buy",
        "contentType": "video",
        "accountFile": account_file,
        "shared": {
            "title": first_payload.get("title"),
            "description": first_payload.get("description"),
            "tags": first_payload.get("tags"),
            "selectedMusic": first_payload.get("selectedMusic"),
            "contentDeclaration": first_payload.get("contentDeclaration"),
        },
        "publishMode": publish_mode,
        "schedule": {
            "timezone": "Asia/Shanghai",
            "startTime": items[0]["scheduleTimeOverride"] if scheduled else "",
            "intervalMinutes": 1 if scheduled else 0,
        },
        "items": items,
    }
    try:
        prepared_batch = prepare_batch_for_execution(batch, now=now)
    except Exception as exc:
        raise ValueError(f"来源批次无法安全续发：{exc}") from exc
    return prepared_batch, item_indexes


def prepare_douyin_batch_resume(task_id: int, *, now: datetime) -> dict[str, object]:
    """只读校验来源任务，返回可供用户确认的受控续发批次。"""

    try:
        source, snapshots = _load_douyin_batch_resume_source(int(task_id))
    except ValueError as exc:
        return _resume_blocked(int(task_id), str(exc))
    pending_snapshots = [
        (item, payload)
        for item, payload in snapshots
        if item.get("status") == "pending"
    ]
    if not pending_snapshots:
        return _resume_blocked(
            int(task_id),
            "来源批次没有未开始的视频",
            source_task=source,
        )
    with connect() as conn:
        existing_child = conn.execute(
            "SELECT taskNo FROM publish_tasks WHERE resumeSourceTaskId = ? LIMIT 1",
            (int(task_id),),
        ).fetchone()
    if existing_child:
        return _resume_blocked(
            int(task_id),
            f"该来源任务已创建续发子任务：{existing_child['taskNo']}",
            source_task=source,
            pending_count=len(pending_snapshots),
        )
    try:
        batch, item_indexes = _build_douyin_batch_from_pending_payloads(
            pending_snapshots,
            now=now,
        )
    except ValueError as exc:
        return _resume_blocked(
            int(task_id),
            str(exc),
            source_task=source,
            pending_count=len(pending_snapshots),
        )
    return {
        "resumeAllowed": True,
        "blockedReason": "",
        "pendingCount": len(pending_snapshots),
        "sourceTaskId": int(task_id),
        "sourceTaskNo": str(source.get("taskNo") or ""),
        "itemIndexes": item_indexes,
        "batch": batch,
    }


def create_douyin_batch_resume(task_id: int, *, now: datetime) -> dict[str, object]:
    """重新校验后创建独立续发子任务，绝不改写来源任务。"""

    prepared = prepare_douyin_batch_resume(int(task_id), now=now)
    if prepared.get("resumeAllowed") is not True:
        raise ValueError(str(prepared.get("blockedReason") or "当前任务不可继续发布"))
    child = create_douyin_batch_task(
        dict(prepared["batch"]),
        mode="oneclick_resume",
        schedule_now=now,
        resume_source_task_id=int(prepared["sourceTaskId"]),
        batch_item_indexes=list(prepared["itemIndexes"]),
    )
    source_task_no = str(prepared["sourceTaskNo"])
    item_indexes = list(prepared["itemIndexes"])
    now_text = _now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt)
            VALUES (?, 'info', 'batch_resume_created', ?, ?)
            """,
            (
                int(task_id),
                f"已创建续发子任务 {child['taskNo']}，包含原批次第 {'、'.join(map(str, item_indexes))} 条视频",
                now_text,
            ),
        )
        conn.execute(
            """
            INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt)
            VALUES (?, 'info', 'batch_resume_created', ?, ?)
            """,
            (
                int(child["id"]),
                f"来源任务 {source_task_no}，续发原批次第 {'、'.join(map(str, item_indexes))} 条视频",
                now_text,
            ),
        )
        conn.commit()
    return {
        "task": child,
        "batch": prepared["batch"],
        "sourceTaskNo": source_task_no,
        "itemIndexes": item_indexes,
    }


def mark_task_running(task_id: int, message: str) -> None:
    """标记一键发本地预检开始执行。"""

    now = _now()
    with connect() as conn:
        conn.execute(
            """
            UPDATE publish_tasks
            SET status = 'running', startedAt = COALESCE(startedAt, ?),
                workerPid = ?, workerHeartbeatAt = ?
            WHERE id = ?
            """,
            (now, os.getpid(), now, int(task_id)),
        )
        conn.execute(
            "INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt) VALUES (?, 'info', 'running', ?, ?)",
            (int(task_id), message, now),
        )
        conn.commit()


def touch_task_heartbeat(task_id: int) -> bool:
    """续租受控发布进程；不改变已经结束的任务。"""

    now = _now()
    with connect() as conn:
        updated = conn.execute(
            """
            UPDATE publish_tasks
            SET workerPid = ?, workerHeartbeatAt = ?
            WHERE id = ? AND status IN ('pending', 'running')
            """,
            (os.getpid(), now, int(task_id)),
        )
        conn.commit()
    return updated.rowcount == 1


def _redact_facebook_message(value: object) -> str:
    """Keep a bounded diagnostic while removing credentials and local paths."""

    message = " ".join(str(value or "").split())
    message = re.sub(
        r"(?i)\b(?:cookie|authorization|token|access[_-]?token|verificationcode|"
        r"password|secret|sessionpath)"
        r"\s*[:=]\s*[^\s;,]+",
        "[redacted]",
        message,
    )
    message = re.sub(
        r"(?i)\bbearer\s+[^\s;,]+",
        "[redacted]",
        message,
    )
    message = re.sub(
        r"/(?:Users|private|var|tmp)/[^\s;,]+",
        "[local-path]",
        message,
    )
    return message[:500]


def _facebook_public_phase(value: object) -> str:
    phase = str(value or "")
    phase = {
        "succeeded": "published_readback_confirmed",
        "safe_failed": "failed",
        "readback_unique": "published_readback_confirmed",
        "readback_none": "ambiguous",
        "readback_mismatch": "ambiguous",
    }.get(phase, phase)
    if phase not in _FACEBOOK_PUBLIC_PHASES:
        raise ValueError("Facebook Page 任务阶段无效")
    return phase


def _facebook_task_item_in_transaction(conn, task_id: int):
    rows = conn.execute(
        """
        SELECT item.*, task.mode AS taskMode, task.payloadJson AS taskPayloadJson,
               task.status AS taskStatus
        FROM publish_task_items AS item
        JOIN publish_tasks AS task ON task.id = item.taskId
        WHERE item.taskId = ? AND item.platformType = 9
        ORDER BY item.id
        """,
        (int(task_id),),
    ).fetchall()
    if len(rows) != 1 or str(rows[0]["taskMode"] or "") != "oneclick_publish":
        raise ValueError("Facebook Page 正式任务不存在或目标不唯一")
    return rows[0]


def _insert_facebook_task_event(
    conn,
    *,
    task_id: int,
    item_id: int,
    level: str,
    event_type: str,
    message: str,
    receipt_json: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO publish_task_events
            (taskId, itemId, level, eventType, message, detailJson, createdAt)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(task_id),
            int(item_id),
            str(level),
            str(event_type),
            str(message),
            str(receipt_json),
            str(created_at),
        ),
    )


def _write_facebook_task_projection_in_transaction(
    conn,
    task_id: int,
    *,
    phase: str,
    message: str,
    error_code: str,
    receipt: Mapping[str, object],
    event_type: str | None = None,
    now_text: str | None = None,
    status_override: str | None = None,
) -> bool:
    """Persist the Page item, parent, event and heartbeat in one transaction."""

    public_phase = _facebook_public_phase(phase)
    item_status = str(status_override or _FACEBOOK_PHASE_STATUS[public_phase])
    if item_status not in {
        "running",
        "waiting_user_verification",
        "success",
        "failed",
    }:
        raise ValueError("Facebook Page 任务状态无效")
    public_message = _redact_facebook_message(message)
    safe_error = "" if item_status in {
        "running",
        "waiting_user_verification",
        "success",
    } else _stable_error_code(error_code)
    if item_status == "failed" and not safe_error:
        safe_error = (
            "facebook_publish_rejected"
            if public_phase == "confirmed_not_published"
            else "facebook_publish_failed"
        )
    try:
        safe_receipt = project_facebook_page_receipt(receipt)
    except ValueError as exc:
        raise ValueError("Facebook Page 任务回执无效") from exc
    safe_receipt["phase"] = public_phase
    receipt_json = json.dumps(
        safe_receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    item = _facebook_task_item_in_transaction(conn, int(task_id))
    terminal = item_status in {"success", "failed"}
    changed_at = str(now_text or _now())
    reel_id = str(safe_receipt.get("reelId") or "")
    reel_url = str(safe_receipt.get("url") or "")
    published_at = str(safe_receipt.get("publishedAt") or "")
    conn.execute(
        """
        UPDATE publish_task_items
        SET status = ?, message = ?, errorCode = ?, receiptJson = ?,
            attempts = CASE WHEN startedAt IS NULL THEN attempts + 1 ELSE attempts END,
            startedAt = COALESCE(startedAt, ?),
            finishedAt = CASE WHEN ? THEN ? ELSE NULL END,
            platformPostId = CASE WHEN ? <> '' THEN ? ELSE platformPostId END,
            postUrl = CASE WHEN ? <> '' THEN ? ELSE postUrl END,
            publishedAt = CASE WHEN ? <> '' THEN ? ELSE publishedAt END
        WHERE id = ? AND taskId = ?
        """,
        (
            item_status,
            public_message,
            safe_error,
            receipt_json,
            changed_at,
            int(terminal),
            changed_at,
            reel_id,
            reel_id,
            reel_url,
            reel_url,
            published_at,
            published_at,
            int(item["id"]),
            int(task_id),
        ),
    )
    task_status = item_status
    success_count = int(item_status == "success")
    failed_count = int(item_status == "failed")
    conn.execute(
        """
        UPDATE publish_tasks
        SET status = ?, successCount = ?, failedCount = ?, skippedCount = 0,
            lastError = CASE WHEN ? <> '' THEN ? ELSE NULL END,
            startedAt = COALESCE(startedAt, ?),
            finishedAt = CASE WHEN ? THEN ? ELSE NULL END,
            workerPid = CASE WHEN ? THEN NULL ELSE ? END,
            workerHeartbeatAt = ?
        WHERE id = ?
        """,
        (
            task_status,
            success_count,
            failed_count,
            safe_error,
            public_message,
            changed_at,
            int(terminal),
            changed_at,
            int(terminal),
            os.getpid(),
            changed_at,
            int(task_id),
        ),
    )
    _insert_facebook_task_event(
        conn,
        task_id=int(task_id),
        item_id=int(item["id"]),
        level="error" if safe_error else "info",
        event_type=str(event_type or _FACEBOOK_PHASE_EVENT[public_phase]),
        message=public_message,
        receipt_json=receipt_json,
        created_at=changed_at,
    )
    return True


def record_facebook_progress(
    task_id: int,
    *,
    phase: str,
    message: str,
    receipt: Mapping[str, object],
    _expected_state: str | None = None,
    _require_unleased: bool = False,
    _allow_stored_rejected_decision: bool = False,
) -> None:
    """Atomically persist one Page claim edge and its public task projection."""

    from . import controlled_publish

    public_phase = _facebook_public_phase(phase)
    with connect() as conn:
        controlled_publish._ensure_facebook_page_claim_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            _facebook_task_item_in_transaction(conn, int(task_id))
            claim_row = conn.execute(
                "SELECT * FROM facebook_page_publish_claims WHERE taskId = ?",
                (int(task_id),),
            ).fetchone()
            if claim_row is None:
                raise ValueError("Facebook Page 任务缺少 claim")
            claim = dict(claim_row)
            current_state = str(claim.get("state") or "")
            expected_page = str(claim.get("pageReference") or "")
            if receipt.get("pageId") not in {None, expected_page}:
                raise controlled_publish.ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page 任务回执主体与 claim 不一致。",
                )
            receipt_values = dict(receipt)
            receipt_values.setdefault("pageId", expected_page)
            receipt = receipt_values
            desired_state = {
                "final_action_claimed": "final_action_claimed",
                "final_action_clicked": "final_action_clicked",
                "published_readback_confirmed": "succeeded",
                "ambiguous": "ambiguous",
                "confirmed_not_published": "confirmed_not_published",
                "failed": "safe_failed",
            }.get(public_phase)
            authoritative_receipt: Mapping[str, object] = receipt
            if (
                _expected_state is not None
                and desired_state is not None
                and current_state == desired_state
            ):
                raise controlled_publish.ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page claim 生命周期不允许重复跳转。",
                )
            if desired_state is not None and current_state != desired_state:
                expected = str(_expected_state or current_state)
                transition = controlled_publish._mark_facebook_page_checkpoint_in_transaction(
                    conn,
                    int(task_id),
                    expected_state=expected,
                    new_state=desired_state,
                    receipt=receipt,
                    require_unleased=bool(_require_unleased),
                    allow_stored_rejected_decision=bool(
                        _allow_stored_rejected_decision
                    ),
                )
                authoritative_receipt = dict(transition["receipt"])
            elif _expected_state is not None and current_state != str(
                _expected_state
            ):
                raise controlled_publish.ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page claim 生命周期状态已变化。",
                )
            elif desired_state == "succeeded":
                authoritative_receipt = dict(
                    controlled_publish._validated_facebook_page_claim_evidence(
                        claim
                    )["receipt"]
                )
            _write_facebook_task_projection_in_transaction(
                conn,
                int(task_id),
                phase=public_phase,
                message=message,
                error_code=(
                    "facebook_publish_outcome_unknown"
                    if public_phase == "ambiguous"
                    else "facebook_worker_interrupted"
                    if public_phase == "failed"
                    else ""
                ),
                receipt=authoritative_receipt,
                status_override=(
                    "running"
                    if public_phase == "published_readback_confirmed"
                    else None
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def mark_facebook_result(
    task_id: int,
    *,
    ok: bool,
    message: str,
    error_code: str = "",
    receipt: Mapping[str, object] | None = None,
    event_type: str | None = None,
) -> None:
    """Close or repair a formal Page task from the claim authority."""

    from . import controlled_publish

    supplied = receipt or {}
    with connect() as conn:
        controlled_publish._ensure_facebook_page_claim_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            _facebook_task_item_in_transaction(conn, int(task_id))
            row = conn.execute(
                "SELECT * FROM facebook_page_publish_claims WHERE taskId = ?",
                (int(task_id),),
            ).fetchone()
            if row is None:
                raise ValueError("Facebook Page 任务缺少 claim")
            claim = dict(row)
            state = str(claim.get("state") or "")
            supplied_values = dict(supplied)
            expected_page = str(claim.get("pageReference") or "")
            if supplied_values.get("pageId") not in {None, expected_page}:
                raise controlled_publish.ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page 结果回执主体与 claim 不一致。",
                )
            supplied_values.setdefault("pageId", expected_page)
            supplied = supplied_values
            authoritative: Mapping[str, object] = supplied
            public_phase = "failed"
            safe_code = _stable_error_code(error_code)
            if state == "succeeded":
                evidence = controlled_publish._validated_facebook_page_claim_evidence(
                    claim
                )
                authoritative = dict(evidence["receipt"])
                if ok:
                    projected_supplied = project_facebook_page_receipt(supplied)
                    for key in ("pageId", "reelId", "url", "publishedAt"):
                        if projected_supplied.get(key) != authoritative.get(key):
                            raise controlled_publish.ControlledPublishError(
                                "facebook_claim_lifecycle_invalid",
                                "Facebook Page 成功回填与 claim 回执不一致。",
                            )
                public_phase = "published_readback_confirmed"
                safe_code = ""
            elif ok:
                raise controlled_publish.ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page 成功回填前缺少成功 claim。",
                )
            elif state == "reserved":
                transition = controlled_publish._mark_facebook_page_checkpoint_in_transaction(
                    conn,
                    int(task_id),
                    expected_state="reserved",
                    new_state="safe_failed",
                    receipt=supplied,
                )
                authoritative = dict(transition["receipt"])
                safe_code = safe_code or "facebook_worker_interrupted"
            elif state in {"final_action_claimed", "final_action_clicked"}:
                transition = controlled_publish._mark_facebook_page_checkpoint_in_transaction(
                    conn,
                    int(task_id),
                    expected_state=state,
                    new_state="ambiguous",
                    receipt=supplied,
                )
                authoritative = dict(transition["receipt"])
                public_phase = "ambiguous"
                safe_code = safe_code or "facebook_publish_outcome_unknown"
            elif state == "ambiguous":
                controlled_publish._validated_facebook_page_claim_evidence(claim)
                public_phase = "ambiguous"
                safe_code = safe_code or "facebook_publish_outcome_unknown"
            elif state == "confirmed_not_published":
                controlled_publish._validated_facebook_page_claim_evidence(claim)
                public_phase = "confirmed_not_published"
                safe_code = safe_code or "facebook_publish_rejected"
            elif state == "safe_failed":
                controlled_publish._validated_facebook_page_claim_evidence(claim)
                safe_code = safe_code or "facebook_worker_interrupted"
            else:
                raise controlled_publish.ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page claim 状态无效。",
                )
            _write_facebook_task_projection_in_transaction(
                conn,
                int(task_id),
                phase=public_phase,
                message=message,
                error_code=safe_code,
                receipt=authoritative,
                event_type=event_type,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _reconcile_stale_facebook_page_claim_in_transaction(
    conn,
    task_id: int,
) -> bool:
    """Repair one already-proven stale Page task under the caller's lock."""

    from . import controlled_publish

    row = conn.execute(
        "SELECT * FROM facebook_page_publish_claims WHERE taskId = ?",
        (int(task_id),),
    ).fetchone()
    if row is None:
        return False
    claim = dict(row)
    state = str(claim.get("state") or "")
    item = _facebook_task_item_in_transaction(conn, int(task_id))
    task_status = str(item["taskStatus"] or "")
    if task_status not in {"pending", "running", "waiting_user_verification"}:
        return False
    page_receipt: Mapping[str, object] = {
        "pageId": str(claim.get("pageReference") or "")
    }
    if state == "succeeded":
        evidence = controlled_publish._validated_facebook_page_claim_evidence(
            claim
        )
        return _write_facebook_task_projection_in_transaction(
            conn,
            int(task_id),
            phase="published_readback_confirmed",
            message="Facebook Page 已根据完整 claim 回执修复成功状态",
            error_code="",
            receipt=dict(evidence["receipt"]),
            event_type="facebook_success_projection_repaired",
        )
    if state == "reserved":
        boundary = conn.execute(
            """
            SELECT 1 FROM publish_task_events
            WHERE taskId = ? AND eventType IN (
                'facebook_final_action_claimed',
                'facebook_final_action_clicked',
                'facebook_publish_outcome_ambiguous'
            ) LIMIT 1
            """,
            (int(task_id),),
        ).fetchone()
        if boundary is not None:
            # A contradictory boundary cannot release replay.  Keep the claim
            # blocking and surface the uncertainty without inventing an edge.
            return _write_facebook_task_projection_in_transaction(
                conn,
                int(task_id),
                phase="ambiguous",
                message="Facebook Page 存在最终动作记录，必须只读核对",
                error_code="facebook_publish_outcome_unknown",
                receipt=page_receipt,
            )
        transition = controlled_publish._mark_facebook_page_checkpoint_in_transaction(
            conn,
            int(task_id),
            expected_state="reserved",
            new_state="safe_failed",
            receipt=page_receipt,
        )
        return _write_facebook_task_projection_in_transaction(
            conn,
            int(task_id),
            phase="failed",
            message="Facebook Page worker 在最终动作前失联，未发布",
            error_code="facebook_worker_interrupted",
            receipt=dict(transition["receipt"]),
            event_type="facebook_worker_interrupted",
        )
    if state in {"final_action_claimed", "final_action_clicked"}:
        controlled_publish._validated_facebook_page_claim_evidence(claim)
        transition = controlled_publish._mark_facebook_page_checkpoint_in_transaction(
            conn,
            int(task_id),
            expected_state=state,
            new_state="ambiguous",
            receipt=page_receipt,
        )
        return _write_facebook_task_projection_in_transaction(
            conn,
            int(task_id),
            phase="ambiguous",
            message="Facebook Page 最终动作后 worker 失联，必须只读核对",
            error_code="facebook_publish_outcome_unknown",
            receipt=dict(transition["receipt"]),
        )
    evidence = controlled_publish._validated_facebook_page_claim_evidence(claim)
    if state == "ambiguous":
        return _write_facebook_task_projection_in_transaction(
            conn,
            int(task_id),
            phase="ambiguous",
            message="Facebook Page 发布结果尚未唯一确认",
            error_code="facebook_publish_outcome_unknown",
            receipt=dict(evidence["receipt"]),
        )
    if state == "safe_failed":
        return _write_facebook_task_projection_in_transaction(
            conn,
            int(task_id),
            phase="failed",
            message="Facebook Page worker 在最终动作前已安全停止",
            error_code="facebook_worker_interrupted",
            receipt=dict(evidence["receipt"]),
            event_type="facebook_worker_interrupted",
        )
    if state == "confirmed_not_published":
        return _write_facebook_task_projection_in_transaction(
            conn,
            int(task_id),
            phase="confirmed_not_published",
            message="Facebook Page 已确认未创建目标 Reel",
            error_code="facebook_publish_rejected",
            receipt=dict(evidence["receipt"]),
        )
    raise controlled_publish.ControlledPublishError(
        "facebook_claim_lifecycle_invalid",
        "Facebook Page claim 状态无效。",
    )


def reconcile_stale_facebook_page_claim(
    task_id: int,
    *,
    lease_seconds: int = 30,
    now: datetime | None = None,
) -> bool:
    """Reconcile one dead/stale Page worker without replaying its publish."""

    current = now or datetime.now()
    from . import controlled_publish

    with connect() as conn:
        controlled_publish._ensure_facebook_page_claim_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status, workerPid, workerHeartbeatAt, startedAt, createdAt
                FROM publish_tasks WHERE id = ?
                """,
                (int(task_id),),
            ).fetchone()
            if row is None or str(row["status"] or "") not in {
                "pending",
                "running",
                "waiting_user_verification",
            }:
                conn.rollback()
                return False
            worker_pid = int(row["workerPid"] or 0)
            if worker_pid > 0:
                try:
                    os.kill(worker_pid, 0)
                    conn.rollback()
                    return False
                except OSError:
                    pass
            reference_text = str(
                row["workerHeartbeatAt"] or row["startedAt"] or row["createdAt"] or ""
            )
            try:
                reference = datetime.fromisoformat(reference_text)
            except ValueError:
                reference = current - timedelta(
                    seconds=max(1, int(lease_seconds)) + 1
                )
            if (current - reference).total_seconds() <= max(
                1, int(lease_seconds)
            ):
                conn.rollback()
                return False
            changed = _reconcile_stale_facebook_page_claim_in_transaction(
                conn,
                int(task_id),
            )
            if changed:
                conn.commit()
            else:
                conn.rollback()
            return changed
        except Exception:
            conn.rollback()
            raise


def _fail_active_task_in_transaction(
    conn,
    task_id: int,
    *,
    error_code: str,
    message: str,
    event_type: str = "controlled_task_aborted",
    receipt: Mapping[str, object] | None = None,
) -> bool:
    """Close an active task without committing the caller's transaction."""

    code = str(error_code or "controlled_task_aborted").strip()
    public_message = f"{str(message).strip()}（错误码 {code}）"
    receipt_json = ""
    projected_receipt: dict[str, object] = {}
    if receipt is not None:
        projected_receipt = _tiktok_receipt_projection(receipt)
        if projected_receipt:
            receipt_json = json.dumps(
                projected_receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
    clear_scheduled_published_at = bool(
        projected_receipt.get("scheduleMode") == "platform_native"
        and "publishedAt" in projected_receipt
        and projected_receipt.get("publishedAt") is None
    )
    now = _now()
    updated = conn.execute(
        """
        UPDATE publish_task_items
        SET status = 'failed', message = ?, errorCode = ?, attempts = attempts + 1,
            startedAt = COALESCE(startedAt, ?), finishedAt = ?,
            receiptJson = CASE WHEN ? <> '' THEN ? ELSE receiptJson END,
            publishedAt = CASE WHEN ? THEN '' ELSE publishedAt END
        WHERE taskId = ? AND status IN ('pending', 'running')
        """,
        (
            public_message,
            code,
            now,
            now,
            receipt_json,
            receipt_json,
            clear_scheduled_published_at,
            int(task_id),
        ),
    )
    if updated.rowcount == 0:
        return False
    summary = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM publish_task_items WHERE taskId = ?
        """,
        (int(task_id),),
    ).fetchone()
    success = int(summary["success"] or 0)
    failed = int(summary["failed"] or 0)
    status = "partial_failed" if success and failed else "failed" if failed else "success"
    conn.execute(
        """
        UPDATE publish_tasks
        SET status = ?, successCount = ?, failedCount = ?, lastError = ?,
            finishedAt = ?, workerHeartbeatAt = ?
        WHERE id = ?
        """,
        (status, success, failed, public_message, now, now, int(task_id)),
    )
    conn.execute(
        """
        INSERT INTO publish_task_events
            (taskId, level, eventType, message, createdAt)
        VALUES (?, 'error', ?, ?, ?)
        """,
        (int(task_id), str(event_type), public_message, now),
    )
    return True


def fail_active_task(
    task_id: int,
    *,
    error_code: str,
    message: str,
    event_type: str = "controlled_task_aborted",
    receipt: Mapping[str, object] | None = None,
) -> bool:
    """把未取得终态回执的执行项一次性关闭，防止永久 pending。"""

    with connect() as conn:
        changed = _fail_active_task_in_transaction(
            conn,
            int(task_id),
            error_code=error_code,
            message=message,
            event_type=event_type,
            receipt=receipt,
        )
        if changed:
            conn.commit()
        return changed


def _reconcile_stale_controlled_task_in_transaction(
    conn,
    task_id: int,
    *,
    lease_seconds: int = 30,
    now: datetime | None = None,
) -> bool:
    """Reconcile one stale task without committing the caller's transaction."""

    current = now or datetime.now()
    row = conn.execute(
        """
        SELECT task.mode, task.status, task.workerPid,
               task.workerHeartbeatAt, task.startedAt, task.createdAt,
               EXISTS(
                   SELECT 1 FROM publish_task_items AS item
                   WHERE item.taskId = task.id
                     AND item.platformType = 7
                     AND COALESCE(item.platformPostId, '') <> ''
               ) AS youtubeHasKnownVideo
               ,EXISTS(
                   SELECT 1 FROM publish_task_events AS event
                   WHERE event.taskId = task.id
                     AND event.eventType IN (
                         'tiktok_final_action_triggered',
                         'tiktok_publish_outcome_ambiguous'
                     )
               ) AS tiktokFinalActionTriggered,
               task.payloadJson
        FROM publish_tasks AS task WHERE task.id = ?
        """,
        (int(task_id),),
    ).fetchone()
    if not row:
        return False
    if str(row["mode"] or "") not in {
        "oneclick_preflight",
        "oneclick_publish",
        "oneclick_platform_form_check",
        "oneclick_draft",
        "oneclick_matrix_local_check",
        "oneclick_matrix_preflight",
        "oneclick_matrix_publish",
    } or str(row["status"] or "") not in {"pending", "running"}:
        return False
    worker_pid = int(row["workerPid"] or 0) if "workerPid" in row.keys() else 0
    if worker_pid > 0:
        try:
            os.kill(worker_pid, 0)
            return False
        except OSError:
            pass
    reference_text = str(
        row["workerHeartbeatAt"] or row["startedAt"] or row["createdAt"] or ""
    )
    try:
        reference = datetime.fromisoformat(reference_text)
    except ValueError:
        reference = current - timedelta(seconds=max(1, int(lease_seconds)) + 1)
    if (current - reference).total_seconds() <= max(1, int(lease_seconds)):
        return False
    youtube_has_known_video = bool(row["youtubeHasKnownVideo"])
    payloads = _payloads_from_json(row["payloadJson"])
    is_facebook_page_task = (
        len(payloads) == 1 and int(payloads[0].get("type") or 0) == 9
    )
    if is_facebook_page_task:
        claim_table = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'facebook_page_publish_claims'
            """
        ).fetchone()
        if claim_table is not None:
            claim = conn.execute(
                "SELECT 1 FROM facebook_page_publish_claims WHERE taskId = ?",
                (int(task_id),),
            ).fetchone()
            if claim is not None:
                return _reconcile_stale_facebook_page_claim_in_transaction(
                    conn,
                    int(task_id),
                )
    is_tiktok_task = (
        len(payloads) == 1 and int(payloads[0].get("type") or 0) == 6
    )
    tiktok_final_action_triggered = bool(row["tiktokFinalActionTriggered"])
    if is_tiktok_task and tiktok_final_action_triggered:
        payload = payloads[0]
        account_ids = list(payload.get("accountIds") or [])
        account_id = (
            int(account_ids[0])
            if len(account_ids) == 1 and type(account_ids[0]) is int
            else 0
        )
        scheduled_tiktok = (
            payload.get("scheduleMode") == "platform_native"
            and payload.get("scheduleTimezone") == "Asia/Shanghai"
            and type(payload.get("scheduledAt")) is str
        )
        platform_accepted = False
        if scheduled_tiktok:
            accepted_event = conn.execute(
                """
                SELECT 1
                FROM publish_task_events
                WHERE taskId = ? AND eventType = 'tiktok_scheduled_accepted'
                LIMIT 1
                """,
                (int(task_id),),
            ).fetchone()
            prior_item = conn.execute(
                """
                SELECT receiptJson
                FROM publish_task_items
                WHERE taskId = ? AND platformType = 6
                ORDER BY id
                LIMIT 1
                """,
                (int(task_id),),
            ).fetchone()
            prior_receipt: dict[str, object] = {}
            if prior_item and str(prior_item["receiptJson"] or ""):
                try:
                    loaded_receipt = json.loads(str(prior_item["receiptJson"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    loaded_receipt = {}
                prior_receipt = _tiktok_receipt_projection(loaded_receipt)
            platform_accepted = bool(
                accepted_event or prior_receipt.get("platformAccepted") is True
            )
        receipt = {
            "accountId": account_id,
            "visibility": "public",
            "platformWriteOccurred": True,
            "finalActionTriggered": True,
            "contentId": None,
            "contentUrl": None,
            "publishedAt": None,
            "phase": "ambiguous",
        }
        if scheduled_tiktok:
            receipt.update(
                {
                    "scheduleMode": "platform_native",
                    "scheduledAt": payload["scheduledAt"],
                    "scheduleTimezone": payload["scheduleTimezone"],
                }
            )
            if platform_accepted:
                receipt["platformAccepted"] = True
        return _fail_active_task_in_transaction(
            conn,
            int(task_id),
            error_code=(
                "tiktok_schedule_outcome_unknown"
                if scheduled_tiktok
                else "tiktok_publish_outcome_unknown"
            ),
            message=(
                "TikTok 定时动作后发布进程失联，必须先人工核对定时内容列表"
                if scheduled_tiktok
                else "TikTok 最终动作后发布进程失联，必须先人工核对内容列表"
            ),
            event_type="tiktok_publish_outcome_ambiguous",
            receipt=receipt,
        )
    return _fail_active_task_in_transaction(
        conn,
        int(task_id),
        error_code=(
            "youtube_manual_reconciliation_required"
            if youtube_has_known_video
            else "controlled_worker_lease_expired"
        ),
        message=(
            "YouTube 已取得精确视频 ID，但发布进程失联；"
            "必须先核对该视频，禁止自动重传"
            if youtube_has_known_video
            else "受控发布进程已失联，未取得平台最终回执"
        ),
        event_type=(
            "youtube_manual_reconciliation_required"
            if youtube_has_known_video
            else "controlled_worker_lease_expired"
        ),
    )


def reconcile_stale_controlled_task(
    task_id: int,
    *,
    lease_seconds: int = 30,
    now: datetime | None = None,
) -> bool:
    """查询时收口失联的本机任务；仅处理受控发布模式。"""

    with connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = _reconcile_stale_controlled_task_in_transaction(
                conn,
                int(task_id),
                lease_seconds=lease_seconds,
                now=now,
            )
            if changed:
                conn.commit()
            else:
                conn.rollback()
            return changed
        except Exception:
            conn.rollback()
            raise


def mark_task_paused(task_id: int, message: str, *, pause_reason_code: str) -> None:
    """标记批量任务已受控暂停，未开始的条目必须保持 pending。"""

    if pause_reason_code not in DOUYIN_BATCH_PAUSE_REASONS:
        raise ValueError("未知的批量暂停原因")
    now = _now()
    with connect() as conn:
        conn.execute(
            "UPDATE publish_tasks SET status = 'paused', pauseReasonCode = ? WHERE id = ?",
            (pause_reason_code, int(task_id)),
        )
        conn.execute(
            "INSERT INTO publish_task_events (taskId, level, eventType, message, createdAt) VALUES (?, 'warning', 'batch_paused', ?, ?)",
            (int(task_id), message, now),
        )
        conn.commit()


def pause_douyin_batch_before_submit(
    task_id: int, item_id: int, message: str
) -> None:
    """客户端退出前，原子回退尚未获得最终成功回执的当前条目。"""

    now = _now()
    with connect() as conn:
        updated = conn.execute(
            "UPDATE publish_task_items SET status = 'pending', message = ?, finishedAt = NULL WHERE id = ? AND taskId = ? AND status <> 'success'",
            (message, int(item_id), int(task_id)),
        )
        if updated.rowcount != 1:
            raise ValueError("最终提交前条目状态无法安全回退")
        conn.execute(
            "UPDATE publish_tasks SET status = 'paused', pauseReasonCode = ? WHERE id = ?",
            (PAUSE_REASON_CLIENT_SHUTDOWN, int(task_id)),
        )
        conn.execute(
            "INSERT INTO publish_task_events (taskId, itemId, level, eventType, message, createdAt) VALUES (?, ?, 'warning', 'batch_paused_client_shutdown', ?, ?)",
            (int(task_id), int(item_id), message, now),
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


def record_platform_progress(
    task_id: int,
    platform_type: int,
    *,
    message: str,
    content_type: str | None = None,
    event_type: str,
    receipt: dict | None = None,
) -> None:
    """保存不代表成功的平台中间回执，防止已上传视频被重复提交。"""

    if int(platform_type) != 7:
        raise ValueError("当前仅 YouTube 官方通道支持中间回执")
    safe_receipt = _youtube_receipt_projection(receipt)
    video_id = str(safe_receipt.get("videoId") or "")
    if not video_id:
        raise ValueError("YouTube 中间回执缺少精确视频 ID")
    receipt_json = json.dumps(
        safe_receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    watch_url = str(safe_receipt.get("watchUrl") or "")
    now = _now()
    content_clause = ""
    params: list[object] = [
        str(message),
        now,
        receipt_json,
        video_id,
        watch_url,
        watch_url,
        int(task_id),
        int(platform_type),
    ]
    if content_type:
        content_clause = " AND contentType = ?"
        params.append(str(content_type))
    with connect() as conn:
        updated = conn.execute(
            f"""
            UPDATE publish_task_items
            SET status = CASE WHEN status = 'pending' THEN 'running' ELSE status END,
                message = ?, startedAt = COALESCE(startedAt, ?),
                receiptJson = ?, platformPostId = ?,
                postUrl = CASE WHEN ? <> '' THEN ? ELSE postUrl END
            WHERE taskId = ? AND platformType = ?
              AND status IN ('pending', 'running'){content_clause}
            """,
            tuple(params),
        )
        if updated.rowcount < 1:
            raise ValueError("YouTube 任务条目不存在或已结束")
        conn.execute(
            """
            UPDATE publish_tasks
            SET status = 'running', workerPid = ?, workerHeartbeatAt = ?
            WHERE id = ? AND status IN ('pending', 'running')
            """,
            (os.getpid(), now, int(task_id)),
        )
        conn.execute(
            """
            INSERT INTO publish_task_events
                (taskId, level, eventType, message, createdAt)
            VALUES (?, 'info', ?, ?, ?)
            """,
            (int(task_id), str(event_type), str(message), now),
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
    readback: dict | None = None,
    error_code: str = "",
    receipt: dict | None = None,
) -> None:
    """按平台与内容类型回填结果；事件类型必须准确表达预检或正式提交。"""

    if int(platform_type) == 9:
        with connect() as conn:
            task_mode = conn.execute(
                "SELECT mode FROM publish_tasks WHERE id = ?",
                (int(task_id),),
            ).fetchone()
        if task_mode is not None and str(task_mode["mode"] or "") == "oneclick_publish":
            mark_facebook_result(
                int(task_id),
                ok=bool(ok),
                message=message,
                error_code=error_code,
                receipt=receipt if receipt is not None else (readback or {}),
                event_type=event_type,
            )
            return

    now = _now()
    status = "success" if ok else "failed"
    public_readback = _batch_readback_projection(readback)
    public_receipt = (
        _youtube_receipt_projection(receipt if receipt is not None else readback)
        if int(platform_type) == 7
        else project_facebook_page_receipt(
            receipt if receipt is not None else (readback or {})
        )
        if int(platform_type) == 9
        else _tiktok_receipt_projection(
            receipt if receipt is not None else readback
        )
        if int(platform_type) == 6
        else public_readback
    )
    receipt_json = (
        json.dumps(
            public_receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if public_receipt
        else ""
    )
    stable_error_code = "" if ok else _stable_error_code(error_code)
    receipt_values = {
        key: str(public_readback.get(key) or "")
        for key in ("platformPostId", "postUrl", "publishedAt")
    }
    if int(platform_type) == 7:
        receipt_values["platformPostId"] = str(public_receipt.get("videoId") or "")
        receipt_values["postUrl"] = str(public_receipt.get("watchUrl") or "")
    elif int(platform_type) == 6:
        receipt_values["platformPostId"] = str(
            public_receipt.get("contentId") or ""
        )
        receipt_values["postUrl"] = str(public_receipt.get("contentUrl") or "")
        receipt_values["publishedAt"] = str(
            public_receipt.get("publishedAt") or ""
        )
    elif int(platform_type) == 9:
        receipt_values["platformPostId"] = str(
            public_receipt.get("reelId") or ""
        )
        receipt_values["postUrl"] = str(public_receipt.get("url") or "")
        receipt_values["publishedAt"] = str(
            public_receipt.get("publishedAt") or ""
        )
    clear_tiktok_scheduled_published_at = bool(
        int(platform_type) == 6
        and ok
        and public_receipt.get("scheduleMode") == "platform_native"
        and "publishedAt" in public_receipt
        and public_receipt.get("publishedAt") is None
    )
    keep_identifiers = bool(ok or int(platform_type) in {6, 7, 9})
    with connect() as conn:
        batch_item = conn.execute(
            """
            SELECT task.payloadJson,
                   EXISTS(
                       SELECT 1
                       FROM publish_task_items AS item
                       WHERE item.taskId = task.id AND item.batchItemIndex IS NOT NULL
                   ) AS hasBatchItems
            FROM publish_tasks AS task
            WHERE task.id = ?
            LIMIT 1
            """,
            (int(task_id),),
        ).fetchone()
        if batch_item and (
            bool(batch_item["hasBatchItems"])
            or workflow_from_payload_json(batch_item["payloadJson"])
            == "douyin-commerce-batch"
        ):
            raise ValueError("抖音带货批量任务必须由批量执行器逐视频回填")
        if content_type:
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = ?, message = ?, attempts = attempts + 1,
                    startedAt = COALESCE(startedAt, ?), finishedAt = ?,
                    errorCode = ?,
                    receiptJson = CASE WHEN ? <> '' THEN ? ELSE receiptJson END,
                    platformPostId = CASE WHEN ? THEN ? ELSE platformPostId END,
                    postUrl = CASE WHEN ? THEN ? ELSE postUrl END,
                    publishedAt = CASE WHEN ? THEN ? ELSE publishedAt END
                WHERE taskId = ? AND platformType = ? AND contentType = ?
                """,
                (
                    status, message, now, now,
                    stable_error_code, receipt_json, receipt_json,
                    bool(keep_identifiers and receipt_values["platformPostId"]), receipt_values["platformPostId"],
                    bool(keep_identifiers and receipt_values["postUrl"]), receipt_values["postUrl"],
                    bool(
                        ok
                        and (
                            receipt_values["publishedAt"]
                            or clear_tiktok_scheduled_published_at
                        )
                    ), receipt_values["publishedAt"],
                    int(task_id), int(platform_type), str(content_type),
                ),
            )
        else:
            # 兼容旧调用与历史任务。新预检调用都会传入 content_type。
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = ?, message = ?, attempts = attempts + 1,
                    startedAt = COALESCE(startedAt, ?), finishedAt = ?,
                    errorCode = ?,
                    receiptJson = CASE WHEN ? <> '' THEN ? ELSE receiptJson END,
                    platformPostId = CASE WHEN ? THEN ? ELSE platformPostId END,
                    postUrl = CASE WHEN ? THEN ? ELSE postUrl END,
                    publishedAt = CASE WHEN ? THEN ? ELSE publishedAt END
                WHERE taskId = ? AND platformType = ?
                """,
                (
                    status, message, now, now,
                    stable_error_code, receipt_json, receipt_json,
                    bool(keep_identifiers and receipt_values["platformPostId"]), receipt_values["platformPostId"],
                    bool(keep_identifiers and receipt_values["postUrl"]), receipt_values["postUrl"],
                    bool(
                        ok
                        and (
                            receipt_values["publishedAt"]
                            or clear_tiktok_scheduled_published_at
                        )
                    ), receipt_values["publishedAt"],
                    int(task_id), int(platform_type),
                ),
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


def _write_batch_progress_or_failure(
    task_id: int,
    item_id: int,
    *,
    ok: bool,
    message: str,
    event_type: str,
    readback: dict | None,
) -> None:
    """写入公开进展或失败事件；该路径没有成功状态能力。"""

    now = _now()
    safe_readback = _batch_readback_projection(readback)
    requested_status = "failed" if not ok else "running"
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


def mark_batch_item_result(
    task_id: int,
    item_id: int,
    *,
    ok: bool,
    message: str,
    event_type: str,
    readback: dict | None,
) -> None:
    """公开事件入口只可记录进展或失败，永远不能把条目置为成功。"""

    _write_batch_progress_or_failure(
        task_id, item_id, ok=ok, message=message, event_type=event_type,
        readback=readback,
    )


def record_douyin_sms_cooldown(
    task_id: int, item_id: int, *, triggered_at_utc: datetime
) -> None:
    """仅保存冷却计时所需的非敏感审计信息。"""

    triggered_at_utc = _require_utc_datetime(triggered_at_utc)
    mark_batch_item_result(
        task_id,
        item_id,
        ok=True,
        event_type="douyin_sms_cooldown_started",
        message="本条已实际触发短信验证，后续最终提交遵守 60 秒冷却",
        readback={
            "cooldownStartedAt": triggered_at_utc.astimezone(ZoneInfo("UTC")).isoformat(),
            "cooldownSeconds": "60",
        },
    )
