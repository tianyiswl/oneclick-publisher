# -*- coding: utf-8 -*-
"""TikTok 单账号单视频的严格本地合同。

本模块的 ``preflight`` 只读本地账号记录、会话 JSON 和视频
文件。它不导入上传器，也不启动 Playwright。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from utils.base_social_media import (
    launch_publish_browser,
    new_publish_context,
    set_init_script,
)
from utils.log import tiktok_logger
from utils.publish_observer import publish_context

from . import task_service
from .overseas_tiktok_errors import TikTokPublishError
from .overseas_tiktok_identity import (
    TikTokIdentityError,
    normalize_tiktok_handle,
    read_tiktok_signed_in_navigation_identity,
    validate_identity_binding,
)
from .paths import COOKIE_DIR, DB_PATH
from .overseas_tiktok_session_scope import (
    TikTokSessionScopeError,
    load_sanitized_tiktok_storage_state_file,
    replace_tiktok_storage_state_file,
)
from .tiktok_schedule_contract import (
    SHANGHAI,
    SHANGHAI_NAME,
    TikTokScheduleContractError,
    TikTokScheduleIntent,
    parse_tiktok_schedule_fields,
    validate_tiktok_schedule_window,
)


TIKTOK_CONTENT_LIMIT = 2200
TIKTOK_MODES = frozenset({"preflight", "platform_form_check", "formal"})
TIKTOK_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm"})
TIKTOK_UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
TIKTOK_NAVIGATION_TIMEOUT_MS = 120_000
_TIKTOK_ACCEPTED_EVIDENCE = frozenset(
    {
        "platform_feedback:your video has been uploaded",
        "platform_feedback:video posted successfully",
        "platform_feedback:post published",
        "platform_feedback:posted successfully",
        "platform_feedback:upload successful",
        "platform_feedback:视频已发布",
        "platform_feedback:发布成功",
        "platform_feedback:上传成功",
    }
)

# Kept lazy so importing this module for local preflight never imports the
# uploader (which imports the caption composer back from this module) and never
# constructs Playwright.  Tests may replace either seam with an offline fake.
TiktokVideo = None
async_playwright = None

_RUNTIME_MODE_BY_MODE = {
    "preflight": "preflight",
    "platform_form_check": "platform_form_check",
    "formal": "publish",
}
_TOPIC_KEYS = frozenset({"tags", "topics", "hashtags"})
_VIDEO_LIST_KEYS = frozenset({"fileList", "videoPaths", "videos", "assetPaths", "assets"})
_VIDEO_ALIAS_KEYS = _VIDEO_LIST_KEYS | {"videoPath"}
_CONTENT_ALIAS_KEYS = _TOPIC_KEYS | {"title", "description", "body"}
_ACCOUNT_ALIAS_KEYS = frozenset(
    {
        "id",
        "accountId",
        "accountIds",
        "accountReference",
        "tiktokExpectedAccountReference",
    }
)
_SESSION_ALIAS_KEYS = frozenset(
    {"accountList", "accountFile", "sessionFile", "filePath"}
)
_SENSITIVE_ALIAS_KEYS = (
    _ACCOUNT_ALIAS_KEYS
    | _SESSION_ALIAS_KEYS
    | _VIDEO_ALIAS_KEYS
    | _CONTENT_ALIAS_KEYS
)
_ROOT_SENSITIVE_KEYS = frozenset(
    {
        "accountId",
        "accountIds",
        "accountReference",
        "tiktokExpectedAccountReference",
        "accountList",
        "accountFile",
        "sessionFile",
        *_VIDEO_ALIAS_KEYS,
        *_CONTENT_ALIAS_KEYS,
    }
)
_ACCOUNT_ROW_SENSITIVE_KEYS = frozenset(
    {
        "id",
        "accountId",
        "accountReference",
        "accountList",
        "accountFile",
        "sessionFile",
        "filePath",
    }
)
_TARGET_SENSITIVE_KEYS = frozenset(
    {
        "accountId",
        "accountReference",
        "accountList",
        "accountFile",
        "sessionFile",
        "filePath",
    }
)
_CONTENT_SENSITIVE_KEYS = _VIDEO_ALIAS_KEYS | _CONTENT_ALIAS_KEYS
_SCHEDULE_ALIAS_KEYS = frozenset(
    {
        "enabletimer",
        "dailytimes",
        "videosperday",
        "startdays",
        "timejitterminutes",
        "schedule",
        "scheduletime",
        "scheduletimezone",
        "scheduledat",
        "publishschedule",
        "publishat",
        "publishtime",
        "timer",
        "timerenabled",
        "localtime",
        "timezone",
    }
)
_CANONICAL_ROOT_SCHEDULE_KEYS = frozenset(
    {
        "enableTimer",
        "scheduleTime",
        "scheduleTimezone",
        "videosPerDay",
        "dailyTimes",
        "startDays",
        "timeJitterMinutes",
        "schedule",
        "scheduleMode",
        "scheduledAt",
    }
)
_CANONICAL_ROOT_SCHEDULE_KEYS_BY_CASEFOLD = {
    key.casefold(): key for key in _CANONICAL_ROOT_SCHEDULE_KEYS
}


def payload_has_tiktok_platform_signal(payload: Mapping[str, Any]) -> bool:
    """Return whether any supported platform discriminator selects TikTok."""

    for key in ("type", "platformType"):
        try:
            if key in payload and int(payload[key]) == 6:
                return True
        except (TypeError, ValueError):
            continue
    if str(payload.get("platform") or "").strip().casefold() == "tiktok":
        return True
    candidates: list[object] = []
    if "target" in payload:
        candidates.append(payload["target"])
    if isinstance(payload.get("targets"), Mapping):
        candidates.append(payload["targets"])
    elif isinstance(payload.get("targets"), (list, tuple)):
        candidates.extend(payload["targets"])
    return any(
        isinstance(candidate, Mapping)
        and str(candidate.get("platform") or "").strip().casefold() == "tiktok"
        for candidate in candidates
    )


def _fail(error_code: str, message: str) -> None:
    raise TikTokPublishError(error_code, message)


def _is_nonempty(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return bool(value)
    return bool(value)


def _walk_mappings(value: object) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_mappings(nested)


def _walk_tiktok_payload_mappings(
    value: object,
    *,
    at_root: bool = False,
) -> Iterable[Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        if isinstance(value, (list, tuple)):
            for nested in value:
                yield from _walk_tiktok_payload_mappings(nested)
        return
    yield value
    for key, nested in value.items():
        if at_root and key == "platformOverrides":
            if not isinstance(nested, Mapping):
                continue
            for platform, override in nested.items():
                if str(platform).strip().casefold() == "tiktok":
                    yield from _walk_tiktok_payload_mappings(override)
            continue
        yield from _walk_tiktok_payload_mappings(nested)


def _validate_sensitive_alias_locations(payload: Mapping[str, Any]) -> None:
    def visit(value: object, allowed: frozenset[str]) -> None:
        if isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested, allowed)
            return
        if not isinstance(value, Mapping):
            return
        for key, nested in value.items():
            if key in _SENSITIVE_ALIAS_KEYS and key not in allowed:
                _fail(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 敏感字段出现在不允许的位置",
                )
            visit(nested, frozenset())

    for key, value in payload.items():
        if key in _SENSITIVE_ALIAS_KEYS and key not in _ROOT_SENSITIVE_KEYS:
            _fail(
                "tiktok_unsupported_publish_setting",
                "TikTok 敏感字段出现在不允许的位置",
            )
        if key == "account":
            visit(value, _ACCOUNT_ROW_SENSITIVE_KEYS)
        elif key == "accounts":
            visit(value, _ACCOUNT_ROW_SENSITIVE_KEYS)
        elif key == "target":
            visit(value, _TARGET_SENSITIVE_KEYS)
        elif key == "targets":
            visit(value, _TARGET_SENSITIVE_KEYS)
        elif key == "content":
            visit(value, _CONTENT_SENSITIVE_KEYS)
        elif key == "platformOverrides":
            if not isinstance(value, Mapping):
                continue
            for platform, override in value.items():
                if str(platform).strip().casefold() == "tiktok":
                    visit(override, _CONTENT_SENSITIVE_KEYS)
        elif key not in _SENSITIVE_ALIAS_KEYS:
            visit(value, frozenset())
def _ai_requested(value: object) -> bool:
    if not isinstance(value, Mapping):
        return value is True or _is_nonempty(value)
    return any(
        _is_nonempty(nested)
        for mapping in _walk_mappings(value)
        for nested in mapping.values()
    )


def _validate_schedule_locations(payload: Mapping[str, Any]) -> None:
    def visit_nonroot(value: object) -> None:
        if isinstance(value, (list, tuple)):
            for nested in value:
                visit_nonroot(nested)
            return
        if not isinstance(value, Mapping):
            return
        for raw_key, nested in value.items():
            key = str(raw_key).strip().casefold()
            if key in _SCHEDULE_ALIAS_KEYS or "schedule" in key:
                _fail(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 定时字段只允许出现在根位置",
                )
            visit_nonroot(nested)

    for key, value in payload.items():
        if key == "schedule":
            if (
                not isinstance(value, Mapping)
                or set(value) - {"enabled", "timezone"}
                or value.get("enabled") is not False
                or (
                    "timezone" in value
                    and (
                        type(value["timezone"]) is not str
                        or not value["timezone"].strip()
                    )
                )
                or (
                    "timezone" in value
                    and value["timezone"] != payload.get("scheduleTimezone")
                )
            ):
                _fail(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 根定时字段无效",
                )
            continue
        if str(key).strip().casefold() == "timezone":
            _fail(
                "tiktok_unsupported_publish_setting",
                "TikTok timezone 只允许出现在根 schedule 内",
            )
        if key == "platformOverrides":
            if isinstance(value, Mapping):
                for platform, override in value.items():
                    if str(platform).strip().casefold() == "tiktok":
                        visit_nonroot(override)
            continue
        if isinstance(value, (Mapping, list, tuple)):
            visit_nonroot(value)


def _validate_schedule_fields(payload: Mapping[str, Any]) -> TikTokScheduleIntent:
    for raw_key in payload:
        canonical_key = _CANONICAL_ROOT_SCHEDULE_KEYS_BY_CASEFOLD.get(
            str(raw_key).strip().casefold()
        )
        if canonical_key is not None and raw_key != canonical_key:
            _fail(
                "tiktok_schedule_invalid",
                "TikTok 排期字段名无效",
            )
    _validate_schedule_locations(payload)
    has_schedule_mode = "scheduleMode" in payload
    has_scheduled_at = "scheduledAt" in payload
    if has_schedule_mode != has_scheduled_at:
        _fail(
            "tiktok_unsupported_publish_setting",
            "TikTok 排期快照必须同时包含模式和时间",
        )
    exact_integers = {
        "videosPerDay": 1,
        "startDays": 0,
        "timeJitterMinutes": 0,
    }
    if any(
        type(payload.get(key)) is not int or payload.get(key) != expected
        for key, expected in exact_integers.items()
    ):
        _fail(
            "tiktok_unsupported_publish_setting",
            "TikTok 首版定时参数必须保持立即发布默认值",
        )

    known_schedule_keys = {
        "enabletimer",
        "scheduletime",
        "schedulemode",
        "scheduledat",
        "schedule",
        "publishschedule",
        "scheduletimezone",
        "videosperday",
        "dailytimes",
        "startdays",
        "timejitterminutes",
        "localtime",
        "timezone",
        "enabled",
    }
    explicit_unknown = {"publishat", "publishtime", "timer", "timerenabled"}
    for mapping in _walk_tiktok_payload_mappings(payload, at_root=True):
        for raw_key, value in mapping.items():
            key = str(raw_key).strip().casefold()
            if key in explicit_unknown or (
                "schedule" in key and key not in known_schedule_keys
            ):
                _fail(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 定时字段无效",
                )
            if key in {"publishschedule", "localtime"}:
                _fail(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 定时字段无效",
                )
            if key == "schedule" and value is not None and isinstance(value, Mapping):
                if payload.get("enableTimer") is True:
                    _fail(
                        "tiktok_unsupported_publish_setting",
                        "TikTok 定时字段冲突",
                    )

    try:
        intent = parse_tiktok_schedule_fields(
            enable_timer=payload.get("enableTimer"),
            schedule_time=payload.get("scheduleTime"),
            schedule_timezone=payload.get("scheduleTimezone"),
            daily_times=payload.get("dailyTimes"),
        )
    except TikTokScheduleContractError as exc:
        _fail(exc.error_code, exc.public_message)
    if "schedule" in payload:
        _fail(
            "tiktok_unsupported_publish_setting",
            "TikTok 不接受旧式根定时字段",
        )
    if has_schedule_mode and (
        payload.get("scheduleMode") != intent.mode
        or payload.get("scheduledAt") != intent.local_time
    ):
        _fail(
            "tiktok_unsupported_publish_setting",
            "TikTok 排期快照与定时设置不一致",
        )
    return intent


def _shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def _prepared_schedule_intent(
    prepared: Mapping[str, Any],
) -> TikTokScheduleIntent:
    mode = prepared.get("scheduleMode")
    if mode == "immediate":
        return TikTokScheduleIntent("immediate", None, SHANGHAI_NAME, None)
    if (
        mode != "platform_native"
        or type(prepared.get("scheduledAt")) is not str
        or prepared.get("scheduleTimezone") != SHANGHAI_NAME
    ):
        _fail("tiktok_schedule_invalid", "TikTok 排期快照无效")
    try:
        return parse_tiktok_schedule_fields(
            enable_timer=True,
            schedule_time=prepared["scheduledAt"],
            schedule_timezone=prepared["scheduleTimezone"],
            daily_times=[prepared["scheduledAt"][-5:]],
        )
    except TikTokScheduleContractError as exc:
        _fail(exc.error_code, exc.public_message)


def validate_tiktok_final_schedule_window(
    prepared: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    intent = _prepared_schedule_intent(prepared)
    try:
        validate_tiktok_schedule_window(
            intent,
            now=now or _shanghai_now(),
            minimum_lead=timedelta(minutes=15),
        )
    except TikTokScheduleContractError as exc:
        _fail(exc.error_code, exc.public_message)


def _validate_unsupported_settings(payload: Mapping[str, Any]) -> str:
    visibility_values: list[str] = []
    for mapping in _walk_tiktok_payload_mappings(payload, at_root=True):
        for raw_key, value in mapping.items():
            key = str(raw_key).strip().lower()
            if key == "visibility" and _is_nonempty(value):
                visibility_values.append(str(value).strip().lower())
            elif key in {
                "aigenerated",
                "aidisclosure",
                "containsaigeneratedcontent",
                "aideclarationexplicitlyconfirmed",
            }:
                if _ai_requested(value):
                    _fail(
                        "tiktok_unsupported_publish_setting",
                        "TikTok 首版不支持 AI 内容声明",
                    )
            elif key in {"coverpath", "coverpaths", "covers", "customcover"}:
                if _is_nonempty(value):
                    _fail(
                        "tiktok_unsupported_publish_setting",
                        "TikTok 首版不支持自定义封面",
                    )
            elif key in {
                "collection",
                "collectionid",
                "collectionname",
                "collections",
            }:
                if _is_nonempty(value):
                    _fail(
                        "tiktok_unsupported_publish_setting",
                        "TikTok 首版不支持合集",
                    )
            elif key in {"mention", "mentions", "mentionlist"}:
                if _is_nonempty(value):
                    _fail(
                        "tiktok_unsupported_publish_setting",
                        "TikTok 首版不支持提及",
                    )
    if not visibility_values:
        return "public"
    if any(value != "public" for value in visibility_values):
        _fail(
            "tiktok_unsupported_publish_setting",
            "TikTok 首版只支持公开发布",
        )
    return "public"


def _strict_integer(value: object) -> int:
    if type(value) is not int or int(value) <= 0:
        _fail("tiktok_account_invalid", "TikTok 账号记录无效")
    return int(value)


def _single_list(value: object, *, error_code: str, message: str) -> list[object]:
    if not isinstance(value, (list, tuple)) or len(value) != 1:
        _fail(error_code, message)
    return list(value)


def _nested_account_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    if "account" in payload:
        if not isinstance(payload["account"], Mapping):
            _fail("tiktok_account_invalid", "TikTok 账号记录无效")
        rows.append(payload["account"])
    if "accounts" in payload:
        raw_rows = _single_list(
            payload["accounts"],
            error_code="tiktok_account_invalid",
            message="TikTok 首版每次必须精确选择一个账号",
        )
        if not isinstance(raw_rows[0], Mapping):
            _fail("tiktok_account_invalid", "TikTok 账号记录无效")
        rows.append(raw_rows[0])
    return rows


def _controlled_targets(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    targets: list[Mapping[str, Any]] = []
    if "target" in payload:
        if not isinstance(payload["target"], Mapping):
            _fail("tiktok_account_invalid", "TikTok 发布目标无效")
        targets.append(payload["target"])
    if "targets" in payload:
        raw_targets = _single_list(
            payload["targets"],
            error_code="tiktok_account_invalid",
            message="TikTok 首版每次必须精确选择一个账号",
        )
        if not isinstance(raw_targets[0], Mapping):
            _fail("tiktok_account_invalid", "TikTok 发布目标无效")
        targets.append(raw_targets[0])
    for target in targets:
        if "platform" in target and str(target.get("platform") or "").strip().casefold() != "tiktok":
            _fail("tiktok_account_invalid", "TikTok 发布目标平台不一致")
    return targets


def _content_mappings(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    mappings: list[Mapping[str, Any]] = [payload]
    content = payload.get("content")
    if content is not None:
        if not isinstance(content, Mapping):
            _fail("tiktok_unsupported_publish_setting", "TikTok 内容字段无效")
        mappings.append(content)
    overrides = payload.get("platformOverrides")
    if overrides is not None:
        if not isinstance(overrides, Mapping):
            _fail("tiktok_unsupported_publish_setting", "TikTok 平台覆盖字段无效")
        for platform, override in overrides.items():
            if str(platform).strip().casefold() != "tiktok":
                continue
            if not isinstance(override, Mapping):
                _fail("tiktok_unsupported_publish_setting", "TikTok 平台覆盖字段无效")
            mappings.append(override)
    return mappings


def _read_account_record(account_id: int) -> Mapping[str, Any] | None:
    """用 SQLite 只读模式取一条账号，不运行 schema 迁移或状态提升。"""

    path = Path(DB_PATH).expanduser().resolve()
    if not path.is_file():
        return None
    connection = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            """
            SELECT id, type, status, COALESCE(authMode, 'browser') AS authMode,
                   filePath, accountReference
            FROM user_info
            WHERE id = ?
            LIMIT 1
            """,
            (int(account_id),),
        ).fetchone()
        return dict(row) if row is not None else None
    except (OSError, sqlite3.Error, ValueError):
        return None
    finally:
        if connection is not None:
            connection.close()


def _account_identity(payload: Mapping[str, Any]) -> tuple[int, Mapping[str, Any]]:
    id_groups: list[list[int]] = []
    if "accountIds" in payload:
        id_groups.append(
            [
                _strict_integer(item)
                for item in _single_list(
                    payload["accountIds"],
                    error_code="tiktok_account_invalid",
                    message="TikTok 首版每次必须精确选择一个账号",
                )
            ]
        )
    if "accountId" in payload:
        id_groups.append([_strict_integer(payload["accountId"])])
    nested_rows = _nested_account_rows(payload)
    controlled_targets = _controlled_targets(payload)
    for row in nested_rows:
        if "id" in row:
            id_groups.append([_strict_integer(row["id"])])
        elif "accountId" in row:
            id_groups.append([_strict_integer(row["accountId"])])
    for target in controlled_targets:
        if "accountId" not in target:
            _fail("tiktok_account_invalid", "TikTok 发布目标缺少账号")
        id_groups.append([_strict_integer(target["accountId"])])
    if not id_groups:
        _fail(
            "tiktok_account_invalid",
            "TikTok 首版每次必须精确选择一个账号",
        )
    ids = {item for group in id_groups for item in group}
    if len(ids) != 1:
        _fail(
            "tiktok_account_invalid",
            "TikTok 首版每次必须精确选择一个账号",
        )
    account_id = next(iter(ids))
    account = _read_account_record(account_id)
    if not isinstance(account, Mapping):
        _fail("tiktok_account_invalid", "TikTok 账号记录不存在")
    if (
        int(account.get("id") or 0) != account_id
        or int(account.get("type") or 0) != 6
        or int(account.get("status") or 0) != 1
        or str(account.get("authMode") or "browser") != "browser"
    ):
        _fail("tiktok_account_invalid", "TikTok 账号异常，请重新检测登录")
    expected_reference = normalize_tiktok_handle(account.get("accountReference"))
    if not expected_reference:
        _fail("tiktok_account_invalid", "TikTok 账号缺少稳定主体绑定")

    reference_values = [expected_reference]
    for key in ("accountReference", "tiktokExpectedAccountReference"):
        if key in payload:
            reference_values.append(normalize_tiktok_handle(payload.get(key)))
    for row in nested_rows:
        if "accountReference" in row:
            reference_values.append(normalize_tiktok_handle(row.get("accountReference")))
        for key, expected in (
            ("type", 6),
            ("status", 1),
            ("authMode", "browser"),
        ):
            if key in row and row.get(key) != expected:
                _fail("tiktok_account_invalid", "TikTok 账号记录无效")
    for target in controlled_targets:
        if "accountReference" in target:
            reference_values.append(
                normalize_tiktok_handle(target.get("accountReference"))
            )
        for key, expected in (
            ("type", 6),
            ("status", 1),
            ("authMode", "browser"),
        ):
            if key in target and target.get(key) != expected:
                _fail("tiktok_account_invalid", "TikTok 发布目标账号无效")
    if any(not value or value != expected_reference for value in reference_values):
        _fail("tiktok_account_invalid", "TikTok 账号主体绑定不一致")
    return account_id, account


def _valid_storage_cookie(cookie: object) -> bool:
    if not isinstance(cookie, Mapping):
        return False
    required = {"name", "value", "domain", "path"}
    optional = {"expires", "httpOnly", "secure", "sameSite", "partitionKey"}
    if not required.issubset(cookie) or set(cookie) - required - optional:
        return False
    if any(type(cookie[key]) is not str for key in required):
        return False
    if not cookie["name"] or not cookie["domain"] or not cookie["path"]:
        return False
    if "expires" in cookie:
        try:
            valid_expires = type(cookie["expires"]) in {int, float} and math.isfinite(
                cookie["expires"]
            )
        except (OverflowError, TypeError, ValueError):
            valid_expires = False
        if not valid_expires:
            return False
    if "httpOnly" in cookie and type(cookie["httpOnly"]) is not bool:
        return False
    if "secure" in cookie and type(cookie["secure"]) is not bool:
        return False
    if "sameSite" in cookie:
        if type(cookie["sameSite"]) is not str or cookie["sameSite"] not in {
            "Strict",
            "Lax",
            "None",
        }:
            return False
    if "partitionKey" in cookie and type(cookie["partitionKey"]) is not str:
        return False
    return True


def _valid_storage_origin(origin_row: object) -> bool:
    if not isinstance(origin_row, Mapping) or set(origin_row) != {
        "origin",
        "localStorage",
    }:
        return False
    origin = origin_row["origin"]
    local_storage = origin_row["localStorage"]
    if type(origin) is not str or type(local_storage) is not list:
        return False
    try:
        parsed = urlsplit(origin)
        parsed.port
        valid_origin = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False
    if not valid_origin:
        return False
    return all(
        isinstance(item, Mapping)
        and set(item) == {"name", "value"}
        and type(item["name"]) is str
        and type(item["value"]) is str
        for item in local_storage
    )


def _session_path(payload: Mapping[str, Any], account: Mapping[str, Any]) -> Path:
    if "accountList" not in payload:
        _fail(
            "tiktok_account_invalid",
            "TikTok 首版每次必须精确选择一个账号",
        )
    listed = _single_list(
        payload["accountList"],
        error_code="tiktok_account_invalid",
        message="TikTok 首版每次必须精确选择一个账号",
    )
    values = [str(listed[0] or "").strip(), str(account.get("filePath") or "").strip()]
    for key in ("accountFile", "sessionFile"):
        if key in payload:
            values.append(str(payload.get(key) or "").strip())
    for mapping in [*_nested_account_rows(payload), *_controlled_targets(payload)]:
        if "accountList" in mapping:
            nested_list = _single_list(
                mapping["accountList"],
                error_code="tiktok_account_invalid",
                message="TikTok 首版每次必须精确选择一个账号",
            )
            values.append(str(nested_list[0] or "").strip())
        for key in ("accountFile", "sessionFile", "filePath"):
            if key in mapping:
                values.append(str(mapping.get(key) or "").strip())
    if not values[0] or len(set(values)) != 1:
        _fail("tiktok_account_invalid", "TikTok 账号会话引用不一致")

    relative = Path(values[0])
    if (
        relative.is_absolute()
        or relative.name != values[0]
        or "\\" in values[0]
        or relative.suffix.lower() != ".json"
    ):
        _fail("tiktok_account_invalid", "TikTok 账号会话引用无效")
    root = COOKIE_DIR.resolve()
    unresolved = root / relative
    if unresolved.is_symlink():
        _fail("tiktok_account_invalid", "TikTok 账号会话引用无效")
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        _fail("tiktok_account_invalid", "TikTok 账号会话引用无效")
    if not resolved.is_file():
        _fail("tiktok_session_missing", "TikTok 本地登录会话不存在")
    try:
        session = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail("tiktok_account_invalid", "TikTok 本地登录会话无法安全解析")
    if not isinstance(session, Mapping):
        _fail("tiktok_account_invalid", "TikTok 本地登录会话格式无效")
    cookies = session.get("cookies")
    origins = session.get("origins")
    if (
        type(cookies) is not list
        or type(origins) is not list
        or not all(_valid_storage_cookie(item) for item in cookies)
        or not all(_valid_storage_origin(item) for item in origins)
    ):
        _fail("tiktok_account_invalid", "TikTok 本地登录会话格式无效")
    return resolved


def _raw_video_value(value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("path")
    if not isinstance(value, (str, Path)) or not str(value).strip():
        _fail("tiktok_video_file_invalid", "TikTok 视频素材无效")
    return str(value).strip()


def _video_path(payload: Mapping[str, Any]) -> Path:
    if "fileList" not in payload:
        _fail("tiktok_video_file_invalid", "TikTok 首版每次必须精确选择一条视频")
    primary = _single_list(
        payload["fileList"],
        error_code="tiktok_video_file_invalid",
        message="TikTok 首版每次必须精确选择一条视频",
    )
    primary_value = _raw_video_value(primary[0])
    alias_values: list[str] = []
    for key in _VIDEO_LIST_KEYS - {"fileList"}:
        if key not in payload:
            continue
        values = _single_list(
            payload[key],
            error_code="tiktok_unsupported_publish_setting",
            message="TikTok 视频素材字段冲突",
        )
        alias_values.append(_raw_video_value(values[0]))
    if "videoPath" in payload:
        alias_values.append(_raw_video_value(payload["videoPath"]))
    for content in _content_mappings(payload)[1:]:
        for key in _VIDEO_LIST_KEYS:
            if key not in content:
                continue
            values = _single_list(
                content[key],
                error_code="tiktok_unsupported_publish_setting",
                message="TikTok 视频素材字段冲突",
            )
            alias_values.append(_raw_video_value(values[0]))
        if "videoPath" in content:
            alias_values.append(_raw_video_value(content["videoPath"]))
    if any(value != primary_value for value in alias_values):
        _fail("tiktok_unsupported_publish_setting", "TikTok 视频素材字段冲突")

    raw_path = Path(primary_value).expanduser()
    if raw_path.is_symlink():
        _fail("tiktok_video_file_invalid", "TikTok 视频必须是本地普通文件")
    resolved = raw_path.resolve()
    if not resolved.is_file() or resolved.suffix.lower() not in TIKTOK_VIDEO_SUFFIXES:
        _fail("tiktok_video_file_invalid", "TikTok 视频不存在或格式不支持")
    return resolved


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFKC", value).strip()


def _one_text(payload: Mapping[str, Any], keys: frozenset[str], *, label: str) -> str:
    values: list[str] = []
    for mapping in _content_mappings(payload):
        for key in keys:
            if key in mapping:
                values.append(_clean_text(mapping.get(key)))
    if not values or not values[0]:
        _fail("tiktok_unsupported_publish_setting", f"TikTok {label}不能为空")
    if any(value != values[0] for value in values):
        _fail("tiktok_unsupported_publish_setting", f"TikTok {label}字段冲突")
    return values[0]


def _normalize_topic(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = normalized.lstrip("#").strip()
    return "".join(normalized.split())


def _topics(payload: Mapping[str, Any]) -> list[str]:
    topic_lists: list[list[str]] = []
    for mapping in _content_mappings(payload):
        for key in _TOPIC_KEYS:
            if key not in mapping:
                continue
            raw = mapping.get(key)
            if not isinstance(raw, (list, tuple)) or not all(
                isinstance(item, str) for item in raw
            ):
                _fail("tiktok_unsupported_publish_setting", "TikTok 话题必须是字符串列表")
            normalized = [_normalize_topic(item) for item in raw]
            if any(not item or "#" in item or "@" in item for item in normalized):
                _fail("tiktok_unsupported_publish_setting", "TikTok 话题格式无效")
            topic_lists.append(normalized)
    if not topic_lists:
        return []
    if any(items != topic_lists[0] for items in topic_lists):
        _fail("tiktok_unsupported_publish_setting", "TikTok 话题字段冲突")
    topics = topic_lists[0]
    seen: set[str] = set()
    for topic in topics:
        key = topic.casefold()
        if key in seen:
            _fail("tiktok_unsupported_publish_setting", "TikTok 话题归一化后重复")
        seen.add(key)
    return topics


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        _fail("tiktok_video_file_invalid", "TikTok 视频素材无法安全读取")
    return digest.hexdigest()


def compose_tiktok_caption(title: str, body: str, topics: Iterable[str]) -> str:
    """Compose the one canonical string used for limits and fingerprints."""

    plain_caption = f"{title}\n\n{body}"
    topic_suffix = " ".join(f"#{topic}" for topic in topics)
    return f"{plain_caption} {topic_suffix}" if topic_suffix else plain_caption


def validate_tiktok_payload(
    payload: Mapping[str, Any],
    *,
    mode: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """规范化 TikTok 输入，只读本地资料，不启动平台。"""

    if not isinstance(payload, Mapping) or mode not in TIKTOK_MODES:
        _fail("tiktok_unsupported_publish_setting", "TikTok 执行模式无效")
    _validate_sensitive_alias_locations(payload)
    platform_types: list[int] = []
    for key in ("type", "platformType"):
        if key in payload:
            try:
                platform_types.append(int(payload[key]))
            except (TypeError, ValueError):
                _fail("tiktok_unsupported_publish_setting", "TikTok 平台类型无效")
    if not platform_types or any(value != 6 for value in platform_types):
        _fail("tiktok_unsupported_publish_setting", "当前载荷不是 TikTok 视频")
    if "platform" in payload and str(payload.get("platform") or "").strip().lower() != "tiktok":
        _fail("tiktok_unsupported_publish_setting", "当前载荷不是 TikTok 视频")
    content_types = [
        str(mapping.get("contentType") or "").strip().lower()
        for mapping in (payload, payload.get("content"))
        if isinstance(mapping, Mapping) and "contentType" in mapping
    ]
    if not content_types or any(value != "video" for value in content_types):
        _fail(
            "tiktok_unsupported_publish_setting",
            "TikTok 首版只验收单视频通道",
        )
    if "mode" in payload and str(payload.get("mode") or "") != mode:
        _fail("tiktok_unsupported_publish_setting", "TikTok 执行模式无效")
    if str(payload.get("runtimeMode") or "") != _RUNTIME_MODE_BY_MODE[mode]:
        _fail("tiktok_unsupported_publish_setting", "TikTok 执行模式无效")
    expected_dry_run = mode == "preflight"
    if payload.get("debugDryRun") is not expected_dry_run:
        message = (
            "TikTok 预发布检查必须保持 debugDryRun=true"
            if mode == "preflight"
            else "TikTok 执行模式与本地预检标记不一致"
        )
        _fail("tiktok_unsupported_publish_setting", message)

    schedule_intent = _validate_schedule_fields(payload)
    try:
        validate_tiktok_schedule_window(
            schedule_intent,
            now=now or _shanghai_now(),
            minimum_lead=(
                timedelta(minutes=30)
                if mode == "preflight"
                else timedelta(minutes=15)
            ),
        )
    except TikTokScheduleContractError as exc:
        _fail(exc.error_code, exc.public_message)
    visibility = _validate_unsupported_settings(payload)
    account_id, account = _account_identity(payload)
    session_path = _session_path(payload, account)
    video_path = _video_path(payload)
    current_video_sha256 = _sha256_file(video_path)
    authorized_video_sha256 = payload.get("tiktokVideoSha256")
    if payload.get("tiktokControlledPublish") is True:
        if (
            type(authorized_video_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", authorized_video_sha256) is None
            or authorized_video_sha256 != current_video_sha256
        ):
            _fail(
                "tiktok_video_snapshot_mismatch",
                "TikTok 视频素材与已授权快照不一致",
            )
    title = _one_text(payload, frozenset({"title"}), label="标题")
    body = _one_text(
        payload,
        frozenset({"description", "body"}),
        label="正文",
    )
    if "@" in title or "@" in body:
        _fail(
            "tiktok_unsupported_publish_setting",
            "TikTok 标题或正文包含原始 @ 文字，首版不支持提及",
        )
    topics = _topics(payload)
    plain_caption = f"{title}\n\n{body}"
    combined = compose_tiktok_caption(title, body, topics)
    if len(combined) > TIKTOK_CONTENT_LIMIT:
        _fail(
            "tiktok_content_too_long",
            "TikTok 标题、正文与话题组合后超过 2200 字符，未做截断",
        )

    return {
        "accountId": account_id,
        "accountFile": str(session_path),
        "expectedAccountReference": normalize_tiktok_handle(
            account.get("accountReference")
        ),
        "videoPath": str(video_path),
        "videoSha256": (
            str(authorized_video_sha256)
            if payload.get("tiktokControlledPublish") is True
            else current_video_sha256
        ),
        "title": title,
        "body": body,
        "topics": topics,
        "plainCaption": plain_caption,
        "textSha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        "visibility": visibility,
        "mode": mode,
        "scheduleMode": schedule_intent.mode,
        "scheduledAt": schedule_intent.local_time,
        "scheduleTimezone": schedule_intent.timezone,
    }


def run_tiktok_local_preflight(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """运行零平台写入的 TikTok 本地预检。"""

    prepared = validate_tiktok_payload(payload, mode="preflight", now=now)
    return {
        "type": 6,
        "platform": "TikTok",
        "ok": True,
        "phase": "local_preflight_passed",
        "message": "TikTok 本地预检通过；未打开平台、未上传视频",
        "snapshot": {
            "accountId": prepared["accountId"],
            "videoSha256": prepared["videoSha256"],
            "textSha256": prepared["textSha256"],
            "topics": list(prepared["topics"]),
            "visibility": prepared["visibility"],
            "mode": prepared["mode"],
            "scheduleMode": prepared["scheduleMode"],
            "scheduledAt": prepared["scheduledAt"],
            "scheduleTimezone": prepared["scheduleTimezone"],
        },
        "receipt": {
            "accountId": prepared["accountId"],
            "visibility": prepared["visibility"],
            "platformWriteOccurred": False,
            "finalActionTriggered": False,
            "contentId": None,
            "contentUrl": None,
            "publishedAt": None,
            "scheduleMode": prepared["scheduleMode"],
            "scheduledAt": prepared["scheduledAt"],
            "scheduleTimezone": prepared["scheduleTimezone"],
        },
    }


def _record_tiktok_event(
    task_id: int,
    event_type: str,
    message: str,
    *,
    level: str = "info",
) -> None:
    task_service.record_task_event(
        int(task_id),
        str(event_type),
        str(message),
        level=level,
    )


def _load_tiktok_uploader_class():
    uploader_class = TiktokVideo
    if uploader_class is not None:
        return uploader_class
    module = importlib.import_module("uploader.tk_uploader.main")
    return module.TiktokVideo


def _load_async_playwright_factory():
    factory = async_playwright
    if factory is not None:
        return factory
    module = importlib.import_module("playwright.async_api")
    return module.async_playwright


def _require_current_account_snapshot(prepared: Mapping[str, Any]) -> dict[str, Any]:
    account = _read_account_record(int(prepared["accountId"]))
    if not isinstance(account, Mapping):
        _fail("tiktok_account_invalid", "TikTok 账号记录已变更")
    expected_file = Path(str(prepared["accountFile"])).name
    if (
        int(account.get("id") or 0) != int(prepared["accountId"])
        or int(account.get("type") or 0) != 6
        or int(account.get("status") or 0) != 1
        or str(account.get("authMode") or "browser") != "browser"
        or Path(str(account.get("filePath") or "")).name != expected_file
        or normalize_tiktok_handle(account.get("accountReference"))
        != str(prepared["expectedAccountReference"])
    ):
        _fail("tiktok_account_invalid", "TikTok 账号记录已变更")
    return dict(account)


def _validate_live_identity(account: Mapping[str, Any], identity) -> None:
    try:
        validate_identity_binding(
            account,
            identity,
            allow_initial_bind=False,
        )
    except TikTokIdentityError as exc:
        raise TikTokPublishError(exc.error_code, exc.public_message) from exc


async def _wait_before_identity_read(uploader, page) -> None:
    waiter = getattr(uploader, "_wait_for_manual_intervention", None)
    if not callable(waiter):
        _fail(
            "tiktok_platform_execution_failed",
            "TikTok 平台执行在最终动作前失败，未点击 Post",
        )
    await waiter(page)


def _log_internal_failure(stage: str, exc: BaseException) -> None:
    """只在本地记录受控的异常类型，不记录异常文本。"""

    try:
        tiktok_logger.error(
            f"[tiktok-controlled] {stage} failed ({type(exc).__name__})"
        )
    except Exception:
        # Logging must never replace the fixed public failure boundary.
        return


def _verify_authorized_form_snapshot(
    prepared: Mapping[str, Any],
    form_receipt: object,
) -> dict[str, Any]:
    if not isinstance(form_receipt, Mapping):
        _fail("tiktok_form_snapshot_mismatch", "TikTok 表单快照无法安全核对")
    expected_caption = compose_tiktok_caption(
        str(prepared["title"]),
        str(prepared["body"]),
        prepared["topics"],
    )
    expected = {
        "plainCaption": str(prepared["plainCaption"]),
        "topicEntities": list(prepared["topics"]),
        "visibility": "public",
        "finalCaption": expected_caption,
        "finalActionReady": True,
    }
    if prepared.get("scheduleMode") == "platform_native":
        expected.update(
            {
                "scheduleMode": "platform_native",
                "scheduledAt": prepared.get("scheduledAt"),
                "scheduleTimezone": "Asia/Shanghai",
                "scheduleToggleEnabled": True,
                "finalActionLabel": "Schedule",
            }
        )
    else:
        expected["finalActionLabel"] = "Post"
    actual = {
        "plainCaption": form_receipt.get("plainCaption"),
        "topicEntities": form_receipt.get("topicEntities"),
        "visibility": form_receipt.get("visibility"),
        "finalCaption": form_receipt.get("finalCaption"),
        "finalActionReady": form_receipt.get("finalActionReady"),
    }
    if prepared.get("scheduleMode") == "platform_native":
        actual.update(
            {
                "scheduleMode": form_receipt.get("scheduleMode"),
                "scheduledAt": form_receipt.get("scheduledAt"),
                "scheduleTimezone": form_receipt.get("scheduleTimezone"),
                "scheduleToggleEnabled": form_receipt.get(
                    "scheduleToggleEnabled"
                ),
                "finalActionLabel": form_receipt.get("finalActionLabel"),
            }
        )
    else:
        actual["finalActionLabel"] = form_receipt.get(
            "finalActionLabel", "Post"
        )
    if actual != expected:
        _fail("tiktok_form_snapshot_mismatch", "TikTok 表单快照与授权内容不一致")
    if hashlib.sha256(expected_caption.encode("utf-8")).hexdigest() != str(
        prepared["textSha256"]
    ):
        _fail("tiktok_form_snapshot_mismatch", "TikTok 表单快照与授权内容不一致")
    if _sha256_file(Path(str(prepared["videoPath"]))) != str(
        prepared["videoSha256"]
    ):
        _fail("tiktok_form_snapshot_mismatch", "TikTok 视频在最终动作前已发生变化")
    return expected


async def _tiktok_verification_reason(page) -> str | None:
    module = importlib.import_module("uploader.tk_uploader.main")
    return module.tiktok_security_intervention_reason(
        str(getattr(page, "url", "") or ""),
        await module._body_text(page),
    )


def _instrument_manual_verification(uploader, *, task_id: int) -> None:
    original = getattr(uploader, "_wait_for_manual_intervention", None)
    if not callable(original):
        return

    async def tracked(page):
        reason = await _tiktok_verification_reason(page)
        if reason:
            _record_tiktok_event(
                task_id,
                "tiktok_waiting_user_verification",
                "TikTok 正在同一浏览器等待用户完成安全验证",
                level="warning",
            )
        result = await original(page)
        if reason:
            _record_tiktok_event(
                task_id,
                "tiktok_user_verification_resolved",
                "TikTok 用户安全验证已完成，继续同一页面",
            )
        return result

    uploader._wait_for_manual_intervention = tracked


class _FinalActionButton:
    def __init__(self, button, trigger) -> None:
        self._button = button
        self._trigger = trigger

    async def click(self, *args, **kwargs):
        self._trigger()
        return await self._button.click(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._button, name)


def _instrument_final_action(uploader, *, trigger) -> bool:
    original = getattr(uploader, "_final_action_button", None)
    if not callable(original):
        return False

    async def tracked(base):
        button = await original(base)
        return None if button is None else _FinalActionButton(button, trigger)

    uploader._final_action_button = tracked
    return True


def _is_explicit_platform_rejection(exc: BaseException) -> bool:
    if isinstance(exc, TikTokPublishError):
        return exc.error_code == "tiktok_publish_rejected"
    return str(exc) == "TikTok 页面提示最终发布失败"


def _valid_published_at(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _exact_content_readback(
    result: Mapping[str, Any],
    *,
    expected_handle: str,
) -> dict[str, str] | None:
    nested = result.get("receipt")
    source = nested if isinstance(nested, Mapping) else result
    content_id = source.get("contentId")
    content_url = source.get("contentUrl")
    published_at = source.get("publishedAt")
    if content_id is None and content_url is None and published_at is None:
        return None
    if (
        type(content_id) is not str
        or not content_id.isdigit()
        or type(content_url) is not str
        or not _valid_published_at(published_at)
    ):
        return None
    try:
        parsed = urlsplit(content_url)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or (parsed.hostname or "").casefold() not in {"tiktok.com", "www.tiktok.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/")
        != f"/@{expected_handle}/video/{content_id}"
    ):
        return None
    return {
        "contentId": content_id,
        "contentUrl": content_url,
        "publishedAt": str(published_at),
    }


def _exact_scheduled_readback(
    result: Mapping[str, Any],
    *,
    expected_scheduled_at: str,
) -> dict[str, Any] | None:
    source = (
        result.get("receipt")
        if isinstance(result.get("receipt"), Mapping)
        else result
    )
    if (
        result.get("status") != "scheduled"
        or result.get("phase") != "scheduled_readback_confirmed"
        or source.get("scheduleMode") != "platform_native"
        or source.get("scheduledAt") != expected_scheduled_at
        or source.get("scheduleTimezone") != "Asia/Shanghai"
        or source.get("platformAccepted") is not True
        or source.get("scheduledReadbackConfirmed") is not True
        or source.get("publishedAt") is not None
    ):
        return None
    return {
        "contentId": source.get("contentId"),
        "contentUrl": source.get("contentUrl"),
        "scheduleMode": "platform_native",
        "scheduledAt": expected_scheduled_at,
        "scheduleTimezone": "Asia/Shanghai",
        "platformAccepted": True,
        "scheduledReadbackConfirmed": True,
        "publishedAt": None,
    }


def _platform_result_is_accepted(result: object) -> bool:
    if not isinstance(result, Mapping) or result.get("status") != "published":
        return False
    return result.get("evidence") in _TIKTOK_ACCEPTED_EVIDENCE


async def _run_tiktok_platform(
    prepared: Mapping[str, Any],
    *,
    mode: str,
    task_id: int,
) -> dict[str, Any]:
    playwright_manager = _load_async_playwright_factory()()
    playwright = None
    browser = None
    context = None
    identity_page = None
    page = None
    final_state = {
        "triggered": False,
        "action": (
            "schedule"
            if prepared.get("scheduleMode") == "platform_native"
            else "post"
        ),
    }
    platform_write_occurred = False
    completed_successfully = False
    schedule_checkpoint_state = {"accepted": False}

    def trigger_final_action() -> None:
        if final_state["triggered"]:
            return
        validate_tiktok_final_schedule_window(
            prepared,
            now=_shanghai_now(),
        )
        _record_tiktok_event(
            task_id,
            "tiktok_final_action_triggered",
            "TikTok 最终动作即将执行，已禁止自动重试",
        )
        final_state["triggered"] = True

    async def refresh_isolated_session() -> None:
        _require_current_account_snapshot(prepared)
        raw_state = await context.storage_state()
        _require_current_account_snapshot(prepared)
        replace_tiktok_storage_state_file(
            Path(str(prepared["accountFile"])),
            raw_state,
        )

    try:
        playwright = await playwright_manager.start()
        browser = await launch_publish_browser(playwright)
        try:
            storage_state = load_sanitized_tiktok_storage_state_file(
                Path(str(prepared["accountFile"]))
            )
        except TikTokSessionScopeError as exc:
            raise TikTokPublishError(exc.error_code, exc.public_message) from None
        context = await new_publish_context(
            browser,
            storage_state=storage_state,
        )
        context = await set_init_script(context)
        identity_page = await context.new_page()
        identity_probe_url = (
            "https://www.tiktok.com/@"
            f"{str(prepared['expectedAccountReference'])}"
        )
        await identity_page.goto(
            identity_probe_url,
            wait_until="domcontentloaded",
            timeout=TIKTOK_NAVIGATION_TIMEOUT_MS,
        )

        uploader_class = _load_tiktok_uploader_class()
        uploader = uploader_class(
            str(prepared["title"]),
            str(prepared["videoPath"]),
            list(prepared["topics"]),
            prepared.get("scheduledAt"),
            str(prepared["accountFile"]),
            description=str(prepared["body"]),
            schedule_timezone=str(prepared["scheduleTimezone"]),
            dry_run=mode == "platform_form_check",
            dry_run_hold_browser=False,
            expected_account_reference=str(prepared["expectedAccountReference"]),
            execution_mode=mode,
        )
        form_stage_messages = {
            "upload_entry_waiting": "TikTok 正在等待视频选择入口",
            "video_selected": "TikTok 已选择视频文件",
            "caption_editor_waiting": "TikTok 正在等待文案编辑区域",
            "caption_editor_ready": "TikTok 文案编辑区域已就绪",
            "caption_write_started": "TikTok 正在写入文案",
            "caption_write_verified": "TikTok 文案写入已回读",
            "topics_started": "TikTok 正在核对官方话题",
            "topics_verified": "TikTok 官方话题已回读",
            "visibility_started": "TikTok 正在核对公开范围",
            "visibility_verified": "TikTok 公开范围已回读",
            "post_ready_waiting": "TikTok 正在等待最终按钮就绪",
            "form_snapshot_started": "TikTok 正在执行最终表单快照核对",
            "form_snapshot_verified": "TikTok 最终表单快照已核对",
        }

        def observe_form_stage(stage: str) -> None:
            message = form_stage_messages.get(str(stage))
            if message is None:
                return
            _record_tiktok_event(
                task_id,
                f"tiktok_form_{stage}",
                message,
            )

        uploader.form_stage_observer = observe_form_stage

        uploader.authorized_snapshot_validator = (
            lambda live: _verify_authorized_form_snapshot(prepared, live)
        )

        def persist_schedule_checkpoint(stage: str) -> None:
            if stage != "scheduled_accepted":
                raise TikTokPublishError(
                    "tiktok_schedule_outcome_unknown",
                    "TikTok 排期检查点无效",
                    outcome_ambiguous=True,
                )
            _record_tiktok_event(
                task_id,
                "tiktok_scheduled_accepted",
                "TikTok 已明确受理排期，正在只读核对内容列表",
            )
            schedule_checkpoint_state["accepted"] = True

        uploader.schedule_checkpoint_observer = persist_schedule_checkpoint
        _instrument_manual_verification(uploader, task_id=task_id)
        final_button_instrumented = _instrument_final_action(
            uploader,
            trigger=trigger_final_action,
        )
        if not final_button_instrumented:
            _fail(
                "tiktok_platform_execution_failed",
                "TikTok 最终动作无法安全监测，已停止在点击前",
            )

        await _wait_before_identity_read(uploader, identity_page)
        account_snapshot = _require_current_account_snapshot(prepared)
        identity = await read_tiktok_signed_in_navigation_identity(identity_page)
        _validate_live_identity(account_snapshot, identity)

        page = await context.new_page()
        await page.goto(
            TIKTOK_UPLOAD_URL,
            wait_until="domcontentloaded",
            timeout=TIKTOK_NAVIGATION_TIMEOUT_MS,
        )
        await page.wait_for_timeout(2_500)

        _record_tiktok_event(
            task_id,
            "tiktok_platform_form_started",
            "TikTok 开始上传并核对发布表单",
        )
        base = await uploader._base(page)
        platform_write_occurred = True
        form_receipt = await uploader.prepare_form(page, base)
        verified_form = _verify_authorized_form_snapshot(prepared, form_receipt)

        await identity_page.goto(
            identity_probe_url,
            wait_until="domcontentloaded",
            timeout=TIKTOK_NAVIGATION_TIMEOUT_MS,
        )
        await _wait_before_identity_read(uploader, identity_page)
        current_account = _require_current_account_snapshot(prepared)
        current_identity = await read_tiktok_signed_in_navigation_identity(
            identity_page
        )
        _validate_live_identity(current_account, current_identity)
        _record_tiktok_event(
            task_id,
            "tiktok_platform_form_verified",
            "TikTok 表单、账号和公开设置已精确回读",
        )

        receipt: dict[str, Any] = {
            "accountId": int(prepared["accountId"]),
            "visibility": "public",
            "mode": mode,
            "platformWriteOccurred": True,
            "finalActionTriggered": False,
            "contentId": None,
            "contentUrl": None,
            "publishedAt": None,
            "topicEntities": list(verified_form["topicEntities"]),
            "scheduleMode": prepared["scheduleMode"],
            "scheduledAt": prepared["scheduledAt"],
            "scheduleTimezone": prepared["scheduleTimezone"],
            "scheduleToggleEnabled": verified_form.get(
                "scheduleToggleEnabled", False
            ),
            "finalActionLabel": verified_form["finalActionLabel"],
            "finalActionReady": verified_form["finalActionReady"],
            "finalAction": final_state["action"],
        }
        if mode == "platform_form_check":
            receipt["phase"] = "platform_form_verified"
            await refresh_isolated_session()
            completed_successfully = True
            return {
                "type": 6,
                "platform": "TikTok",
                "ok": True,
                "phase": "platform_form_verified",
                "message": "TikTok 表单检查通过；未点击 Post",
                "receipt": receipt,
            }

        uploader.publish_confirmed = True
        submitted = await uploader.submit_once(page, base)
        if not final_state["triggered"]:
            _fail(
                "tiktok_publish_outcome_unknown",
                "TikTok 最终动作没有可持久化证据",
            )
        receipt["finalActionTriggered"] = True
        receipt["phase"] = "platform_accepted"
        if prepared["scheduleMode"] == "platform_native":
            if not schedule_checkpoint_state["accepted"]:
                raise TikTokPublishError(
                    "tiktok_schedule_outcome_unknown",
                    "TikTok 排期缺少已持久化的受理检查点",
                    outcome_ambiguous=True,
                    receipt=receipt,
                )
            readback = _exact_scheduled_readback(
                submitted,
                expected_scheduled_at=str(prepared["scheduledAt"]),
            )
            if readback is None:
                raise TikTokPublishError(
                    "tiktok_schedule_outcome_unknown",
                    "TikTok 排期受理后未能唯一回读内容列表",
                    outcome_ambiguous=True,
                    receipt=receipt,
                )
            phase = "scheduled_readback_confirmed"
            event_type = "tiktok_scheduled_readback_confirmed"
            message = "TikTok 已精确回读同一排期内容"
            receipt.update(readback)
            receipt["phase"] = phase
            _record_tiktok_event(task_id, event_type, message)
            await refresh_isolated_session()
            completed_successfully = True
            return {
                "type": 6,
                "platform": "TikTok",
                "ok": True,
                "phase": phase,
                "message": message,
                "receipt": receipt,
            }
        readback = _exact_content_readback(
            submitted,
            expected_handle=str(prepared["expectedAccountReference"]),
        )
        if not _platform_result_is_accepted(submitted) and readback is None:
            raise TikTokPublishError(
                "tiktok_publish_outcome_unknown",
                "TikTok 最终动作后的平台结果无法确认，请人工核对内容列表",
                outcome_ambiguous=True,
                receipt=receipt,
            )

        if readback is None:
            phase = "platform_accepted"
            event_type = "tiktok_platform_accepted"
            message = "TikTok 已返回明确受理反馈，尚无精确内容回读"
        else:
            phase = "published_readback_confirmed"
            event_type = "tiktok_published_readback_confirmed"
            message = "TikTok 已精确回读同一公开内容"
            receipt.update(readback)
        receipt["phase"] = phase
        _record_tiktok_event(task_id, event_type, message)
        await refresh_isolated_session()
        completed_successfully = True
        return {
            "type": 6,
            "platform": "TikTok",
            "ok": True,
            "phase": phase,
            "message": message,
            "receipt": receipt,
        }
    except Exception as exc:
        if final_state["triggered"]:
            receipt = {
                "accountId": int(prepared["accountId"]),
                "visibility": "public",
                "mode": "formal",
                "phase": "ambiguous",
                "platformWriteOccurred": True,
                "finalActionTriggered": True,
                "contentId": None,
                "contentUrl": None,
                "publishedAt": None,
                "scheduleMode": prepared["scheduleMode"],
                "scheduleTimezone": prepared["scheduleTimezone"],
            }
            if prepared.get("scheduledAt") is not None:
                receipt["scheduledAt"] = prepared["scheduledAt"]
            if schedule_checkpoint_state["accepted"]:
                receipt["platformAccepted"] = True
            if _is_explicit_platform_rejection(exc):
                receipt["phase"] = "final_action_triggered"
                _record_tiktok_event(
                    task_id,
                    "tiktok_publish_rejected",
                    "TikTok 已明确拒绝本次发布",
                    level="error",
                )
                raise TikTokPublishError(
                    "tiktok_publish_rejected",
                    "TikTok 页面明确提示发布失败",
                    receipt=receipt,
                ) from None
            try:
                _record_tiktok_event(
                    task_id,
                    "tiktok_publish_outcome_ambiguous",
                    "TikTok 最终动作后的结果不明，禁止自动重试",
                    level="error",
                )
            except Exception:
                pass
            if isinstance(exc, TikTokPublishError) and exc.outcome_ambiguous:
                merged_receipt = dict(receipt)
                merged_receipt.update(exc.receipt)
                merged_receipt.update(
                    {
                        "phase": "ambiguous",
                        "finalActionTriggered": True,
                        "scheduleMode": prepared["scheduleMode"],
                        "scheduleTimezone": prepared["scheduleTimezone"],
                    }
                )
                if prepared.get("scheduledAt") is not None:
                    merged_receipt["scheduledAt"] = prepared["scheduledAt"]
                merged_receipt.pop("platformAccepted", None)
                if schedule_checkpoint_state["accepted"]:
                    merged_receipt["platformAccepted"] = True
                raise TikTokPublishError(
                    exc.error_code,
                    exc.public_message,
                    outcome_ambiguous=True,
                    receipt=merged_receipt,
                ) from None
            _log_internal_failure("after-final-action", exc)
            scheduled_action = prepared["scheduleMode"] == "platform_native"
            raise TikTokPublishError(
                (
                    "tiktok_schedule_outcome_unknown"
                    if scheduled_action
                    else "tiktok_publish_outcome_unknown"
                ),
                (
                    "TikTok 定时最终动作后的结果无法确认，请人工核对内容列表"
                    if scheduled_action
                    else "TikTok 最终动作后的平台结果无法确认，请人工核对内容列表"
                ),
                outcome_ambiguous=True,
                receipt=receipt,
            ) from None
        if isinstance(exc, TikTokPublishError):
            raise
        if isinstance(exc, TikTokIdentityError):
            raise TikTokPublishError(exc.error_code, exc.public_message) from None
        _log_internal_failure("before-final-action", exc)
        raise TikTokPublishError(
            "tiktok_platform_execution_failed",
            "TikTok 平台执行在最终动作前失败，未点击 Post",
            receipt={
                "accountId": int(prepared["accountId"]),
                "visibility": "public",
                "mode": mode,
                "phase": "failed_before_final_action",
                "platformWriteOccurred": platform_write_occurred,
                "finalActionTriggered": False,
                "contentId": None,
                "contentUrl": None,
                "publishedAt": None,
            },
        ) from None
    finally:
        cleanup_failures: list[tuple[str, BaseException]] = []
        for label, resource in (
            ("identity-page-close", identity_page),
            ("page-close", page),
            ("context-close", context),
            ("browser-close", browser),
        ):
            if resource is None:
                continue
            try:
                await resource.close()
            except Exception as exc:
                cleanup_failures.append((label, exc))
                _log_internal_failure(label, exc)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as exc:
                cleanup_failures.append(("playwright-stop", exc))
                _log_internal_failure("playwright-stop", exc)
        if (
            cleanup_failures
            and completed_successfully
            and mode == "platform_form_check"
        ):
            raise TikTokPublishError(
                "tiktok_cleanup_failed",
                "TikTok 浏览器资源未能确认关闭，任务已安全停止",
                receipt={
                    "accountId": int(prepared["accountId"]),
                    "visibility": "public",
                    "mode": mode,
                    "phase": "failed_before_final_action",
                    "platformWriteOccurred": platform_write_occurred,
                    "finalActionTriggered": False,
                    "contentId": None,
                    "contentUrl": None,
                    "publishedAt": None,
                },
            )


def run_tiktok_platform_sync(
    payload: Mapping[str, Any],
    *,
    mode: str,
    task_id: int,
) -> dict[str, Any]:
    """Run form check or formal posting in one isolated page/context."""

    if mode not in {"platform_form_check", "formal"}:
        _fail("tiktok_unsupported_publish_setting", "TikTok 平台执行模式无效")
    if type(task_id) is not int or task_id <= 0:
        _fail("tiktok_unsupported_publish_setting", "TikTok 任务标识无效")
    try:
        prepared = validate_tiktok_payload(payload, mode=mode)
        with publish_context(mode=mode, background_mode=False):
            return asyncio.run(
                _run_tiktok_platform(
                    prepared,
                    mode=mode,
                    task_id=task_id,
                )
            )
    except TikTokPublishError:
        raise
    except Exception as exc:
        _log_internal_failure("platform-entry", exc)
        raise TikTokPublishError(
            "tiktok_platform_execution_failed",
            "TikTok 平台执行在最终动作前失败，未点击 Post",
        ) from None


__all__ = [
    "TIKTOK_CONTENT_LIMIT",
    "TIKTOK_MODES",
    "TIKTOK_VIDEO_SUFFIXES",
    "TikTokPublishError",
    "compose_tiktok_caption",
    "payload_has_tiktok_platform_signal",
    "run_tiktok_local_preflight",
    "run_tiktok_platform_sync",
    "validate_tiktok_final_schedule_window",
    "validate_tiktok_payload",
]
