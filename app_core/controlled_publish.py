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
from zoneinfo import ZoneInfo

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
from .overseas_meta_content import (
    build_facebook_page_caption,
    facebook_page_caption_sha256,
)
from .overseas_meta_errors import (
    FacebookPagePublishError,
    project_facebook_page_receipt,
)
from .overseas_meta_page_identity import facebook_page_v1_enabled
from .tiktok_schedule_contract import (
    TikTokScheduleContractError,
    TikTokScheduleIntent,
    parse_tiktok_schedule_fields,
    tiktok_irreversible_evidence_sql,
    validate_tiktok_schedule_window,
)


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
_TIKTOK_ROOT_SCHEDULE_ALIASES = {"scheduledAt", "publishAt"}
_TIKTOK_TARGET_SCHEDULE_ALIASES = {
    "scheduledAt",
    "publishAt",
    "enableTimer",
    "scheduleTime",
    "scheduleTimezone",
    "dailyTimes",
}
_PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}")
_YOUTUBE_VISIBILITIES = frozenset(
    {"private", "unlisted", "public", "scheduled_public"}
)
_SAFE_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_FACEBOOK_PAGE_CLAIM_TRANSITIONS = {
    "reserved": frozenset({"final_action_claimed", "safe_failed"}),
    "final_action_claimed": frozenset({"final_action_clicked", "ambiguous"}),
    "final_action_clicked": frozenset({"succeeded", "ambiguous"}),
    "ambiguous": frozenset({"succeeded", "confirmed_not_published"}),
    "safe_failed": frozenset(),
    "confirmed_not_published": frozenset(),
    "succeeded": frozenset(),
}
_FACEBOOK_PAGE_REPLAY_RELEASE_STATES = frozenset(
    {"safe_failed", "confirmed_not_published"}
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


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _tiktok_target_schedule(
    value: object,
    *,
    now: datetime,
) -> TikTokScheduleIntent:
    if value is None:
        enable_timer = False
        schedule_time = None
        schedule_timezone = "Asia/Shanghai"
        daily_times: object = []
    elif isinstance(value, Mapping) and set(value) == {"localTime", "timezone"}:
        enable_timer = True
        schedule_time = value["localTime"]
        schedule_timezone = value["timezone"]
        daily_times = (
            [schedule_time[-5:]] if type(schedule_time) is str else []
        )
    else:
        raise ControlledPublishError(
            "tiktok_schedule_invalid",
            "TikTok 排期字段、格式或时区无效",
        )
    try:
        intent = parse_tiktok_schedule_fields(
            enable_timer=enable_timer,
            schedule_time=schedule_time,
            schedule_timezone=schedule_timezone,
            daily_times=daily_times,
        )
        validate_tiktok_schedule_window(
            intent,
            now=now,
            minimum_lead=timedelta(minutes=30),
        )
    except TikTokScheduleContractError as exc:
        if exc.error_code == "tiktok_schedule_invalid":
            raise ControlledPublishError(
                "tiktok_schedule_invalid",
                "TikTok 排期字段、格式或时区无效",
            ) from exc
        raise ControlledPublishError(exc.error_code, exc.public_message) from exc
    return intent


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
    schedule_now: datetime | None = None,
) -> list[dict[str, Any]]:
    """把 manifest 与明确账号转换为 UI/CLI 共用发布服务载荷。"""

    request = _require_mapping(
        request, "controlled_request_invalid", "受控发布请求必须是 JSON 对象"
    )
    raw_targets = request.get("targets")
    facebook_target_count = (
        sum(
            1
            for item in raw_targets
            if isinstance(item, Mapping)
            and oneclick_capabilities.canonical_platform(
                str(item.get("platform") or "")
            )
            == "Facebook Reels"
        )
        if isinstance(raw_targets, list)
        else 0
    )
    if facebook_target_count:
        if not facebook_page_v1_enabled():
            raise ControlledPublishError(
                "facebook_page_feature_disabled",
                "Facebook Page 发布功能尚未开启。",
            )
        if len(raw_targets) != 1 or facebook_target_count != 1:
            raise ControlledPublishError(
                "facebook_unsupported_publish_setting",
                "Facebook Page 首版一次只支持一个 Page 和一个视频。",
            )
    has_tiktok_target = isinstance(raw_targets, list) and any(
        isinstance(item, Mapping)
        and oneclick_capabilities.canonical_platform(
            str(item.get("platform") or "")
        )
        == "TikTok"
        for item in raw_targets
    )
    if has_tiktok_target and set(request).intersection(
        _TIKTOK_ROOT_SCHEDULE_ALIASES
    ):
        raise ControlledPublishError(
            "tiktok_schedule_invalid",
            "TikTok 排期字段、格式或时区无效",
        )
    unexpected = set(request) - _REQUEST_KEYS
    if facebook_target_count:
        # Compatibility-only caller evidence is deliberately ignored.  The
        # formal path always re-reads the task item stored by the preflight.
        unexpected -= {"preflightReceiptHash", "preflightReceipt", "receipt"}
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
    if facebook_target_count and mode == "direct":
        raise ControlledPublishError(
            "facebook_preflight_required",
            "Facebook Page 首版必须先完成受控预检。",
        )
    if facebook_target_count and mode == "platform_form_check":
        raise ControlledPublishError(
            "facebook_unsupported_publish_setting",
            "Facebook Page 首版只支持 preflight 和 formal 受控流程。",
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
        if facebook_target_count:
            message = str(exc)
            if "视频包必须且只能包含一个视频素材" in message:
                raise ControlledPublishError(
                    "facebook_unsupported_publish_setting",
                    "Facebook Page 首版一次只支持一个本地视频。",
                ) from exc
            if "assets[" in message and "不存在" in message:
                raise ControlledPublishError(
                    "facebook_video_file_invalid",
                    "Facebook Page 视频素材无法安全读取。",
                ) from exc
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
    if facebook_target_count:
        disclosure = dict(bundle.get("aiDisclosure") or {})
        if (
            bundle.get("contentType") != "video"
            or len(bundle.get("assetPaths") or []) != 1
            or bool((bundle.get("publishSchedule") or {}).get("enabled"))
            or bool(disclosure.get("containsAiGeneratedContent"))
            or bool(disclosure.get("contentKinds"))
            or bool(disclosure.get("assetPaths"))
            or bool(disclosure.get("allowPlatformAutoDeclaration"))
        ):
            raise ControlledPublishError(
                "facebook_unsupported_publish_setting",
                "Facebook Page 首版只支持单视频、立即公开发布。",
            )

    payloads: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    youtube_target_count = 0
    for raw_target in targets:
        target = _require_mapping(
            raw_target, "controlled_target_invalid", "每个目标必须是 JSON 对象"
        )
        target_platform = oneclick_capabilities.canonical_platform(
            str(target.get("platform") or "")
        )
        target_unexpected = set(target) - _TARGET_KEYS
        if (
            target_platform == "TikTok"
            and target_unexpected.intersection(_TIKTOK_TARGET_SCHEDULE_ALIASES)
        ):
            raise ControlledPublishError(
                "tiktok_schedule_invalid",
                "TikTok 排期字段、格式或时区无效",
            )
        if target_unexpected and target_platform == "Facebook Reels":
            raise ControlledPublishError(
                "facebook_unsupported_publish_setting",
                "Facebook Page 发布设置包含首版不支持的字段。",
            )
        if target_unexpected:
            raise ControlledPublishError("controlled_target_invalid", "平台目标包含不支持字段")
        platform = target_platform
        platform_type = _PLATFORM_TYPE_BY_NAME.get(platform)
        account_id = target.get("accountId")
        if platform_type is None or type(account_id) is not int or account_id <= 0:
            raise ControlledPublishError("controlled_target_invalid", "平台和 accountId 必须明确有效")
        if (platform_type, account_id) in seen:
            raise ControlledPublishError("controlled_target_duplicate", "同一平台账号不能重复")
        seen.add((platform_type, account_id))
        account = by_id.get(account_id)
        if platform_type == 9 and (
            not account or int(account.get("type") or 0) != platform_type
        ):
            raise ControlledPublishError(
                "facebook_account_invalid",
                "Facebook Page 账号记录无效。",
            )
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
        tiktok_schedule_intent: TikTokScheduleIntent | None = None
        tiktok_settings: dict[str, object] | None = None
        tiktok_video_sha256 = ""
        facebook_expected_page_reference = ""
        facebook_video_sha256 = ""
        facebook_settings: dict[str, object] | None = None
        if platform_type == 9:
            if not str(account.get("filePath") or "").strip():
                raise ControlledPublishError(
                    "facebook_session_missing",
                    "Facebook Page 账号缺少可用的本地会话引用。",
                )
            try:
                facebook_expected_page_reference = (
                    account_service.validate_saved_facebook_page_account(account)
                )
            except (FacebookPagePublishError, TypeError, ValueError):
                raise ControlledPublishError(
                    "facebook_account_invalid",
                    "Facebook Page 账号记录无效。",
                ) from None
            if target.get("schedule") is not None:
                raise ControlledPublishError(
                    "facebook_unsupported_publish_setting",
                    "Facebook Page 首版不支持定时发布。",
                )
            raw_settings = target.get("settings")
            if (
                not isinstance(raw_settings, Mapping)
                or set(raw_settings) != {"visibility"}
                or str(raw_settings.get("visibility") or "").strip().lower()
                != "public"
            ):
                raise ControlledPublishError(
                    "facebook_unsupported_publish_setting",
                    "Facebook Page 首版必须明确选择立即公开发布。",
                )
            facebook_video_sha256 = _facebook_video_sha256(
                Path(str(bundle["assetPaths"][0]))
            )
            facebook_settings = {"visibility": "public"}
        if platform_type == 6:
            tiktok_schedule_intent = _tiktok_target_schedule(
                target.get("schedule"),
                now=schedule_now if schedule_now is not None else _shanghai_now(),
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
            tiktok_video_sha256 = _stream_sha256(
                Path(str(bundle["assetPaths"][0]))
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
        facebook_final_caption = ""
        facebook_caption_sha256 = ""
        if platform_type == 9:
            facebook_final_caption = build_facebook_page_caption(
                title=title,
                body=description,
                topics=tags,
            )
            facebook_caption_sha256 = facebook_page_caption_sha256(
                facebook_final_caption
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
        if tiktok_schedule_intent is not None:
            enable_timer = tiktok_schedule_intent.mode == "platform_native"
            schedule_time = tiktok_schedule_intent.local_time or ""
            schedule_timezone = tiktok_schedule_intent.timezone
        else:
            enable_timer, schedule_time, schedule_timezone = _schedule(
                target.get("schedule")
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
                    "tiktokVideoSha256": tiktok_video_sha256,
                    "scheduleMode": tiktok_schedule_intent.mode,
                    "scheduledAt": tiktok_schedule_intent.local_time,
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
        elif platform_type == 9 and facebook_settings is not None:
            manifest_intent = {
                "schemaVersion": str(bundle.get("schemaVersion") or ""),
                "contentType": str(bundle.get("contentType") or ""),
                "title": str(bundle.get("title") or ""),
                "body": str(bundle.get("body") or ""),
                "tags": [str(item) for item in bundle.get("tags") or []],
                "preferredPlatforms": sorted(
                    oneclick_capabilities.canonical_platform(item)
                    for item in bundle.get("preferredPlatforms") or []
                ),
                "platformOverride": dict(override),
            }
            encoded_manifest_intent = json.dumps(
                manifest_intent,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload.update(facebook_settings)
            payload.update(
                {
                    "coverPath": "",
                    "coverPaths": {},
                    "backgroundMode": False,
                    "scheduleMode": "immediate",
                    "scheduledAt": None,
                    "facebookControlledPublish": True,
                    "facebookExpectedPageReference": (
                        facebook_expected_page_reference
                    ),
                    "facebookFinalCaption": facebook_final_caption,
                    "facebookCaptionSha256": facebook_caption_sha256,
                    "facebookVideoSha256": facebook_video_sha256,
                    "facebookManifestIntentSha256": hashlib.sha256(
                        encoded_manifest_intent.encode("utf-8")
                    ).hexdigest(),
                }
            )
        payloads.append(payload)
    return payloads


def _file_identity(value: object) -> str:
    path = Path(str(value or ""))
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return str(value or "")


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ControlledPublishError(
            "tiktok_video_file_invalid",
            "TikTok 视频素材无法安全读取",
        ) from exc
    return digest.hexdigest()


def _facebook_video_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ControlledPublishError(
            "facebook_video_file_invalid",
            "Facebook Page 视频素材无法安全读取。",
        ) from exc
    return digest.hexdigest()


def _publish_intent_projection(
    payload: Mapping[str, Any],
    *,
    include_local_account_id: bool,
) -> dict[str, Any]:
    platform_type = int(payload.get("type") or 0)
    if platform_type == 9:
        projection: dict[str, Any] = {
            "type": platform_type,
            "contentType": str(payload.get("contentType") or ""),
            "manifestIntentSha256": str(
                payload.get("facebookManifestIntentSha256") or ""
            ),
            "title": str(payload.get("title") or ""),
            "description": str(payload.get("description") or ""),
            "tags": [str(item) for item in payload.get("tags") or []],
            "assets": [
                str(payload.get("facebookVideoSha256") or "")
                or _file_identity(item)
                for item in payload.get("fileList") or []
            ],
            "facebookExpectedPageReference": str(
                payload.get("facebookExpectedPageReference") or ""
            ),
            "facebookFinalCaption": str(
                payload.get("facebookFinalCaption") or ""
            ),
            "facebookCaptionSha256": str(
                payload.get("facebookCaptionSha256") or ""
            ),
            "visibility": str(payload.get("visibility") or ""),
            "scheduleMode": str(payload.get("scheduleMode") or ""),
            "scheduledAt": str(payload.get("scheduledAt") or ""),
            "scheduleTime": str(payload.get("scheduleTime") or ""),
            "scheduleTimezone": str(payload.get("scheduleTimezone") or ""),
            "enableTimer": bool(payload.get("enableTimer")),
        }
        if include_local_account_id:
            projection["accountIds"] = [
                int(item) for item in payload.get("accountIds") or []
            ]
        return projection

    tiktok_video_sha256 = str(payload.get("tiktokVideoSha256") or "")
    return {
        "type": platform_type,
        "accountIds": [int(item) for item in payload.get("accountIds") or []],
        "contentType": str(payload.get("contentType") or ""),
        "title": str(payload.get("title") or ""),
        "description": str(payload.get("description") or ""),
        "tags": [str(item) for item in payload.get("tags") or []],
        "assets": (
            [tiktok_video_sha256]
            if platform_type == 6
            else [_file_identity(item) for item in payload.get("fileList") or []]
        ),
        "cover": _file_identity(payload.get("coverPath")),
        "scheduleMode": str(payload.get("scheduleMode") or ""),
        "scheduledAt": str(payload.get("scheduledAt") or ""),
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
        "tiktokVideoSha256": tiktok_video_sha256,
    }


def publish_intent_fingerprint(payloads: Iterable[Mapping[str, Any]]) -> str:
    """Hash stable publish intent while excluding runtime/auth/session state."""

    payload_rows = [dict(payload) for payload in payloads]
    if (
        len(payload_rows) == 1
        and str(payload_rows[0].get("workflow") or "") == "douyin-graphic-matrix"
    ):
        from .douyin_graphic_matrix_service import matrix_scope_fingerprint

        return matrix_scope_fingerprint(payload_rows[0])
    normalized = sorted(
        (
            _publish_intent_projection(
                payload,
                include_local_account_id=True,
            )
            for payload in payload_rows
        ),
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def facebook_replay_fingerprint(
    payloads: Iterable[Mapping[str, Any]],
) -> str:
    """Hash one Page intent while excluding only the local account row ID."""

    payload_rows = [dict(payload) for payload in payloads]
    if len(payload_rows) != 1 or int(payload_rows[0].get("type") or 0) != 9:
        raise ControlledPublishError(
            "facebook_unsupported_publish_setting",
            "Facebook Page 重放指纹只支持单 Page 请求。",
        )
    normalized = [
        _publish_intent_projection(
            payload_rows[0],
            include_local_account_id=False,
        )
    ]
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_safe_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_safe_hash(value: object) -> str:
    return hashlib.sha256(_canonical_safe_json(value).encode("utf-8")).hexdigest()


def _sqlite_row_dict(
    cursor: sqlite3.Cursor,
    row: sqlite3.Row | tuple[Any, ...] | None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = [str(item[0]) for item in cursor.description or ()]
    return dict(zip(columns, row))


def _facebook_authorization_invalid() -> ControlledPublishError:
    return ControlledPublishError(
        "facebook_publish_authorization_invalid",
        "Facebook Page 正式发布授权无效，必须重新完成平台预检。",
    )


def _single_facebook_page_payload(
    payloads: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = [dict(payload) for payload in payloads]
    if (
        len(rows) != 1
        or type(rows[0].get("type")) is not int
        or rows[0]["type"] != 9
    ):
        raise _facebook_authorization_invalid()
    return rows[0]


def facebook_preflight_receipt_hash(
    conn: sqlite3.Connection,
    preflight_task_id: int,
    payloads: Iterable[Mapping[str, Any]],
) -> str:
    """Re-read and hash one verified Page form receipt from SQLite only."""

    current_payload = _single_facebook_page_payload(payloads)
    task_cursor = conn.execute(
        """
        SELECT id, mode, status, payloadJson
        FROM publish_tasks WHERE id = ?
        """,
        (int(preflight_task_id),),
    )
    task = _sqlite_row_dict(task_cursor, task_cursor.fetchone())
    if (
        task is None
        or str(task.get("mode") or "") != "oneclick_preflight"
        or str(task.get("status") or "") != "success"
    ):
        raise _facebook_authorization_invalid()
    try:
        stored_payloads = json.loads(str(task.get("payloadJson") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _facebook_authorization_invalid() from exc
    if (
        not isinstance(stored_payloads, list)
        or len(stored_payloads) != 1
        or not isinstance(stored_payloads[0], Mapping)
    ):
        raise _facebook_authorization_invalid()
    preflight_payload = _single_facebook_page_payload(stored_payloads)
    if publish_intent_fingerprint([preflight_payload]) != publish_intent_fingerprint(
        [current_payload]
    ):
        raise _facebook_authorization_invalid()

    item_cursor = conn.execute(
        """
        SELECT id, status, accountId, filePath, fileName, receiptJson
        FROM publish_task_items
        WHERE taskId = ? AND platformType = 9
        ORDER BY id
        """,
        (int(preflight_task_id),),
    )
    item_rows = item_cursor.fetchall()
    items = [
        _sqlite_row_dict(item_cursor, row)
        for row in item_rows
    ]
    if (
        len(items) != 1
        or items[0] is None
        or str(items[0].get("status") or "") != "success"
    ):
        raise _facebook_authorization_invalid()
    item = items[0]
    try:
        loaded_receipt = json.loads(str(item.get("receiptJson") or ""))
        safe_receipt = project_facebook_page_receipt(loaded_receipt)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _facebook_authorization_invalid() from exc

    account_ids = preflight_payload.get("accountIds")
    current_account_ids = current_payload.get("accountIds")
    if (
        not isinstance(account_ids, list)
        or len(account_ids) != 1
        or type(account_ids[0]) is not int
        or account_ids[0] <= 0
        or current_account_ids != account_ids
        or int(item.get("accountId") or 0) != account_ids[0]
        or safe_receipt.get("accountId") != account_ids[0]
    ):
        raise _facebook_authorization_invalid()

    page_reference = str(
        preflight_payload.get("facebookExpectedPageReference") or ""
    )
    video_hash = str(preflight_payload.get("facebookVideoSha256") or "")
    caption_hash = str(preflight_payload.get("facebookCaptionSha256") or "")
    caption = str(preflight_payload.get("facebookFinalCaption") or "")
    file_list = preflight_payload.get("fileList")
    if (
        not page_reference.isascii()
        or not page_reference.isdigit()
        or not _SAFE_SHA256_RE.fullmatch(video_hash)
        or not _SAFE_SHA256_RE.fullmatch(caption_hash)
        or facebook_page_caption_sha256(caption) != caption_hash
        or not isinstance(file_list, list)
        or len(file_list) != 1
        or type(file_list[0]) is not str
    ):
        raise _facebook_authorization_invalid()
    video_path = Path(file_list[0])
    try:
        current_video_hash = _facebook_video_sha256(video_path)
        current_video_size = video_path.stat().st_size
    except (ControlledPublishError, OSError) as exc:
        raise _facebook_authorization_invalid() from exc
    if (
        current_video_hash != video_hash
        or safe_receipt.get("phase") != "platform_form_verified"
        or safe_receipt.get("pageId") != page_reference
        or safe_receipt.get("videoName") != video_path.name
        or safe_receipt.get("videoSize") != current_video_size
        or safe_receipt.get("videoSha256") != video_hash
        or safe_receipt.get("captionSha256") != caption_hash
        or safe_receipt.get("visibility") != "public"
        or safe_receipt.get("finalButtonEnabled") is not True
        or safe_receipt.get("finalActionTriggered") is not False
    ):
        raise _facebook_authorization_invalid()
    return _canonical_safe_hash(safe_receipt)


def scope_fingerprint(payloads: Iterable[Mapping[str, Any]]) -> str:
    """Compatibility wrapper over the stable publish-intent fingerprint."""

    return publish_intent_fingerprint(payloads)


def _ensure_authorization_schema(conn: sqlite3.Connection) -> None:
    from .database import _add_columns

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
    _add_columns(
        conn,
        "controlled_publish_authorizations",
        (
            ("authorizationScope", "TEXT NOT NULL DEFAULT 'formal'"),
            ("preflightReceiptHash", "TEXT NOT NULL DEFAULT ''"),
        ),
    )


def create_authorization_schema(conn: sqlite3.Connection) -> None:
    _ensure_authorization_schema(conn)
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
    authorization_scope: str = "formal",
    preflight_receipt_hash: str = "",
) -> dict[str, Any]:
    if type(preflight_task_id) is not int or preflight_task_id <= 0:
        raise ControlledPublishError("controlled_preflight_required", "预检 taskId 无效")
    if not 1 <= int(ttl_seconds) <= 900:
        raise ControlledPublishError("controlled_authorization_ttl_invalid", "授权有效期不正确")
    if authorization_scope != "formal":
        raise ControlledPublishError(
            "controlled_authorization_scope_mismatch",
            "一次性授权范围无效",
        )
    if preflight_receipt_hash and (
        type(preflight_receipt_hash) is not str
        or not _SAFE_SHA256_RE.fullmatch(preflight_receipt_hash)
    ):
        raise ControlledPublishError(
            "controlled_authorization_scope_mismatch",
            "预检回执哈希无效",
        )
    create_authorization_schema(conn)
    created = _utc(now)
    expires = created + timedelta(seconds=int(ttl_seconds))
    authorization_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO controlled_publish_authorizations
            (authorizationId, preflightTaskId, scopeFingerprint,
             authorizationScope, preflightReceiptHash,
             createdAt, expiresAt, consumedAt)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            authorization_id,
            preflight_task_id,
            scope_fingerprint(payloads),
            authorization_scope,
            preflight_receipt_hash,
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
    try:
        _consume_authorization_in_transaction(
            conn,
            authorization_id,
            preflight_task_id,
            payloads,
            now=now,
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def _consume_authorization_in_transaction(
    conn: sqlite3.Connection,
    authorization_id: str,
    preflight_task_id: int,
    payloads: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> None:
    """Validate and consume without committing the caller's transaction."""

    payload_rows = [dict(payload) for payload in payloads]
    _ensure_authorization_schema(conn)
    current = _utc(now)
    row = conn.execute(
        "SELECT * FROM controlled_publish_authorizations WHERE authorizationId = ?",
        (str(authorization_id or "").strip(),),
    ).fetchone()
    if row is None:
        raise ControlledPublishError("controlled_authorization_invalid", "一次性授权不存在")
    data = dict(row)
    if str(data.get("preflightReceiptHash") or "") or any(
        int(payload.get("type") or 0) == 9 for payload in payload_rows
    ):
        raise _facebook_authorization_invalid()
    if data.get("consumedAt"):
        raise ControlledPublishError("controlled_authorization_consumed", "一次性授权已经使用")
    if int(data.get("preflightTaskId") or 0) != int(preflight_task_id):
        raise ControlledPublishError("controlled_authorization_scope_mismatch", "授权不属于本次预检")
    expires = datetime.fromisoformat(str(data["expiresAt"])).astimezone(timezone.utc)
    if current >= expires:
        raise ControlledPublishError("controlled_authorization_expired", "一次性授权已经过期")
    if str(data.get("scopeFingerprint") or "") != scope_fingerprint(payload_rows):
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
        raise ControlledPublishError("controlled_authorization_consumed", "一次性授权已经使用")


def _ensure_facebook_page_claim_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facebook_page_publish_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pageReference TEXT NOT NULL,
            publishIntentFingerprint TEXT NOT NULL,
            replayFingerprint TEXT NOT NULL,
            preflightTaskId INTEGER NOT NULL,
            preflightReceiptHash TEXT NOT NULL,
            taskId INTEGER NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK(state IN (
                'reserved',
                'final_action_claimed',
                'final_action_clicked',
                'ambiguous',
                'succeeded',
                'confirmed_not_published',
                'safe_failed'
            )),
            blocksReplay INTEGER NOT NULL CHECK(
                (state IN ('safe_failed', 'confirmed_not_published')
                 AND blocksReplay = 0)
                OR
                (state NOT IN ('safe_failed', 'confirmed_not_published')
                 AND blocksReplay = 1)
            ),
            workerStartedAt TEXT,
            baselineJson TEXT NOT NULL DEFAULT '{}',
            baselineHash TEXT NOT NULL DEFAULT '',
            formSnapshotJson TEXT NOT NULL DEFAULT '{}',
            formSnapshotHash TEXT NOT NULL DEFAULT '',
            clickedAt TEXT NOT NULL DEFAULT '',
            platformDecisionJson TEXT NOT NULL DEFAULT '{}',
            platformDecisionHash TEXT NOT NULL DEFAULT '',
            receiptJson TEXT NOT NULL DEFAULT '{}',
            receiptHash TEXT NOT NULL DEFAULT '',
            reelId TEXT NOT NULL DEFAULT '',
            reelUrl TEXT NOT NULL DEFAULT '',
            createdAt TEXT NOT NULL,
            updatedAt TEXT NOT NULL,
            FOREIGN KEY(taskId) REFERENCES publish_tasks(id),
            FOREIGN KEY(preflightTaskId) REFERENCES publish_tasks(id)
        )
        """
    )
    from .database import _add_columns

    _add_columns(
        conn,
        "facebook_page_publish_claims",
        (
            ("platformDecisionHash", "TEXT NOT NULL DEFAULT ''"),
            ("receiptHash", "TEXT NOT NULL DEFAULT ''"),
        ),
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_facebook_page_active_replay_claim
        ON facebook_page_publish_claims(pageReference, replayFingerprint)
        WHERE blocksReplay = 1
        """
    )


def _consume_facebook_authorization_in_transaction(
    conn: sqlite3.Connection,
    authorization_id: str,
    preflight_task_id: int,
    *,
    publish_intent: str,
    preflight_receipt_hash: str,
    now: datetime | None = None,
) -> None:
    _ensure_authorization_schema(conn)
    current = _utc(now)
    cursor = conn.execute(
        """
        SELECT * FROM controlled_publish_authorizations
        WHERE authorizationId = ?
        """,
        (str(authorization_id or "").strip(),),
    )
    row = _sqlite_row_dict(cursor, cursor.fetchone())
    try:
        expires = datetime.fromisoformat(str((row or {})["expiresAt"]))
        if expires.tzinfo is None:
            raise ValueError
        expires = expires.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        raise _facebook_authorization_invalid() from None
    if (
        row is None
        or row.get("consumedAt")
        or int(row.get("preflightTaskId") or 0) != int(preflight_task_id)
        or str(row.get("authorizationScope") or "") != "formal"
        or str(row.get("scopeFingerprint") or "") != publish_intent
        or str(row.get("preflightReceiptHash") or "")
        != preflight_receipt_hash
        or current >= expires
    ):
        raise _facebook_authorization_invalid()
    updated = conn.execute(
        """
        UPDATE controlled_publish_authorizations
        SET consumedAt = ?
        WHERE authorizationId = ?
          AND consumedAt IS NULL
          AND preflightTaskId = ?
          AND authorizationScope = 'formal'
          AND scopeFingerprint = ?
          AND preflightReceiptHash = ?
        """,
        (
            current.isoformat(),
            str(authorization_id).strip(),
            int(preflight_task_id),
            publish_intent,
            preflight_receipt_hash,
        ),
    )
    if updated.rowcount != 1:
        raise _facebook_authorization_invalid()


def _bind_facebook_page_task_item(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    account_id: int,
    authorization_snapshot_hash: str,
) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) AS itemCount
        FROM publish_task_items
        WHERE taskId = ? AND platformType = 9
        """,
        (int(task_id),),
    ).fetchone()
    item_count = int(row["itemCount"] if isinstance(row, sqlite3.Row) else row[0])
    if item_count != 1:
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 正式任务明细无法原子绑定。",
        )
    updated = conn.execute(
        """
        UPDATE publish_task_items
        SET accountId = ?, authorizationSnapshotHash = ?
        WHERE taskId = ? AND platformType = 9
        """,
        (
            int(account_id),
            authorization_snapshot_hash,
            int(task_id),
        ),
    )
    if updated.rowcount != 1:
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 正式任务明细无法原子绑定。",
        )


def _create_claimed_facebook_page_task(
    payloads: list[dict[str, Any]],
    *,
    preflight_task_id: int,
    authorization_id: str,
) -> dict[str, Any]:
    """Consume evidence and reserve one Page replay claim in one transaction."""

    payload = _single_facebook_page_payload(payloads)
    account_ids = payload.get("accountIds")
    page_reference = str(payload.get("facebookExpectedPageReference") or "")
    if (
        type(preflight_task_id) is not int
        or preflight_task_id <= 0
        or not isinstance(account_ids, list)
        or len(account_ids) != 1
        or type(account_ids[0]) is not int
        or account_ids[0] <= 0
        or not page_reference.isascii()
        or not page_reference.isdigit()
    ):
        raise _facebook_authorization_invalid()
    from . import task_service
    from .database import connect

    untrusted_runtime_fields = {
        "preflightReceiptHash",
        "preflightReceipt",
        "receipt",
        "baseline",
        "formSnapshot",
        "platformDecision",
        "blocksReplay",
        "cookies",
        "password",
        "token",
        "verificationCode",
    }
    stored_payload = {
        key: value
        for key, value in payload.items()
        if key not in untrusted_runtime_fields
    }
    stored_payloads = [stored_payload]
    publish_intent = publish_intent_fingerprint(stored_payloads)
    replay_fingerprint = facebook_replay_fingerprint(stored_payloads)
    with connect() as conn:
        _ensure_authorization_schema(conn)
        _ensure_facebook_page_claim_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            preflight_receipt_hash = facebook_preflight_receipt_hash(
                conn,
                int(preflight_task_id),
                stored_payloads,
            )
            _consume_facebook_authorization_in_transaction(
                conn,
                authorization_id,
                int(preflight_task_id),
                publish_intent=publish_intent,
                preflight_receipt_hash=preflight_receipt_hash,
            )
            task = task_service._insert_pending_task(
                conn,
                stored_payloads,
                mode="oneclick_publish",
            )
            _bind_facebook_page_task_item(
                conn,
                int(task["id"]),
                account_id=int(account_ids[0]),
                authorization_snapshot_hash=publish_intent,
            )
            now = _utc(None).isoformat()
            conn.execute(
                """
                INSERT INTO facebook_page_publish_claims (
                    pageReference, publishIntentFingerprint,
                    replayFingerprint, preflightTaskId,
                    preflightReceiptHash, taskId, state, blocksReplay,
                    createdAt, updatedAt
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', 1, ?, ?)
                """,
                (
                    page_reference,
                    publish_intent,
                    replay_fingerprint,
                    int(preflight_task_id),
                    preflight_receipt_hash,
                    int(task["id"]),
                    now,
                    now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if (
                "facebook_page_publish_claims.pageReference"
                in str(exc)
                and "facebook_page_publish_claims.replayFingerprint"
                in str(exc)
            ):
                raise ControlledPublishError(
                    "facebook_duplicate_submit_blocked",
                    "同一 Facebook Page 发布意图已有防重记录，已阻止重复提交。",
                ) from exc
            raise
        except Exception:
            conn.rollback()
            raise
    from . import publish_service

    return publish_service.start_controlled_facebook_publish(int(task["id"]))


def require_facebook_page_execution_claim(
    task_id: int,
    payloads: Iterable[Mapping[str, Any]],
) -> None:
    """Atomically grant one worker start for an exact reserved Page claim."""

    payload = _single_facebook_page_payload(payloads)
    page_reference = str(payload.get("facebookExpectedPageReference") or "")
    account_ids = payload.get("accountIds")
    if (
        not isinstance(account_ids, list)
        or len(account_ids) != 1
        or type(account_ids[0]) is not int
        or account_ids[0] <= 0
    ):
        raise _facebook_authorization_invalid()
    publish_intent = publish_intent_fingerprint([payload])
    replay_fingerprint = facebook_replay_fingerprint([payload])
    from .database import connect

    with connect() as conn:
        _ensure_facebook_page_claim_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE facebook_page_publish_claims
                SET workerStartedAt = ?, updatedAt = ?
                WHERE taskId = ?
                  AND state = 'reserved'
                  AND workerStartedAt IS NULL
                  AND pageReference = ?
                  AND publishIntentFingerprint = ?
                  AND replayFingerprint = ?
                  AND EXISTS (
                      SELECT 1 FROM publish_tasks AS task
                      WHERE task.id = facebook_page_publish_claims.taskId
                        AND task.mode = 'oneclick_publish'
                        AND task.status = 'pending'
                  )
                  AND 1 = (
                      SELECT COUNT(*) FROM publish_task_items AS counted_item
                      WHERE counted_item.taskId =
                            facebook_page_publish_claims.taskId
                  )
                  AND EXISTS (
                      SELECT 1 FROM publish_task_items AS item
                      WHERE item.taskId = facebook_page_publish_claims.taskId
                        AND item.platformType = 9
                        AND item.status = 'pending'
                        AND item.accountId = ?
                        AND item.authorizationSnapshotHash = ?
                  )
                """,
                (
                    _utc(None).isoformat(),
                    _utc(None).isoformat(),
                    int(task_id),
                    page_reference,
                    publish_intent,
                    replay_fingerprint,
                    int(account_ids[0]),
                    publish_intent,
                ),
            )
            if updated.rowcount != 1:
                raise _facebook_authorization_invalid()
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _safe_facebook_baseline(
    value: object,
    *,
    expected_page_reference: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 点击前基线无效。",
        )
    page_id = value.get("pageId")
    rows = value.get("rows")
    if page_id != expected_page_reference or not isinstance(rows, list):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 点击前基线无效。",
        )
    projected_rows: list[dict[str, object]] = []
    seen_reel_ids: set[str] = set()
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise ControlledPublishError(
                "facebook_claim_lifecycle_invalid",
                "Facebook Page 点击前基线无效。",
            )
        reel_id = raw_row.get("reelId")
        caption_hash = raw_row.get("captionSha256")
        try:
            safe_row = project_facebook_page_receipt(
                {
                    "pageId": page_id,
                    "reelId": reel_id,
                    "url": raw_row.get("url"),
                    "publishedAt": raw_row.get("publishedAt"),
                    "captionSha256": caption_hash,
                }
            )
        except ValueError as exc:
            raise ControlledPublishError(
                "facebook_claim_lifecycle_invalid",
                "Facebook Page 点击前基线无效。",
            ) from exc
        if (
            type(reel_id) is not str
            or not reel_id
            or reel_id in seen_reel_ids
            or safe_row.get("url") is None
            or safe_row.get("publishedAt") is None
            or type(caption_hash) is not str
            or not _SAFE_SHA256_RE.fullmatch(caption_hash)
        ):
            raise ControlledPublishError(
                "facebook_claim_lifecycle_invalid",
                "Facebook Page 点击前基线无效。",
            )
        seen_reel_ids.add(reel_id)
        projected_rows.append(
            {
                "reelId": reel_id,
                "url": safe_row["url"],
                "publishedAt": safe_row["publishedAt"],
                "captionSha256": caption_hash,
            }
        )
    projected_rows.sort(key=lambda row: str(row["reelId"]))
    return {"pageId": page_id, "rows": projected_rows}


def _safe_facebook_form_snapshot(
    value: object,
    *,
    expected_page_reference: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 最终表单快照无效。",
        )
    page_id = value.get("pageId")
    video_name = value.get("videoName")
    video_size = value.get("videoSize")
    video_hash = value.get("videoSha256")
    caption_hash = value.get("captionSha256")
    visibility = value.get("visibility")
    final_button_label = value.get("finalButtonLabel")
    final_button_ready = value.get("finalButtonReady")
    if (
        page_id != expected_page_reference
        or type(video_name) is not str
        or not video_name
        or len(video_name) > 512
        or "\n" in video_name
        or "\r" in video_name
        or type(video_size) is not int
        or video_size < 0
        or type(video_hash) is not str
        or not _SAFE_SHA256_RE.fullmatch(video_hash)
        or type(caption_hash) is not str
        or not _SAFE_SHA256_RE.fullmatch(caption_hash)
        or visibility != "public"
        or type(final_button_label) is not str
        or not final_button_label
        or len(final_button_label) > 128
        or "\n" in final_button_label
        or "\r" in final_button_label
        or final_button_ready is not True
    ):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 最终表单快照无效。",
        )
    return {
        "pageId": page_id,
        "videoName": video_name,
        "videoSize": video_size,
        "videoSha256": video_hash,
        "captionSha256": caption_hash,
        "visibility": visibility,
        "finalButtonLabel": final_button_label,
        "finalButtonReady": final_button_ready,
    }


def _load_hashed_facebook_snapshot(
    raw_json: str,
    raw_hash: str,
) -> object | None:
    if raw_json == "{}" and raw_hash == "":
        return None
    if not _SAFE_SHA256_RE.fullmatch(raw_hash):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page claim 快照完整性校验失败。",
        )
    try:
        loaded = json.loads(raw_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page claim 快照完整性校验失败。",
        ) from exc
    if (
        _canonical_safe_json(loaded) != raw_json
        or _canonical_safe_hash(loaded) != raw_hash
    ):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page claim 快照完整性校验失败。",
        )
    return loaded


def _safe_stored_facebook_platform_decision(
    value: object,
    *,
    expected_page_reference: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "pageId",
        "kind",
        "observedAt",
        "evidenceSha256",
    }:
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 已存平台决定不安全。",
        )
    page_id = value.get("pageId")
    kind = value.get("kind")
    observed_at = value.get("observedAt")
    evidence_hash = value.get("evidenceSha256")
    try:
        observed = datetime.fromisoformat(str(observed_at))
        if observed.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 已存平台决定不安全。",
        ) from None
    if (
        page_id != expected_page_reference
        or kind not in {"accepted", "rejected_no_creation", "unknown"}
        or type(evidence_hash) is not str
        or not _SAFE_SHA256_RE.fullmatch(evidence_hash)
    ):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 已存平台决定不安全。",
        )
    return dict(value)


def _safe_stored_facebook_receipt(
    value: object,
    *,
    expected_page_reference: str,
) -> dict[str, object]:
    try:
        projected = project_facebook_page_receipt(value)
    except ValueError as exc:
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 已存回执不安全。",
        ) from exc
    if projected != value or projected.get("pageId") not in (
        None,
        expected_page_reference,
    ):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 已存回执不安全。",
        )
    return projected


