# -*- coding: utf-8 -*-
"""账号隔离的抖音地点本地缓存。

该模块仅持久化平台已读回的公开地点字段；不访问浏览器或平台，也不保存
Cookie、DOM 标记等会话数据。缓存候选必须在发布前由调用方重新校验。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import database


LOCATION_STATUS_REUSABLE = "reusable"
LOCATION_STATUS_NEEDS_REVALIDATION = "needs_revalidation"
LOCATION_STATUS_INVALID = "invalid"
LOCATION_CACHE_PAGE_SIZE = 10
LOCATION_CACHE_CAPACITY = 100
LOCATION_REVALIDATE_AFTER = timedelta(days=7)

_LOCATION_REVALIDATION_ERROR_CODES = frozenset(
    {
        "publish_location_not_found_after_all_pages",
        "publish_location_readback_mismatch",
        "publish_location_candidate_ambiguous",
        "publish_location_commission_mismatch",
    }
)
_LOCATION_NOT_FOUND_ERROR_CODES = frozenset(
    {
        "publish_location_not_found_after_all_pages",
        "publish_location_readback_mismatch",
    }
)
class DouyinLocationCacheError(ValueError):
    """地点缓存的调用参数或公开候选字段不安全。"""


@dataclass(frozen=True)
class LocationCacheQuery:
    account_id: str
    scope: str
    keyword: str
    commission_filter: str


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise DouyinLocationCacheError(f"地点缓存{field}无效")
    result = " ".join(value.replace("\u200b", " ").split())
    if not result:
        raise DouyinLocationCacheError(f"地点缓存{field}不能为空")
    return result


def _optional_text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _query(value: object) -> LocationCacheQuery:
    if not isinstance(value, LocationCacheQuery):
        raise DouyinLocationCacheError("地点缓存查询条件无效")
    return LocationCacheQuery(
        account_id=_text(value.account_id, "账号"),
        scope=_text(value.scope, "范围"),
        keyword=_text(value.keyword, "关键词"),
        commission_filter=_text(value.commission_filter, "返佣筛选"),
    )


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise DouyinLocationCacheError("地点缓存时间无效")
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _timestamp(value: datetime | None) -> str:
    return _now(value).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _candidate(value: object, *, require_scope: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DouyinLocationCacheError("地点缓存候选格式无效")
    candidate = {
        "poiId": _text(value.get("poiId"), "POI"),
        "name": _text(value.get("name"), "名称"),
        "address": _text(value.get("address"), "地址"),
        "commissionType": _text(value.get("commissionType"), "返佣类型"),
        "distance": _optional_text(value.get("distance")),
        "source": _optional_text(value.get("source")),
        "productCount": _count(value.get("productCount")),
        "commissionProductCount": _count(value.get("commissionProductCount")),
        "commissionLabel": _optional_text(value.get("commissionLabel")),
    }
    if require_scope:
        candidate["scope"] = _text(value.get("scope"), "范围")
    return candidate


def _pagination(value: object, field: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise DouyinLocationCacheError(f"地点缓存{field}必须是内置整数")
    return value


def location_requires_revalidation(row: object, *, now: datetime | None = None) -> bool:
    """判断缓存行是否不能直接复用。"""

    if not isinstance(row, Mapping):
        return True
    if row.get("status") != LOCATION_STATUS_REUSABLE:
        return True
    verified_at = _parse_timestamp(row.get("verifiedAt"))
    if verified_at is None:
        return True
    return _now(now) - verified_at >= LOCATION_REVALIDATE_AFTER


def _public(row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "poiId": str(row["poiId"]),
        "name": str(row["name"]),
        "address": str(row["address"]),
        "distance": str(row["distance"] or ""),
        "source": str(row["source"] or ""),
        "commissionType": str(row["commissionType"]),
        "productCount": row["productCount"],
        "commissionProductCount": row["commissionProductCount"],
        "commissionLabel": str(row["commissionLabel"] or ""),
        "scope": str(row["scope"]),
        "status": str(row["status"]),
        "verifiedAt": str(row["verifiedAt"]),
    }


def _evict_excess_locations(conn: Any, query: LocationCacheQuery) -> None:
    count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM douyin_location_cache_keywords
        WHERE accountId = ? AND scope = ? AND keyword = ? AND commissionFilter = ?
        """,
        (
            query.account_id,
            query.scope,
            query.keyword,
            query.commission_filter,
        ),
    ).fetchone()["count"]
    excess = count - LOCATION_CACHE_CAPACITY
    if excess <= 0:
        return
    rows = conn.execute(
        """
        SELECT locationCacheId
        FROM douyin_location_cache_keywords
        WHERE accountId = ? AND scope = ? AND keyword = ? AND commissionFilter = ?
        ORDER BY position DESC, locationCacheId DESC
        LIMIT ?
        """,
        (
            query.account_id,
            query.scope,
            query.keyword,
            query.commission_filter,
            excess,
        ),
    ).fetchall()
    ids = [row["locationCacheId"] for row in rows]
    if not ids:
        return
    placeholders = ", ".join("?" for _ in ids)
    conn.execute(
        f"""
        DELETE FROM douyin_location_cache_keywords
        WHERE accountId = ? AND scope = ? AND keyword = ? AND commissionFilter = ?
          AND locationCacheId IN ({placeholders})
        """,
        (
            query.account_id,
            query.scope,
            query.keyword,
            query.commission_filter,
            *ids,
        ),
    )
    conn.execute(
        """
        DELETE FROM douyin_location_cache
        WHERE NOT EXISTS (
            SELECT 1
            FROM douyin_location_cache_keywords AS keyword
            WHERE keyword.locationCacheId = douyin_location_cache.id
        )
        """
    )


