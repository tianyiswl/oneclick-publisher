# -*- coding: utf-8 -*-
"""抖音地点预设的本地持久化与严格匹配服务。"""

from __future__ import annotations

from datetime import datetime
import re
import uuid
from typing import Any

from . import database
from .douyin_location_service import normalize_location_candidate


class DouyinLocationPresetError(ValueError):
    """地点预设缺少完整身份，或不能唯一匹配当前候选。"""


def _text(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def _account_id(value: object) -> int:
    try:
        account_id = int(value)
    except (TypeError, ValueError) as exc:
        raise DouyinLocationPresetError("请选择有效的抖音账号") from exc
    if account_id <= 0:
        raise DouyinLocationPresetError("请选择有效的抖音账号")
    return account_id


def _location(value: object) -> dict[str, str]:
    location = normalize_location_candidate(value)
    if not location or not location["poiId"] or not location["name"] or not location["address"]:
        raise DouyinLocationPresetError("地点预设需要 POI、名称和完整地址")
    return location


def _address_match_key(value: object) -> str:
    """仅消除平台回读中的排版差异，不抹去真实道路与门牌信息。"""

    text = _text(value).casefold()
    # 抖音地点列表偶尔只改变“市”“与”及标点/空格的展示方式；这些不是
    # 地点身份差异。道路、门牌和其它中文内容仍全部保留，不能降级为店名匹配。
    text = text.replace("市", "").replace("与", "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text)


def save_location_preset(account_id: object, location: object, scope: object) -> dict[str, Any]:
    """保存账号专属地点预设，只落盘完整且可复核的 POI 身份。"""

    normalized_account_id = _account_id(account_id)
    normalized_location = _location(location)
    normalized_scope = _text(scope)
    if not normalized_scope:
        raise DouyinLocationPresetError("地点预设缺少适用范围")
    verified_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    with database.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM douyin_location_presets WHERE accountId = ? AND poiId = ?",
            (normalized_account_id, normalized_location["poiId"]),
        ).fetchone()
        preset_id = _text(existing["id"]) if existing else str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO douyin_location_presets
                (id, accountId, poiId, name, address, scope, verifiedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(accountId, poiId) DO UPDATE SET
                name = excluded.name,
                address = excluded.address,
                scope = excluded.scope,
                verifiedAt = excluded.verifiedAt
            """,
            (
                preset_id,
                normalized_account_id,
                normalized_location["poiId"],
                normalized_location["name"],
                normalized_location["address"],
                normalized_scope,
                verified_at,
            ),
        )
    return {
        "id": preset_id,
        "accountId": normalized_account_id,
        "poiId": normalized_location["poiId"],
        "name": normalized_location["name"],
        "address": normalized_location["address"],
        "scope": normalized_scope,
        "verifiedAt": verified_at,
    }


def list_location_presets(account_id: object) -> list[dict[str, Any]]:
    """读取指定账号的地点预设，绝不跨账号返回。"""

    normalized_account_id = _account_id(account_id)
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, accountId, poiId, name, address, scope, verifiedAt
            FROM douyin_location_presets
            WHERE accountId = ?
            ORDER BY verifiedAt DESC, id DESC
            """,
            (normalized_account_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def match_location_preset(preset: object, candidates: object) -> dict[str, str]:
    """在当前候选中安全复核预设，优先使用官方 POI 身份。"""

    expected = _location(preset)
    if not isinstance(candidates, list):
        raise DouyinLocationPresetError("地点候选列表格式无效")
    normalized_candidates: list[dict[str, str]] = []
    for candidate in candidates:
        normalized = normalize_location_candidate(candidate)
        if not normalized:
            continue
        normalized_candidates.append(normalized)

    def unique_match(rows: list[dict[str, str]], reason: str) -> dict[str, str] | None:
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise DouyinLocationPresetError(f"当前地点候选存在多个{reason}的地点")
        return None

    # 普通地点结果包含稳定的官方 POI ID：地址文案有轻微变化时仍应认定为同一地点。
    if not expected["poiId"].startswith("visible-poi:"):
        matched = unique_match(
            [row for row in normalized_candidates if row["poiId"] == expected["poiId"]],
            "POI ID 一致",
        )
        if matched is not None:
            return matched

    matched = unique_match(
        [
            row
            for row in normalized_candidates
            if row["poiId"] == expected["poiId"]
            and row["name"] == expected["name"]
            and row["address"] == expected["address"]
        ],
        "完全一致",
    )
    if matched is not None:
        return matched

    # 新版带货页未暴露官方 POI ID 时，visible-poi 是由可见名称、地址生成的。
    # 若地址仅是“上海市/上海”“路与路/路路”之类的排版变化，允许唯一候选恢复。
    matched = unique_match(
        [
            row
            for row in normalized_candidates
            if row["name"] == expected["name"]
            and _address_match_key(row["address"]) == _address_match_key(expected["address"])
        ],
        "名称与规范化地址一致",
    )
    if matched is not None:
        return matched
    raise DouyinLocationPresetError("当前地点候选未找到与预设完全一致的地点")
