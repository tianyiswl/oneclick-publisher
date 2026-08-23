# -*- coding: utf-8 -*-
"""小红书视频地点候选的本地查询与选择校验服务。

只在保存的小红书会话中查询官方地点接口。候选、选择和缓存均保留
POI ID、名称和完整地址，避免把同名地点误当作同一个地点。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .paths import COOKIE_DIR


XHS_LOCATION_PAGE_URL = (
    "https://creator.xiaohongshu.com/publish/publish?source=official"
)
XHS_LOCATION_SEARCH_ENDPOINT = (
    "https://edith.xiaohongshu.com/web_api/sns/v1/local/poi/creator/search"
)
XHS_PLATFORM = "xiaohongshu"
XHS_PLATFORM_TYPE = 1
DEFAULT_SCOPE = "platform-default"
VIDEO_CONTENT_TYPE = "video"
MAX_RESULTS = 12
_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ITEMS = 60
_cache_lock = threading.Lock()
_cache: dict[
    tuple[int, int, str, str, str], tuple[float, list[dict[str, str]]]
] = {}


class XhsLocationSearchError(RuntimeError):
    """小红书地点查询或选择无法安全完成。"""


def _normalized(value: object) -> str:
    return " ".join(str("" if value is None else value).replace("\u200b", "").split())


def normalize_location_keyword(value: object) -> str:
    keyword = _normalized(value)
    if not keyword:
        raise XhsLocationSearchError("请输入地点搜索词")
    if len(keyword) > 50:
        raise XhsLocationSearchError("地点搜索词不能超过 50 个字")
    return keyword


def _candidate_field(value: dict[str, Any], *names: str) -> str:
    for name in names:
        normalized = _normalized(value.get(name))
        if normalized:
            return normalized
    return ""


def _normalize_location_candidate_fields(
    value: object,
    *,
    poi_id_fields: tuple[str, ...],
    address_fields: tuple[str, ...],
    poi_type_fields: tuple[str, ...] = ("poiType",),
) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    poi_id = _candidate_field(value, *poi_id_fields)
    name = _candidate_field(value, "name")
    address = _candidate_field(value, *address_fields)
    poi_type = _candidate_field(value, *poi_type_fields)
    if not poi_id or not name or not address:
        return None
    return {
        "poiId": poi_id,
        "name": name,
        "address": address,
        "poiType": poi_type,
        "platform": XHS_PLATFORM,
    }


def normalize_official_location_candidate(value: object) -> dict[str, str] | None:
    """只接受 creator/search 原始行提供的官方完整地址字段。"""

    return _normalize_location_candidate_fields(
        value,
        poi_id_fields=("poiId", "newPoiId", "poi_id", "new_poi_id"),
        address_fields=("fullAddress", "full_address"),
        poi_type_fields=("poiType", "poi_type"),
    )


def normalize_canonical_location_candidate(value: object) -> dict[str, str] | None:
    """只接受已标记为小红书的内部 canonical 候选。"""

    if not isinstance(value, dict) or _normalized(value.get("platform")) != XHS_PLATFORM:
        return None
    return _normalize_location_candidate_fields(
        value,
        poi_id_fields=("poiId",),
        address_fields=("address",),
    )


def _poi_rows(value: object) -> list[object]:
    if not isinstance(value, dict):
        raise XhsLocationSearchError("小红书地点服务返回了无效数据")
    rows = value.get("poiList")
    if rows is None:
        rows = value.get("poi_list")
    if rows is None and isinstance(value.get("data"), dict):
        rows = value["data"].get("poiList")
        if rows is None:
            rows = value["data"].get("poi_list")
    if not isinstance(rows, list):
        raise XhsLocationSearchError("小红书地点服务返回了无效数据")
    return rows


def normalize_location_response(
    value: object, *, limit: int | None = MAX_RESULTS
) -> list[dict[str, str]]:
    """校验整页官方候选后，再裁剪界面需要显示的候选数。"""

    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        raise XhsLocationSearchError("地点候选数量无效")
    result: list[dict[str, str]] = []
    by_poi_id: dict[str, dict[str, str]] = {}
    for row in _poi_rows(value):
        candidate = normalize_official_location_candidate(row)
        if not candidate:
            continue
        existing = by_poi_id.get(candidate["poiId"])
        if existing is not None:
            if (
                existing["name"] != candidate["name"]
                or existing["address"] != candidate["address"]
            ):
                raise XhsLocationSearchError("小红书地点候选出现重复 POI，无法安全选择")
            continue
        by_poi_id[candidate["poiId"]] = candidate
        result.append(candidate)
    return result if limit is None else result[:limit]


def _match_identity(value: object) -> tuple[str, str, str] | None:
    if not isinstance(value, dict):
        return None
    poi_id = _candidate_field(value, "poiId", "newPoiId")
    name = _candidate_field(value, "name")
    address = _candidate_field(value, "fullAddress", "address")
    if not poi_id or not name or not address:
        return None
    return poi_id, name, address


def location_match_indexes(target: object, candidates: object) -> list[int]:
    """返回与目标 POI 的 ID、名称、完整地址均一致的候选下标。"""

    identity = _match_identity(target)
    if not identity or not isinstance(candidates, list):
        return []
    indexes: list[int] = []
    for index, candidate in enumerate(candidates):
        if _match_identity(candidate) == identity:
            indexes.append(index)
    return indexes


def _account_identity(account: object) -> int:
    if not isinstance(account, dict):
        raise XhsLocationSearchError("小红书账号记录无效")
    try:
        account_id = int(account.get("id") or 0)
        platform_type = int(account.get("type") or 0)
    except (TypeError, ValueError) as exc:
        raise XhsLocationSearchError("小红书账号记录无效") from exc
    if account_id <= 0 or platform_type != XHS_PLATFORM_TYPE:
        raise XhsLocationSearchError("请选择有效的小红书账号")
    return account_id


def _normalize_scope(value: object) -> str:
    scope = _normalized(value) or DEFAULT_SCOPE
    if scope != DEFAULT_SCOPE:
        raise XhsLocationSearchError("小红书地点搜索范围无效")
    return scope


def _normalize_content_type(value: object) -> str:
    content_type = _normalized(value) or VIDEO_CONTENT_TYPE
    if content_type != VIDEO_CONTENT_TYPE:
        raise XhsLocationSearchError("小红书地点仅支持视频")
    return content_type


def _positive_int(value: object, error: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise XhsLocationSearchError(error) from exc
    if result <= 0:
        raise XhsLocationSearchError(error)
    return result


def _nonempty(value: object) -> bool:
    if isinstance(value, dict):
        return bool(value)
    return bool(_normalized(value))


def normalize_location_selection(
    payload: object, *, expected_account_id: int | None = None
) -> dict[str, object] | None:
    """在浏览器启动前确认小红书视频地点选择仍属于当前账号和搜索词。"""

    if not isinstance(payload, dict):
        raise XhsLocationSearchError("小红书地点选择无效")
    try:
        platform_type = int(payload.get("type") or 0)
    except (TypeError, ValueError) as exc:
        raise XhsLocationSearchError("小红书地点仅支持小红书视频") from exc
    if platform_type != XHS_PLATFORM_TYPE:
        raise XhsLocationSearchError("小红书地点仅支持小红书视频")
    _normalize_content_type(payload.get("contentType"))
    for key in ("locationKeyword", "locationPoi", "locationScope"):
        if _nonempty(payload.get(key)):
            raise XhsLocationSearchError("小红书地点不能使用通用地点字段")

    keyword_value = payload.get("xhsLocationKeyword")
    scope_value = payload.get("xhsLocationScope")
    poi_value = payload.get("xhsLocationPoi")
    if not _nonempty(keyword_value) and not _nonempty(scope_value) and not _nonempty(poi_value):
        return None
    keyword = normalize_location_keyword(keyword_value)
    scope = _normalize_scope(scope_value)
    if not isinstance(poi_value, dict):
        raise XhsLocationSearchError("请选择完整的小红书地点")
    raw_platform = _normalized(poi_value.get("platform"))
    if raw_platform != XHS_PLATFORM:
        raise XhsLocationSearchError("地点平台与小红书不一致")
    if _positive_int(poi_value.get("platformType"), "地点平台类型无效") != XHS_PLATFORM_TYPE:
        raise XhsLocationSearchError("地点平台类型无效")
    candidate = normalize_canonical_location_candidate(poi_value)
    if not candidate:
        raise XhsLocationSearchError("请选择完整的小红书地点")
    source_account_id = _positive_int(
        poi_value.get("sourceAccountId"), "地点来源账号无效"
    )
    if not _normalized(poi_value.get("scope")):
        raise XhsLocationSearchError("地点选择上下文无效")
    selected_scope = _normalize_scope(poi_value.get("scope"))
    if not _normalized(poi_value.get("contentType")):
        raise XhsLocationSearchError("地点选择上下文无效")
    selected_content_type = _normalize_content_type(poi_value.get("contentType"))
    selected_keyword = normalize_location_keyword(poi_value.get("searchKeyword"))
    if selected_keyword != keyword:
        raise XhsLocationSearchError("地点选择与当前搜索词不一致")
    if selected_scope != scope or selected_content_type != VIDEO_CONTENT_TYPE:
        raise XhsLocationSearchError("地点选择上下文无效")
    account_ids = payload.get("accountIds")
    if not isinstance(account_ids, list) or len(account_ids) != 1:
        raise XhsLocationSearchError("小红书地点只能选择一个账号")
    account_id = _positive_int(account_ids[0], "小红书账号无效")
    if account_id != source_account_id:
        raise XhsLocationSearchError("地点来源账号与当前账号不一致")
    if expected_account_id is not None:
        expected = _positive_int(expected_account_id, "期望账号无效")
        if expected != source_account_id:
            raise XhsLocationSearchError("地点来源账号与当前账号不一致")
    return {
        **candidate,
        "sourceAccountId": source_account_id,
        "platformType": XHS_PLATFORM_TYPE,
        "scope": scope,
        "contentType": VIDEO_CONTENT_TYPE,
        "searchKeyword": keyword,
    }


def _cached(key: tuple[int, int, str, str, str]) -> list[dict[str, str]] | None:
    with _cache_lock:
        value = _cache.get(key)
        if not value:
            return None
        stored_at, rows = value
        if time.monotonic() - stored_at >= _CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        return deepcopy(rows)


def _store_cache(key: tuple[int, int, str, str, str], rows: list[dict[str, str]]) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ITEMS and key not in _cache:
            oldest = min(_cache, key=lambda cached_key: _cache[cached_key][0])
            _cache.pop(oldest, None)
        _cache[key] = (time.monotonic(), deepcopy(rows))


def _storage_state(account: dict[str, Any]) -> Path:
    state = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not state.is_file():
        raise XhsLocationSearchError("一键发本地小红书会话不存在，请先重新登录")
    return state


async def _search(account: dict[str, Any], keyword: str) -> list[dict[str, str]]:
    """在官方编辑页同源请求 POI；该私有边界由离线测试替身。"""

    state = _storage_state(account)
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = None
        operation_completed = False
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(XHS_LOCATION_PAGE_URL, wait_until="domcontentloaded", timeout=45_000)
            current_url = urlsplit(str(page.url))
            if (
                current_url.scheme != "https"
                or current_url.hostname != "creator.xiaohongshu.com"
                or current_url.path != "/publish/publish"
            ):
                raise XhsLocationSearchError("小红书会话已失效，请先在账号管理中重新登录")
            response = await page.evaluate(
                """async (request) => {
                    const controller = new AbortController();
                    const timeout = setTimeout(() => controller.abort(), 12000);
                    try {
                        const result = await fetch(request.endpoint, {
                            method: request.method,
                            credentials: request.credentials,
                            headers: request.headers,
                            body: JSON.stringify(request.body),
                            signal: controller.signal,
                        });
                        return { httpStatus: result.status, data: await result.json().catch(() => null) };
                    } finally { clearTimeout(timeout); }
                }""",
                {
                    "endpoint": XHS_LOCATION_SEARCH_ENDPOINT,
                    "method": "POST",
                    "credentials": "include",
                    "headers": {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    "body": {
                        "latitude": 0,
                        "longitude": 0,
                        "keyword": keyword,
                        "page": 1,
                        "size": 50,
                        "source": "WEB",
                        "type": 3,
                    },
                },
            )
            if not isinstance(response, dict) or int(response.get("httpStatus") or 0) != 200:
                status = response.get("httpStatus") if isinstance(response, dict) else "未知"
                raise XhsLocationSearchError(f"小红书地点服务请求失败：HTTP {status}")
            rows = normalize_location_response(response.get("data"))
            operation_completed = True
            return rows
        except XhsLocationSearchError:
            raise
        except Exception as exc:
            if "abort" in str(exc).lower():
                raise XhsLocationSearchError("小红书地点搜索超时，请稍后重试") from exc
            raise XhsLocationSearchError(f"小红书地点搜索异常：{exc}") from exc
        finally:
            cleanup_error: BaseException | None = None
            if context:
                try:
                    await context.close()
                except BaseException as exc:
                    cleanup_error = exc
            try:
                await browser.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            if operation_completed and cleanup_error is not None:
                raise cleanup_error


def search_xhs_locations(
    account: dict[str, Any],
    keyword: object,
    scope: object = DEFAULT_SCOPE,
    content_type: object = VIDEO_CONTENT_TYPE,
) -> list[dict[str, str]]:
    """同步查询入口，按账号、平台、范围、内容类型和完整关键词短缓存。"""

    account_id = _account_identity(account)
    normalized_keyword = normalize_location_keyword(keyword)
    normalized_scope = _normalize_scope(scope)
    normalized_content_type = _normalize_content_type(content_type)
    key = (
        account_id,
        XHS_PLATFORM_TYPE,
        normalized_scope,
        normalized_content_type,
        normalized_keyword,
    )
    cached = _cached(key)
    if cached is not None:
        return cached[:MAX_RESULTS]
    rows = asyncio.run(_search(dict(account), normalized_keyword))
    _store_cache(key, rows)
    return deepcopy(rows[:MAX_RESULTS])
