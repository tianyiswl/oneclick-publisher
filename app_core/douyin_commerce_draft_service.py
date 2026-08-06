# -*- coding: utf-8 -*-
"""抖音带货“内容准备”的本地保存。

这里只保存用户在客户端已选择的账号引用、视频引用、标题、文案和话题，方便
下次重新进入流程时恢复填写内容。话题历史只保存用户主动输入过的纯文本标签，
用于本机快速复用。音乐、地点、门店、临时编辑器会话、Cookie、二维码及任何
平台回读均不落盘；它不是平台草稿，也不会触发上传或发表。
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


def list_tag_history(*, limit: int = 12) -> list[str]:
    """读取本机历史标签，按最近使用优先，不触发任何平台动作。"""

    safe_limit = max(1, min(int(limit or 12), 40))
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT tag FROM tag_history
            WHERE TRIM(tag) <> ''
            ORDER BY lastUsedAt DESC, id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [tag for row in rows if (tag := _text(row["tag"]).lstrip("#"))]


def remember_tag_history(tags: object) -> list[str]:
    """记录用户主动保存的标签历史；只写本机 SQLite 的非敏感文本。"""

    normalized = _tags(tags)
    if not normalized:
        return list_tag_history()
    updated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    with database.connect() as conn:
        for tag in normalized:
            conn.execute(
                """
                INSERT INTO tag_history (tag, useCount, lastUsedAt)
                VALUES (?, 1, ?)
                ON CONFLICT(tag) DO UPDATE SET
                    useCount = tag_history.useCount + 1,
                    lastUsedAt = excluded.lastUsedAt
                """,
                (tag, updated_at),
            )
    return list_tag_history()
