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
from .overseas_tiktok_identity import normalize_tiktok_handle


_PLATFORM_TYPE_BY_NAME = {
    oneclick_capabilities.canonical_platform(name): platform_type
    for platform_type, name in account_service.PLATFORMS.items()
}
_REQUEST_KEYS = {
    "projectId",
    "manifestPath",
    "mode",
    "targets",
    "confirmedPreflightTaskId",
    "authorizationId",
    "directAuthorizationId",
    "platformFormCheckConfirmed",
}
_TARGET_KEYS = {"platform", "accountId", "schedule", "settings"}
_PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}")
_YOUTUBE_VISIBILITIES = frozenset(
    {"private", "unlisted", "public", "scheduled_public"}
)


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


def _youtube_settings(value: object, *, mode: str) -> dict[str, object]:
    settings = (
        {}
        if value is None
        else _require_mapping(
            value,
            "youtube_settings_invalid",
            "YouTube 发布设置必须是对象",
        )
    )
    if set(settings) - {"visibility", "madeForKids", "notifySubscribers"}:
        raise ControlledPublishError(
            "youtube_settings_invalid", "YouTube 发布设置包含不支持字段"
        )
    visibility = str(settings.get("visibility") or "private").strip().lower()
    if visibility not in _YOUTUBE_VISIBILITIES:
        raise ControlledPublishError(
            "youtube_visibility_invalid", "YouTube 可见性无效"
        )
    audience = settings.get("madeForKids")
    if mode in {"formal", "direct"} and type(audience) is not bool:
        raise ControlledPublishError(
            "youtube_audience_required",
            "YouTube 正式发布必须明确选择是否面向儿童",
        )
    if audience is not None and type(audience) is not bool:
        raise ControlledPublishError(
            "youtube_audience_invalid", "YouTube 受众设置无效"
        )
    notify = settings.get("notifySubscribers", True)
    if type(notify) is not bool:
        raise ControlledPublishError(
            "youtube_notify_invalid", "YouTube 通知设置无效"
        )
    return {
        "visibility": visibility,
        "madeForKids": audience,
        "notifySubscribers": notify,
    }


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
    if mode not in {"preflight", "platform_form_check", "formal", "direct"}:
        raise ControlledPublishError(
            "controlled_mode_invalid",
            "执行模式只能是 preflight、platform_form_check、formal 或 direct",
        )
    if (
        mode == "platform_form_check"
        and request.get("platformFormCheckConfirmed") is not True
    ):
        raise ControlledPublishError(
            "tiktok_platform_form_check_confirmation_required",
            "TikTok 平台表单检查会上传并填写，必须显式确认",
        )
    if mode == "formal":
        if type(request.get("confirmedPreflightTaskId")) is not int or not str(
            request.get("authorizationId") or ""
        ).strip():
            raise ControlledPublishError(
                "controlled_authorization_required",
                "正式发布必须携带已完成预检和一次性本地授权",
            )
    if mode == "direct" and not str(
        request.get("directAuthorizationId") or ""
    ).strip():
        raise ControlledPublishError(
            "controlled_direct_authorization_required",
            "后台直发必须携带绑定当次内容的一次性授权",
        )

    project_id = str(request.get("projectId") or "").strip().lower()
    if project_id and not _PROJECT_ID_RE.fullmatch(project_id):
        raise ControlledPublishError(
            "content_project_id_invalid",
            "项目标识必须是 2-64 位小写英文、数字、点、下划线或连字符",
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
    tiktok_target_count = sum(
        1
        for item in targets
        if isinstance(item, Mapping)
        and oneclick_capabilities.canonical_platform(
            str(item.get("platform") or "")
        )
        == "TikTok"
    )
    if tiktok_target_count:
        if len(targets) != 1 or tiktok_target_count != 1:
            raise ControlledPublishError(
                "tiktok_target_invalid", "TikTok 首版一次只能选择一个账号"
            )
        if mode == "direct":
            raise ControlledPublishError(
                "tiktok_direct_mode_unsupported",
                "TikTok 首版不支持 direct；必须由本地预检后进入 formal",
            )
    elif mode == "platform_form_check":
        raise ControlledPublishError(
            "tiktok_target_invalid", "平台表单检查首版只支持单个 TikTok 账号"
        )
    account_rows = [
        dict(row)
        for row in (
            accounts
            if accounts is not None
            else account_service.list_publishable_accounts()
        )
    ]
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
    youtube_target_count = 0
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
        youtube_settings: dict[str, object] | None = None
        tiktok_expected_reference = ""
        tiktok_settings: dict[str, object] | None = None
        if platform_type == 6:
            if target.get("schedule") is not None:
                raise ControlledPublishError(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 首版只支持立即公开发布",
                )
            if (
                type(account.get("status")) is not int
                or account.get("status") != 1
                or str(account.get("authMode") or "") != "browser"
            ):
                raise ControlledPublishError(
                    "tiktok_account_invalid", "TikTok 账号未通过同一主体检测"
                )
            tiktok_expected_reference = normalize_tiktok_handle(
                account.get("accountReference")
            )
            if not tiktok_expected_reference:
                raise ControlledPublishError(
                    "tiktok_account_invalid", "TikTok 账号缺少稳定主体绑定"
                )
            if bundle.get("contentType") != "video" or len(bundle["assetPaths"]) != 1:
                raise ControlledPublishError(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 首版只支持单个本地视频",
                )
            raw_settings = target.get("settings")
            if not isinstance(raw_settings, Mapping) or set(raw_settings) != {
                "visibility"
            }:
                raise ControlledPublishError(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 首版必须明确选择公开发布",
                )
            if str(raw_settings.get("visibility") or "").strip().lower() != "public":
                raise ControlledPublishError(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 首版只支持立即公开发布",
                )
            tiktok_settings = {"visibility": "public"}
        if platform_type == 7:
            youtube_target_count += 1
            if youtube_target_count > 1:
                raise ControlledPublishError(
                    "youtube_target_invalid",
                    "一次 YouTube 任务只能选择一个官方 OAuth 账号",
                )
            if str(account.get("authMode") or "") != account_service.AUTH_MODE_YOUTUBE_OAUTH:
                raise ControlledPublishError(
                    "youtube_oauth_required",
                    "YouTube 发布必须使用官方 OAuth 账号",
                )
            if int(account.get("status") or 0) != 1:
                raise ControlledPublishError(
                    "youtube_account_invalid",
                    "YouTube OAuth 账号当前不是正常状态",
                )
            expected_channel_id = str(
                account.get("accountReference") or ""
            ).strip()
            if not expected_channel_id:
                raise ControlledPublishError(
                    "youtube_account_invalid",
                    "YouTube OAuth 账号缺少已确认频道身份",
                )
            if (
                mode in {"formal", "direct"}
                and int(account.get("oauthScopeVersion") or 1) < 2
            ):
                raise ControlledPublishError(
                    "youtube_oauth_scope_upgrade_required",
                    "YouTube 账号需要重新授权发布权限",
                )
            if bundle.get("contentType") != "video" or len(bundle["assetPaths"]) != 1:
                raise ControlledPublishError(
                    "youtube_content_invalid",
                    "一次 YouTube 任务必须只包含一个视频",
                )
            youtube_settings = _youtube_settings(target.get("settings"), mode=mode)
        override = override_by_platform.get(platform, {})
        title = str(override.get("title") or bundle.get("commonTitle") or bundle["title"]).strip()
        description = str(override.get("body") or bundle.get("commonBody") or "").strip()
        tags = list(override.get("tags") or bundle.get("commonTags") or [])
        if not title or not description:
            raise ControlledPublishError(
                "controlled_platform_fields_missing", f"{platform}缺少独立标题或正文"
            )
        if platform_type == 6 and ("@" in title or "@" in description):
            raise ControlledPublishError(
                "tiktok_unsupported_publish_setting",
                "TikTok 标题或正文包含原始 @ 文字，首版不支持提及",
            )
        if platform_type == 3 and re.search(r"(?:^|\s)@[^\s@#]+", description):
            raise ControlledPublishError(
                "controlled_mentions_unsupported",
                "抖音正文包含原始 @文字；当前内容包没有独立 mentions 字段和官方候选回读，不能冒充有效提及",
            )
        enable_timer, schedule_time, schedule_timezone = _schedule(target.get("schedule"))
        if platform_type == 6 and enable_timer:
            raise ControlledPublishError(
                "tiktok_unsupported_publish_setting",
                "TikTok 首版只支持立即公开发布",
            )
        if youtube_settings is not None:
            scheduled_public = youtube_settings["visibility"] == "scheduled_public"
            if enable_timer != scheduled_public:
                raise ControlledPublishError(
                    "youtube_schedule_invalid",
                    "YouTube 排期只适用于定时公开，定时公开也必须提供排期",
                )
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
            "runtimeMode": (
                "platform_form_check"
                if mode == "platform_form_check"
                else "publish"
                if mode in {"formal", "direct"}
                else "preflight"
            ),
            "debugDryRun": mode == "preflight",
            "saveDraftOnly": False,
            "debugDryRunHoldBrowser": False,
            "backgroundMode": mode in {"preflight", "direct"},
            "originalDeclaration": bool(bundle.get("originalDeclaration")),
            "aiGenerated": bool(disclosure.get("containsAiGeneratedContent")),
            "aiDeclarationExplicitlyConfirmed": bool(
                mode in {"formal", "direct"}
                and disclosure.get("containsAiGeneratedContent")
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
        if project_id:
            payload["contentProjectId"] = project_id
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
        elif platform_type == 6 and tiktok_settings is not None:
            payload.update(tiktok_settings)
            payload.update(
                {
                    "coverPath": "",
                    "coverPaths": {},
                    "backgroundMode": False,
                    "overseasVideoPublishConfirmed": mode == "formal",
                    "tiktokControlledPublish": True,
                    "tiktokExpectedAccountReference": tiktok_expected_reference,
                    "tiktokExecutionIntent": (
                        "platform_form_check"
                        if mode == "platform_form_check"
                        else "formal_public"
                    ),
                }
            )
        elif platform_type == 7 and youtube_settings is not None:
            payload.update(youtube_settings)
            payload.update(
                {
                    "youtubeOfficialApi": True,
                    "youtubeExpectedChannelId": expected_channel_id,
                    "backgroundMode": True,
                }
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

    payload_rows = [dict(payload) for payload in payloads]
    if (
        len(payload_rows) == 1
        and str(payload_rows[0].get("workflow") or "") == "douyin-graphic-matrix"
    ):
        from .douyin_graphic_matrix_service import matrix_scope_fingerprint

        return matrix_scope_fingerprint(payload_rows[0])
    normalized = []
    for payload in payload_rows:
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
                "madeForKids": payload.get("madeForKids"),
                "notifySubscribers": payload.get("notifySubscribers"),
                "youtubeExpectedChannelId": str(
                    payload.get("youtubeExpectedChannelId") or ""
                ),
                "youtubeOfficialApi": bool(payload.get("youtubeOfficialApi")),
                "tiktokExpectedAccountReference": str(
                    payload.get("tiktokExpectedAccountReference") or ""
                ),
                "tiktokExecutionIntent": str(
                    payload.get("tiktokExecutionIntent") or ""
                ),
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


def create_direct_authorization_schema(conn: sqlite3.Connection) -> None:
    """创建对话直发的一次性授权表。

    表内只保存不可逆的发布范围指纹和时间，不保存正文、
    Cookie、验证码或其他会话数据。
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS controlled_direct_publish_authorizations (
            authorizationId TEXT PRIMARY KEY,
            scopeFingerprint TEXT NOT NULL,
            source TEXT NOT NULL,
            createdAt TEXT NOT NULL,
            expiresAt TEXT NOT NULL,
            consumedAt TEXT
        )
        """
    )
    conn.commit()


def create_direct_authorization(
    conn: sqlite3.Connection,
    payloads: Iterable[Mapping[str, Any]],
    *,
    source: str,
    now: datetime | None = None,
    ttl_seconds: int = 60,
) -> dict[str, Any]:
    """为当次对话中已明确确认的发布对象创建短期授权。"""

    normalized_source = str(source or "").strip()
    if normalized_source not in {"content-project-gateway", "desktop-client"}:
        raise ControlledPublishError(
            "controlled_direct_authorization_source_invalid",
            "后台直发授权来源无效",
        )
    if not 1 <= int(ttl_seconds) <= 300:
        raise ControlledPublishError(
            "controlled_direct_authorization_ttl_invalid",
            "后台直发授权有效期不正确",
        )
    rows = [dict(item) for item in payloads]
    if not rows:
        raise ControlledPublishError(
            "controlled_direct_authorization_scope_invalid",
            "后台直发授权没有可绑定的内容",
        )
    create_direct_authorization_schema(conn)
    created = _utc(now)
    expires = created + timedelta(seconds=int(ttl_seconds))
    authorization_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO controlled_direct_publish_authorizations
            (authorizationId, scopeFingerprint, source, createdAt, expiresAt, consumedAt)
        VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (
            authorization_id,
            scope_fingerprint(rows),
            normalized_source,
            created.isoformat(),
            expires.isoformat(),
        ),
    )
    conn.commit()
    return {
        "authorizationId": authorization_id,
        "expiresAt": expires.isoformat(),
        "singleUse": True,
        "source": normalized_source,
    }


def consume_direct_authorization(
    conn: sqlite3.Connection,
    authorization_id: str,
    payloads: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> None:
    """原子消费与内容、账号和排期精确绑定的直发授权。"""

    create_direct_authorization_schema(conn)
    current = _utc(now)
    normalized_id = str(authorization_id or "").strip()
    row = conn.execute(
        """
        SELECT * FROM controlled_direct_publish_authorizations
        WHERE authorizationId = ?
        """,
        (normalized_id,),
    ).fetchone()
    if row is None:
        raise ControlledPublishError(
            "controlled_direct_authorization_invalid",
            "后台直发一次性授权不存在",
        )
    data = dict(row)
    if data.get("consumedAt"):
        raise ControlledPublishError(
            "controlled_direct_authorization_consumed",
            "后台直发一次性授权已经使用",
        )
    expires = datetime.fromisoformat(str(data["expiresAt"])).astimezone(
        timezone.utc
    )
    if current >= expires:
        raise ControlledPublishError(
            "controlled_direct_authorization_expired",
            "后台直发一次性授权已经过期",
        )
    if str(data.get("scopeFingerprint") or "") != scope_fingerprint(payloads):
        raise ControlledPublishError(
            "controlled_direct_authorization_scope_mismatch",
            "后台直发的内容、账号或排期已经变化",
        )
    cursor = conn.execute(
        """
        UPDATE controlled_direct_publish_authorizations
        SET consumedAt = ?
        WHERE authorizationId = ? AND consumedAt IS NULL
        """,
        (current.isoformat(), normalized_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise ControlledPublishError(
            "controlled_direct_authorization_consumed",
            "后台直发一次性授权已经使用",
        )
    conn.commit()


def authorize_direct_request(
    request: Mapping[str, Any],
    *,
    source: str = "content-project-gateway",
    ttl_seconds: int = 60,
) -> dict[str, Any]:
    """为已获得当次对话明确授权的请求签发一次性凭证。"""

    from .database import connect

    data = dict(
        _require_mapping(
            request,
            "controlled_request_invalid",
            "受控发布请求必须是 JSON 对象",
        )
    )
    data["mode"] = "direct"
    # 只用于生成被授权的精确载荷；真实 ID 在返回后才由
    # 网关加入请求，外部调用者不能自行伪造一个可消费的记录。
    data["directAuthorizationId"] = "pending-local-authorization"
    payloads = build_controlled_payloads(data)
    with connect() as conn:
        return create_direct_authorization(
            conn,
            payloads,
            source=source,
            ttl_seconds=ttl_seconds,
        )


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
    matrix_mode = mode in {
        "oneclick_matrix_local_check",
        "oneclick_matrix_preflight",
        "oneclick_matrix_publish",
    }
    phase = (
        "local_check"
        if mode == "oneclick_matrix_local_check"
        else "platform_form_check"
        if mode == "oneclick_platform_form_check"
        else "preflight"
        if "preflight" in mode
        else "formal"
        if "publish" in mode
        else "unknown"
    )
    events = [
        dict(event)
        for event in task.get("events") or []
        if isinstance(event, Mapping)
    ]
    event_types = [str(event.get("eventType") or "") for event in events]

    def _latest_event_index(*types: str) -> int:
        accepted = set(types)
        return max(
            (index for index, value in enumerate(event_types) if value in accepted),
            default=-1,
        )

    task_status_value = str(task.get("status") or "pending")
    user_action: dict[str, Any] | None = None
    if "tiktok_publish_outcome_ambiguous" in event_types:
        stage = "ambiguous"
    elif task_status_value == "success":
        stage = "succeeded"
    elif task_status_value in {"failed", "partial_failed"}:
        stage = "failed"
    elif phase == "preflight":
        stage = "checking"
    elif phase == "local_check":
        stage = "local_check"
    else:
        tiktok_verification_required_index = _latest_event_index(
            "tiktok_waiting_user_verification"
        )
        tiktok_verification_resolved_index = _latest_event_index(
            "tiktok_user_verification_resolved"
        )
        verification_required_index = _latest_event_index(
            "wechat_verification_required",
            "wechat_publish_qr_required",
        )
        verification_succeeded_index = _latest_event_index(
            "wechat_verification_succeeded"
        )
        user_confirmation_index = _latest_event_index(
            "wechat_publish_user_action_required"
        )
        final_submit_index = _latest_event_index("wechat_final_submit_clicked")
        if tiktok_verification_required_index > tiktok_verification_resolved_index:
            stage = "waiting_verification"
            user_action = {
                "type": "tiktok_verification",
                "message": "TikTok 正在同一浏览器等待完成安全验证",
            }
        elif verification_required_index > verification_succeeded_index:
            stage = "waiting_verification"
            user_action = {
                "type": "wechat_qr",
                "message": "公众号发表需要微信验证，请在一键发客户端完成扫码",
            }
        elif user_confirmation_index >= 0:
            stage = "waiting_confirmation"
            user_action = {
                "type": "platform_confirmation",
                "message": "平台出现未知确认弹窗，请在一键发客户端处理",
            }
        elif final_submit_index >= 0:
            stage = "reconciling"
        else:
            stage = "preparing"
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
        account_id = int(item.get("accountId") or 0) if matrix_mode else 0
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
        if item.get("receiptJson"):
            try:
                loaded_receipt = json.loads(str(item.get("receiptJson") or ""))
                receipt = loaded_receipt if isinstance(loaded_receipt, dict) else None
            except json.JSONDecodeError:
                receipt = None
        elif phase == "formal" and status == "success":
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
                "errorCode": (
                    str(item.get("errorCode") or "")
                    or _projected_error_code(message)
                ),
                "errorText": message if status == "failed" else "",
                "receipt": receipt,
                "contentId": str(item.get("platformPostId") or ""),
                "scheduledAt": str(
                    (receipt or {}).get("scheduledAt")
                    or item.get("scheduleTime")
                    or item.get("scheduleSummary")
                    or related_payload.get("scheduleTime")
                    or ""
                ),
            }
        )
    tiktok_receipt_phases = {
        str((platform.get("receipt") or {}).get("phase") or "")
        for platform in platforms
        if int(platform.get("platformType") or 0) == 6
    }
    if (
        "tiktok_publish_outcome_ambiguous" in event_types
        or "ambiguous" in tiktok_receipt_phases
    ):
        stage = "ambiguous"
        user_action = None
    elif "tiktok_waiting_user_verification" in event_types and (
        _latest_event_index("tiktok_waiting_user_verification")
        > _latest_event_index("tiktok_user_verification_resolved")
    ):
        stage = "waiting_verification"
        user_action = {
            "type": "tiktok_verification",
            "message": "TikTok 正在同一浏览器等待完成安全验证",
        }
    elif "tiktok_published_readback_confirmed" in event_types or (
        "published_readback_confirmed" in tiktok_receipt_phases
    ):
        stage = "published_readback_confirmed"
    elif "tiktok_platform_accepted" in event_types or (
        "platform_accepted" in tiktok_receipt_phases
    ):
        stage = "platform_accepted"
    elif "tiktok_final_action_triggered" in event_types:
        stage = "reconciling"
    elif "tiktok_platform_form_verified" in event_types or (
        "platform_form_verified" in tiktok_receipt_phases
    ):
        stage = "platform_form_verified"
    elif "tiktok_local_preflight_passed" in event_types or (
        "local_preflight_passed" in tiktok_receipt_phases
    ):
        stage = "local_preflight_passed"
    result = {
        "taskId": int(task.get("id") or 0),
        "taskNo": str(task.get("taskNo") or ""),
        "mode": mode,
        "phase": phase,
        "status": task_status_value,
        "stage": stage,
        "userAction": user_action,
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


def _require_tiktok_local_preflight_task(
    task: Mapping[str, Any],
    payloads: Iterable[Mapping[str, Any]],
) -> None:
    rows = [dict(item) for item in payloads]
    tiktok_rows = [item for item in rows if int(item.get("type") or 0) == 6]
    if not tiktok_rows:
        return
    event_types = {
        str(event.get("eventType") or "")
        for event in task.get("events") or []
        if isinstance(event, Mapping)
    }
    valid_snapshot = (
        len(rows) == 1
        and len(tiktok_rows) == 1
        and str(tiktok_rows[0].get("runtimeMode") or "") == "preflight"
        and tiktok_rows[0].get("debugDryRun") is True
        and str(tiktok_rows[0].get("tiktokExecutionIntent") or "")
        == "formal_public"
        and bool(
            str(tiktok_rows[0].get("tiktokExpectedAccountReference") or "").strip()
        )
        and "tiktok_local_preflight_passed" in event_types
    )
    if not valid_snapshot:
        raise ControlledPublishError(
            "tiktok_local_preflight_required",
            "TikTok 正式发布必须绑定同一输入的成功本地预检",
        )


def authorize_completed_check(
    task_id: int,
    *,
    ttl_seconds: int = 600,
) -> dict[str, Any]:
    """为成功的平台预检或图文矩阵本地检查创建一次性授权。"""

    from . import task_service
    from .database import connect

    task = task_service.get_task(int(task_id))
    accepted_modes = {"oneclick_preflight", "oneclick_matrix_local_check"}
    if not task or str(task.get("mode") or "") not in accepted_modes:
        raise ControlledPublishError("controlled_preflight_required", "授权对象不是受控检查任务")
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
    normalized_payloads = [dict(item) for item in payloads if isinstance(item, dict)]
    _require_tiktok_local_preflight_task(task, normalized_payloads)
    with connect() as conn:
        return create_authorization(
            conn,
            int(task_id),
            normalized_payloads,
            ttl_seconds=ttl_seconds,
        )


def authorize_completed_preflight(
    task_id: int,
    *,
    ttl_seconds: int = 600,
) -> dict[str, Any]:
    """兼容旧调用名称；授权规则由 ``authorize_completed_check`` 统一处理。"""

    return authorize_completed_check(task_id, ttl_seconds=ttl_seconds)


def consume_matrix_authorization(
    authorization_id: str,
    checked_task_id: int,
    matrix: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    if str(matrix.get("workflow") or "") != "douyin-graphic-matrix":
        raise ControlledPublishError(
            "controlled_authorization_scope_mismatch", "授权内容不是抖音图文矩阵"
        )
    from .database import connect

    with connect() as conn:
        consume_authorization(
            conn,
            authorization_id,
            int(checked_task_id),
            [matrix],
            now=now,
        )


def _find_successful_formal_scope_task(
    tasks: Iterable[Mapping[str, Any]],
    payloads: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """查找同一账号、内容与排期已有的正式成功任务。"""

    expected = scope_fingerprint(payloads)
    for raw_task in tasks:
        if (
            str(raw_task.get("mode") or "") != "oneclick_publish"
            or str(raw_task.get("status") or "") != "success"
        ):
            continue
        try:
            stored = json.loads(str(raw_task.get("payloadJson") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(stored, list) or not all(
            isinstance(item, Mapping) for item in stored
        ):
            continue
        if scope_fingerprint(stored) == expected:
            return dict(raw_task)
    return None


def _find_blocking_formal_scope_task(
    tasks: Iterable[Mapping[str, Any]],
    payloads: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """查找同一发布范围已成功或已进入最终提交的任务。

    最终按钮点击后即使 CLI 回读失败，平台也可能已经接受内容。
    这种情况只能做只读核对，不得自动重发。
    """

    expected = scope_fingerprint(payloads)
    for raw_task in tasks:
        if str(raw_task.get("mode") or "") != "oneclick_publish":
            continue
        try:
            stored = json.loads(str(raw_task.get("payloadJson") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(stored, list) or not all(
            isinstance(item, Mapping) for item in stored
        ):
            continue
        if scope_fingerprint(stored) != expected:
            continue
        if str(raw_task.get("status") or "") == "success":
            return dict(raw_task)
        event_types = {
            str(event.get("eventType") or "")
            for event in raw_task.get("events") or []
            if isinstance(event, Mapping)
        }
        if event_types.intersection(
            {
                "wechat_final_submit_clicked",
                "tiktok_final_action_triggered",
                "tiktok_publish_outcome_ambiguous",
            }
        ):
            return dict(raw_task)
    return None


def submit_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """创建预检或经一次性授权的正式任务。"""

    from . import publish_service, task_service
    from .database import connect

    payloads = build_controlled_payloads(request)
    mode = str(request.get("mode") or "preflight").strip().lower()
    if mode in {"formal", "direct"}:
        recent = task_service.list_tasks(limit=500)
        detailed = [
            task_service.get_task(int(row.get("id") or 0)) or dict(row)
            for row in recent
            if int(row.get("id") or 0) > 0
        ]
        existing = _find_blocking_formal_scope_task(detailed, payloads)
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
    if mode == "formal":
        preflight_id = int(request["confirmedPreflightTaskId"])
        preflight = task_service.get_task(preflight_id)
        if not preflight or str(preflight.get("mode") or "") != "oneclick_preflight":
            raise ControlledPublishError("controlled_preflight_required", "正式发布缺少对应预检任务")
        if str(preflight.get("status") or "") != "success":
            raise ControlledPublishError(
                "controlled_preflight_not_successful", "对应预检尚未全部成功"
            )
        try:
            preflight_payloads = json.loads(
                str(preflight.get("payloadJson") or "[]")
            )
        except json.JSONDecodeError as exc:
            raise ControlledPublishError(
                "controlled_preflight_invalid", "预检任务快照不可读取"
            ) from exc
        if not isinstance(preflight_payloads, list):
            raise ControlledPublishError(
                "controlled_preflight_invalid", "预检任务快照不可读取"
            )
        _require_tiktok_local_preflight_task(
            preflight,
            [item for item in preflight_payloads if isinstance(item, Mapping)],
        )
        with connect() as conn:
            consume_authorization(
                conn,
                str(request["authorizationId"]),
                preflight_id,
                payloads,
            )
    elif mode == "direct":
        with connect() as conn:
            consume_direct_authorization(
                conn,
                str(request["directAuthorizationId"]),
                payloads,
            )
    task = publish_service.start_desktop_publish(payloads)
    stored = task_service.get_task(int(task["id"])) or task
    return project_task(stored)


def _find_successful_silicon_formal_task(
    tasks: Iterable[Mapping[str, Any]],
    *,
    article_id: str,
    package_sha256: str,
    account_id: int,
) -> dict[str, Any] | None:
    """查找同一冻结文章已有的正式成功回执，防止重复发表。"""

    for raw_task in tasks:
        if (
            str(raw_task.get("mode") or "") != "oneclick_publish"
            or str(raw_task.get("status") or "") != "success"
        ):
            continue
        try:
            payloads = json.loads(str(raw_task.get("payloadJson") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payloads, list):
            continue
        for payload in payloads:
            if not isinstance(payload, Mapping):
                continue
            if (
                int(payload.get("type") or 0) == 10
                and payload.get("accountIds") == [account_id]
                and str(payload.get("siliconEvolutionArticleId") or "")
                == article_id
                and str(payload.get("siliconEvolutionPackageSha256") or "")
                == package_sha256
            ):
                return dict(raw_task)
    return None


_SILICON_REQUEST_KEYS = {
    "projectId",
    "articleId",
    "packagePath",
    "packageSha256",
    "accountId",
    "mode",
    "confirmedPreflightTaskId",
    "directAuthorizationId",
}


def _prepare_silicon_evolution_request(
    data: Mapping[str, Any],
    *,
    mode: str,
) -> tuple[Any, AutoPublishProfile, dict[str, Any], int]:
    """校验冻结包和公众号主体，生成唯一执行器载荷。"""

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
            mode="direct" if mode == "direct" else "publish" if mode == "formal" else "preflight",
        )
        payload["accountList"] = [str(account.get("filePath") or "")]
        payload["contentProjectId"] = "silicon-evolution"
    except (FrozenWechatPublishPackageError, SiliconEvolutionAutoPublishError) as exc:
        raise ControlledPublishError(
            "silicon_evolution_package_invalid", str(exc)
        ) from exc
    return package, profile, payload, account_id


def authorize_silicon_evolution_direct_request(
    request: Mapping[str, Any],
    *,
    ttl_seconds: int = 60,
) -> dict[str, Any]:
    """为已确认的硅基进化冻结包直发请求签发一次性授权。"""

    from .database import connect

    data = dict(
        _require_mapping(
            request,
            "silicon_evolution_request_invalid",
            "硅基进化自动直发请求必须是对象",
        )
    )
    data["mode"] = "direct"
    if str(data.get("projectId") or "") != "silicon-evolution":
        raise ControlledPublishError(
            "silicon_evolution_project_mismatch", "自动直发项目不是硅基进化"
        )
    _, _, payload, _ = _prepare_silicon_evolution_request(data, mode="direct")
    with connect() as conn:
        return create_direct_authorization(
            conn,
            [payload],
            source="content-project-gateway",
            ttl_seconds=ttl_seconds,
        )


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
    if mode not in {"preflight", "formal", "direct"}:
        raise ControlledPublishError(
            "silicon_evolution_mode_invalid",
            "自动直发模式只能是 preflight、formal 或 direct",
        )
    package, profile, payload, account_id = _prepare_silicon_evolution_request(
        data,
        mode=mode,
    )
    if mode in {"formal", "direct"}:
        recent_tasks = task_service.list_tasks(limit=500)
        detailed_tasks = [
            task_service.get_task(int(row.get("id") or 0)) or dict(row)
            for row in recent_tasks
            if int(row.get("id") or 0) > 0
        ]
        blocking = _find_blocking_formal_scope_task(detailed_tasks, [payload])
        if blocking and str(blocking.get("status") or "") != "success":
            raise ControlledPublishError(
                "silicon_evolution_publish_outcome_ambiguous",
                (
                    "同一冻结文章已点击最终发表，结果需先做只读核对，"
                    "已阻止自动重发；"
                    f"taskId={int(blocking.get('id') or 0)}"
                ),
            )
        existing = _find_successful_silicon_formal_task(
            recent_tasks,
            article_id=package.article_id,
            package_sha256=package.package_sha256,
            account_id=account_id,
        )
        if existing:
            raise ControlledPublishError(
                "silicon_evolution_already_published",
                (
                    "同一冻结文章已有正式成功回执，已阻止重复发表；"
                    f"taskId={int(existing.get('id') or 0)}"
                ),
            )
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
    elif mode == "direct":
        authorization_id = str(data.get("directAuthorizationId") or "").strip()
        if not authorization_id:
            raise ControlledPublishError(
                "silicon_evolution_direct_authorization_required",
                "自动直发缺少绑定冻结包的一次性授权",
            )
        with connect() as conn:
            consume_direct_authorization(conn, authorization_id, [payload])
    task = publish_service.start_desktop_publish([payload])
    stored = task_service.get_task(int(task["id"])) or task
    return project_task(stored)
