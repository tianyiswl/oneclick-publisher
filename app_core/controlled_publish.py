# -*- coding: utf-8 -*-
"""本机受控发布请求、一次性授权和稳定任务投影。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import account_service, content_bundle, oneclick_capabilities
from .silicon_evolution_auto_publish import (
    AutoPublishProfile,
    SiliconEvolutionAutoPublishError,
    build_silicon_evolution_payload,
    require_matching_successful_preflight,
)
from .silicon_evolution_publish_package import (
    FrozenWechatPublishPackageError,
    load_frozen_wechat_publish_package,
)


_PLATFORM_TYPE_BY_NAME = {
    oneclick_capabilities.canonical_platform(name): platform_type
    for platform_type, name in account_service.PLATFORMS.items()
}
_REQUEST_KEYS = {
    "manifestPath",
    "mode",
    "targets",
    "confirmedPreflightTaskId",
    "authorizationId",
}
_TARGET_KEYS = {"platform", "accountId", "schedule"}


class ControlledPublishError(ValueError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(message)


def _require_mapping(value: object, error_code: str, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlledPublishError(error_code, message)
    return value


def _schedule(value: object) -> tuple[bool, str, str]:
    if value in (None, {}):
        return False, "", "Asia/Shanghai"
    data = _require_mapping(value, "controlled_schedule_invalid", "平台发布时间必须是对象或 null")
    if set(data) - {"localTime", "timezone"}:
        raise ControlledPublishError("controlled_schedule_invalid", "平台发布时间包含不支持字段")
    local_time = str(data.get("localTime") or "").strip()
    timezone_name = str(data.get("timezone") or "").strip()
    try:
        parsed = datetime.strptime(local_time, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ControlledPublishError(
            "controlled_schedule_invalid", "平台发布时间必须为 YYYY-MM-DD HH:mm"
        ) from exc
    if parsed.strftime("%Y-%m-%d %H:%M") != local_time or timezone_name != "Asia/Shanghai":
        raise ControlledPublishError(
            "controlled_schedule_invalid", "当前受控发布只接受 Asia/Shanghai 的完整本地时间"
        )
    return True, local_time, timezone_name


def _preferred_cover(platform_type: int, covers: Mapping[str, str]) -> str:
    ratios = ("3:4", "4:3") if platform_type in {1, 3, 6, 8, 9} else ("4:3", "3:4")
    return next((str(covers[ratio]) for ratio in ratios if covers.get(ratio)), "")


def build_controlled_payloads(
    request: Mapping[str, Any],
    *,
    accounts: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把 manifest 与明确账号转换为 UI/CLI 共用发布服务载荷。"""

    request = _require_mapping(
        request, "controlled_request_invalid", "受控发布请求必须是 JSON 对象"
    )
    unexpected = set(request) - _REQUEST_KEYS
    if unexpected:
        raise ControlledPublishError(
            "controlled_request_invalid", "受控发布请求包含不支持字段"
        )
    mode = str(request.get("mode") or "preflight").strip().lower()
    if mode not in {"preflight", "formal"}:
        raise ControlledPublishError(
            "controlled_mode_invalid", "执行模式只能是 preflight 或 formal"
        )
    if mode == "formal":
        if type(request.get("confirmedPreflightTaskId")) is not int or not str(
            request.get("authorizationId") or ""
        ).strip():
            raise ControlledPublishError(
                "controlled_authorization_required",
                "正式发布必须携带已完成预检和一次性本地授权",
            )

    manifest_path = str(request.get("manifestPath") or "").strip()
    if not manifest_path:
        raise ControlledPublishError("controlled_manifest_required", "必须提供 manifestPath")
    try:
        bundle = content_bundle.load_content_bundle(manifest_path)
    except content_bundle.ContentBundleError as exc:
        raise ControlledPublishError("controlled_manifest_invalid", str(exc)) from exc

    targets = request.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ControlledPublishError("controlled_targets_required", "至少需要一个明确平台账号")
    account_rows = [dict(row) for row in (accounts if accounts is not None else account_service.list_accounts())]
    by_id = {int(row.get("id") or 0): row for row in account_rows if int(row.get("id") or 0) > 0}
    preferred = {
        oneclick_capabilities.canonical_platform(name)
        for name in bundle["preferredPlatforms"]
    }
    override_by_platform = {
        oneclick_capabilities.canonical_platform(name): dict(value)
        for name, value in bundle["platformOverrides"].items()
    }

    payloads: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for raw_target in targets:
        target = _require_mapping(
            raw_target, "controlled_target_invalid", "每个目标必须是 JSON 对象"
        )
        if set(target) - _TARGET_KEYS:
            raise ControlledPublishError("controlled_target_invalid", "平台目标包含不支持字段")
        platform = oneclick_capabilities.canonical_platform(str(target.get("platform") or ""))
        platform_type = _PLATFORM_TYPE_BY_NAME.get(platform)
        account_id = target.get("accountId")
        if platform_type is None or type(account_id) is not int or account_id <= 0:
            raise ControlledPublishError("controlled_target_invalid", "平台和 accountId 必须明确有效")
        if (platform_type, account_id) in seen:
            raise ControlledPublishError("controlled_target_duplicate", "同一平台账号不能重复")
        seen.add((platform_type, account_id))
        account = by_id.get(account_id)
        if not account or int(account.get("type") or 0) != platform_type:
            raise ControlledPublishError(
                "controlled_account_mismatch", f"账号 {account_id} 不属于 {platform}"
            )
        if preferred and platform not in preferred:
            raise ControlledPublishError(
                "controlled_platform_not_in_bundle", f"内容包没有声明目标平台：{platform}"
            )
        override = override_by_platform.get(platform, {})
        title = str(override.get("title") or bundle.get("commonTitle") or bundle["title"]).strip()
        description = str(override.get("body") or bundle.get("commonBody") or "").strip()
        tags = list(override.get("tags") or bundle.get("commonTags") or [])
        if not title or not description:
            raise ControlledPublishError(
                "controlled_platform_fields_missing", f"{platform}缺少独立标题或正文"
            )
        if platform_type == 3 and re.search(r"(?:^|\s)@[^\s@#]+", description):
            raise ControlledPublishError(
                "controlled_mentions_unsupported",
                "抖音正文包含原始 @文字；当前内容包没有独立 mentions 字段和官方候选回读，不能冒充有效提及",
            )
        enable_timer, schedule_time, schedule_timezone = _schedule(target.get("schedule"))
        covers = dict(bundle["coverPaths"])
        cover_path = _preferred_cover(platform_type, covers)
        display_name = str(account.get("profileName") or account.get("userName") or "")
        disclosure = dict(bundle.get("aiDisclosure") or {})
        payload: dict[str, Any] = {
            "contentType": bundle["contentType"],
            "type": platform_type,
            "title": title,
            "description": description,
            "tags": tags,
            "fileList": list(bundle["assetPaths"]),
            "accountList": [str(account.get("filePath") or "")],
            "accountIds": [account_id],
            "accountDisplayNames": [display_name],
            "coverPath": cover_path,
            "coverPaths": covers,
            "runtimeMode": "publish" if mode == "formal" else "preflight",
            "debugDryRun": mode == "preflight",
            "saveDraftOnly": False,
            "debugDryRunHoldBrowser": False,
            "backgroundMode": mode == "preflight",
            "originalDeclaration": bool(bundle.get("originalDeclaration")),
            "aiGenerated": bool(disclosure.get("containsAiGeneratedContent")),
            "aiDeclarationExplicitlyConfirmed": bool(
                mode == "formal" and disclosure.get("containsAiGeneratedContent")
            ),
            "visibility": "public",
            "collectionName": "",
            "enableTimer": enable_timer,
            "scheduleTime": schedule_time or None,
            "scheduleTimezone": schedule_timezone,
            "videosPerDay": 1,
            "dailyTimes": [schedule_time[-5:]] if schedule_time else [],
            "startDays": 0,
            "timeJitterMinutes": 0,
            "controlledManifestPath": str(Path(manifest_path).expanduser().resolve()),
        }
        if platform_type == 1:
            payload.update(
                {
                    "aiDisclosure": disclosure,
                    "xhsLocationKeyword": "",
                    "xhsLocationScope": "",
                    "xhsLocationPoi": None,
                }
            )
        elif platform_type == 3:
            payload.update(
                {"locationKeyword": "", "locationScope": "", "locationPoi": {}}
            )
        payloads.append(payload)
    return payloads


