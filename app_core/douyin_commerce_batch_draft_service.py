# -*- coding: utf-8 -*-
"""抖音带货批次内容的本地草稿服务。

本模块只保存客户端恢复填写所需的非敏感字段；不会读取或写入 Cookie、会话、
上传状态，也不会创建平台草稿或触发任何发布动作。
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Mapping

from . import database
from .douyin_commerce_service import (
    LOCATION_SCOPE_DOMESTIC,
    normalize_commerce_location_scope,
    normalize_content_declaration,
)
from .douyin_commerce_location_commission import (
    DEFAULT_COMMISSION_FILTER,
    normalize_commission_filter,
    normalize_observed_commission_type,
)
from .douyin_location_service import normalize_location_candidate
from .douyin_music_service import normalize_music_readback


class DouyinCommerceBatchDraftError(ValueError):
    """批次草稿不符合本地恢复所需的最小结构。"""


def _text(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def _media_path(value: object) -> str:
    """路径不是自然语言；仅去掉首尾空白，保留合法的内部字符。"""

    return str(value or "").strip()


def _account_id(value: object) -> int:
    try:
        account_id = int(value)
    except (TypeError, ValueError) as exc:
        raise DouyinCommerceBatchDraftError("请选择一个有效的抖音账号") from exc
    if account_id <= 0:
        raise DouyinCommerceBatchDraftError("请选择一个有效的抖音账号")
    return account_id


def _tags(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DouyinCommerceBatchDraftError("批次草稿话题必须是列表")
    result: list[str] = []
    for item in value:
        tag = _text(item).lstrip("#").strip()
        if tag and tag not in result:
            result.append(tag)
    return result


def _shared(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise DouyinCommerceBatchDraftError("批次草稿缺少共享内容")
    raw_music = value.get("selectedMusic")
    music: dict[str, str] = {}
    if isinstance(raw_music, Mapping) and raw_music:
        normalized_music = normalize_music_readback(raw_music)
        if not normalized_music:
            raise DouyinCommerceBatchDraftError("批次草稿收藏音乐身份无效")
        music = normalized_music
    declaration = _text(value.get("contentDeclaration"))
    if declaration:
        try:
            declaration = normalize_content_declaration(declaration)
        except Exception as exc:
            raise DouyinCommerceBatchDraftError(str(exc)) from exc
    return {
        "title": _text(value.get("title")),
        "description": str(value.get("description") or "").strip(),
        "tags": _tags(value.get("tags")),
        "selectedMusic": music,
        "contentDeclaration": declaration,
    }


def _location_preset(value: object) -> dict[str, object]:
    """只保存恢复发布意图所需的地点身份，不保留候选或 DOM 状态。"""

    if not isinstance(value, Mapping) or not value:
        return {}
    location = normalize_location_candidate(dict(value))
    if not location or not location.get("address"):
        raise DouyinCommerceBatchDraftError("批次草稿地点快照缺少 POI、名称或完整地址")
    try:
        scope = normalize_commerce_location_scope(value.get("scope"))
    except Exception as exc:
        raise DouyinCommerceBatchDraftError(str(exc)) from exc
    result = {
        "id": _text(value.get("id")),
        "poiId": location["poiId"],
        "name": location["name"],
        "address": location["address"],
        "scope": scope,
        "verifiedAt": _text(value.get("verifiedAt")),
        "commissionFilter": normalize_commission_filter(
            value.get("commissionFilter"), default="all"
        ),
        "observedCommissionType": normalize_observed_commission_type(
            value.get("observedCommissionType"), default="unknown"
        ),
    }
    for field in ("productCount", "commissionProductCount"):
        count = value.get(field)
        result[field] = count if type(count) is int and count >= 0 else None
    return result


def _last_location_search(value: object) -> dict[str, str]:
    """保存最后一次搜索意图；候选列表必须在新平台会话中重新读取。"""

    raw = value if isinstance(value, Mapping) else {}
    try:
        scope = normalize_commerce_location_scope(
            raw.get("scope") or LOCATION_SCOPE_DOMESTIC
        )
    except Exception as exc:
        raise DouyinCommerceBatchDraftError(str(exc)) from exc
    return {
        "scope": scope,
        "keyword": _text(raw.get("keyword")),
        "commissionFilter": normalize_commission_filter(
            raw.get("commissionFilter"), default=DEFAULT_COMMISSION_FILTER
        ),
    }


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise DouyinCommerceBatchDraftError("批次草稿条目数量必须在 1 至 20 之间")
    result: list[dict[str, object]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise DouyinCommerceBatchDraftError(f"第 {index} 个批次条目格式无效")
        media_path = _media_path(item.get("mediaPath"))
        if not media_path:
            raise DouyinCommerceBatchDraftError(f"第 {index} 个批次条目缺少本地媒体路径")
        media_id = item.get("mediaId")
        media_id = media_id if type(media_id) is int and media_id > 0 else None
        schedule_time = _text(item.get("scheduleTimeOverride"))
        location_preset_id = _text(item.get("locationPresetId"))
        location_preset = _location_preset(item.get("locationPreset"))
        if location_preset:
            if not location_preset.get("id") and location_preset_id:
                location_preset["id"] = location_preset_id
            location_preset_id = _text(location_preset.get("id")) or location_preset_id
        result.append(
            {
                "mediaId": media_id,
                "mediaPath": media_path,
                "locationPresetId": location_preset_id,
                "locationPreset": location_preset,
                # 旧草稿只保存每条 enableTimer。新草稿以批次 publishMode 为准；
                # 这里仍保留兼容字段，不能用 bool("false") 把字符串误判为已定时。
                "enableTimer": item.get("enableTimer") is True,
                "scheduleTimeOverride": schedule_time,
            }
        )
    return result


def _publish_mode(value: object) -> str:
    mode = _text(value or "immediate")
    if mode not in {"immediate", "interval-schedule"}:
        raise DouyinCommerceBatchDraftError("批次草稿发布方式无效")
    return mode


def _schedule(value: object, *, publish_mode: str) -> dict[str, object]:
    """保存本机排期控件值，不在草稿服务判断是否已到发布时间。"""

    raw = value if isinstance(value, Mapping) else {}
    timezone = _text(raw.get("timezone") or "Asia/Shanghai")
    if timezone != "Asia/Shanghai":
        raise DouyinCommerceBatchDraftError("批次草稿仅支持 Asia/Shanghai")
    start_time = _text(raw.get("startTime"))
    try:
        interval = int(raw.get("intervalMinutes") or 0)
    except (TypeError, ValueError) as exc:
        raise DouyinCommerceBatchDraftError("批次草稿定时间隔无效") from exc
    if publish_mode == "interval-schedule" and (not start_time or interval <= 0):
        raise DouyinCommerceBatchDraftError("按间隔定时的草稿缺少起始时间或间隔")
    return {
        "timezone": "Asia/Shanghai",
        "startTime": start_time if publish_mode == "interval-schedule" else "",
        "intervalMinutes": interval if publish_mode == "interval-schedule" else 0,
    }


def normalize_batch_draft(payload: Mapping[str, Any]) -> dict[str, Any]:
    """规范化白名单字段，丢弃 Cookie、会话等未知敏感字段。"""

    if not isinstance(payload, Mapping):
        raise DouyinCommerceBatchDraftError("批次草稿格式无效")
    account_file = _text(payload.get("accountFile"))
    if not account_file:
        raise DouyinCommerceBatchDraftError("批次草稿缺少账号文件")
    # schemaVersion=1 的旧草稿没有 publishMode/schedule：保持其逐条
    # enableTimer 的语义，恢复时会转成最接近的批次模式。
    items = _items(payload.get("items"))
    legacy_timer = any(item["enableTimer"] is True for item in items)
    publish_mode = _publish_mode(
        payload.get("publishMode") or ("interval-schedule" if legacy_timer else "immediate")
    )
    return {
        "schemaVersion": 4,
        "accountId": _account_id(payload.get("accountId")),
        "accountFile": account_file,
        "shared": _shared(payload.get("shared")),
        "lastLocationSearch": _last_location_search(payload.get("lastLocationSearch")),
        "publishMode": publish_mode,
        "schedule": _schedule(payload.get("schedule"), publish_mode=publish_mode),
        "items": items,
    }


def save_batch_draft(payload: Mapping[str, Any]) -> dict[str, Any]:
    """覆盖保存一份本地批次草稿，不与抖音平台通信。"""

    normalized = normalize_batch_draft(payload)
    updated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO douyin_commerce_batch_drafts (id, payloadJson, updatedAt)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payloadJson = excluded.payloadJson,
                updatedAt = excluded.updatedAt
            """,
            (serialized, updated_at),
        )
    return {"payload": normalized, "updatedAt": updated_at}


def load_batch_draft() -> dict[str, Any] | None:
    """读取最近一份本地批次草稿；损坏内容会明确报错。"""

    with database.connect() as conn:
        row = conn.execute(
            "SELECT payloadJson, updatedAt FROM douyin_commerce_batch_drafts WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(str(row["payloadJson"] or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DouyinCommerceBatchDraftError("已保存的批次草稿无法读取") from exc
    return {"payload": normalize_batch_draft(raw), "updatedAt": _text(row["updatedAt"])}
