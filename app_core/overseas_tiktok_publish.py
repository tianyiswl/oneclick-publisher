# -*- coding: utf-8 -*-
"""TikTok 单账号单视频的严格本地合同。

本模块的 ``preflight`` 只读本地账号记录、会话 JSON 和视频
文件。它不导入上传器，也不启动 Playwright。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping

from .overseas_tiktok_errors import TikTokPublishError
from .overseas_tiktok_identity import normalize_tiktok_handle
from .paths import COOKIE_DIR, DB_PATH


TIKTOK_CONTENT_LIMIT = 2200
TIKTOK_MODES = frozenset({"preflight", "platform_form_check", "formal"})
TIKTOK_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm"})

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
def _schedule_requested(value: object) -> bool:
    if not isinstance(value, Mapping):
        return _is_nonempty(value)
    return bool(value.get("enabled") is True) or any(
        _is_nonempty(value.get(key))
        for key in ("localTime", "scheduleTime", "scheduledAt")
    )


def _ai_requested(value: object) -> bool:
    if not isinstance(value, Mapping):
        return value is True or _is_nonempty(value)
    return any(
        _is_nonempty(nested)
        for mapping in _walk_mappings(value)
        for nested in mapping.values()
    )


def _validate_immediate_schedule(payload: Mapping[str, Any]) -> None:
    if payload.get("enableTimer") is not False:
        _fail(
            "tiktok_unsupported_publish_setting",
            "TikTok 首版不支持定时时间，只支持立即公开发布",
        )
    if type(payload.get("dailyTimes")) is not list or payload["dailyTimes"] != []:
        _fail(
            "tiktok_unsupported_publish_setting",
            "TikTok 首版不支持定时时间列表",
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
            if key in {
                "scheduletime",
                "scheduledat",
                "publishschedule",
                "localtime",
            } and _is_nonempty(value):
                _fail(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 首版不支持定时时间",
                )
            if key == "schedule" and value is not None:
                if not isinstance(value, Mapping) or value.get("enabled") is not False:
                    _fail(
                        "tiktok_unsupported_publish_setting",
                        "TikTok 首版不支持定时时间",
                    )


def _validate_unsupported_settings(payload: Mapping[str, Any]) -> str:
    visibility_values: list[str] = []
    for mapping in _walk_tiktok_payload_mappings(payload, at_root=True):
        for raw_key, value in mapping.items():
            key = str(raw_key).strip().lower()
            if key == "visibility" and _is_nonempty(value):
                visibility_values.append(str(value).strip().lower())
            elif key == "enabletimer" and value is True:
                _fail(
                    "tiktok_unsupported_publish_setting",
                    "TikTok 首版不支持定时时间，只支持立即公开发布",
                )
            elif key in {"scheduletime", "scheduledat", "schedule", "publishschedule"}:
                if _schedule_requested(value):
                    _fail(
                        "tiktok_unsupported_publish_setting",
                        "TikTok 首版不支持定时时间，只支持立即公开发布",
                    )
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
        or not all(isinstance(item, Mapping) for item in cookies)
        or not all(isinstance(item, Mapping) for item in origins)
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

    _validate_immediate_schedule(payload)
    visibility = _validate_unsupported_settings(payload)
    account_id, account = _account_identity(payload)
    session_path = _session_path(payload, account)
    video_path = _video_path(payload)
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
        "videoSha256": _sha256_file(video_path),
        "title": title,
        "body": body,
        "topics": topics,
        "plainCaption": plain_caption,
        "textSha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        "visibility": visibility,
        "mode": mode,
    }


def run_tiktok_local_preflight(payload: Mapping[str, Any]) -> dict[str, Any]:
    """运行零平台写入的 TikTok 本地预检。"""

    prepared = validate_tiktok_payload(payload, mode="preflight")
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
        },
        "receipt": {
            "accountId": prepared["accountId"],
            "visibility": prepared["visibility"],
            "platformWriteOccurred": False,
            "finalActionTriggered": False,
            "contentId": None,
            "contentUrl": None,
            "publishedAt": None,
        },
    }


__all__ = [
    "TIKTOK_CONTENT_LIMIT",
    "TIKTOK_MODES",
    "TIKTOK_VIDEO_SUFFIXES",
    "TikTokPublishError",
    "compose_tiktok_caption",
    "payload_has_tiktok_platform_signal",
    "run_tiktok_local_preflight",
    "validate_tiktok_payload",
]
