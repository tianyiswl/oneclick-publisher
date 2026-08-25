# -*- coding: utf-8 -*-
"""账号隔离的抖音地点本地缓存。

该模块仅持久化平台已读回的公开地点字段；不访问浏览器或平台，也不保存
Cookie、DOM 标记等会话数据。缓存候选必须在发布前由调用方重新校验。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import threading
from typing import Any

from . import database
from .douyin_location_search_plan import (
    MUNICIPALITIES,
    PROVINCE_CITIES,
    LocationSearchPlan,
    build_location_search_plan,
)


LOCATION_STATUS_REUSABLE = "reusable"
LOCATION_STATUS_NEEDS_REVALIDATION = "needs_revalidation"
LOCATION_STATUS_INVALID = "invalid"
LOCATION_CACHE_PAGE_SIZE = 10
LOCATION_CACHE_CAPACITY = 100
LOCATION_REVALIDATE_AFTER = timedelta(days=7)
_LOCATION_EVICTION_TOMBSTONE_TTL = timedelta(minutes=5)
_LOCATION_EVICTION_TOMBSTONE_CAPACITY = LOCATION_CACHE_CAPACITY * 10
_LOCATION_SCOPES = frozenset({"local", "domestic"})
_LOCATION_COMMISSION_FILTERS = frozenset(
    {"all", "commission", "no_commission"}
)
_LOCATION_COMMISSION_TYPES = frozenset({"commission", "no_commission"})
_CITY_PROVINCES: dict[str, tuple[str, ...]] = {
    city: tuple(
        province
        for province, cities in PROVINCE_CITIES.items()
        if city in cities
    )
    for city in {
        city for cities in PROVINCE_CITIES.values() for city in cities
    }
}
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
_evicted_location_lock = threading.Lock()
_evicted_location_tombstones: OrderedDict[
    tuple[str, ...], tuple[datetime, dict[str, Any]]
] = OrderedDict()


class DouyinLocationCacheError(ValueError):
    """地点缓存的调用参数或公开候选字段不安全。"""


@dataclass(frozen=True)
class LocationCacheQuery:
    account_id: str
    scope: str
    keyword: str
    commission_filter: str
    province_context: str = ""


_LOCATION_SEARCH_PLAN_SCHEMA_VERSION = 1
_LOCATION_SEARCH_PLAN_KINDS = frozenset({"plain", "city", "province"})


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise DouyinLocationCacheError(f"地点缓存{field}无效")
    result = " ".join(value.replace("\u200b", " ").split())
    if not result:
        raise DouyinLocationCacheError(f"地点缓存{field}不能为空")
    return result


def _optional_text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _search_keyword(value: object) -> str:
    """缓存键沿用地点搜索计划的空白规范化。"""

    return re.sub(r"[\s\u3000]+", "", _text(value, "关键词"))


def _province_name(value: object) -> str:
    normalized = re.sub(r"[\s\u3000]+", "", _optional_text(value))
    for province in PROVINCE_CITIES:
        if normalized in {
            province,
            f"{province}省",
            f"{province}自治区",
            f"{province}壮族自治区",
            f"{province}回族自治区",
            f"{province}维吾尔自治区",
        }:
            return province
    return ""


def _top_level_province(address: object) -> str:
    """只从地址开头解析顶层省级地区，不用中间子串猜省份。"""

    normalized = re.sub(r"[\s\u3000]+", "", _optional_text(address))
    aliases: list[tuple[str, str]] = []
    for province in PROVINCE_CITIES:
        aliases.extend(
            (
                (f"{province}省", province),
                (f"{province}壮族自治区", province),
                (f"{province}回族自治区", province),
                (f"{province}维吾尔自治区", province),
                (f"{province}自治区", province),
                (province, province),
            )
        )
    for municipality in MUNICIPALITIES:
        aliases.extend(((f"{municipality}市", municipality), (municipality, municipality)))
    aliases.extend(
        (
            ("香港特别行政区", "香港"),
            ("澳门特别行政区", "澳门"),
            ("台湾省", "台湾"),
        )
    )
    for alias, province in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        if normalized.startswith(alias):
            # 无“省/自治区”后缀时，只有紧跟该省的已知地级区
            # 才能证明这是顶层省名。这会把“海南藏族自治州”留为
            # 顶层省份未知，而不是猜成海南省。
            if alias == province and province in PROVINCE_CITIES:
                remainder = normalized[len(alias) :]
                if not any(
                    remainder.startswith(city)
                    for city in PROVINCE_CITIES[province]
                ):
                    continue
            return province
    # 平台有时省略省名，只从地址开头的唯一归属地级市
    # 回推顶级省份。与其他省名发生跨层级冲突的短名
    # （例如青海海南州）依然失败关闭。
    city_aliases: list[tuple[str, str]] = []
    for city, owners in _CITY_PROVINCES.items():
        if len(owners) != 1 or (
            city in PROVINCE_CITIES and owners[0] != city
        ):
            continue
        city_aliases.append((f"{city}市", owners[0]))
    for alias, province in sorted(
        city_aliases, key=lambda item: len(item[0]), reverse=True
    ):
        if normalized.startswith(alias):
            return province
    return ""


def _leading_city(keyword: str, province_context: str = "") -> tuple[str, str]:
    city_names = (
        PROVINCE_CITIES.get(province_context, ())
        if province_context
        else tuple(MUNICIPALITIES)
        + tuple(city for cities in PROVINCE_CITIES.values() for city in cities)
    )
    aliases = [(f"{city}市", city) for city in city_names]
    aliases.extend((city, city) for city in city_names)
    for alias, city in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        if keyword.startswith(alias) and keyword[len(alias) :]:
            return city, keyword[len(alias) :]
    return "", ""


def filter_locations_for_search_keyword(
    keyword: object,
    candidates: list[Mapping[str, Any]],
    *,
    province_context: object = "",
) -> list[Mapping[str, Any]]:
    """搜索词以省级地区开头时，排除其他地区的同名门店。

    完整关键词已出现在门店名时保留候选，避免把“北京烤鸭”
    这类品牌/品类词误判为只搜北京。
    """

    normalized_keyword = "".join(_optional_text(keyword).split()).casefold()
    parent_province = _province_name(province_context)
    region = ""
    remainder = ""
    if parent_province:
        region, remainder = _leading_city(normalized_keyword, parent_province)
        if not region:
            plan = build_location_search_plan(normalized_keyword)
            if plan.search_kind == "province" and plan.province == parent_province:
                region = parent_province
                remainder = plan.merchant_term
    else:
        plan = build_location_search_plan(normalized_keyword)
        if plan.search_kind == "province":
            parent_province = plan.province
            region = plan.province
            remainder = plan.merchant_term
        elif plan.search_kind == "city":
            region, remainder = _leading_city(normalized_keyword)
            if region:
                parent_province = next(
                    (
                        province
                        for province, cities in PROVINCE_CITIES.items()
                        if region in cities
                    ),
                    region if region in MUNICIPALITIES else "",
                )
    if not region or not remainder:
        return list(candidates)
    filtered: list[Mapping[str, Any]] = []
    cross_level_collision = bool(
        region in PROVINCE_CITIES
        and any(
            owner != region for owner in _CITY_PROVINCES.get(region, ())
        )
    )
    for candidate in candidates:
        name = "".join(_optional_text(candidate.get("name")).split()).casefold()
        address = "".join(
            _optional_text(candidate.get("address")).split()
        ).casefold()
        top_level = _top_level_province(address)
        if normalized_keyword in name and not cross_level_collision:
            filtered.append(candidate)
            continue
        if (
            top_level == parent_province
            and (region == parent_province or region in address)
            and remainder in f"{name}{address}"
        ):
            filtered.append(candidate)
    return filtered


def _query(value: object) -> LocationCacheQuery:
    if not isinstance(value, LocationCacheQuery):
        raise DouyinLocationCacheError("地点缓存查询条件无效")
    safe_query = LocationCacheQuery(
        account_id=_text(value.account_id, "账号"),
        scope=_text(value.scope, "范围"),
        keyword=_search_keyword(value.keyword),
        commission_filter=_text(value.commission_filter, "返佣筛选"),
        province_context=_province_name(value.province_context),
    )
    if safe_query.scope not in _LOCATION_SCOPES:
        raise DouyinLocationCacheError("地点缓存范围无效")
    if safe_query.commission_filter not in _LOCATION_COMMISSION_FILTERS:
        raise DouyinLocationCacheError("地点缓存返佣筛选无效")
    return safe_query


def _storage_keyword(query: LocationCacheQuery) -> str:
    city, _remainder = _leading_city(query.keyword, query.province_context)
    owners = _CITY_PROVINCES.get(city, ())
    ambiguous_city = bool(
        city
        and (
            len(owners) > 1
            or (city in PROVINCE_CITIES and city != query.province_context)
        )
    )
    if not query.province_context or not ambiguous_city:
        return query.keyword
    identity = json.dumps(
        [query.province_context, query.keyword],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "province-context:" + hashlib.sha256(identity).hexdigest()


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
        if candidate["scope"] not in _LOCATION_SCOPES:
            raise DouyinLocationCacheError("地点缓存范围无效")
    if candidate["commissionType"] not in _LOCATION_COMMISSION_TYPES:
        raise DouyinLocationCacheError("地点缓存返佣类型无效")
    return candidate


def _validate_candidates_for_query(
    query: LocationCacheQuery,
    candidates: list[Mapping[str, Any]],
) -> None:
    if query.commission_filter == "all":
        return
    if any(
        candidate["commissionType"] != query.commission_filter
        for candidate in candidates
    ):
        raise DouyinLocationCacheError("地点缓存候选与返佣筛选不匹配")


def _pagination(value: object, field: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise DouyinLocationCacheError(f"地点缓存{field}必须是内置整数")
    return value


def _eligible_total(value: object) -> int:
    if type(value) is not int or not 0 <= value <= LOCATION_CACHE_CAPACITY:
        raise DouyinLocationCacheError("地点搜索进度有效地址数无效")
    return value


def _plan_optional_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise DouyinLocationCacheError(f"地点搜索进度{field}无效")
    return " ".join(value.replace("\u200b", " ").split())


def _search_plan_payload(plan: object) -> dict[str, object]:
    if not isinstance(plan, LocationSearchPlan):
        raise DouyinLocationCacheError("地点搜索进度计划无效")
    payload = {
        "schemaVersion": plan.schema_version,
        "originalKeyword": plan.original_keyword,
        "searchKind": plan.search_kind,
        "province": plan.province,
        "merchantTerm": plan.merchant_term,
        "subqueries": list(plan.subqueries),
        "currentIndex": plan.current_index,
        "currentLoadCount": plan.current_load_count,
        "completedIndices": list(plan.completed_indices),
        "lastErrorCode": plan.last_error_code,
    }
    _plan_from_payload(payload)
    return payload


def _plan_from_payload(payload: object) -> LocationSearchPlan:
    if not isinstance(payload, Mapping):
        raise DouyinLocationCacheError("地点搜索进度格式无效")
    schema_version = payload.get("schemaVersion")
    if (
        type(schema_version) is not int
        or schema_version != _LOCATION_SEARCH_PLAN_SCHEMA_VERSION
    ):
        raise DouyinLocationCacheError("地点搜索进度版本无效")
    try:
        original_keyword = _text(payload["originalKeyword"], "搜索进度原始关键词")
        search_kind = _text(payload["searchKind"], "搜索进度类型")
        province = _plan_optional_text(payload["province"], "省份")
        merchant_term = _plan_optional_text(payload["merchantTerm"], "商户词")
        subqueries_value = payload["subqueries"]
        current_index = payload["currentIndex"]
        current_load_count = payload["currentLoadCount"]
        completed_value = payload["completedIndices"]
        last_error_code = _plan_optional_text(payload["lastErrorCode"], "错误码")
    except KeyError as error:
        raise DouyinLocationCacheError("地点搜索进度字段缺失") from error
    if search_kind not in _LOCATION_SEARCH_PLAN_KINDS:
        raise DouyinLocationCacheError("地点搜索进度类型无效")
    if not isinstance(subqueries_value, list) or not subqueries_value:
        raise DouyinLocationCacheError("地点搜索进度子词无效")
    subqueries = tuple(_text(item, "搜索进度子词") for item in subqueries_value)
    if len(set(subqueries)) != len(subqueries):
        raise DouyinLocationCacheError("地点搜索进度子词重复")
    if type(current_index) is not int or not 0 <= current_index < len(subqueries):
        raise DouyinLocationCacheError("地点搜索进度当前下标无效")
    if type(current_load_count) is not int or current_load_count < 0:
        raise DouyinLocationCacheError("地点搜索进度加载次数无效")
    if not isinstance(completed_value, list) or any(
        type(index) is not int or not 0 <= index < len(subqueries)
        for index in completed_value
    ):
        raise DouyinLocationCacheError("地点搜索进度已完成子词无效")
    completed_indices = tuple(completed_value)
    if completed_indices != tuple(sorted(set(completed_indices))):
        raise DouyinLocationCacheError("地点搜索进度已完成子词无效")
    return LocationSearchPlan(
        schema_version=schema_version,
        original_keyword=original_keyword,
        search_kind=search_kind,
        province=province,
        merchant_term=merchant_term,
        subqueries=subqueries,
        current_index=current_index,
        current_load_count=current_load_count,
        completed_indices=completed_indices,
        last_error_code=last_error_code,
    )


def save_location_search_plan(
    query: LocationCacheQuery,
    plan: LocationSearchPlan,
    *,
    eligible_total: int,
) -> None:
    """将一个完整计划 JSON 与累计有效地点数原子写入缓存。"""

    safe_query = _query(query)
    safe_total = _eligible_total(eligible_total)
    payload = _search_plan_payload(plan)
    if payload["originalKeyword"] != safe_query.keyword:
        raise DouyinLocationCacheError("地点搜索进度计划关键词不匹配")
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO douyin_location_search_progress (
                accountId, scope, keyword, commissionFilter, planJson,
                eligibleTotal, updatedAt
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(accountId, scope, keyword, commissionFilter)
            DO UPDATE SET
                planJson = excluded.planJson,
                eligibleTotal = excluded.eligibleTotal,
                updatedAt = excluded.updatedAt
            """,
            (
                safe_query.account_id,
                safe_query.scope,
                safe_query.keyword,
                safe_query.commission_filter,
                payload_json,
                safe_total,
                _timestamp(None),
            ),
        )


