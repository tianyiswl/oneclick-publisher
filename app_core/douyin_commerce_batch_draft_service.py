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


class DouyinCommerceBatchDraftError(ValueError):
    """批次草稿不符合本地恢复所需的最小结构。"""


def _text(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


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
    return {
        "title": _text(value.get("title")),
        "description": str(value.get("description") or "").strip(),
        "tags": _tags(value.get("tags")),
    }


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise DouyinCommerceBatchDraftError("批次草稿条目数量必须在 1 至 20 之间")
    result: list[dict[str, object]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise DouyinCommerceBatchDraftError(f"第 {index} 个批次条目格式无效")
        media_path = _text(item.get("mediaPath"))
        if not media_path:
            raise DouyinCommerceBatchDraftError(f"第 {index} 个批次条目缺少本地媒体路径")
        schedule_time = _text(item.get("scheduleTimeOverride"))
        result.append(
            {
                "mediaPath": media_path,
                "locationPresetId": _text(item.get("locationPresetId")),
                "enableTimer": bool(item.get("enableTimer")),
                "scheduleTimeOverride": schedule_time,
            }
        )
    return result


def normalize_batch_draft(payload: Mapping[str, Any]) -> dict[str, Any]:
    """规范化白名单字段，丢弃 Cookie、会话等未知敏感字段。"""

    if not isinstance(payload, Mapping):
        raise DouyinCommerceBatchDraftError("批次草稿格式无效")
    account_file = _text(payload.get("accountFile"))
    if not account_file:
        raise DouyinCommerceBatchDraftError("批次草稿缺少账号文件")
    return {
        "schemaVersion": 1,
        "accountId": _account_id(payload.get("accountId")),
        "accountFile": account_file,
        "shared": _shared(payload.get("shared")),
        "items": _items(payload.get("items")),
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