def _safe_facebook_platform_decision(
    value: object,
    *,
    expected_page_reference: str,
) -> dict[str, object]:
    try:
        from uploader.meta_uploader.content_list import FacebookPlatformDecision
    except (AttributeError, ImportError):
        return {}
    if not isinstance(FacebookPlatformDecision, type) or type(
        value
    ) is not FacebookPlatformDecision:
        return {}
    try:
        page_id = value.page_id
        kind = value.kind
        observed_at = value.observed_at
        evidence_hash = value.evidence_sha256
        observed = datetime.fromisoformat(str(observed_at))
        if observed.tzinfo is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 平台决定证据无效。",
        ) from None
    if (
        page_id != expected_page_reference
        or kind not in {"accepted", "rejected_no_creation", "unknown"}
        or type(evidence_hash) is not str
        or not _SAFE_SHA256_RE.fullmatch(evidence_hash)
    ):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 平台决定证据无效。",
        )
    return {
        "pageId": page_id,
        "kind": kind,
        "observedAt": str(observed_at),
        "evidenceSha256": evidence_hash,
    }


def _facebook_task7_reel_types() -> tuple[type, type] | None:
    try:
        from uploader.meta_uploader.content_list import (
            FacebookReelMatch,
            FacebookReelReceipt,
        )
    except (AttributeError, ImportError):
        return None
    if not isinstance(FacebookReelMatch, type) or not isinstance(
        FacebookReelReceipt,
        type,
    ):
        return None
    return FacebookReelMatch, FacebookReelReceipt