def load_location_search_plan(query: LocationCacheQuery) -> LocationSearchPlan | None:
    """读取完整计划；损坏或未知版本绝不降级为已完成。"""

    safe_query = _query(query)
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT planJson, eligibleTotal
            FROM douyin_location_search_progress
            WHERE accountId = ? AND scope = ? AND keyword = ?
              AND commissionFilter = ?
            """,
            (
                safe_query.account_id,
                safe_query.scope,
                safe_query.keyword,
                safe_query.commission_filter,
            ),
        ).fetchone()
    if row is None:
        return None
    _eligible_total(row["eligibleTotal"])
    try:
        payload = json.loads(row["planJson"])
    except (TypeError, json.JSONDecodeError) as error:
        raise DouyinLocationCacheError("地点搜索进度 JSON 无效") from error
    plan = _plan_from_payload(payload)
    if plan.original_keyword != safe_query.keyword:
        raise DouyinLocationCacheError("地点搜索进度计划关键词不匹配")
    return plan


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
        "firstSeenAt": str(row["firstSeenAt"]),
        "lastSelectedAt": str(row["lastSelectedAt"] or ""),
        "lastPublishSuccessAt": str(row["lastPublishSuccessAt"] or ""),
        "lastFailureAt": str(row["lastFailureAt"] or ""),
        "lastErrorCode": str(row["lastErrorCode"] or ""),
    }


def _candidate_identity(
    candidate: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    return tuple(
        str(candidate[key])
        for key in ("poiId", "name", "address", "commissionType")
    )


def _evicted_location_key(
    account_id: object,
    scope: object,
    candidate: Mapping[str, Any],
) -> tuple[str, ...]:
    return (
        str(database.DB_PATH),
        str(account_id),
        str(scope),
        *_candidate_identity(candidate),
    )


def _prune_evicted_location_tombstones(current: datetime) -> None:
    expired = [
        key
        for key, (evicted_at, _row) in _evicted_location_tombstones.items()
        if current >= evicted_at
        and current - evicted_at >= _LOCATION_EVICTION_TOMBSTONE_TTL
    ]
    for key in expired:
        _evicted_location_tombstones.pop(key, None)
    while len(_evicted_location_tombstones) > _LOCATION_EVICTION_TOMBSTONE_CAPACITY:
        _evicted_location_tombstones.popitem(last=False)


def _remember_evicted_locations(
    rows: list[Mapping[str, Any]],
    *,
    now: datetime,
) -> None:
    with _evicted_location_lock:
        _prune_evicted_location_tombstones(now)
        for row in rows:
            key = _evicted_location_key(row["accountId"], row["scope"], row)
            _evicted_location_tombstones[key] = (now, dict(row))
            _evicted_location_tombstones.move_to_end(key)
        _prune_evicted_location_tombstones(now)


def _take_recent_evicted_location(
    account_id: str,
    candidate: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    key = _evicted_location_key(account_id, candidate["scope"], candidate)
    with _evicted_location_lock:
        _prune_evicted_location_tombstones(now)
        entry = _evicted_location_tombstones.pop(key, None)
    if entry is None:
        return None
    evicted_at, row = entry
    if now >= evicted_at and now - evicted_at >= _LOCATION_EVICTION_TOMBSTONE_TTL:
        return None
    return row


def _discard_evicted_location(
    account_id: str,
    candidate: Mapping[str, Any],
) -> None:
    key = _evicted_location_key(account_id, candidate["scope"], candidate)
    with _evicted_location_lock:
        _evicted_location_tombstones.pop(key, None)


def _restore_recent_reusable_eviction(
    conn: Any,
    account_id: str,
    candidate: Mapping[str, Any],
    *,
    selected_at: datetime,
) -> Mapping[str, Any] | None:
    row = _take_recent_evicted_location(
        account_id,
        candidate,
        now=selected_at,
    )
    if row is None or row.get("status") != LOCATION_STATUS_REUSABLE:
        return None
    verified_at = _parse_timestamp(row.get("verifiedAt"))
    if verified_at is None or selected_at - verified_at >= LOCATION_REVALIDATE_AFTER:
        return None
    timestamp = selected_at.isoformat()
    conn.execute(
        """
        INSERT INTO douyin_location_cache (
            accountId, scope, poiId, name, address, commissionType,
            distance, source, productCount, commissionProductCount,
            commissionLabel, status, verifiedAt, firstSeenAt, lastSeenAt,
            lastSelectedAt, lastPublishSuccessAt, lastFailureAt,
            lastErrorCode, revalidationFailures
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["accountId"],
            row["scope"],
            row["poiId"],
            row["name"],
            row["address"],
            row["commissionType"],
            row["distance"],
            row["source"],
            row["productCount"],
            row["commissionProductCount"],
            row["commissionLabel"],
            row["status"],
            row["verifiedAt"],
            row["firstSeenAt"],
            row["lastSeenAt"],
            timestamp,
            row["lastPublishSuccessAt"],
            row["lastFailureAt"],
            row["lastErrorCode"],
            row["revalidationFailures"],
        ),
    )
    return conn.execute(
        """
        SELECT id FROM douyin_location_cache
        WHERE accountId = ? AND scope = ? AND poiId = ? AND name = ?
          AND address = ? AND commissionType = ?
        """,
        (
            account_id,
            candidate["scope"],
            candidate["poiId"],
            candidate["name"],
            candidate["address"],
            candidate["commissionType"],
        ),
    ).fetchone()


