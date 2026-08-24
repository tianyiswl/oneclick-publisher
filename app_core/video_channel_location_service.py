# -*- coding: utf-8 -*-
"""视频号视频地点候选的查询、选择校验与编辑页重核服务。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import math
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .paths import COOKIE_DIR


VIDEO_CHANNEL_LOCATION_PAGE_URL = "https://channels.weixin.qq.com/platform/"
VIDEO_CHANNEL_LOCATION_SEARCH_ENDPOINT = (
    "https://channels.weixin.qq.com/micro/content/cgi-bin/"
    "mmfinderassistant-bin/helper/helper_search_location"
)
VIDEO_CHANNEL_PLATFORM = "video-channel"
VIDEO_CHANNEL_PLATFORM_TYPE = 2
DEFAULT_SCOPE = "platform-default"
VIDEO_CONTENT_TYPE = "video"
MAX_RESULTS = 12
_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ITEMS = 60
_cache_lock = threading.Lock()
_cache: dict[
    tuple[int, int, str, str, str], tuple[float, list[dict[str, object]]]
] = {}


class VideoChannelLocationError(RuntimeError):
    """视频号地点查询或选择无法安全完成。"""


def _normalized(value: object) -> str:
    return " ".join(str("" if value is None else value).replace("\u200b", "").split())


def normalize_location_keyword(value: object) -> str:
    keyword = _normalized(value)
    if not keyword:
        raise VideoChannelLocationError("请输入地点搜索词")
    if len(keyword) > 50:
        raise VideoChannelLocationError("地点搜索词不能超过 50 个字")
    return keyword


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_official_location_candidate(value: object) -> dict[str, object] | None:
    """只接纳视频号官方响应中的地点 ID、名称和完整地址。"""

    if not isinstance(value, dict):
        return None
    poi_id = _normalized(value.get("uid"))
    name = _normalized(value.get("name"))
    address = _normalized(value.get("fullAddress"))
    if not poi_id or not name or not address:
        return None
    candidate: dict[str, object] = {
        "poiId": poi_id,
        "name": name,
        "address": address,
    }
    longitude = _finite_number(value.get("longitude"))
    latitude = _finite_number(value.get("latitude"))
    if longitude is not None:
        candidate["longitude"] = longitude
    if latitude is not None:
        candidate["latitude"] = latitude
    checksum = _normalized(value.get("poiCheckSum"))
    if checksum:
        candidate["poiCheckSum"] = checksum
    candidate["platform"] = VIDEO_CHANNEL_PLATFORM
    return candidate


def normalize_canonical_location_candidate(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    if _normalized(value.get("platform")) != VIDEO_CHANNEL_PLATFORM:
        return None
    return normalize_official_location_candidate(
        {
            "uid": value.get("poiId"),
            "name": value.get("name"),
            "fullAddress": value.get("address"),
            "longitude": value.get("longitude"),
            "latitude": value.get("latitude"),
            "poiCheckSum": value.get("poiCheckSum"),
        }
    )


def _response_rows(value: object) -> list[object]:
    if not isinstance(value, dict):
        raise VideoChannelLocationError("视频号地点服务返回了无效数据")
    try:
        err_code = int(value.get("errCode"))
    except (TypeError, ValueError) as exc:
        raise VideoChannelLocationError("视频号地点服务返回了无效数据") from exc
    if err_code != 0:
        raise VideoChannelLocationError("视频号地点服务返回失败")
    data = value.get("data")
    rows = data.get("list") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise VideoChannelLocationError("视频号地点服务返回了无效数据")
    return rows


def normalize_location_response(
    value: object,
    *,
    limit: int | None = MAX_RESULTS,
) -> list[dict[str, object]]:
    if limit is not None and (
        not isinstance(limit, int) or isinstance(limit, bool) or limit < 0
    ):
        raise VideoChannelLocationError("地点候选数量无效")
    result: list[dict[str, object]] = []
    by_poi_id: dict[str, dict[str, object]] = {}
    for row in _response_rows(value):
        candidate = normalize_official_location_candidate(row)
        if candidate is None:
            continue
        poi_id = str(candidate["poiId"])
        existing = by_poi_id.get(poi_id)
        if existing is not None:
            if (
                existing["name"] != candidate["name"]
                or existing["address"] != candidate["address"]
            ):
                raise VideoChannelLocationError(
                    "视频号地点候选出现重复地点 ID，无法安全选择"
                )
            continue
        by_poi_id[poi_id] = candidate
        result.append(candidate)
    return result if limit is None else result[:limit]


def _identity(value: object) -> tuple[str, str, str] | None:
    if not isinstance(value, dict):
        return None
    poi_id = _normalized(value.get("poiId") or value.get("uid"))
    name = _normalized(value.get("name"))
    address = _normalized(value.get("address") or value.get("fullAddress"))
    if not poi_id or not name or not address:
        return None
    return poi_id, name, address


def location_match_indexes(target: object, candidates: object) -> list[int]:
    identity = _identity(target)
    if identity is None or not isinstance(candidates, list):
        return []
    return [
        index
        for index, candidate in enumerate(candidates)
        if _identity(candidate) == identity
    ]


def _positive_int(value: object, message: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise VideoChannelLocationError(message) from exc
    if result <= 0:
        raise VideoChannelLocationError(message)
    return result


def _nonempty(value: object) -> bool:
    if isinstance(value, dict):
        return bool(value)
    return bool(_normalized(value))


def _normalize_scope(value: object) -> str:
    scope = _normalized(value) or DEFAULT_SCOPE
    if scope != DEFAULT_SCOPE:
        raise VideoChannelLocationError("视频号地点搜索范围无效")
    return scope


def _normalize_content_type(value: object) -> str:
    content_type = _normalized(value) or VIDEO_CONTENT_TYPE
    if content_type != VIDEO_CONTENT_TYPE:
        raise VideoChannelLocationError("视频号地点仅支持视频")
    return content_type


def _account_identity(account: object) -> int:
    if not isinstance(account, dict):
        raise VideoChannelLocationError("视频号账号记录无效")
    account_id = _positive_int(account.get("id"), "视频号账号记录无效")
    if _positive_int(account.get("type"), "视频号账号记录无效") != VIDEO_CHANNEL_PLATFORM_TYPE:
        raise VideoChannelLocationError("请选择有效的视频号账号")
    return account_id


def normalize_location_selection(
    payload: object,
    *,
    expected_account_id: int | None = None,
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        raise VideoChannelLocationError("视频号地点选择无效")
    for key in ("locationKeyword", "locationPoi", "locationScope"):
        if _nonempty(payload.get(key)):
            raise VideoChannelLocationError("视频号地点不能使用通用地点字段")
    if _positive_int(payload.get("type"), "视频号地点仅支持视频号视频") != VIDEO_CHANNEL_PLATFORM_TYPE:
        raise VideoChannelLocationError("视频号地点仅支持视频号视频")
    _normalize_content_type(payload.get("contentType"))
    keyword_value = payload.get("videoChannelLocationKeyword")
    scope_value = payload.get("videoChannelLocationScope")
    poi_value = payload.get("videoChannelLocationPoi")
    if not any(_nonempty(value) for value in (keyword_value, scope_value, poi_value)):
        return None
    keyword = normalize_location_keyword(keyword_value)
    scope = _normalize_scope(scope_value)
    candidate = normalize_canonical_location_candidate(poi_value)
    if candidate is None or not isinstance(poi_value, dict):
        raise VideoChannelLocationError("请选择完整的视频号地点")
    if _positive_int(poi_value.get("platformType"), "地点平台类型无效") != VIDEO_CHANNEL_PLATFORM_TYPE:
        raise VideoChannelLocationError("地点平台类型无效")
    source_account_id = _positive_int(
        poi_value.get("sourceAccountId"),
        "地点来源账号无效",
    )
    selected_scope = _normalize_scope(poi_value.get("scope"))
    selected_content_type = _normalize_content_type(poi_value.get("contentType"))
    selected_keyword = normalize_location_keyword(poi_value.get("searchKeyword"))
    if selected_keyword != keyword:
        raise VideoChannelLocationError("地点选择与当前搜索词不一致")
    if selected_scope != scope or selected_content_type != VIDEO_CONTENT_TYPE:
        raise VideoChannelLocationError("地点选择上下文无效")
    account_ids = payload.get("accountIds")
    if not isinstance(account_ids, list) or len(account_ids) != 1:
        raise VideoChannelLocationError("视频号地点只能选择一个账号")
    account_id = _positive_int(account_ids[0], "视频号账号无效")
    if account_id != source_account_id:
        raise VideoChannelLocationError("地点来源账号与当前账号不一致")
    if expected_account_id is not None and _positive_int(
        expected_account_id,
        "期望账号无效",
    ) != source_account_id:
        raise VideoChannelLocationError("地点来源账号与当前账号不一致")
    return {
        **candidate,
        "sourceAccountId": source_account_id,
        "platformType": VIDEO_CHANNEL_PLATFORM_TYPE,
        "scope": scope,
        "contentType": VIDEO_CONTENT_TYPE,
        "searchKeyword": keyword,
    }


def _cached(
    key: tuple[int, int, str, str, str],
) -> list[dict[str, object]] | None:
    with _cache_lock:
        value = _cache.get(key)
        if value is None:
            return None
        stored_at, rows = value
        if time.monotonic() - stored_at >= _CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        return deepcopy(rows)


def _store_cache(
    key: tuple[int, int, str, str, str],
    rows: list[dict[str, object]],
) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ITEMS and key not in _cache:
            oldest = min(_cache, key=lambda item: _cache[item][0])
            _cache.pop(oldest, None)
        _cache[key] = (time.monotonic(), deepcopy(rows))


def _storage_state(account: dict[str, Any]) -> Path:
    state = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not state.is_file():
        raise VideoChannelLocationError("一键发本地视频号会话不存在，请先重新登录")
    return state


async def _search(
    account: dict[str, Any],
    keyword: str,
) -> list[dict[str, object]]:
    state = _storage_state(account)
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = None
        completed = False
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await page.goto(
                VIDEO_CHANNEL_LOCATION_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            current = urlsplit(str(page.url))
            if current.scheme != "https" or current.hostname != "channels.weixin.qq.com":
                raise VideoChannelLocationError(
                    "视频号会话已失效，请先在账号管理中重新登录"
                )
            response = await page.evaluate(
                """async request => {
                    const controller = new AbortController();
                    const timeout = setTimeout(() => controller.abort(), 12000);
                    try {
                        const result = await fetch(request.endpoint, {
                            method: 'POST', credentials: 'include',
                            headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                            body: JSON.stringify({
                                query: request.keyword, cookies: '', longitude: 0, latitude: 0,
                                timestamp: String(Date.now()), _log_finder_uin: '',
                                _log_finder_id: '', rawKeyBuff: '', pluginSessionId: null,
                                scene: 7, reqScene: 7,
                            }),
                            signal: controller.signal,
                        });
                        return {httpStatus: result.status, data: await result.json().catch(() => null)};
                    } finally { clearTimeout(timeout); }
                }""",
                {
                    "endpoint": VIDEO_CHANNEL_LOCATION_SEARCH_ENDPOINT,
                    "keyword": keyword,
                },
            )
            status = int(response.get("httpStatus") or 0) if isinstance(response, dict) else 0
            if status < 200 or status >= 300:
                raise VideoChannelLocationError(
                    f"视频号地点服务请求失败：HTTP {status or '未知'}"
                )
            rows = normalize_location_response(response.get("data"))
            completed = True
            return rows
        except VideoChannelLocationError:
            raise
        except Exception as exc:
            if "abort" in str(exc).lower():
                raise VideoChannelLocationError("视频号地点搜索超时，请稍后重试") from exc
            raise VideoChannelLocationError(f"视频号地点搜索异常：{exc}") from exc
        finally:
            cleanup_error: BaseException | None = None
            if context is not None:
                try:
                    await context.close()
                except BaseException as exc:
                    cleanup_error = exc
            try:
                await browser.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            if completed and cleanup_error is not None:
                raise cleanup_error


def search_video_channel_locations(
    account: dict[str, Any],
    keyword: object,
    scope: object = DEFAULT_SCOPE,
    content_type: object = VIDEO_CONTENT_TYPE,
) -> list[dict[str, object]]:
    account_id = _account_identity(account)
    normalized_keyword = normalize_location_keyword(keyword)
    normalized_scope = _normalize_scope(scope)
    normalized_content_type = _normalize_content_type(content_type)
    key = (
        account_id,
        VIDEO_CHANNEL_PLATFORM_TYPE,
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


async def apply_video_channel_location(
    page,
    payload: object,
    *,
    expected_account_id: int | None = None,
) -> dict[str, object] | None:
    """在当前视频编辑页重搜、唯一匹配、选择并回读地点。"""

    selection = normalize_location_selection(
        payload,
        expected_account_id=expected_account_id,
    )
    if selection is None:
        return None

    target_frame = None
    for _attempt in range(10):
        for frame in page.frames:
            if await frame.locator(".post-position-wrap").count():
                target_frame = frame
                break
        if target_frame is not None:
            break
        await page.wait_for_timeout(500)
    if target_frame is None:
        raise VideoChannelLocationError("视频号编辑页未找到位置入口")

    trigger = target_frame.locator(".post-position-wrap").first
    if not await trigger.is_visible(timeout=2_000):
        raise VideoChannelLocationError("视频号编辑页位置入口不可用")
    await trigger.click(timeout=5_000)
    search = target_frame.locator('input[placeholder="搜索附近位置"]').first
    await search.wait_for(state="visible", timeout=5_000)
    keyword = str(selection["searchKeyword"])
    await search.fill(keyword, timeout=5_000)
    search_button = target_frame.locator(".weui-desktop-search__btn").first
    if not await search_button.count() or not await search_button.is_visible(timeout=1_000):
        raise VideoChannelLocationError("视频号地点搜索按钮不可用")

    try:
        async with page.expect_response(
            lambda response: "helper_search_location" in response.url,
            timeout=10_000,
        ) as response_info:
            await search_button.click(timeout=5_000)
        response = await response_info.value
        status = int(response.status or 0)
        if status < 200 or status >= 300:
            raise VideoChannelLocationError(
                f"视频号地点服务请求失败：HTTP {status or '未知'}"
            )
        candidates = normalize_location_response(
            await response.json(),
            limit=None,
        )
    except VideoChannelLocationError:
        raise
    except Exception as exc:
        raise VideoChannelLocationError("视频号当前编辑页地点搜索失败") from exc

    matches = location_match_indexes(selection, candidates)
    if len(matches) != 1:
        raise VideoChannelLocationError(
            f"视频号地点“{keyword}”没有唯一一致的地点身份，已安全停止"
        )

    await page.wait_for_timeout(500)
    items = target_frame.locator(".location-item:visible")
    dom_matches: list[int] = []
    for index in range(await items.count()):
        item = items.nth(index)
        name_locator = item.locator(".name")
        address_locator = item.locator(".desc")
        name = _normalized(await name_locator.inner_text()) if await name_locator.count() else ""
        address = (
            _normalized(await address_locator.inner_text())
            if await address_locator.count()
            else ""
        )
        if name == selection["name"] and address == selection["address"]:
            dom_matches.append(index)
    if len(dom_matches) != 1:
        raise VideoChannelLocationError(
            f"视频号地点“{keyword}”在编辑页没有唯一同名同址候选，已安全停止"
        )

    await items.nth(dom_matches[0]).click(timeout=5_000)
    await page.wait_for_timeout(300)
    readback = _normalized(await trigger.inner_text(timeout=5_000))
    if readback != selection["name"]:
        raise VideoChannelLocationError(
            f"视频号地点回读不一致：期望“{selection['name']}”，页面显示“{readback or '空'}”"
        )
    return selection