def _safe_facebook_reel_match(
    value: object,
    *,
    expected_page_reference: str,
    baseline: object,
    form_snapshot: object,
    clicked_at: str,
) -> dict[str, object]:
    task7_types = _facebook_task7_reel_types()
    if task7_types is None or type(value) is not task7_types[0]:
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 成功终态缺少可信唯一 Reel 匹配证据。",
        )
    try:
        reel_receipt = value.receipt
        if (
            type(value.status) is not str
            or value.status != "unique"
            or type(value.new_count) is not int
            or value.new_count < 1
            or type(value.matching_count) is not int
            or value.matching_count != 1
            or type(reel_receipt) is not task7_types[1]
        ):
            raise ValueError
        if not isinstance(baseline, Mapping) or not isinstance(
            form_snapshot,
            Mapping,
        ):
            raise ValueError
        caption_hash = form_snapshot.get("captionSha256")
        if (
            type(caption_hash) is not str
            or not _SAFE_SHA256_RE.fullmatch(caption_hash)
        ):
            raise ValueError
        safe_receipt = project_facebook_page_receipt(
            {
                "pageId": reel_receipt.page_id,
                "reelId": reel_receipt.reel_id,
                "url": reel_receipt.url,
                "publishedAt": reel_receipt.published_at,
                "captionSha256": caption_hash,
                "visibility": "public",
                "phase": "published_readback_confirmed",
                "platformWriteOccurred": True,
                "finalActionTriggered": True,
            }
        )
        clicked = datetime.fromisoformat(clicked_at)
        published = datetime.fromisoformat(str(reel_receipt.published_at))
        if clicked.tzinfo is None or published.tzinfo is None:
            raise ValueError
        old_reel_ids = {
            row.get("reelId")
            for row in baseline.get("rows", [])
            if isinstance(row, Mapping)
        }
    except (AttributeError, TypeError, ValueError) as exc:
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 唯一 Reel 匹配证据无效。",
        ) from exc
    if (
        safe_receipt.get("pageId") != expected_page_reference
        or safe_receipt.get("reelId") in old_reel_ids
        or published.astimezone(timezone.utc) < clicked.astimezone(timezone.utc)
    ):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 唯一 Reel 匹配证据与 claim 不一致。",
        )
    return safe_receipt