def _file_identity(value: object) -> str:
    path = Path(str(value or ""))
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return str(value or "")


def scope_fingerprint(payloads: Iterable[Mapping[str, Any]]) -> str:
    """只对发布对象和内容做指纹；不把 Cookie 或会话文件写入授权。"""

    normalized = []
    for payload in payloads:
        normalized.append(
            {
                "type": int(payload.get("type") or 0),
                "accountIds": [int(item) for item in payload.get("accountIds") or []],
                "contentType": str(payload.get("contentType") or ""),
                "title": str(payload.get("title") or ""),
                "description": str(payload.get("description") or ""),
                "tags": [str(item) for item in payload.get("tags") or []],
                "assets": [_file_identity(item) for item in payload.get("fileList") or []],
                "cover": _file_identity(payload.get("coverPath")),
                "scheduleTime": str(payload.get("scheduleTime") or ""),
                "scheduleTimezone": str(payload.get("scheduleTimezone") or ""),
                "originalDeclaration": bool(payload.get("originalDeclaration")),
                "aiGenerated": bool(payload.get("aiGenerated")),
                "aiDisclosure": payload.get("aiDisclosure") or {},
                "visibility": str(payload.get("visibility") or ""),
            }
        )
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def create_authorization_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS controlled_publish_authorizations (
            authorizationId TEXT PRIMARY KEY,
            preflightTaskId INTEGER NOT NULL,
            scopeFingerprint TEXT NOT NULL,
            createdAt TEXT NOT NULL,
            expiresAt TEXT NOT NULL,
            consumedAt TEXT
        )
        """
    )
    conn.commit()


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ControlledPublishError("controlled_authorization_clock_invalid", "授权时钟必须包含时区")
    return current.astimezone(timezone.utc)


def create_authorization(
    conn: sqlite3.Connection,
    preflight_task_id: int,
    payloads: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    ttl_seconds: int = 600,
) -> dict[str, Any]:
    if type(preflight_task_id) is not int or preflight_task_id <= 0:
        raise ControlledPublishError("controlled_preflight_required", "预检 taskId 无效")
    if not 1 <= int(ttl_seconds) <= 900:
        raise ControlledPublishError("controlled_authorization_ttl_invalid", "授权有效期不正确")
    create_authorization_schema(conn)
    created = _utc(now)
    expires = created + timedelta(seconds=int(ttl_seconds))
    authorization_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO controlled_publish_authorizations
            (authorizationId, preflightTaskId, scopeFingerprint, createdAt, expiresAt, consumedAt)
        VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (
            authorization_id,
            preflight_task_id,
            scope_fingerprint(payloads),
            created.isoformat(),
            expires.isoformat(),
        ),
    )
    conn.commit()
    return {
        "authorizationId": authorization_id,
        "preflightTaskId": preflight_task_id,
        "expiresAt": expires.isoformat(),
        "singleUse": True,
    }


def consume_authorization(
    conn: sqlite3.Connection,
    authorization_id: str,
    preflight_task_id: int,
    payloads: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> None:
    create_authorization_schema(conn)
    current = _utc(now)
    row = conn.execute(
        "SELECT * FROM controlled_publish_authorizations WHERE authorizationId = ?",
        (str(authorization_id or "").strip(),),
    ).fetchone()
    if row is None:
        raise ControlledPublishError("controlled_authorization_invalid", "一次性授权不存在")
    data = dict(row)
    if data.get("consumedAt"):
        raise ControlledPublishError("controlled_authorization_consumed", "一次性授权已经使用")
    if int(data.get("preflightTaskId") or 0) != int(preflight_task_id):
        raise ControlledPublishError("controlled_authorization_scope_mismatch", "授权不属于本次预检")
    expires = datetime.fromisoformat(str(data["expiresAt"])).astimezone(timezone.utc)
    if current >= expires:
        raise ControlledPublishError("controlled_authorization_expired", "一次性授权已经过期")
    if str(data.get("scopeFingerprint") or "") != scope_fingerprint(payloads):
        raise ControlledPublishError("controlled_authorization_scope_mismatch", "正式发布内容与预检不一致")
    cursor = conn.execute(
        """
        UPDATE controlled_publish_authorizations
        SET consumedAt = ?
        WHERE authorizationId = ? AND consumedAt IS NULL
        """,
        (current.isoformat(), str(authorization_id).strip()),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise ControlledPublishError("controlled_authorization_consumed", "一次性授权已经使用")
    conn.commit()


_ERROR_CODE_RE = re.compile(
    r"(?:错误码|error(?:Code)?)\s*[:：=]?\s*([a-z][a-z0-9_]{2,})",
    re.IGNORECASE,
)
_LEGACY_ERROR_CODES = (
    ("没有返回任何可用的话题候选", "douyin_topic_candidates_unavailable"),
    ("等待小红书平台成功回执超时", "xhs_receipt_timeout"),
)


def _projected_error_code(message: str) -> str:
    match = _ERROR_CODE_RE.search(message)
    if match:
        return match.group(1)
    return next(
        (code for marker, code in _LEGACY_ERROR_CODES if marker in message),
        "",
    )


def project_task(task: Mapping[str, Any] | None) -> dict[str, Any]:
    """输出给 Codex/本地 API 的稳定、无凭据任务 JSON。"""

    if not isinstance(task, Mapping):
        raise ControlledPublishError("controlled_task_not_found", "任务不存在")
    mode = str(task.get("mode") or "")
    phase = "preflight" if "preflight" in mode else "formal" if "publish" in mode else "unknown"
    try:
        raw_payloads = json.loads(str(task.get("payloadJson") or "[]"))
    except json.JSONDecodeError:
        raw_payloads = []
    payloads_by_type: dict[int, list[dict[str, Any]]] = {}
    for raw_payload in raw_payloads if isinstance(raw_payloads, list) else []:
        if isinstance(raw_payload, dict):
            payloads_by_type.setdefault(int(raw_payload.get("type") or 0), []).append(raw_payload)
    platforms = []
    for item in task.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        message = " ".join(str(item.get("message") or "").split())
        status = str(item.get("status") or "pending")
        platform_type = int(item.get("platformType") or 0)
        related_payloads = payloads_by_type.get(platform_type, [])
        related_payload = related_payloads[0] if len(related_payloads) == 1 else {}
        account_id = 0
        account_files = list(related_payload.get("accountList") or [])
        account_ids = list(related_payload.get("accountIds") or [])
        item_account_file = str(item.get("accountFile") or "")
        if item_account_file in account_files:
            index = account_files.index(item_account_file)
            if index < len(account_ids):
                account_id = int(account_ids[index] or 0)
        elif len(account_ids) == 1:
            account_id = int(account_ids[0] or 0)
        receipt = None
        if phase == "formal" and status == "success":
            receipt = {
                key: item.get(key)
                for key in ("platformPostId", "postUrl", "publishedAt")
                if item.get(key) not in (None, "")
            } or {"message": message}
        platforms.append(
            {
                "platform": account_service.PLATFORMS.get(platform_type, f"平台{platform_type}"),
                "platformType": platform_type,
                "accountId": account_id,
                "account": str(
                    item.get("accountDisplayName")
                    or item.get("accountName")
                    or item.get("accountLabel")
                    or item.get("profileName")
                    or item.get("userName")
                    or ""
                ),
                "status": status,
                "errorCode": _projected_error_code(message),
                "errorText": message if status == "failed" else "",
                "receipt": receipt,
                "contentId": str(item.get("platformPostId") or ""),
                "scheduledAt": str(
                    item.get("scheduleTime")
                    or item.get("scheduleSummary")
                    or related_payload.get("scheduleTime")
                    or ""
                ),
            }
        )
    result = {
        "taskId": int(task.get("id") or 0),
        "taskNo": str(task.get("taskNo") or ""),
        "mode": mode,
        "phase": phase,
        "status": str(task.get("status") or "pending"),
        "occurredAt": str(
            task.get("finishedAt")
            or task.get("startedAt")
            or task.get("createdAt")
            or ""
        ),
        "platforms": platforms,
    }
    silicon_payloads = [
        item
        for rows in payloads_by_type.values()
        for item in rows
        if item.get("siliconEvolutionArticleId")
    ]
    if len(silicon_payloads) == 1:
        payload = silicon_payloads[0]
        account_ids = list(payload.get("accountIds") or [])
        result.update(
            {
                "articleId": str(payload.get("siliconEvolutionArticleId") or ""),
                "packageSha256": str(
                    payload.get("siliconEvolutionPackageSha256") or ""
                ),
                "accountId": int(account_ids[0] or 0)
                if len(account_ids) == 1
                else 0,
            }
        )
    return result


def task_status(task_id: int) -> dict[str, Any]:
    from . import task_service

    task_service.reconcile_stale_controlled_task(int(task_id))
    return project_task(task_service.get_task(int(task_id)))


def authorize_completed_preflight(
    task_id: int,
    *,
    ttl_seconds: int = 600,
) -> dict[str, Any]:
    """在用户于对话中确认后，为已成功预检创建一次性授权。"""

    from . import task_service
    from .database import connect

    task = task_service.get_task(int(task_id))
    if not task or str(task.get("mode") or "") != "oneclick_preflight":
        raise ControlledPublishError("controlled_preflight_required", "授权对象不是受控预检任务")
    if str(task.get("status") or "") != "success":
        raise ControlledPublishError(
            "controlled_preflight_not_successful", "只有全部平台预检成功才能授权正式发布"
        )
    try:
        payloads = json.loads(str(task.get("payloadJson") or "[]"))
    except json.JSONDecodeError as exc:
        raise ControlledPublishError("controlled_preflight_invalid", "预检任务快照不可读取") from exc
    if not isinstance(payloads, list) or not payloads:
        raise ControlledPublishError("controlled_preflight_invalid", "预检任务没有发布快照")
    with connect() as conn:
        return create_authorization(
            conn,
            int(task_id),
            [dict(item) for item in payloads if isinstance(item, dict)],
            ttl_seconds=ttl_seconds,
        )


def submit_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """创建预检或经一次性授权的正式任务。"""

    from . import publish_service, task_service
    from .database import connect

    payloads = build_controlled_payloads(request)
    mode = str(request.get("mode") or "preflight").strip().lower()
    if mode == "formal":
        preflight_id = int(request["confirmedPreflightTaskId"])
        preflight = task_service.get_task(preflight_id)
        if not preflight or str(preflight.get("mode") or "") != "oneclick_preflight":
            raise ControlledPublishError("controlled_preflight_required", "正式发布缺少对应预检任务")
        if str(preflight.get("status") or "") != "success":
            raise ControlledPublishError(
                "controlled_preflight_not_successful", "对应预检尚未全部成功"
            )
        with connect() as conn:
            consume_authorization(
                conn,
                str(request["authorizationId"]),
                preflight_id,
                payloads,
            )
    task = publish_service.start_desktop_publish(payloads)
    stored = task_service.get_task(int(task["id"])) or task
    return project_task(stored)


_SILICON_REQUEST_KEYS = {
    "projectId",
    "articleId",
    "packagePath",
    "packageSha256",
    "accountId",
    "mode",
    "confirmedPreflightTaskId",
}


def submit_silicon_evolution_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """从 V1.2 冻结包创建硅基进化公众号预检或自动正式任务。"""

    from . import publish_service, task_service
    from .database import connect

    data = _require_mapping(
        request,
        "silicon_evolution_request_invalid",
        "硅基进化自动直发请求必须是对象",
    )
    if set(data) - _SILICON_REQUEST_KEYS:
        raise ControlledPublishError(
            "silicon_evolution_request_invalid", "硅基进化自动直发包含不支持字段"
        )
    if str(data.get("projectId") or "") != "silicon-evolution":
        raise ControlledPublishError(
            "silicon_evolution_project_mismatch", "自动直发项目不是硅基进化"
        )
    mode = str(data.get("mode") or "")
    if mode not in {"preflight", "formal"}:
        raise ControlledPublishError(
            "silicon_evolution_mode_invalid", "自动直发模式只能是 preflight 或 formal"
        )
    account_id = data.get("accountId")
    if type(account_id) is not int or account_id <= 0:
        raise ControlledPublishError(
            "silicon_evolution_account_invalid", "自动直发公众号账号无效"
        )
    accounts = [
        dict(row)
        for row in account_service.list_accounts()
        if int(row.get("id") or 0) == account_id
    ]
    if len(accounts) != 1 or int(accounts[0].get("type") or 0) != 10:
        raise ControlledPublishError(
            "silicon_evolution_account_mismatch", "自动直发账号不是唯一公众号账号"
        )
    account = accounts[0]
    display_name = str(
        account.get("profileName") or account.get("userName") or ""
    ).strip()
    if display_name != "硅基进化":
        raise ControlledPublishError(
            "silicon_evolution_account_mismatch", "自动直发账号不是硅基进化"
        )
    try:
        package = load_frozen_wechat_publish_package(
            Path(str(data.get("packagePath") or "")),
            expected_sha256=str(data.get("packageSha256") or ""),
        )
        if package.article_id != str(data.get("articleId") or ""):
            raise SiliconEvolutionAutoPublishError("自动直发文章编号与冻结包不匹配")
        profile = AutoPublishProfile(
            project_id="silicon-evolution",
            account_id=account_id,
            account_display_name=display_name,
            enabled=True,
        )
        payload = build_silicon_evolution_payload(
            package,
            profile,
            account_id=account_id,
            mode="publish" if mode == "formal" else "preflight",
        )
        payload["accountList"] = [str(account.get("filePath") or "")]
    except (FrozenWechatPublishPackageError, SiliconEvolutionAutoPublishError) as exc:
        raise ControlledPublishError(
            "silicon_evolution_package_invalid", str(exc)
        ) from exc
    if mode == "formal":
        preflight_id = data.get("confirmedPreflightTaskId")
        if type(preflight_id) is not int or preflight_id <= 0:
            raise ControlledPublishError(
                "silicon_evolution_preflight_required", "自动直发缺少成功预检 taskId"
            )
        preflight = task_service.get_task(preflight_id)
        try:
            preflight_payloads = require_matching_successful_preflight(
                preflight,
                package,
                profile,
            )
        except SiliconEvolutionAutoPublishError as exc:
            raise ControlledPublishError(
                "silicon_evolution_preflight_mismatch", str(exc)
            ) from exc
        with connect() as conn:
            grant = create_authorization(conn, preflight_id, preflight_payloads)
            consume_authorization(
                conn,
                str(grant["authorizationId"]),
                preflight_id,
                [payload],
            )
    task = publish_service.start_desktop_publish([payload])
    stored = task_service.get_task(int(task["id"])) or task
    return project_task(stored)
