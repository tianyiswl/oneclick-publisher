"""发布配置的最小本地实现。

展示版其他配置功能仍保持无副作用占位，但话题解析会直接进入发布
载荷，不能用通用占位值吞掉内容包标签。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import json
import re
from typing import Any

from . import database


TAG_PATTERN = re.compile(r"#?([\w\u4e00-\u9fff-]+)")


class PublishConfigError(ValueError):
    """发布中心本地配置无法安全保存或读取。"""


def parse_tags(text: str) -> list[str]:
    """把用户输入解析为去重话题列表。"""

    tags: list[str] = []
    for match in TAG_PATTERN.finditer(text or ""):
        tag = match.group(1).strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _publish_draft_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验草稿是可独立恢复的 JSON 对象，不接受静默丢字段。"""

    if not isinstance(payload, Mapping):
        raise PublishConfigError("发布中心保存内容格式无效")
    normalized = dict(payload)
    try:
        # 先完整序列化再反序列化，确保返回内容与真正落盘的数据完全一致。
        return json.loads(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PublishConfigError("发布中心保存内容包含无法写入的字段") from exc


def save_publish_draft(payload: Mapping[str, Any]) -> dict[str, Any]:
    """覆盖保存最近一次发布中心填写内容，不设置过期时间。"""

    normalized = _publish_draft_payload(payload)
    updated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    try:
        with database.connect() as conn:
            conn.execute(
                """
                INSERT INTO publish_drafts (id, payloadJson, updatedAt)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payloadJson = excluded.payloadJson,
                    updatedAt = excluded.updatedAt
                """,
                (serialized, updated_at),
            )
    except Exception as exc:
        raise PublishConfigError("发布中心内容未能写入本机数据库") from exc
    return {"payload": normalized, "updatedAt": updated_at}


def load_publish_draft() -> dict[str, Any] | None:
    """读取最近一次保存内容；不存在时返回空，损坏时明确报错。"""

    try:
        with database.connect() as conn:
            row = conn.execute(
                "SELECT payloadJson, updatedAt FROM publish_drafts WHERE id = 1"
            ).fetchone()
    except Exception as exc:
        raise PublishConfigError("发布中心已保存内容无法读取") from exc
    if row is None:
        return None
    try:
        raw = json.loads(str(row["payloadJson"] or ""))
        normalized = _publish_draft_payload(raw)
    except (TypeError, ValueError, json.JSONDecodeError, PublishConfigError) as exc:
        raise PublishConfigError("发布中心已保存内容无法读取") from exc
    return {
        "payload": normalized,
        "updatedAt": str(row["updatedAt"] or "").strip(),
    }


def __getattr__(_name):
    return lambda *_args, **_kwargs: []