def mark_facebook_page_checkpoint(
    task_id: int,
    *,
    expected_state: str,
    new_state: str,
    receipt: Mapping[str, object],
    require_unleased: bool = False,
) -> None:
    """Persist one legal Page lifecycle edge by strict compare-and-swap."""

    if new_state not in _FACEBOOK_PAGE_CLAIM_TRANSITIONS.get(expected_state, ()):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page claim 生命周期跳转无效。",
        )
    if require_unleased and (
        expected_state != "reserved" or new_state != "safe_failed"
    ):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 未启动 worker 补偿边界无效。",
        )
    if not isinstance(receipt, Mapping):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page claim 回执无效。",
        )
    from .database import connect

    with connect() as conn:
        _ensure_facebook_page_claim_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                SELECT * FROM facebook_page_publish_claims WHERE taskId = ?
                """,
                (int(task_id),),
            )
            claim = _sqlite_row_dict(cursor, cursor.fetchone())
            if claim is None or str(claim.get("state") or "") != expected_state:
                raise ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page claim 生命周期状态已变化。",
                )
            if require_unleased and str(claim.get("workerStartedAt") or ""):
                raise ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page worker lease 已由其他启动者持有。",
                )
            page_reference = str(claim.get("pageReference") or "")
            receipt_page_id = receipt.get("pageId")
            if receipt_page_id not in (None, page_reference):
                raise ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page claim 回执主体不匹配。",
                )
            try:
                safe_receipt = project_facebook_page_receipt(receipt)
            except ValueError as exc:
                raise ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page claim 回执无效。",
                ) from exc

            baseline_json = str(claim.get("baselineJson") or "{}")
            baseline_hash = str(claim.get("baselineHash") or "")
            form_json = str(claim.get("formSnapshotJson") or "{}")
            form_hash = str(claim.get("formSnapshotHash") or "")
            decision_json = str(claim.get("platformDecisionJson") or "{}")
            decision_hash = str(claim.get("platformDecisionHash") or "")
            receipt_json = str(claim.get("receiptJson") or "{}")
            receipt_hash = str(claim.get("receiptHash") or "")
            stored_baseline = _load_hashed_facebook_snapshot(
                baseline_json,
                baseline_hash,
            )
            if stored_baseline is not None and _safe_facebook_baseline(
                stored_baseline,
                expected_page_reference=page_reference,
            ) != stored_baseline:
                raise ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page claim 基线快照不安全。",
                )
            stored_form = _load_hashed_facebook_snapshot(form_json, form_hash)
            if stored_form is not None and _safe_facebook_form_snapshot(
                stored_form,
                expected_page_reference=page_reference,
            ) != stored_form:
                raise ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page claim 表单快照不安全。",
                )
            stored_decision = _load_hashed_facebook_snapshot(
                decision_json,
                decision_hash,
            )
            if stored_decision is not None:
                _safe_stored_facebook_platform_decision(
                    stored_decision,
                    expected_page_reference=page_reference,
                )
            stored_receipt = _load_hashed_facebook_snapshot(
                receipt_json,
                receipt_hash,
            )
            if stored_receipt is not None:
                _safe_stored_facebook_receipt(
                    stored_receipt,
                    expected_page_reference=page_reference,
                )
            if "baseline" in receipt:
                baseline = _safe_facebook_baseline(
                    receipt["baseline"],
                    expected_page_reference=page_reference,
                )
                next_baseline_json = _canonical_safe_json(baseline)
                if baseline_json != "{}" and next_baseline_json != baseline_json:
                    raise ControlledPublishError(
                        "facebook_claim_lifecycle_invalid",
                        "Facebook Page claim 基线快照不可改写。",
                    )
                baseline_json = next_baseline_json
                baseline_hash = _canonical_safe_hash(baseline)
            if "formSnapshot" in receipt:
                form_snapshot = _safe_facebook_form_snapshot(
                    receipt["formSnapshot"],
                    expected_page_reference=page_reference,
                )
                next_form_json = _canonical_safe_json(form_snapshot)
                if form_json != "{}" and next_form_json != form_json:
                    raise ControlledPublishError(
                        "facebook_claim_lifecycle_invalid",
                        "Facebook Page claim 表单快照不可改写。",
                    )
                form_json = next_form_json
                form_hash = _canonical_safe_hash(form_snapshot)
            if new_state == "final_action_claimed" and (
                baseline_json == "{}"
                or not baseline_hash
                or form_json == "{}"
                or not form_hash
            ):
                raise ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page 最终动作前缺少完整基线或表单证据。",
                )
            decision = _safe_facebook_platform_decision(
                receipt.get("platformDecision"),
                expected_page_reference=page_reference,
            )
            if decision:
                decision_json = _canonical_safe_json(decision)
                decision_hash = _canonical_safe_hash(decision)
            if new_state == "confirmed_not_published" and (
                not decision or decision.get("kind") != "rejected_no_creation"
            ):
                raise ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page 未发布终态缺少平台拒绝证据。",
                )
            clicked_at = str(claim.get("clickedAt") or "")
            if new_state == "succeeded":
                safe_receipt = _safe_facebook_reel_match(
                    receipt.get("reelMatch"),
                    expected_page_reference=page_reference,
                    baseline=stored_baseline,
                    form_snapshot=stored_form,
                    clicked_at=clicked_at,
                )
            safe_receipt.pop("baselineHash", None)
            safe_receipt.pop("formSnapshotHash", None)
            if baseline_hash:
                safe_receipt["baselineHash"] = baseline_hash
            if form_hash:
                safe_receipt["formSnapshotHash"] = form_hash
            receipt_json = _canonical_safe_json(safe_receipt)
            receipt_hash = _canonical_safe_hash(safe_receipt)
            now = _utc(None).isoformat()
            if new_state == "final_action_clicked":
                clicked_at = now
            blocks_replay = (
                0 if new_state in _FACEBOOK_PAGE_REPLAY_RELEASE_STATES else 1
            )
            updated = conn.execute(
                """
                UPDATE facebook_page_publish_claims
                SET state = ?, blocksReplay = ?, baselineJson = ?,
                    baselineHash = ?, formSnapshotJson = ?,
                    formSnapshotHash = ?, clickedAt = ?,
                    platformDecisionJson = ?, platformDecisionHash = ?,
                    receiptJson = ?, receiptHash = ?,
                    reelId = ?, reelUrl = ?, updatedAt = ?
                WHERE taskId = ? AND state = ?
                  AND (? = 0 OR workerStartedAt IS NULL)
                """,
                (
                    new_state,
                    blocks_replay,
                    baseline_json,
                    baseline_hash,
                    form_json,
                    form_hash,
                    clicked_at,
                    decision_json,
                    decision_hash,
                    receipt_json,
                    receipt_hash,
                    str(safe_receipt.get("reelId") or claim.get("reelId") or ""),
                    str(safe_receipt.get("url") or claim.get("reelUrl") or ""),
                    now,
                    int(task_id),
                    expected_state,
                    int(require_unleased),
                ),
            )
            if updated.rowcount != 1:
                raise ControlledPublishError(
                    "facebook_claim_lifecycle_invalid",
                    "Facebook Page claim 生命周期状态已变化。",
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _load_succeeded_facebook_page_receipt(task_id: int) -> dict[str, object]:
    """Return the hash-verified safe receipt committed by the succeeded claim."""

    from .database import connect

    with connect() as conn:
        _ensure_facebook_page_claim_schema(conn)
        row = conn.execute(
            """
            SELECT pageReference, state, receiptJson, receiptHash,
                   reelId, reelUrl
            FROM facebook_page_publish_claims
            WHERE taskId = ?
            """,
            (int(task_id),),
        ).fetchone()
        claim = dict(row) if row is not None else None
    if claim is None or str(claim.get("state") or "") != "succeeded":
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 成功任务缺少已持久化 claim 回执。",
        )
    stored = _load_hashed_facebook_snapshot(
        str(claim.get("receiptJson") or "{}"),
        str(claim.get("receiptHash") or ""),
    )
    if stored is None:
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 成功 claim 回执哈希无效。",
        )
    safe_receipt = _safe_stored_facebook_receipt(
        stored,
        expected_page_reference=str(claim.get("pageReference") or ""),
    )
    if (
        str(safe_receipt.get("reelId") or "")
        != str(claim.get("reelId") or "")
        or str(safe_receipt.get("url") or "")
        != str(claim.get("reelUrl") or "")
    ):
        raise ControlledPublishError(
            "facebook_claim_lifecycle_invalid",
            "Facebook Page 成功 claim 回执与唯一 Reel 不一致。",
        )
    return safe_receipt


def _ensure_tiktok_claim_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tiktok_controlled_execution_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scopeFingerprint TEXT NOT NULL,
            taskId INTEGER UNIQUE,
            mode TEXT NOT NULL CHECK(mode IN ('formal', 'platform_form_check')),
            state TEXT NOT NULL CHECK(state IN ('reserved', 'claimed', 'started')),
            createdAt TEXT NOT NULL,
            FOREIGN KEY(taskId) REFERENCES publish_tasks(id)
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tiktok_formal_scope_claim
        ON tiktok_controlled_execution_claims(scopeFingerprint)
        WHERE mode = 'formal'
        """
    )


