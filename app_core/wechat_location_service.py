# -*- coding: utf-8 -*-
"""公众号正文地理位置的候选查询、选择校验与编辑器回读。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .paths import COOKIE_DIR


WECHAT_PLATFORM = "wechat_article"
WECHAT_PLATFORM_TYPE = 10
ARTICLE_CONTENT_TYPE = "article"
TEXT_CONTENT_TYPE = "text"
SUPPORTED_CONTENT_TYPES = {ARTICLE_CONTENT_TYPE, TEXT_CONTENT_TYPE}
ARTICLE_INLINE_SCOPE = "article-inline-poi"
WECHAT_HOME_URL = "https://mp.weixin.qq.com/"
MAX_RESULTS = 12
_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ITEMS = 60
_cache_lock = threading.Lock()
_cache: dict[
    tuple[int, int, str, str, str], tuple[float, list[dict[str, str]]]
] = {}


class WechatLocationError(RuntimeError):
    """公众号地点候选或编辑器回读不能被安全确认。"""


def _normalized(value: object) -> str:
    return " ".join(str("" if value is None else value).replace("\u200b", "").split())


def normalize_location_keyword(value: object) -> str:
    keyword = _normalized(value)
    if not keyword:
        raise WechatLocationError("请输入公众号正文地点搜索词")
    if len(keyword) > 50:
        raise WechatLocationError("公众号正文地点搜索词不能超过 50 个字")
    return keyword


def _field(value: dict[str, Any], *names: str) -> str:
    for name in names:
        result = _normalized(value.get(name))
        if result:
            return result
    return ""


def normalize_official_location_candidate(value: object) -> dict[str, str] | None:
    """只接受公众号官方响应中身份完整的 POI。"""

    if not isinstance(value, dict):
        return None
    candidate = {
        "poiId": _field(value, "poiid", "poiId", "poi_id"),
        "name": _field(value, "name"),
        "address": _field(value, "address", "fullAddress", "full_address"),
        "latitude": _field(value, "latitude", "lat"),
        "longitude": _field(value, "longitude", "lng", "lon"),
        "platform": WECHAT_PLATFORM,
    }
    if not all(candidate[key] for key in ("poiId", "name", "address", "latitude", "longitude")):
        return None
    return candidate


def normalize_canonical_location_candidate(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or _normalized(value.get("platform")) != WECHAT_PLATFORM:
        return None
    return normalize_official_location_candidate(value)


def _candidate_lists(value: object) -> list[list[object]]:
    result: list[list[object]] = []
    if isinstance(value, dict):
        for child in value.values():
            result.extend(_candidate_lists(child))
    elif isinstance(value, list):
        result.append(list(value))
        for child in value:
            result.extend(_candidate_lists(child))
    return result


def normalize_location_response(
    value: object,
    *,
    limit: int | None = MAX_RESULTS,
) -> list[dict[str, str]]:
    """从官方响应中定位 POI 列表，并拒绝同 ID 的冲突身份。"""

    if limit is not None and (
        not isinstance(limit, int) or isinstance(limit, bool) or limit < 0
    ):
        raise WechatLocationError("公众号地点候选数量无效")
    if not isinstance(value, (dict, list)):
        raise WechatLocationError("公众号地点服务返回了无效数据")

    rows: list[dict[str, str]] = []
    for candidate_list in _candidate_lists(value):
        normalized = [
            candidate
            for candidate in (
                normalize_official_location_candidate(item)
                for item in candidate_list
            )
            if candidate is not None
        ]
        if len(normalized) > len(rows):
            rows = normalized

    unique: list[dict[str, str]] = []
    by_id: dict[str, dict[str, str]] = {}
    for candidate in rows:
        existing = by_id.get(candidate["poiId"])
        if existing is not None:
            if existing != candidate:
                raise WechatLocationError("公众号地点候选出现重复 POI，无法安全选择")
            continue
        by_id[candidate["poiId"]] = candidate
        unique.append(candidate)
    return unique if limit is None else unique[:limit]


def _identity(value: object) -> tuple[str, str, str, str, str] | None:
    candidate = normalize_official_location_candidate(value)
    if not candidate:
        return None
    return tuple(
        candidate[key]
        for key in ("poiId", "name", "address", "latitude", "longitude")
    )


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
        raise WechatLocationError(message) from exc
    if result <= 0:
        raise WechatLocationError(message)
    return result


def _account_identity(account: object) -> int:
    if not isinstance(account, dict):
        raise WechatLocationError("公众号账号记录无效")
    if _positive_int(account.get("type"), "公众号账号记录无效") != WECHAT_PLATFORM_TYPE:
        raise WechatLocationError("请选择有效的公众号账号")
    return _positive_int(account.get("id"), "公众号账号记录无效")


def _normalize_scope(value: object) -> str:
    scope = _normalized(value) or ARTICLE_INLINE_SCOPE
    if scope != ARTICLE_INLINE_SCOPE:
        raise WechatLocationError("公众号正文地点范围无效")
    return scope


def _normalize_content_type(value: object) -> str:
    content_type = _normalized(value) or ARTICLE_CONTENT_TYPE
    if content_type not in SUPPORTED_CONTENT_TYPES:
        raise WechatLocationError("公众号正文地点仅支持图文或文字")
    return content_type


def _has_value(value: object) -> bool:
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return bool(_normalized(value))


def normalize_location_selection(
    payload: object,
    *,
    expected_account_id: int | None = None,
) -> dict[str, object] | None:
    """校验选择仍绑定当前账号、正文范围与完整搜索词。"""

    if not isinstance(payload, dict):
        raise WechatLocationError("公众号正文地点选择无效")
    if any(
        _has_value(payload.get(key))
        for key in ("locationKeyword", "locationScope", "locationPoi")
    ):
        raise WechatLocationError("公众号正文地点不能使用通用地点字段")
    if _positive_int(payload.get("type"), "公众号正文地点仅支持公众号文章") != WECHAT_PLATFORM_TYPE:
        raise WechatLocationError("公众号正文地点仅支持公众号文章")
    _normalize_content_type(payload.get("contentType"))

    keyword_value = payload.get("wechatLocationKeyword")
    scope_value = payload.get("wechatLocationScope")
    poi_value = payload.get("wechatLocationPoi")
    if not any(_has_value(value) for value in (keyword_value, scope_value, poi_value)):
        return None
    keyword = normalize_location_keyword(keyword_value)
    scope = _normalize_scope(scope_value)
    candidate = normalize_canonical_location_candidate(poi_value)
    if candidate is None or not isinstance(poi_value, dict):
        raise WechatLocationError("请选择完整的公众号官方地点")
    if _positive_int(poi_value.get("platformType"), "公众号地点平台类型无效") != WECHAT_PLATFORM_TYPE:
        raise WechatLocationError("公众号地点平台类型无效")
    source_account_id = _positive_int(
        poi_value.get("sourceAccountId"), "公众号地点来源账号无效"
    )
    if _normalize_scope(poi_value.get("scope")) != scope:
        raise WechatLocationError("公众号地点选择范围已变化，请重新搜索")
    selected_content_type = _normalize_content_type(poi_value.get("contentType"))
    if selected_content_type != _normalize_content_type(payload.get("contentType")):
        raise WechatLocationError("公众号地点内容类型已变化，请重新搜索")
    if normalize_location_keyword(poi_value.get("searchKeyword")) != keyword:
        raise WechatLocationError("公众号地点选择与当前完整搜索词不一致")
    account_ids = payload.get("accountIds")
    if not isinstance(account_ids, list) or len(account_ids) != 1:
        raise WechatLocationError("公众号正文地点只能选择一个账号")
    if _positive_int(account_ids[0], "公众号账号无效") != source_account_id:
        raise WechatLocationError("公众号地点来源账号与当前账号不一致")
    if expected_account_id is not None and _positive_int(
        expected_account_id, "期望公众号账号无效"
    ) != source_account_id:
        raise WechatLocationError("公众号地点来源账号与当前账号不一致")
    return {
        **candidate,
        "sourceAccountId": source_account_id,
        "platformType": WECHAT_PLATFORM_TYPE,
        "scope": scope,
        "contentType": selected_content_type,
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
            oldest = min(_cache, key=lambda candidate: _cache[candidate][0])
            _cache.pop(oldest, None)
        _cache[key] = (time.monotonic(), deepcopy(rows))


def _storage_state(account: dict[str, Any]) -> Path:
    state = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not state.is_file():
        raise WechatLocationError("一键发本地公众号会话不存在，请先重新登录")
    return state


async def _open_article_editor(page) -> None:
    await page.goto(WECHAT_HOME_URL, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(700)
    token = parse_qs(urlparse(str(page.url)).query).get("token", [""])[0]
    if not token:
        raise WechatLocationError("公众号会话已失效，请先在账号管理中重新登录")
    await page.goto(
        "https://mp.weixin.qq.com/cgi-bin/appmsg"
        f"?action=edit&type=10&lang=zh_CN&token={token}",
        wait_until="domcontentloaded",
        timeout=45_000,
    )
    await page.wait_for_timeout(1_000)
    if "/cgi-bin/appmsg" not in urlparse(str(page.url)).path:
        raise WechatLocationError("公众号文章编辑器未能打开")


async def _visible_nodes(locator) -> list:
    nodes = []
    for index in range(await locator.count()):
        node = locator.nth(index)
        try:
            if await node.is_visible():
                nodes.append(node)
        except Exception:
            continue
    return nodes


async def _open_location_dialog(page):
    triggers = await _visible_nodes(page.locator("#editor_poi"))
    if len(triggers) != 1:
        raise WechatLocationError("公众号正文未找到唯一的“地理位置”入口")
    await triggers[0].click(timeout=8_000)
    inputs = await _visible_nodes(page.locator('input[placeholder="请输入要搜索的地点"]'))
    if len(inputs) != 1:
        raise WechatLocationError("公众号正文地点搜索框未唯一显示")
    return inputs[0]


async def _search_current_dialog(page, input_node, keyword: str) -> tuple[list[dict[str, str]], list]:
    try:
        async with page.expect_response(
            lambda response: urlparse(str(response.url)).path == "/cgi-bin/operate_appmsg",
            timeout=15_000,
        ) as response_info:
            await input_node.fill(keyword, timeout=8_000)
        response = await response_info.value
        if int(response.status) != 200:
            raise WechatLocationError(f"公众号地点服务请求失败：HTTP {response.status}")
        rows = normalize_location_response(await response.json(), limit=None)
    except WechatLocationError:
        raise
    except Exception as exc:
        raise WechatLocationError("公众号正文地点搜索超时或响应无效") from exc
    options = await _visible_nodes(page.locator(".search-association__item"))
    return rows, options


async def _search(account: dict[str, Any], keyword: str) -> list[dict[str, str]]:
    state = _storage_state(account)
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(storage_state=str(state))
            page = await context.new_page()
            await _open_article_editor(page)
            input_node = await _open_location_dialog(page)
            rows, _options = await _search_current_dialog(page, input_node, keyword)
            return rows
        finally:
            await browser.close()


def search_wechat_locations(
    account: dict[str, Any],
    keyword: object,
    scope: object = ARTICLE_INLINE_SCOPE,
    content_type: object = ARTICLE_CONTENT_TYPE,
) -> list[dict[str, str]]:
    """按账号、平台、正文范围、内容类型和完整关键词短缓存。"""

    account_id = _account_identity(account)
    normalized_keyword = normalize_location_keyword(keyword)
    normalized_scope = _normalize_scope(scope)
    normalized_content_type = _normalize_content_type(content_type)
    key = (
        account_id,
        WECHAT_PLATFORM_TYPE,
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


async def apply_wechat_location(
    page,
    payload: dict[str, Any],
    *,
    expected_account_id: int | None = None,
) -> dict[str, object] | None:
    """在当前编辑器重新搜索、精确选择并回读正文地点卡片。"""

    selected = normalize_location_selection(
        payload,
        expected_account_id=expected_account_id,
    )
    if selected is None:
        return None
    input_node = await _open_location_dialog(page)
    rows, options = await _search_current_dialog(
        page,
        input_node,
        str(selected["searchKeyword"]),
    )
    indexes = location_match_indexes(selected, rows)
    if len(indexes) != 1:
        raise WechatLocationError("公众号官方地点已变化，提交前重新核验未通过")
    target_index = indexes[0]
    if target_index >= len(options):
        raise WechatLocationError("公众号地点页面候选与官方响应数量不一致")
    option = options[target_index]
    option_name = _normalized(await option.locator(".poi-name").inner_text())
    option_address = _normalized(await option.locator(".poi-address").inner_text())
    if option_name != selected["name"] or option_address != selected["address"]:
        raise WechatLocationError("公众号地点页面候选与官方响应不一致")
    await option.click(timeout=8_000)

    dialogs = await _visible_nodes(page.locator(".weui-desktop-dialog"))
    location_dialogs = [
        dialog
        for dialog in dialogs
        if "插入地理位置" in _normalized(await dialog.inner_text())
    ]
    if len(location_dialogs) != 1:
        raise WechatLocationError("公众号地点确认弹层不唯一")
    insert_buttons = await _visible_nodes(
        location_dialogs[0].get_by_text("插入", exact=True)
    )
    enabled = [button for button in insert_buttons if await button.is_enabled()]
    if len(enabled) != 1:
        raise WechatLocationError("公众号地点插入控件未唯一启用")
    await enabled[0].click(timeout=8_000)

    await page.wait_for_timeout(250)
    links = await _visible_nodes(page.locator("a.wx_poi_link.js_poi_entry[data-poiid]"))
    readbacks: list[dict[str, str]] = []
    for link in links:
        readbacks.append(
            {
                "poiId": _normalized(await link.get_attribute("data-poiid")),
                "name": _normalized(unquote(await link.get_attribute("data-name") or "")),
                "address": _normalized(unquote(await link.get_attribute("data-address") or "")),
                "latitude": _normalized(await link.get_attribute("data-latitude")),
                "longitude": _normalized(await link.get_attribute("data-longitude")),
                "platform": WECHAT_PLATFORM,
            }
        )
    matched_readbacks = location_match_indexes(selected, readbacks)
    if len(matched_readbacks) != 1:
        raise WechatLocationError("公众号正文地点插入后身份回读不一致")
    return {**selected, "editorReadback": readbacks[matched_readbacks[0]]}