def _evict_excess_locations(
    conn: Any,
    query: LocationCacheQuery,
    *,
    now: datetime,
) -> None:
    rows = conn.execute(
        """
        SELECT keyword.locationCacheId, keyword.position, cache.status,
               cache.verifiedAt, cache.lastSeenAt, cache.lastSelectedAt,
               cache.lastPublishSuccessAt, cache.lastFailureAt
        FROM douyin_location_cache_keywords AS keyword
        JOIN douyin_location_cache AS cache
          ON cache.id = keyword.locationCacheId
        WHERE keyword.accountId = ? AND keyword.scope = ?
          AND keyword.keyword = ? AND keyword.commissionFilter = ?
        """,
        (
            query.account_id,
            query.scope,
            _storage_keyword(query),
            query.commission_filter,
        ),
    ).fetchall()
    if len(rows) <= LOCATION_CACHE_CAPACITY:
        return
    cutoff = now - LOCATION_REVALIDATE_AFTER

    def reusable(row: Mapping[str, Any]) -> bool:
        verified_at = _parse_timestamp(row["verifiedAt"])
        return (
            row["status"] == LOCATION_STATUS_REUSABLE
            and verified_at is not None
            and verified_at > cutoff
        )

    def priority(row: Mapping[str, Any]) -> tuple[object, ...]:
        publish_at = _parse_timestamp(row["lastPublishSuccessAt"])
        selected_at = _parse_timestamp(row["lastSelectedAt"])
        verified_at = _parse_timestamp(row["verifiedAt"])
        return (
            publish_at is not None,
            publish_at or datetime.min.replace(tzinfo=timezone.utc),
            selected_at is not None,
            selected_at or datetime.min.replace(tzinfo=timezone.utc),
            verified_at or datetime.min.replace(tzinfo=timezone.utc),
            -int(row["position"]),
            -int(row["locationCacheId"]),
        )

    reusable_rows = sorted(
        (row for row in rows if reusable(row)), key=priority, reverse=True
    )
    history_rows = sorted(
        (row for row in rows if not reusable(row)),
        key=lambda row: (
            _parse_timestamp(row["lastFailureAt"])
            or _parse_timestamp(row["lastSeenAt"])
            or datetime.min.replace(tzinfo=timezone.utc),
            -int(row["position"]),
            -int(row["locationCacheId"]),
        ),
        reverse=True,
    )
    kept_rows = reusable_rows[:LOCATION_CACHE_CAPACITY]
    kept_rows.extend(
        history_rows[: max(0, LOCATION_CACHE_CAPACITY - len(kept_rows))]
    )
    kept_ids = {int(row["locationCacheId"]) for row in kept_rows}
    ids = [
        int(row["locationCacheId"])
        for row in rows
        if int(row["locationCacheId"]) not in kept_ids
    ]
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
            _storage_keyword(query),
            query.commission_filter,
            *ids,
        ),
    )
    orphan_rows = conn.execute(
        f"""
        SELECT cache.* FROM douyin_location_cache AS cache
        WHERE cache.id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM douyin_location_cache_keywords AS keyword
              WHERE keyword.locationCacheId = cache.id
          )
        """,
        ids,
    ).fetchall()
    _remember_evicted_locations(orphan_rows, now=now)
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
    excluded_identities: object | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """读取一个账号、范围、关键词和返佣筛选下可直接复用的缓存页。"""

    safe_query = _query(query)
    safe_offset = _pagination(offset, "offset", minimum=0)
    safe_limit = _pagination(limit, "limit", minimum=1)
    if safe_limit != LOCATION_CACHE_PAGE_SIZE:
        raise DouyinLocationCacheError(f"地点缓存limit必须为{LOCATION_CACHE_PAGE_SIZE}")
    excluded: set[tuple[str, str, str, str]] | None = None
    if excluded_identities is not None:
        if safe_offset != 0:
            raise DouyinLocationCacheError("稳定地点分页不接受offset")
        if not isinstance(excluded_identities, list):
            raise DouyinLocationCacheError("地点缓存排除集无效")
        normalized_excluded = [
            _candidate(candidate) for candidate in excluded_identities
        ]
        excluded = {
            _candidate_identity(candidate) for candidate in normalized_excluded
        }
        if len(excluded) != len(normalized_excluded):
            raise DouyinLocationCacheError("地点缓存排除集出现重复完整身份")
    current = _now(now)
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT cache.poiId, cache.name, cache.address, cache.distance, cache.source,
                   cache.commissionType, cache.productCount, cache.commissionProductCount,
                   cache.commissionLabel, cache.status, cache.verifiedAt, cache.scope,
                   cache.firstSeenAt, cache.lastSelectedAt, cache.lastPublishSuccessAt,
                   cache.lastFailureAt, cache.lastErrorCode
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
                _storage_keyword(safe_query),
                safe_query.commission_filter,
            ),
        ).fetchall()
    public_rows = list(
        filter_locations_for_search_keyword(
            safe_query.keyword,
            [_public(row) for row in rows],
            province_context=safe_query.province_context,
        )
    )
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
    if excluded is None:
        page = reusable[safe_offset : safe_offset + safe_limit]
        has_more = safe_offset + len(page) < len(reusable)
    else:
        unseen = [
            row for row in reusable if _candidate_identity(row) not in excluded
        ]
        page = unseen[:safe_limit]
        has_more = len(page) < len(unseen)
    return {
        "candidates": page,
        "offset": safe_offset,
        "limit": safe_limit,
        "total": len(reusable),
        "hasMore": has_more,
        "requiresRevalidation": bool(requires_revalidation),
    }