def _existing_tiktok_formal_claim(
    conn: sqlite3.Connection,
    fingerprint: str,
) -> sqlite3.Row | None:
    irreversible = tiktok_irreversible_evidence_sql("claim.taskId")
    return conn.execute(
        f"""
        SELECT claim.id, claim.taskId, claim.state,
               task.status AS taskStatus,
               {irreversible} AS hasFinalAction
        FROM tiktok_controlled_execution_claims AS claim
        LEFT JOIN publish_tasks AS task ON task.id = claim.taskId
        WHERE claim.mode = 'formal' AND claim.scopeFingerprint = ?
        LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()


def _release_retryable_tiktok_claim_or_raise(
    conn: sqlite3.Connection,
    fingerprint: str,
) -> None:
    claim = _existing_tiktok_formal_claim(conn, fingerprint)
    if claim is None:
        return
    data = dict(claim)
    task_status = str(data.get("taskStatus") or "")
    if task_status in {"pending", "running"}:
        from . import task_service

        task_service._reconcile_stale_controlled_task_in_transaction(
            conn,
            int(data.get("taskId") or 0),
        )
        claim = _existing_tiktok_formal_claim(conn, fingerprint)
        if claim is None:
            return
        data = dict(claim)
        task_status = str(data.get("taskStatus") or "")
    has_final_action = bool(data.get("hasFinalAction"))
    if has_final_action:
        raise ControlledPublishError(
            "controlled_publish_outcome_ambiguous",
            "同一 TikTok 发布范围已触发最终动作，必须先人工只读核对",
        )
    if task_status == "success":
        raise ControlledPublishError(
            "controlled_already_published",
            "同一 TikTok 发布范围已有成功任务，已阻止重复发布",
        )
    if task_status in {"failed", "partial_failed"}:
        irreversible = tiktok_irreversible_evidence_sql(
            "tiktok_controlled_execution_claims.taskId"
        )
        deleted = conn.execute(
            f"""
            DELETE FROM tiktok_controlled_execution_claims
            WHERE id = ? AND mode = 'formal'
              AND NOT {irreversible}
            """,
            (int(data["id"]),),
        )
        if deleted.rowcount == 1:
            return
        raise ControlledPublishError(
            "controlled_publish_outcome_ambiguous",
            "同一 TikTok 发布范围的最终动作状态已变化，必须先人工核对",
        )
    raise ControlledPublishError(
        "controlled_publish_in_progress",
        "同一 TikTok 发布范围已有等待执行或执行中的受控任务",
    )


def _load_tiktok_preflight_in_transaction(
    conn: sqlite3.Connection,
    preflight_task_id: int,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM publish_tasks WHERE id = ?",
        (int(preflight_task_id),),
    ).fetchone()
    if row is None or str(row["mode"] or "") != "oneclick_preflight":
        raise ControlledPublishError(
            "controlled_preflight_required",
            "正式发布缺少对应预检任务",
        )
    if str(row["status"] or "") != "success":
        raise ControlledPublishError(
            "controlled_preflight_not_successful",
            "对应预检尚未全部成功",
        )
    events = conn.execute(
        "SELECT eventType FROM publish_task_events WHERE taskId = ? ORDER BY id",
        (int(preflight_task_id),),
    ).fetchall()
    task = dict(row)
    task["events"] = [dict(event) for event in events]
    return task


def _create_claimed_tiktok_task(
    payloads: list[dict[str, Any]],
    *,
    mode: str,
    preflight_task_id: int | None = None,
    authorization_id: str = "",
) -> dict[str, Any]:
    """Atomically claim one TikTok execution and create its pending task."""

    if mode not in {"formal", "platform_form_check"}:
        raise ControlledPublishError(
            "controlled_mode_invalid",
            "TikTok claim 模式无效",
        )
    if len(payloads) != 1 or int(payloads[0].get("type") or 0) != 6:
        raise ControlledPublishError(
            "tiktok_target_invalid",
            "TikTok claim 只能绑定一个目标",
        )
    from . import task_service
    from .database import connect

    fingerprint = scope_fingerprint(payloads)
    with connect() as conn:
        _ensure_authorization_schema(conn)
        _ensure_tiktok_claim_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if mode == "formal":
                _release_retryable_tiktok_claim_or_raise(conn, fingerprint)
                if type(preflight_task_id) is not int or preflight_task_id <= 0:
                    raise ControlledPublishError(
                        "controlled_preflight_required",
                        "正式发布缺少对应预检任务",
                    )
                preflight = _load_tiktok_preflight_in_transaction(
                    conn,
                    preflight_task_id,
                )
                try:
                    preflight_payloads = json.loads(
                        str(preflight.get("payloadJson") or "[]")
                    )
                except json.JSONDecodeError as exc:
                    raise ControlledPublishError(
                        "controlled_preflight_invalid",
                        "预检任务快照不可读取",
                    ) from exc
                if not isinstance(preflight_payloads, list):
                    raise ControlledPublishError(
                        "controlled_preflight_invalid",
                        "预检任务快照不可读取",
                    )
                _require_tiktok_local_preflight_task(
                    preflight,
                    [
                        item
                        for item in preflight_payloads
                        if isinstance(item, Mapping)
                    ],
                )
                _consume_authorization_in_transaction(
                    conn,
                    authorization_id,
                    preflight_task_id,
                    payloads,
                )
            claim = conn.execute(
                """
                INSERT INTO tiktok_controlled_execution_claims
                    (scopeFingerprint, taskId, mode, state, createdAt)
                VALUES (?, NULL, ?, 'reserved', ?)
                """,
                (fingerprint, mode, _utc(None).isoformat()),
            )
            task = task_service._insert_pending_task(
                conn,
                payloads,
                mode=(
                    "oneclick_publish"
                    if mode == "formal"
                    else "oneclick_platform_form_check"
                ),
            )
            bound = conn.execute(
                """
                UPDATE tiktok_controlled_execution_claims
                SET taskId = ?, state = 'claimed'
                WHERE id = ? AND state = 'reserved' AND taskId IS NULL
                """,
                (int(task["id"]), int(claim.lastrowid)),
            )
            if bound.rowcount != 1:
                raise ControlledPublishError(
                    "tiktok_controlled_claim_invalid",
                    "TikTok 受控任务 claim 无法绑定",
                )
            conn.commit()
            return task
        except Exception:
            conn.rollback()
            raise


def require_tiktok_execution_claim(
    task_id: int,
    payloads: Iterable[Mapping[str, Any]],
    *,
    mode: str,
) -> None:
    """Verify a task-bound DB claim before a TikTok worker can start."""

    from .database import connect

    rows = [dict(item) for item in payloads]
    if len(rows) != 1 or int(rows[0].get("type") or 0) != 6:
        raise ValueError("TikTok 受控任务 claim 无效")
    with connect() as conn:
        _ensure_tiktok_claim_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            claim = conn.execute(
                """
                SELECT mode, state, scopeFingerprint
                FROM tiktok_controlled_execution_claims
                WHERE taskId = ?
                """,
                (int(task_id),),
            ).fetchone()
            if claim is not None and str(claim["state"] or "") == "started":
                raise ValueError("TikTok 受控任务 claim 已经启动")
            if (
                claim is None
                or str(claim["mode"] or "") != mode
                or str(claim["state"] or "") != "claimed"
                or str(claim["scopeFingerprint"] or "")
                != scope_fingerprint(rows)
            ):
                raise ValueError(
                    "TikTok formal/form-check 缺少有效的受控任务 claim"
                )
            updated = conn.execute(
                """
                UPDATE tiktok_controlled_execution_claims
                SET state = 'started'
                WHERE taskId = ? AND state = 'claimed'
                """,
                (int(task_id),),
            )
            if updated.rowcount != 1:
                raise ValueError("TikTok 受控任务 claim 已经启动")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def compensate_tiktok_worker_start_failure(
    task_id: int,
    *,
    mode: str,
    expected_claim_state: str = "started",
) -> bool:
    """Close only a reversible claim after this process failed to start a worker."""

    from . import task_service
    from .database import connect

    if expected_claim_state not in {"claimed", "started"}:
        raise ValueError("TikTok worker 启动补偿 claim 状态无效")
    with connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            irreversible = tiktok_irreversible_evidence_sql(
                "tiktok_controlled_execution_claims.taskId"
            )
            claim = conn.execute(
                f"""
                SELECT id FROM tiktok_controlled_execution_claims
                WHERE taskId = ? AND mode = ? AND state = ?
                  AND NOT {irreversible}
                """,
                (int(task_id), str(mode), expected_claim_state),
            ).fetchone()
            if claim is None:
                conn.rollback()
                return False
            failed = task_service._fail_active_task_in_transaction(
                conn,
                int(task_id),
                error_code="tiktok_worker_start_failed",
                message="TikTok 受控 worker 启动失败，未触发平台最终动作",
                event_type="tiktok_worker_start_failed",
            )
            if not failed:
                conn.rollback()
                return False
            deleted = conn.execute(
                f"""
                DELETE FROM tiktok_controlled_execution_claims
                WHERE id = ? AND taskId = ? AND mode = ? AND state = ?
                  AND NOT {irreversible}
                """,
                (
                    int(claim["id"]),
                    int(task_id),
                    str(mode),
                    expected_claim_state,
                ),
            )
            if deleted.rowcount != 1:
                raise ControlledPublishError(
                    "tiktok_worker_start_compensation_failed",
                    "TikTok worker 启动失败状态无法安全收口",
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def release_terminal_tiktok_platform_form_check_claim(task_id: int) -> bool:
    """Atomically release one terminal, reversible form-check worker claim."""

    from .database import connect

    with connect() as conn:
        _ensure_tiktok_claim_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            irreversible = tiktok_irreversible_evidence_sql(
                "tiktok_controlled_execution_claims.taskId"
            )
            deleted = conn.execute(
                f"""
                DELETE FROM tiktok_controlled_execution_claims
                WHERE taskId = ?
                  AND mode = 'platform_form_check'
                  AND state = 'started'
                  AND EXISTS (
                      SELECT 1
                      FROM publish_tasks AS task
                      WHERE task.id = tiktok_controlled_execution_claims.taskId
                        AND task.mode = 'oneclick_platform_form_check'
                        AND task.status IN ('success', 'failed', 'partial_failed')
                  )
                  AND NOT {irreversible}
                """,
                (int(task_id),),
            )
            conn.commit()
            return deleted.rowcount == 1
        except Exception:
            conn.rollback()
            raise


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
                    or related_payload.get("scheduledAt")
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
    elif task_status_value in {"failed", "partial_failed"}:
        stage = "failed"
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
    elif "tiktok_scheduled_readback_confirmed" in event_types or (
        "scheduled_readback_confirmed" in tiktok_receipt_phases
    ):
        stage = "scheduled_readback_confirmed"
    elif "tiktok_scheduled_accepted" in event_types or (
        "scheduled_accepted" in tiktok_receipt_phases
    ):
        stage = "scheduled_accepted"
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
        preflight_receipt_hash = ""
        if (
            len(normalized_payloads) == 1
            and int(normalized_payloads[0].get("type") or 0) == 9
        ):
            preflight_receipt_hash = facebook_preflight_receipt_hash(
                conn,
                int(task_id),
                normalized_payloads,
            )
        return create_authorization(
            conn,
            int(task_id),
            normalized_payloads,
            ttl_seconds=ttl_seconds,
            authorization_scope="formal",
            preflight_receipt_hash=preflight_receipt_hash,
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
    is_single_tiktok = (
        len(payloads) == 1 and int(payloads[0].get("type") or 0) == 6
    )
    is_single_facebook_page = (
        len(payloads) == 1 and int(payloads[0].get("type") or 0) == 9
    )
    if is_single_facebook_page and mode == "formal":
        task = _create_claimed_facebook_page_task(
            payloads,
            preflight_task_id=int(request["confirmedPreflightTaskId"]),
            authorization_id=str(request["authorizationId"]),
        )
        stored = task_service.get_task(int(task["id"])) or task
        return project_task(stored)
    if is_single_tiktok and mode in {"formal", "platform_form_check"}:
        task = _create_claimed_tiktok_task(
            payloads,
            mode=mode,
            preflight_task_id=(
                int(request["confirmedPreflightTaskId"])
                if mode == "formal"
                else None
            ),
            authorization_id=(
                str(request["authorizationId"])
                if mode == "formal"
                else ""
            ),
        )
        publish_service.start_controlled_tiktok_publish(int(task["id"]))
        stored = task_service.get_task(int(task["id"])) or task
        return project_task(stored)
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
