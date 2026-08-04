# -*- coding: utf-8 -*-
"""抖音带货“内容准备”的本地保存。

这里只保存用户在客户端已选择的账号引用、视频引用、标题、文案和话题，方便
下次重新进入流程时恢复填写内容。音乐、地点、门店、临时编辑器会话、Cookie、
二维码及任何平台回读均不落盘；它不是平台草稿，也不会触发上传或发表。
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Mapping

from . import database


class DouyinCommerceContentDraftError(ValueError):
    """本地内容保存或恢复的数据不符合最小约定。"""


def _text(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tags(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DouyinCommerceContentDraftError("保存的抖音带货话题格式无效")
    result: list[str] = []
    for item in value:
        tag = _text(item).lstrip("#").strip()
        if tag and tag not in result:
            result.append(tag)
    return result


def normalize_content_draft(payload: Mapping[str, Any]) -> dict[str, Any]:
    """收敛为可恢复、无平台副作用的内容准备字段。"""

    if not isinstance(payload, Mapping):
        raise DouyinCommerceContentDraftError("抖音带货保存内容格式无效")
    return {
        "schemaVersion": 1,
        "accountId": _integer(payload.get("accountId")),
        "accountFile": _text(payload.get("accountFile")),
        "mediaId": _integer(payload.get("mediaId")),
        "mediaPath": _text(payload.get("mediaPath")),
        "title": _text(payload.get("title")),
        "description": str(payload.get("description") or "").strip(),
        "tags": _tags(payload.get("tags")),
    }


def save_content_draft(payload: Mapping[str, Any]) -> dict[str, Any]:
    """覆盖保存一份本机抖音带货内容准备，不创建任何平台草稿。"""

    normalized = normalize_content_draft(payload)
    updated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO douyin_commerce_content_drafts (id, payloadJson, updatedAt)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payloadJson = excluded.payloadJson,
                updatedAt = excluded.updatedAt
            """,
            (serialized, updated_at),
        )
    return {"payload": normalized, "updatedAt": updated_at}


def load_content_draft() -> dict[str, Any] | None:
    """读取最近一次本地保存；损坏数据会明确失败，不猜测恢复。"""

    with database.connect() as conn:
        row = conn.execute(
            "SELECT payloadJson, updatedAt FROM douyin_commerce_content_drafts WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(str(row["payloadJson"] or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DouyinCommerceContentDraftError("已保存的抖音带货内容无法读取") from exc
    return {
        "payload": normalize_content_draft(raw),
        "updatedAt": _text(row["updatedAt"]),
    }
