# -*- coding: utf-8 -*-
"""抖音地点候选的本地查询服务。

服务只在一键发保存的抖音会话中打开官方创作页，通过页面同源
的官方地点查询返回 POI 名称、地址和距离。不读取或导出 Cookie，
不上传素材，不创建草稿，不点击发布。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import threading
import time
from typing import Any

from .paths import COOKIE_DIR


DOUYIN_LOCATION_PAGE_URL = "https://creator.douyin.com/creator-micro/content/upload"
DOUYIN_LOCATION_SEARCH_ENDPOINT = "/aweme/v1/life/video_api/search/poi/"
MIN_KEYWORD_LENGTH = 2
MAX_KEYWORD_LENGTH = 50
MAX_RESULTS = 12
_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ITEMS = 60
_cache_lock = threading.Lock()
_cache: dict[tuple[int, str], tuple[float, list[dict[str, str]]]] = {}


class DouyinLocationSearchError(RuntimeError):
    """抖音地点查询无法安全完成。"""


def _normalized(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", "").split())


def normalize_location_keyword(value: object) -> str:
    keyword = _normalized(value)
    if len(keyword) < MIN_KEYWORD_LENGTH:
        raise DouyinLocationSearchError(f"请至少输入 {MIN_KEYWORD_LENGTH} 个字搜索地点")
    if len(keyword) > MAX_KEYWORD_LENGTH:
        raise DouyinLocationSearchError(f"地点搜索词不能超过 {MAX_KEYWORD_LENGTH} 个字")
    return keyword


def _distance_text(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        distance = float(value)
        if distance < 0:
            return ""
        return f"{distance / 1000:.1f}km" if distance >= 1000 else f"{distance:.0f}m"
    return _normalized(value)


def normalize_location_candidate(value: object) -> dict[str, str] | None:
    """只保留客户端选择与发布回读所需的非敏感 POI 字段。"""

    if not isinstance(value, dict):
        return None
    poi_id = _normalized(value.get("poiId") or value.get("poi_id") or value.get("id"))
    name = _normalized(value.get("name") or value.get("poi_name"))
    address_info = value.get("address_info")
    if isinstance(address_info, dict):
        address = _normalized(
            address_info.get("simple_addr")
            or address_info.get("address")
            or address_info.get("formatted_address")
        )
    else:
        address = ""
    address = _normalized(value.get("address") or value.get("poiAddress") or address)
    distance = _distance_text(value.get("distance"))
    if not poi_id or not name:
        return None
    return {
        "poiId": poi_id,
        "name": name,
        "address": address,
        "distance": distance,
    }


def normalize_location_response(value: object) -> list[dict[str, str]]:
    """规范化官方页面返回的地点结果，并按 POI ID 去重。"""

    if not isinstance(value, dict):
        raise DouyinLocationSearchError("抖音地点服务返回了无效数据")
    try:
        status_code = int(value.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = -1
    if status_code != 0:
        message = _normalized(value.get("status_msg")) or f"状态码 {status_code}"
        raise DouyinLocationSearchError(f"抖音地点搜索失败：{message}")

    rows = []
    for key in ("current_locs", "poi_list"):
        items = value.get(key)
        if isinstance(items, list):
            rows.extend(items)
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        candidate = normalize_location_candidate(row)
        if not candidate or candidate["poiId"] in seen:
            continue
        seen.add(candidate["poiId"])
        result.append(candidate)
        if len(result) >= MAX_RESULTS:
            break
    return result


def _account_identity(account: dict) -> int:
    try:
        account_id = int(account.get("id") or 0)
        platform_type = int(account.get("type") or 0)
    except (TypeError, ValueError) as exc:
        raise DouyinLocationSearchError("抖音账号记录无效") from exc
    if account_id <= 0 or platform_type != 3:
        raise DouyinLocationSearchError("请选择有效的抖音账号")
    return account_id


def _storage_state(account: dict) -> Path:
    state = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not state.is_file():
        raise DouyinLocationSearchError("一键发本地抖音会话不存在，请先重新登录")
    return state


def _cached(account_id: int, keyword: str) -> list[dict[str, str]] | None:
    with _cache_lock:
        value = _cache.get((account_id, keyword))
        if not value:
            return None
        stored_at, rows = value
        if time.monotonic() - stored_at > _CACHE_TTL_SECONDS:
            _cache.pop((account_id, keyword), None)
            return None
        return deepcopy(rows)


def _store_cache(account_id: int, keyword: str, rows: list[dict[str, str]]) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ITEMS:
            oldest = min(_cache, key=lambda key: _cache[key][0])
            _cache.pop(oldest, None)
        _cache[(account_id, keyword)] = (time.monotonic(), deepcopy(rows))


async def _search(account: dict, keyword: str) -> list[dict[str, str]]:
    state = _storage_state(account)
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = None
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(
                DOUYIN_LOCATION_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            if "creator.douyin.com" not in page.url:
                raise DouyinLocationSearchError("抖音会话已失效，请先在账号管理中重新登录")
            response = await page.evaluate(
                """async ({ endpoint, keyword, count }) => {
                    const controller = new AbortController();
                    const timeout = setTimeout(() => controller.abort(), 12000);
                    try {
                        const params = new URLSearchParams({
                            keyword,
                            count: String(count),
                            from_webapp: '1',
                        });
                        const result = await fetch(`${endpoint}?${params.toString()}`, {
                            credentials: 'include',
                            headers: { Accept: 'application/json, text/plain, */*' },
                            signal: controller.signal,
                        });
                        const data = await result.json().catch(() => null);
                        return { httpStatus: result.status, data };
                    } finally {
                        clearTimeout(timeout);
                    }
                }""",
                {
                    "endpoint": DOUYIN_LOCATION_SEARCH_ENDPOINT,
                    "keyword": keyword,
                    "count": MAX_RESULTS,
                },
            )
            if not isinstance(response, dict) or int(response.get("httpStatus") or 0) != 200:
                status = response.get("httpStatus") if isinstance(response, dict) else "未知"
                raise DouyinLocationSearchError(f"抖音地点服务请求失败：HTTP {status}")
            return normalize_location_response(response.get("data"))
        except DouyinLocationSearchError:
            raise
        except Exception as exc:
            if "abort" in str(exc).lower():
                raise DouyinLocationSearchError("抖音地点搜索超时，请稍后重试") from exc
            raise DouyinLocationSearchError(f"抖音地点搜索异常：{exc}") from exc
        finally:
            if context:
                await context.close()
            await browser.close()


def search_douyin_locations(account: dict, keyword: object) -> list[dict[str, str]]:
    """同步入口：供 PyQt 后台线程读取抖音地点候选。"""

    normalized_keyword = normalize_location_keyword(keyword)
    account_id = _account_identity(account)
    cached = _cached(account_id, normalized_keyword)
    if cached is not None:
        return cached
    rows = asyncio.run(_search(dict(account), normalized_keyword))
    _store_cache(account_id, normalized_keyword, rows)
    return deepcopy(rows)


async def search_douyin_locations_in_editor(page: Any, keyword: object) -> list[dict[str, str]]:
    """通过当前已上传的抖音编辑页可见控件搜索 POI。

    抖音带货流程必须复用同一编辑会话；不能为地点查询另开无头浏览器，也不
    依赖私有接口或签名请求。该操作只打开地点搜索、输入关键词并回读候选，
    不选择候选、不写入地点、更不保存草稿或发布。
    """

    normalized_keyword = normalize_location_keyword(keyword)
    try:
        # 延迟导入，避免 oneclick_preflight 的传统流程与本服务产生导入环。
        from . import oneclick_preflight

        rows = await oneclick_preflight.search_douyin_location_candidates(
            page,
            normalized_keyword,
        )
    except Exception as exc:
        if isinstance(exc, DouyinLocationSearchError):
            raise
        raise DouyinLocationSearchError(_normalized(str(exc)) or "抖音地点搜索失败") from exc
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        candidate = normalize_location_candidate(row)
        if not candidate:
            continue
        if not candidate["address"]:
            # 带货任务必须在客户端和确认页展示完整地点，地址缺失就不把
            # 候选提供给用户，避免后续绑定了无法核对的门店。
            continue
        if candidate["poiId"] in seen:
            raise DouyinLocationSearchError("抖音地点候选出现重复 POI，无法安全选择")
        seen.add(candidate["poiId"])
        result.append(candidate)
    if not result:
        raise DouyinLocationSearchError("抖音页面未返回带完整地址的可选官方地点")
    return result