def _normalize_candidates_for_query(
    query: LocationCacheQuery,
    candidates: object,
) -> list[dict[str, Any]]:
    if not isinstance(candidates, list):
        raise DouyinLocationCacheError("地点缓存候选列表无效")
    normalized = [
        dict(candidate)
        for candidate in filter_locations_for_search_keyword(
            query.keyword,
            [_candidate(candidate) for candidate in candidates],
            province_context=query.province_context,
        )
    ]
    _validate_candidates_for_query(query, normalized)
    identities = {_candidate_identity(candidate) for candidate in normalized}
    if len(identities) != len(normalized):
        raise DouyinLocationCacheError("地点缓存候选出现重复完整身份")
    return normalized


def _merge_candidates_in_connection(
    conn: Any,
    query: LocationCacheQuery,
    candidates: list[Mapping[str, Any]],
    *,
    now: datetime,
) -> list[int]:
    timestamp = now.isoformat()
    location_ids: list[int] = []
    for candidate in candidates:
        conn.execute(
            """
            INSERT INTO douyin_location_cache (
                accountId, scope, poiId, name, address, commissionType, distance,
                source, productCount, commissionProductCount, commissionLabel, status,
                verifiedAt, firstSeenAt, lastSeenAt, revalidationFailures
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
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
                lastErrorCode = '',
                revalidationFailures = 0
            """,
            (
                query.account_id,
                query.scope,
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
                query.account_id,
                query.scope,
                candidate["poiId"],
                candidate["name"],
                candidate["address"],
                candidate["commissionType"],
            ),
        ).fetchone()
        location_ids.append(int(cache_row["id"]))
    return location_ids