def get_cached_locations(
    query: LocationCacheQuery,
    *,
    offset: int = 0,
    limit: int = LOCATION_CACHE_PAGE_SIZE,
    now: datetime | None = None,
) -> dict[str, object]:
    """读取一个账号、范围、关键词和返佣筛选下可直接复用的缓存页。"""

    safe_query = _query(query)
    safe_offset = _pagination(offset, "offset", minimum=0)
    safe_limit = _pagination(limit, "limit", minimum=1)
    if safe_limit != LOCATION_CACHE_PAGE_SIZE:
        raise DouyinLocationCacheError(f"地点缓存limit必须为{LOCATION_CACHE_PAGE_SIZE}")
    current = _now(now)
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT cache.poiId, cache.name, cache.address, cache.distance, cache.source,
                   cache.commissionType, cache.productCount, cache.commissionProductCount,
                   cache.commissionLabel, cache.status, cache.verifiedAt, cache.scope
            FROM douyin_location_cache AS cache
            JOIN douyin_location_cache_keywords AS keyword
              ON keyword.locationCacheId = cache.id
            WHERE keyword.accountId = ? AND keyword.scope = ?
              AND keyword.keyword = ? AND keyword.commissionFilter = ?
            ORDER BY keyword.position ASC, cache.id ASC
            """,
            (
                safe_query.account_id,
                safe_query.scope,
                safe_query.keyword,
                safe_query.commission_filter,
            ),
        ).fetchall()
    public_rows = [_public(row) for row in rows]
    reusable = [
        row
        for row in public_rows
        if not location_requires_revalidation(row, now=current)
    ]
    requires_revalidation = any(
        row.get("status") == LOCATION_STATUS_NEEDS_REVALIDATION
        or (
            row.get("status") == LOCATION_STATUS_REUSABLE
            and location_requires_revalidation(row, now=current)
        )
        for row in public_rows
    )
    page = reusable[safe_offset : safe_offset + safe_limit]
    return {
        "candidates": page,
        "offset": safe_offset,
        "limit": safe_limit,
        "total": len(reusable),
        "hasMore": safe_offset + len(page) < len(reusable),
        "requiresRevalidation": bool(requires_revalidation),
    }


def merge_platform_locations(
    query: LocationCacheQuery,
    candidates: object,
    *,
    verified_at: datetime | None = None,
) -> dict[str, object]:
    """原子合并一次平台地点读回，并更新当前关键词的关联顺序。"""

    safe_query = _query(query)
    if not isinstance(candidates, list):
        raise DouyinLocationCacheError("地点缓存候选列表无效")
    normalized = [_candidate(candidate) for candidate in candidates]
    identities = {
        (
            candidate["poiId"],
            candidate["name"],
            candidate["address"],
            candidate["commissionType"],
        )
        for candidate in normalized
    }
    if len(identities) != len(normalized):
        raise DouyinLocationCacheError("地点缓存候选出现重复完整身份")
    timestamp = _timestamp(verified_at)
    with database.connect() as conn:
        for position, candidate in enumerate(normalized):
            conn.execute(
                """
                INSERT INTO douyin_location_cache (
                    accountId, scope, poiId, name, address, commissionType, distance,
                    source, productCount, commissionProductCount, commissionLabel, status,
                    verifiedAt, lastSeenAt, revalidationFailures
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(accountId, scope, poiId, name, address, commissionType)
                DO UPDATE SET
                    distance = excluded.distance,
                    source = excluded.source,
                    productCount = excluded.productCount,
                    commissionProductCount = excluded.commissionProductCount,
                    commissionLabel = excluded.commissionLabel,
                    status = excluded.status,
                    verifiedAt = excluded.verifiedAt,
                    lastSeenAt = excluded.lastSeenAt,
                    revalidationFailures = 0
                """,
                (
                    safe_query.account_id,
                    safe_query.scope,
                    candidate["poiId"],
                    candidate["name"],
                    candidate["address"],
                    candidate["commissionType"],
                    candidate["distance"],
                    candidate["source"],
                    candidate["productCount"],
                    candidate["commissionProductCount"],
                    candidate["commissionLabel"],
                    LOCATION_STATUS_REUSABLE,
                    timestamp,
                    timestamp,
                ),
            )
            cache_row = conn.execute(
                """
                SELECT id FROM douyin_location_cache
                WHERE accountId = ? AND scope = ? AND poiId = ? AND name = ?
                  AND address = ? AND commissionType = ?
                """,
                (
                    safe_query.account_id,
                    safe_query.scope,
                    candidate["poiId"],
                    candidate["name"],
                    candidate["address"],
                    candidate["commissionType"],
                ),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO douyin_location_cache_keywords (
                    locationCacheId, accountId, scope, keyword, commissionFilter, position
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(locationCacheId, keyword, commissionFilter) DO UPDATE SET
                    accountId = excluded.accountId,
                    scope = excluded.scope,
                    position = excluded.position
                """,
                (
                    cache_row["id"],
                    safe_query.account_id,
                    safe_query.scope,
                    safe_query.keyword,
                    safe_query.commission_filter,
                    position,
                ),
            )
        _evict_excess_locations(conn, safe_query)
    return get_cached_locations(safe_query, now=_now(verified_at))