def _associate_keywords_in_connection(
    conn: Any,
    query: LocationCacheQuery,
    location_ids: list[int],
    *,
    now: datetime,
) -> None:
    for position, location_id in enumerate(location_ids):
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
                location_id,
                query.account_id,
                query.scope,
                _storage_keyword(query),
                query.commission_filter,
                position,
            ),
        )
    _evict_excess_locations(conn, query, now=now)


def merge_platform_locations(
    query: LocationCacheQuery,
    candidates: object,
    *,
    verified_at: datetime | None = None,
) -> dict[str, object]:
    """原子合并一次平台地点读回，并更新当前关键词的关联顺序。"""

    safe_query = _query(query)
    normalized = _normalize_candidates_for_query(safe_query, candidates)
    current = _now(verified_at)
    with database.connect() as conn:
        location_ids = _merge_candidates_in_connection(
            conn,
            safe_query,
            normalized,
            now=current,
        )
        _associate_keywords_in_connection(
            conn,
            safe_query,
            location_ids,
            now=current,
        )
    return get_cached_locations(safe_query, now=current)


def merge_platform_locations_for_queries(
    queries: list[LocationCacheQuery],
    candidates: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """将同一平台页的候选原子关联到省份词和实际城市词。"""

    if not isinstance(queries, list):
        raise DouyinLocationCacheError("地点缓存查询列表无效")
    safe_queries = [_query(item) for item in queries]
    if not safe_queries:
        raise DouyinLocationCacheError("地点缓存查询不能为空")
    root_query = safe_queries[0]
    if any(
        query.account_id != root_query.account_id or query.scope != root_query.scope
        for query in safe_queries[1:]
    ):
        raise DouyinLocationCacheError("地点缓存查询账号或范围不一致")
    normalized = _normalize_candidates_for_query(root_query, candidates)
    current = _now(None)
    with database.connect() as conn:
        location_ids = _merge_candidates_in_connection(
            conn,
            root_query,
            normalized,
            now=current,
        )
        location_ids_by_identity = {
            _candidate_identity(candidate): location_id
            for candidate, location_id in zip(normalized, location_ids, strict=True)
        }
        for query in safe_queries:
            query_candidates = list(
                filter_locations_for_search_keyword(
                    query.keyword,
                    normalized,
                    province_context=query.province_context,
                )
            )
            _validate_candidates_for_query(query, query_candidates)
            _associate_keywords_in_connection(
                conn,
                query,
                [
                    location_ids_by_identity[_candidate_identity(candidate)]
                    for candidate in query_candidates
                ],
                now=current,
            )
    return get_cached_locations(root_query, excluded_identities=[])


def reconcile_platform_locations(
    query: LocationCacheQuery,
    candidates: object,
    *,
    confirmed_exhausted: bool,
    verified_at: datetime | None = None,
) -> dict[str, object]:
    """原子刷新平台集合，并只在确认穷尽后累计缺失校对次数。"""

    safe_query = _query(query)
    if type(confirmed_exhausted) is not bool:
        raise DouyinLocationCacheError("地点缓存穷尽状态无效")
    if not isinstance(candidates, list):
        raise DouyinLocationCacheError("地点缓存候选列表无效")
    normalized = [
        dict(candidate)
        for candidate in filter_locations_for_search_keyword(
            safe_query.keyword,
            [_candidate(candidate) for candidate in candidates],
            province_context=safe_query.province_context,
        )
    ]
    _validate_candidates_for_query(safe_query, normalized)
    returned_identities = {
        _candidate_identity(candidate) for candidate in normalized
    }
    if len(returned_identities) != len(normalized):
        raise DouyinLocationCacheError("地点缓存候选出现重复完整身份")
    current = _now(verified_at)
    timestamp = current.isoformat()
    cutoff = (current - LOCATION_REVALIDATE_AFTER).isoformat()
    with database.connect() as conn:
        for position, candidate in enumerate(normalized):
            conn.execute(
                """
                INSERT INTO douyin_location_cache (
                    accountId, scope, poiId, name, address, commissionType, distance,
                    source, productCount, commissionProductCount, commissionLabel, status,
                    verifiedAt, firstSeenAt, lastSeenAt, revalidationFailures
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
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
                    lastErrorCode = '',
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
                    _storage_keyword(safe_query),
                    safe_query.commission_filter,
                    position,
                ),
            )
        if confirmed_exhausted:
            rows = conn.execute(
                """
                SELECT cache.id, cache.poiId, cache.name, cache.address,
                       cache.commissionType
                FROM douyin_location_cache AS cache
                JOIN douyin_location_cache_keywords AS keyword
                  ON keyword.locationCacheId = cache.id
                WHERE keyword.accountId = ? AND keyword.scope = ?
                  AND keyword.keyword = ? AND keyword.commissionFilter = ?
                  AND (
                    cache.status = ?
                    OR (cache.status = ? AND cache.verifiedAt <= ?)
                  )
                """,
                (
                    safe_query.account_id,
                    safe_query.scope,
                    _storage_keyword(safe_query),
                    safe_query.commission_filter,
                    LOCATION_STATUS_NEEDS_REVALIDATION,
                    LOCATION_STATUS_REUSABLE,
                    cutoff,
                ),
            ).fetchall()
            missing_ids = [
                int(row["id"])
                for row in rows
                if (
                    str(row["poiId"]),
                    str(row["name"]),
                    str(row["address"]),
                    str(row["commissionType"]),
                )
                not in returned_identities
            ]
            if missing_ids:
                placeholders = ", ".join("?" for _ in missing_ids)
                conn.execute(
                    f"""
                    UPDATE douyin_location_cache
                    SET revalidationFailures = revalidationFailures + 1,
                        status = CASE
                            WHEN revalidationFailures + 1 >= 2 THEN ?
                            ELSE ?
                        END
                    WHERE id IN ({placeholders})
                    """,
                    (
                        LOCATION_STATUS_INVALID,
                        LOCATION_STATUS_NEEDS_REVALIDATION,
                        *missing_ids,
                    ),
                )
        _evict_excess_locations(conn, safe_query, now=current)
    return get_cached_locations(safe_query, now=current)


def record_location_selection(
    account_id: str,
    candidate: object,
    *,
    query: LocationCacheQuery | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """Record selection without changing validity; restore its query association."""

    safe_account_id = _text(account_id, "账号")
    safe_candidate = _candidate(candidate, require_scope=True)
    safe_query = _query(query) if query is not None else None
    if safe_query is not None:
        if (
            safe_query.account_id != safe_account_id
            or safe_query.scope != safe_candidate["scope"]
        ):
            raise DouyinLocationCacheError("地点缓存选择查询身份不匹配")
        _validate_candidates_for_query(safe_query, [safe_candidate])
    current = _now(occurred_at)
    timestamp = current.isoformat()
    with database.connect() as conn:
        updated = conn.execute(
            """
            UPDATE douyin_location_cache
            SET lastSelectedAt = ?
            WHERE accountId = ? AND scope = ? AND poiId = ? AND name = ?
              AND address = ? AND commissionType = ?
            """,
            (
                timestamp,
                safe_account_id,
                safe_candidate["scope"],
                safe_candidate["poiId"],
                safe_candidate["name"],
                safe_candidate["address"],
                safe_candidate["commissionType"],
            ),
        )
        cache_row = None
        if updated.rowcount == 1:
            cache_row = conn.execute(
                """
                SELECT id FROM douyin_location_cache
                WHERE accountId = ? AND scope = ? AND poiId = ? AND name = ?
                  AND address = ? AND commissionType = ?
                """,
                (
                    safe_account_id,
                    safe_candidate["scope"],
                    safe_candidate["poiId"],
                    safe_candidate["name"],
                    safe_candidate["address"],
                    safe_candidate["commissionType"],
                ),
            ).fetchone()
        elif safe_query is not None:
            cache_row = _restore_recent_reusable_eviction(
                conn,
                safe_account_id,
                safe_candidate,
                selected_at=current,
            )
        if safe_query is None or cache_row is None:
            return
        position = conn.execute(
            """
            SELECT COALESCE(MAX(position) + 1, 0) AS position
            FROM douyin_location_cache_keywords
            WHERE accountId = ? AND scope = ? AND keyword = ?
              AND commissionFilter = ?
            """,
            (
                safe_query.account_id,
                safe_query.scope,
                _storage_keyword(safe_query),
                safe_query.commission_filter,
            ),
        ).fetchone()["position"]
        conn.execute(
            """
            INSERT INTO douyin_location_cache_keywords (
                locationCacheId, accountId, scope, keyword, commissionFilter, position
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(locationCacheId, keyword, commissionFilter) DO UPDATE SET
                accountId = excluded.accountId,
                scope = excluded.scope
            """,
            (
                cache_row["id"],
                safe_query.account_id,
                safe_query.scope,
                _storage_keyword(safe_query),
                safe_query.commission_filter,
                position,
            ),
        )
        _evict_excess_locations(conn, safe_query, now=current)


def record_location_publish_result(
    account_id: str,
    candidate: object,
    *,
    success: bool,
    query: LocationCacheQuery | None = None,
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
            safe_query = _query(query)
            if (
                safe_query.account_id != safe_account_id
                or safe_query.scope != safe_candidate["scope"]
            ):
                raise DouyinLocationCacheError("地点缓存发布查询身份不匹配")
            _validate_candidates_for_query(safe_query, [safe_candidate])
            conn.execute(
                """
                INSERT INTO douyin_location_cache (
                    accountId, scope, poiId, name, address, commissionType,
                    distance, source, productCount, commissionProductCount,
                    commissionLabel, status, verifiedAt, firstSeenAt, lastSeenAt,
                    lastPublishSuccessAt, lastErrorCode, revalidationFailures
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0)
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
                    lastPublishSuccessAt = excluded.lastPublishSuccessAt,
                    lastErrorCode = '',
                    revalidationFailures = 0
                """,
                (
                    safe_account_id,
                    safe_candidate["scope"],
                    safe_candidate["poiId"],
                    safe_candidate["name"],
                    safe_candidate["address"],
                    safe_candidate["commissionType"],
                    safe_candidate["distance"],
                    safe_candidate["source"],
                    safe_candidate["productCount"],
                    safe_candidate["commissionProductCount"],
                    safe_candidate["commissionLabel"],
                    LOCATION_STATUS_REUSABLE,
                    timestamp,
                    timestamp,
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
                    safe_account_id,
                    safe_candidate["scope"],
                    safe_candidate["poiId"],
                    safe_candidate["name"],
                    safe_candidate["address"],
                    safe_candidate["commissionType"],
                ),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO douyin_location_cache_keywords (
                    locationCacheId, accountId, scope, keyword,
                    commissionFilter, position
                ) VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(locationCacheId, keyword, commissionFilter)
                DO UPDATE SET
                    accountId = excluded.accountId,
                    scope = excluded.scope,
                    position = 0
                """,
                (
                    cache_row["id"],
                    safe_query.account_id,
                    safe_query.scope,
                    _storage_keyword(safe_query),
                    safe_query.commission_filter,
                ),
            )
            _evict_excess_locations(
                conn,
                safe_query,
                now=_now(occurred_at),
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
                lastSeenAt = ?,
                lastFailureAt = ?,
                lastErrorCode = ?
            WHERE accountId = ? AND scope = ? AND poiId = ? AND name = ?
              AND address = ? AND commissionType = ?
            """,
            (
                LOCATION_STATUS_INVALID,
                LOCATION_STATUS_NEEDS_REVALIDATION,
                timestamp,
                timestamp,
                error_code,
                safe_account_id,
                safe_candidate["scope"],
                safe_candidate["poiId"],
                safe_candidate["name"],
                safe_candidate["address"],
                safe_candidate["commissionType"],
            ),
        )
        _discard_evicted_location(safe_account_id, safe_candidate)