def record_location_publish_result(
    account_id: str,
    candidate: object,
    *,
    success: bool,
    error_code: str = "",
    occurred_at: datetime | None = None,
) -> None:
    """按发布回读更新一个地点，非地点错误不会改变缓存状态。"""

    safe_account_id = _text(account_id, "账号")
    if type(success) is not bool:
        raise DouyinLocationCacheError("地点缓存发布结果无效")
    if not isinstance(error_code, str):
        raise DouyinLocationCacheError("地点缓存错误码无效")
    safe_candidate = _candidate(candidate, require_scope=True)
    if not success and error_code not in _LOCATION_REVALIDATION_ERROR_CODES:
        return
    timestamp = _timestamp(occurred_at)
    with database.connect() as conn:
        if success:
            conn.execute(
                """
                UPDATE douyin_location_cache
                SET status = ?, verifiedAt = ?, lastSeenAt = ?, revalidationFailures = 0
                WHERE accountId = ? AND scope = ? AND poiId = ? AND name = ?
                  AND address = ? AND commissionType = ?
                """,
                (
                    LOCATION_STATUS_REUSABLE,
                    timestamp,
                    timestamp,
                    safe_account_id,
                    safe_candidate["scope"],
                    safe_candidate["poiId"],
                    safe_candidate["name"],
                    safe_candidate["address"],
                    safe_candidate["commissionType"],
                ),
            )
            return
        failures = "revalidationFailures + 1" if error_code in _LOCATION_NOT_FOUND_ERROR_CODES else "revalidationFailures"
        conn.execute(
            f"""
            UPDATE douyin_location_cache
            SET status = CASE
                    WHEN {failures} >= 2 THEN ?
                    ELSE ?
                END,
                revalidationFailures = {failures},
                lastSeenAt = ?
            WHERE accountId = ? AND scope = ? AND poiId = ? AND name = ?
              AND address = ? AND commissionType = ?
            """,
            (
                LOCATION_STATUS_INVALID,
                LOCATION_STATUS_NEEDS_REVALIDATION,
                timestamp,
                safe_account_id,
                safe_candidate["scope"],
                safe_candidate["poiId"],
                safe_candidate["name"],
                safe_candidate["address"],
                safe_candidate["commissionType"],
            ),
        )
