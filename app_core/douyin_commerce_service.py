# -*- coding: utf-8 -*-
"""抖音本地团购带货的本地契约与受控页面适配。

本模块不调用官方 API、签名接口或第三方服务。抖音本地团购的新版编辑器把
“位置 → 带货模式 → 地点输入”做成同一条可见交互；一键发将发布定位与作品
内容声明作为独立、可回读的步骤。候选及声明只从一键发受控浏览器内的可见页面
读取；任何控件、候选或回读不唯一时都会中止，而不会猜测点击。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from time import monotonic
from typing import Any, Mapping

from .douyin_commerce_location_commission import (
    filter_location_candidates,
    normalize_candidate_commission_fields,
    normalize_commission_filter,
    parse_commission_summary,
)
from .douyin_location_service import normalize_location_candidate, normalize_location_keyword
from .douyin_location_preset_service import (
    DouyinLocationPresetError,
    match_location_preset,
)
from .douyin_music_service import (
    FAVORITE_FIRST_MUSIC_MODE,
    FAVORITE_MANUAL_MUSIC_MODE,
    DouyinMusicError,
    normalize_music_readback,
    validate_favorite_music_mode,
)
from .media_path import normalize_media_path
from utils.log import douyin_logger


DOUYIN_COMMERCE_WORKFLOW = "douyin-commerce"
LOCAL_GROUP_BUY_MODE = "local-group-buy"
LOCATION_SCOPE_LOCAL = "local"
LOCATION_SCOPE_DOMESTIC = "domestic"
LOCATION_SCOPE_LABELS = {
    LOCATION_SCOPE_LOCAL: "本地",
    LOCATION_SCOPE_DOMESTIC: "国内",
}
CONTENT_DECLARATION_OPTIONS = (
    "内容由AI生成",
    "内容为个人观点或见解",
    "内容为转载信息",
    "内容含营销推广信息",
    "虚构演绎，仅供娱乐",
    "无需添加自主声明",
)
_COMMERCE_MODE_TEXT = "带货模式"
_CHECKIN_MODE_TEXT = "打卡模式"
_UNSELECTED_MODE_VALUE = "__oneclick_unselected_commerce_mode__"
_ANCHOR_ROOT_ID = "douyin_creator_pc_anchor_jump"
_LOCATION_DIAGNOSTIC_ENV = "ONECLICK_LOCATION_DIAGNOSTIC_DIR"
_LOCATION_DIAGNOSTIC_LABELS = (
    "添加标签",
    "位置",
    "带货模式",
    "打卡模式",
    "本地",
    "国内",
)
_LOCATION_RESULT_WAIT_TIMEOUT_MS = 30_000
_LOCATION_RESULT_POLL_INTERVAL_MS = 350
_LOCATION_RESULT_STABLE_READS = 3
_LOCATION_LOAD_MORE_REAPPEAR_GRACE_MS = 3_000
_PUBLISH_LOCATION_LIMIT_CODE = "publish_location_load_more_limit"
_LOCATION_DIAGNOSTIC_FIELDS = (
    "errorCode",
    "stage",
    "keyword",
    "scope",
    "loadMoreClicks",
    "candidateCount",
    "candidateLimit",
    "clickLimit",
    "operationTimeoutSeconds",
)
_STORE_EFFECTIVE_VISIBILITY_JS = r"""
                const isEffectivelyVisible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    if (node.closest?.('[hidden], [aria-hidden="true"]')) return false;
                    const view = node.ownerDocument?.defaultView;
                    const targetStyle = view?.getComputedStyle(node);
                    if (!targetStyle
                        || targetStyle.visibility === 'hidden'
                        || targetStyle.visibility === 'collapse') return false;
                    for (let current = node; current; current = current.parentElement) {
                        const style = view?.getComputedStyle(current);
                        if (!style || style.display === 'none'
                            || Number.parseFloat(style.opacity) === 0
                            || style.contentVisibility === 'hidden') return false;
                    }
                    const rect = node.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
"""


def _publish_location_remaining_ms(
    deadline: float | None,
    timeout_ms: int | None = None,
) -> int | None:
    """返回不超过正式发布绝对期限的本次 DOM 等待上限。"""

    normalized_timeout = None if timeout_ms is None else max(1, int(timeout_ms))
    if deadline is None:
        return normalized_timeout
    remaining = int((deadline - monotonic()) * 1000)
    if remaining <= 0:
        raise DouyinCommerceError(_PUBLISH_LOCATION_LIMIT_CODE) from None
    return remaining if normalized_timeout is None else min(normalized_timeout, remaining)


async def _await_publish_location_dom_action(
    action,
    *,
    deadline: float | None,
    timeout_ms: int | None = None,
):
    """在每个可阻塞 DOM 动作前重算剩余时间并强制统一超时码。"""

    effective_timeout = _publish_location_remaining_ms(deadline, timeout_ms)
    deadline_limited = deadline is not None and (
        timeout_ms is None
        or int((deadline - monotonic()) * 1000) <= max(1, int(timeout_ms))
    )
    try:
        awaitable = action(effective_timeout)
        if not deadline_limited:
            return await awaitable
        return await asyncio.wait_for(
            awaitable,
            timeout=max(0.001, effective_timeout / 1000),
        )
    except (TimeoutError, asyncio.TimeoutError):
        if deadline_limited:
            raise DouyinCommerceError(_PUBLISH_LOCATION_LIMIT_CODE) from None
        raise
    except Exception:
        if deadline is not None and monotonic() >= deadline:
            raise DouyinCommerceError(_PUBLISH_LOCATION_LIMIT_CODE) from None
        raise


async def _wait_publish_location_timeout(
    page,
    timeout_ms: int,
    *,
    deadline: float | None,
) -> None:
    """固定收敛等待也不得跨过正式发布的总期限。"""

    if deadline is None:
        await page.wait_for_timeout(max(1, int(timeout_ms)))
        return
    await _await_publish_location_dom_action(
        lambda action_timeout: page.wait_for_timeout(action_timeout),
        deadline=deadline,
        timeout_ms=timeout_ms,
    )


class DouyinCommerceError(RuntimeError):
    """抖音带货流程不能安全继续时抛出。"""

    def __init__(
        self,
        code: str,
        diagnostic: Mapping[str, object] | None = None,
        *,
        click_performed: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = _public_location_diagnostic(diagnostic)
        self.click_performed = click_performed is True


def _public_location_diagnostic(
    diagnostic: Mapping[str, object] | None,
) -> dict[str, object]:
    """仅保留允许交给调用方的地点失败诊断字段。"""

    if not isinstance(diagnostic, Mapping):
        return {}
    return {
        field: diagnostic[field]
        for field in _LOCATION_DIAGNOSTIC_FIELDS
        if field in diagnostic
    }


def _location_failure(
    code: str,
    *,
    stage: str,
    keyword: str,
    scope: str,
    clicks: int,
    candidates: int,
    max_candidates: int,
    max_load_more_clicks: int,
) -> DouyinCommerceError:
    """构建不会暴露页面原文或底层异常的发布定位失败。"""

    return DouyinCommerceError(
        code,
        {
            "errorCode": code,
            "stage": stage,
            "keyword": keyword,
            "scope": scope,
            "loadMoreClicks": clicks,
            "candidateCount": candidates,
            "candidateLimit": max_candidates,
            "clickLimit": max_load_more_clicks,
            "operationTimeoutSeconds": _LOCATION_RESULT_WAIT_TIMEOUT_MS // 1000,
        },
    )


def _normalized(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def normalize_commerce_location_scope(value: object) -> str:
    """将客户端明确选择的地点搜索范围收敛为平台可见的两种范围。"""

    raw = _normalized(value).casefold()
    aliases = {
        LOCATION_SCOPE_LOCAL: LOCATION_SCOPE_LOCAL,
        "本地": LOCATION_SCOPE_LOCAL,
        LOCATION_SCOPE_DOMESTIC: LOCATION_SCOPE_DOMESTIC,
        "国内": LOCATION_SCOPE_DOMESTIC,
    }
    scope = aliases.get(raw, "")
    if not scope:
        raise DouyinCommerceError("请选择发布定位范围：本地或国内")
    return scope


def location_scope_label(value: object) -> str:
    """返回已校验范围对应的抖音页面可见文案。"""

    return LOCATION_SCOPE_LABELS[normalize_commerce_location_scope(value)]


def normalize_content_declaration(value: object) -> str:
    """只接受用户从抖音当前“自主声明”列表明确选择的固定选项。"""

    declaration = _normalized(value)
    if declaration not in CONTENT_DECLARATION_OPTIONS:
        raise DouyinCommerceError("请选择抖音作品内容声明，不能自动推断")
    return declaration


def _store_id(value: Mapping[str, Any]) -> str:
    for key in (
        "storeId",
        "store_id",
        "shopId",
        "shop_id",
        "id",
        "visibleIdentity",
    ):
        current = _normalized(value.get(key))
        if current:
            return current
    return ""


def _visible_store_identity(name: str, address: str) -> str:
    """为未暴露门店 ID 的新版抖音列表生成最小可回读身份。

    当前抖音“带货模式”下拉只在可见 DOM 中提供地点名称、完整地址和商品
    摘要，并不提供门店 ID。名称与完整地址的组合才是用户可核对的稳定身份；
    这里仅保存其不可逆短摘要，原文仍以名称和地址字段另存供界面展示。
    """

    source = f"{_normalized(name)}\n{_normalized(address)}".encode("utf-8")
    return f"visible:{hashlib.sha256(source).hexdigest()[:24]}"


def _visible_location_identity(name: str, address: str) -> str:
    """为新版带货位置结果生成可审计的可见身份。

    带货模式的候选列表未必暴露普通 POI 的内部 ID，但会展示门店名称和完整
    地址。这里不尝试从页面请求或隐藏属性推断 POI ID，而是使用这两个用户可见
    字段的摘要作为本次任务的 ``locationPoi.poiId``。
    """

    source = f"{_normalized(name)}\n{_normalized(address)}".encode("utf-8")
    return f"visible-poi:{hashlib.sha256(source).hexdigest()[:24]}"


def normalize_commerce_store(
    value: object,
    *,
    location_poi: Mapping[str, Any] | None = None,
) -> dict[str, str] | None:
    """将平台可见门店收敛为可审计、可唯一匹配的最小字段。"""

    if not isinstance(value, Mapping):
        return None
    name = _normalized(value.get("name") or value.get("storeName") or value.get("title"))
    address = _normalized(value.get("address") or value.get("storeAddress"))
    poi_id = _normalized(value.get("poiId") or value.get("poi_id"))
    commerce_info = _normalized(
        value.get("commerceInfo") or value.get("commerce_info") or value.get("productInfo")
    )
    if not poi_id and location_poi:
        poi_id = _normalized(location_poi.get("poiId") or location_poi.get("poi_id"))
    store_id = _store_id(value) or _visible_store_identity(name, address)
    # 带货候选必须在页面上同时呈现名称和完整地址。没有地址时即使有文本
    # 也无法让用户或下一次预检可靠确认，不把它当成可绑定门店。
    if not store_id or not name or not address or not poi_id:
        return None
    result = {
        "storeId": store_id,
        "name": name,
        "address": address,
        "poiId": poi_id,
        "source": "douyin-visible",
    }
    if commerce_info:
        result["commerceInfo"] = commerce_info
    return result


def normalize_commerce_store_candidates(
    rows: object,
    *,
    location_poi: Mapping[str, Any],
) -> list[dict[str, str]]:
    """去重并过滤不具备稳定门店身份的可见候选。"""

    if not isinstance(rows, list):
        return []
    expected_poi = _normalized(location_poi.get("poiId") or location_poi.get("poi_id"))
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        candidate = normalize_commerce_store(row, location_poi=location_poi)
        if not candidate:
            continue
        if candidate["storeId"] in seen:
            raise DouyinCommerceError("抖音带货候选出现重复可见身份，无法安全选择")
        if expected_poi and candidate["poiId"] != expected_poi:
            continue
        seen.add(candidate["storeId"])
        result.append(candidate)
    return result


def normalize_commerce_location_candidate(value: object) -> dict[str, Any] | None:
    """规范化新版抖音带货页中可见的“发布定位”候选。

    新版抖音把地点搜索结果展示在带货模式的同一个控件内，但“发布定位”和
    “团购门店绑定”仍是两项不同的业务确认。定位测试只需要名称、完整地址和
    可见身份；绝不因为尚未读取门店身份而拒绝一个可回读的官方地点。
    """

    if not isinstance(value, Mapping):
        return None
    name = _normalized(value.get("name") or value.get("storeName") or value.get("title"))
    address = _normalized(value.get("address") or value.get("storeAddress"))
    if not name or not address:
        return None
    real_poi_id = _normalized(
        value.get("poiId")
        or value.get("poi_id")
        or value.get("locationId")
        or value.get("location_id")
    )
    location = normalize_location_candidate(
        {
            "poiId": real_poi_id or _visible_location_identity(name, address),
            "name": name,
            "address": address,
            "distance": "",
        }
    )
    if not location:
        return None
    return {
        **location,
        **normalize_candidate_commission_fields(value),
        "source": "douyin-visible-commerce-location",
    }


def normalize_commerce_location_candidates(
    rows: object,
    *,
    commission_filter: object = "all",
) -> list[dict[str, Any]]:
    """先按返佣要求筛选，再按 POI 身份去重可见发布定位候选。

    原始商品/返佣文案只在当前 DOM 读取阶段存在；出口仅保留解析后的公开结构
    字段。首版仍只验证发布定位；门店绑定已从一键发流程中移除。
    """

    if not isinstance(rows, list):
        return []
    selected_filter = normalize_commission_filter(
        commission_filter,
        default="all",
    )
    normalized_rows = [
        location
        for row in rows
        if (location := normalize_commerce_location_candidate(row))
    ]
    filtered_rows = filter_location_candidates(normalized_rows, selected_filter)
    result: list[dict[str, Any]] = []
    seen_locations: set[tuple[str, str, str, str]] = set()
    for location in filtered_rows:
        identity = _commerce_location_candidate_identity(location)
        if not all(identity) or identity in seen_locations:
            raise DouyinCommerceError("抖音发布定位候选出现重复完整身份，无法安全选择")
        seen_locations.add(identity)
        result.append(dict(location))
    return result


def normalize_commerce_location_store_candidate(
    value: object,
) -> dict[str, Any] | None:
    """规范化后续“位置 + 团购门店绑定”使用的单条候选。

    此函数只供门店绑定阶段使用。单独的发布定位验证请使用
    :func:`normalize_commerce_location_candidate`，避免把门店回读失败误报为
    定位失败。
    """

    location = normalize_commerce_location_candidate(value)
    if not location:
        return None
    raw_store = dict(value)
    raw_store["poiId"] = location["poiId"]
    store = normalize_commerce_store(raw_store, location_poi=location)
    if not store:
        return None
    return {
        **location,
        "commerceStore": store,
        "source": "douyin-visible-commerce",
    }


def normalize_commerce_location_store_candidates(
    rows: object,
) -> list[dict[str, Any]]:
    """去重并校验新版带货搜索结果的可见位置/门店身份。"""

    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    seen_locations: set[tuple[str, str, str, str]] = set()
    seen_stores: set[str] = set()
    for row in rows:
        candidate = normalize_commerce_location_store_candidate(row)
        if not candidate:
            continue
        location = candidate
        store = candidate["commerceStore"]
        location_identity = _commerce_location_candidate_identity(location)
        store_id = _normalized(store.get("storeId"))
        if location_identity in seen_locations or store_id in seen_stores:
            raise DouyinCommerceError("抖音带货候选出现重复可见身份，无法安全选择")
        seen_locations.add(location_identity)
        seen_stores.add(store_id)
        result.append(candidate)
    return result


def _validate_douyin_commerce_content(
    payload: Mapping[str, Any],
    *,
    action: str,
    require_selected_music: bool,
) -> dict[str, Any]:
    """校验三个带货步骤都共享的账号、视频与文案边界。

    抖音只有上传完成后才会显示音乐、地点和本地团购控件。因此“后台上传”
    阶段不能强行要求地点、门店或定时；最终预检才在此基础上补全这些字段。
    """

    checked = dict(payload)
    if int(checked.get("type") or 0) != 3:
        raise DouyinCommerceError(f"{action}只能选择抖音平台")
    if str(checked.get("workflow") or "") != DOUYIN_COMMERCE_WORKFLOW:
        raise DouyinCommerceError(f"{action}缺少 workflow=douyin-commerce")
    if str(checked.get("commerceMode") or "") != LOCAL_GROUP_BUY_MODE:
        raise DouyinCommerceError("抖音带货首版仅支持本地团购门店模式")
    try:
        checked["musicMode"] = validate_favorite_music_mode(
            checked.get("musicMode") or FAVORITE_FIRST_MUSIC_MODE
        )
    except DouyinMusicError as exc:
        raise DouyinCommerceError(str(exc)) from exc
    if checked["musicMode"] == FAVORITE_MANUAL_MUSIC_MODE:
        selected_music = normalize_music_readback(checked.get("selectedMusic"))
        if require_selected_music and not selected_music:
            raise DouyinCommerceError(
                "抖音带货需要由用户从当前收藏列表选择并回读一首音乐"
            )
        if selected_music:
            checked["selectedMusic"] = selected_music
        else:
            checked.pop("selectedMusic", None)
    else:
        # 旧任务的“收藏第一首”策略不需要，也不能伪造手工选择回读。
        checked.pop("selectedMusic", None)
    if str(checked.get("contentType") or "") != "video":
        raise DouyinCommerceError("抖音带货首版仅支持视频")

    accounts = [
        _normalized(item) for item in checked.get("accountList") or [] if _normalized(item)
    ]
    if len(accounts) != 1:
        raise DouyinCommerceError("抖音带货一次只能选择一个已登录账号")
    checked["accountList"] = accounts

    files = [
        normalize_media_path(item)
        for item in checked.get("fileList") or []
        if normalize_media_path(item)
    ]
    if len(files) != 1 or not Path(files[0]).is_file():
        raise DouyinCommerceError("抖音带货需要且只允许一条可读取的视频素材")
    checked["fileList"] = files

    if not _normalized(checked.get("description")):
        raise DouyinCommerceError(f"{action}缺少作品描述")
    checked["backgroundMode"] = checked.get("backgroundMode") is not False
    return checked


def validate_douyin_commerce_upload_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验“确认并后台上传”步骤，不读取或写入平台。"""

    checked = _validate_douyin_commerce_content(
        payload,
        action="抖音带货上传",
        require_selected_music=False,
    )
    if str(checked.get("runtimeMode") or "") != "preflight":
        raise DouyinCommerceError("抖音带货后台上传必须保持预检模式")
    if checked.get("debugDryRun") is not True:
        raise DouyinCommerceError("抖音带货后台上传必须保持 debugDryRun=true")
    # 上传阶段不允许从前一次流程复用地点、声明或定时数据；这些数据要在
    # 当前临时编辑会话里由用户重新选择并回读。
    checked.pop("locationKeyword", None)
    checked.pop("locationPoi", None)
    checked.pop("locationScope", None)
    checked.pop("commerceStore", None)
    checked.pop("contentDeclaration", None)
    checked.pop("scheduleTime", None)
    checked["enableTimer"] = False
    return checked


def validate_douyin_commerce_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验带货预检/提交的完整本地安全边界，不触发浏览器或平台动作。"""

    checked = _validate_douyin_commerce_content(
        payload,
        action="抖音带货任务",
        require_selected_music=True,
    )

    location = normalize_location_candidate(checked.get("locationPoi"))
    location_name = _normalized(checked.get("locationKeyword"))
    if not location or location_name != location["name"]:
        raise DouyinCommerceError("抖音带货必须选择一键发官方地点候选，不能只传关键词")
    checked["locationKeyword"] = location["name"]
    checked["locationPoi"] = location
    checked["locationScope"] = normalize_commerce_location_scope(
        checked.get("locationScope")
    )
    original_search_keyword = _normalized(checked.get("locationSearchKeyword"))
    if original_search_keyword:
        try:
            checked["locationSearchKeyword"] = normalize_location_keyword(
                original_search_keyword
            )
        except Exception as exc:
            raise DouyinCommerceError(f"抖音带货地点原始搜索词无效：{exc}") from exc
    else:
        checked.pop("locationSearchKeyword", None)
    # 抖音新版带货模式在地点项中显示关联商品信息，但首版不再维护或绑定
    # 独立门店身份。清理旧字段，避免历史任务/候选被误当成当前已绑定门店。
    checked.pop("commerceStore", None)
    checked["contentDeclaration"] = normalize_content_declaration(
        checked.get("contentDeclaration")
    )

    timer_enabled = checked.get("enableTimer") is True
    schedule_time = _normalized(checked.get("scheduleTime"))
    if timer_enabled and not schedule_time:
        raise DouyinCommerceError("抖音带货已开启定时发布，但缺少发布时间")
    if not timer_enabled and schedule_time:
        raise DouyinCommerceError("未开启抖音带货定时发布时，不能保留发布时间")
    checked["enableTimer"] = timer_enabled
    checked["scheduleTime"] = schedule_time
    return checked


def validate_douyin_commerce_discovery_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """校验“读取可绑定门店”的临时编辑预检载荷。

    抖音不会在空白视频编辑页展示本地团购控件，因此读取候选前必须把用户已
    选择的视频、文案和官方 POI 带入一个不保存、不发表的临时编辑页。本函数
    不要求门店或定时字段，且不触发任何浏览器行为。
    """

    checked = dict(payload)
    if int(checked.get("type") or 0) != 3:
        raise DouyinCommerceError("抖音带货门店读取只能选择抖音平台")
    if str(checked.get("workflow") or "") != DOUYIN_COMMERCE_WORKFLOW:
        raise DouyinCommerceError("抖音带货门店读取缺少 workflow=douyin-commerce")
    if str(checked.get("commerceMode") or "") != LOCAL_GROUP_BUY_MODE:
        raise DouyinCommerceError("抖音带货首版仅支持本地团购门店模式")
    try:
        checked["musicMode"] = validate_favorite_music_mode(
            checked.get("musicMode") or FAVORITE_FIRST_MUSIC_MODE
        )
    except DouyinMusicError as exc:
        raise DouyinCommerceError(str(exc)) from exc
    if str(checked.get("contentType") or "") != "video":
        raise DouyinCommerceError("抖音带货首版仅支持视频")
    if str(checked.get("runtimeMode") or "") != "preflight":
        raise DouyinCommerceError("读取可绑定门店必须保持预检模式")
    if checked.get("debugDryRun") is not True:
        raise DouyinCommerceError("读取可绑定门店必须保持 debugDryRun=true")

    accounts = [
        _normalized(item)
        for item in checked.get("accountList") or []
        if _normalized(item)
    ]
    if len(accounts) != 1:
        raise DouyinCommerceError("读取门店一次只能选择一个已登录抖音账号")
    checked["accountList"] = accounts
    files = [
        normalize_media_path(item)
        for item in checked.get("fileList") or []
        if normalize_media_path(item)
    ]
    if len(files) != 1 or not Path(files[0]).is_file():
        raise DouyinCommerceError("读取门店前需要且只允许一条可读取的视频素材")
    checked["fileList"] = files
    if not _normalized(checked.get("title")):
        raise DouyinCommerceError("读取门店前请先填写标题")
    if not _normalized(checked.get("description")):
        raise DouyinCommerceError("读取门店前请先填写作品文案")

    location = normalize_location_candidate(checked.get("locationPoi"))
    location_name = _normalized(checked.get("locationKeyword"))
    if not location or location_name != location["name"]:
        raise DouyinCommerceError("读取门店前必须选择一键发官方地点候选")
    checked["locationKeyword"] = location["name"]
    checked["locationPoi"] = location
    # 门店读取阶段绝不带入旧选择，避免地点或账号变更后误复用。
    checked.pop("commerceStore", None)
    checked.pop("scheduleTime", None)
    checked["enableTimer"] = False
    return checked


def _is_location_linked_store(
    candidate: Mapping[str, Any], location_poi: Mapping[str, Any]
) -> bool:
    """只接受与用户已选官方 POI 名称、完整地址均一致的带货候选。"""

    return (
        _normalized(candidate.get("name")) == _normalized(location_poi.get("name"))
        and _normalized(candidate.get("address")) == _normalized(location_poi.get("address"))
    )


async def _anchor_controls(page):
    """定位页面实际可见的带货位置操作面。

    抖音现存两种界面：旧版依赖固定锚点中的两个 ``semi-select``；新版则在
    同一“添加标签”行直接呈现“位置 + 带货模式 + 地点输入框”。后者没有可点
    的“添加标签”按钮，不能把静态标签误当作入口。两种结构都必须由同一行内
    的完整组合唯一确认；全页出现的孤立“位置”或“带货模式”文本一律不采用。
    """

    result = await page.evaluate(
        f"""() => {{
            const visible = node => {{
                if (!(node instanceof HTMLElement)) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            }};
            const normalize = value => String(value || '')
                .replace(/[\\u200b\\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
            const clearMarkers = () => document.querySelectorAll(
                '[data-oneclick-commerce-mode], [data-oneclick-commerce-store]'
            ).forEach(node => {{
                node.removeAttribute('data-oneclick-commerce-mode');
                node.removeAttribute('data-oneclick-commerce-store');
            }});
            const text = node => normalize(node.innerText || node.textContent);
            const leafText = (root, label) => Array.from(root.querySelectorAll('*'))
                .filter(visible)
                .filter(node => text(node) === label)
                // 展开的下拉菜单会重复渲染“带货模式/打卡模式”等选项；
                // 它们不是表单当前值，不能参与控件组唯一性判断。
                .filter(node => !node.closest(
                    '[role="listbox"], [role="option"], [role="menu"], [role="menuitem"]'
                ))
                .filter(node => !Array.from(node.children)
                    .some(child => visible(child) && text(child) === label));
            const editable = node => visible(node) && !node.disabled && !node.readOnly
                && String(node.type || '').toLowerCase() !== 'hidden';
            const interactive = node => {{
                // Semi Select 的已选文案节点本身也含有 ``select`` 类名片段，
                // 必须先返回完整控件，否则后续只能标记到内部文字节点。
                const owningSelect = node.closest?.('.semi-select');
                if (owningSelect && visible(owningSelect)) return owningSelect;
                for (let current = node, depth = 0;
                    current && current !== document.body && depth < 6;
                    current = current.parentElement, depth += 1) {{
                    const role = String(current.getAttribute?.('role') || '').toLowerCase();
                    const className = String(current.className || '');
                    if (current.classList?.contains('semi-select') || role === 'button'
                        || current.tagName === 'BUTTON' || Number(current.tabIndex) >= 0
                        || /(?:^|[-_\\s])(?:select|dropdown|item|option)(?:$|[-_\\s])/i.test(className)) {{
                        return current;
                    }}
                }}
                return null;
            }};
            const lowestCommonAncestor = (left, right) => {{
                const ancestors = new Set();
                for (let current = left; current && current !== document.body; current = current.parentElement) {{
                    ancestors.add(current);
                }}
                for (let current = right; current && current !== document.body; current = current.parentElement) {{
                    if (ancestors.has(current)) return current;
                }}
                return null;
            }};
            const mark = (mode, store, surface) => {{
                clearMarkers();
                mode.dataset.oneclickCommerceMode = 'active';
                store.dataset.oneclickCommerceStore = 'active';
                const selectedMode = mode.querySelector(
                    '.semi-select-selection-text, [aria-selected="true"]'
                );
                const selectedModeIsPlaceholder = Boolean(
                    selectedMode?.classList?.contains('semi-select-selection-placeholder')
                    || selectedMode?.classList?.contains('semi-select-selection-text-inactive')
                );
                return {{
                    count: 1,
                    // 控件展开时 innerText 会连同所有菜单项一起返回；这里只读
                    // 已选值。新版会给空值渲染“请选择带货模式”等非空提示
                    // 文案，必须依据 placeholder 状态识别为空，不能把它当成
                    // 未知模式而阻断首次地点搜索。
                    mode: selectedModeIsPlaceholder
                        ? '{_UNSELECTED_MODE_VALUE}'
                        : normalize(
                            selectedMode && (selectedMode.innerText || selectedMode.textContent)
                        ) || text(mode),
                    store: normalize(store.value || store.innerText || store.textContent),
                    surface,
                }};
            }};

            // 旧版：固定锚点内的“模式 + 门店”双下拉。
            const anchor = document.getElementById('{_ANCHOR_ROOT_ID}');
            if (anchor && visible(anchor)) {{
                const groups = Array.from(anchor.querySelectorAll('[class*="anchor-item"]'))
                    .filter(visible)
                    .map(group => Array.from(group.children)
                        .filter(node => node.classList?.contains('semi-select') && visible(node)));
                const matched = groups.filter(items => items.length === 2
                    && ['{_COMMERCE_MODE_TEXT}', '{_CHECKIN_MODE_TEXT}']
                        .includes(text(items[0])));
                if (matched.length === 1) {{
                    const [mode, store] = matched[0];
                    return mark(mode, store, 'legacy-anchor');
                }}
                if (matched.length > 1) return {{ count: matched.length, surface: 'legacy-anchor' }};
            }}

            // 新版：位置、带货模式和地点输入框必须属于同一个可见行。这里不把
            // “添加标签”静态标题当作按钮；只接受完整三元组，避免误点共创或话题。
            const positionLeaves = leafText(document, '位置');
            const modeLeaves = leafText(document, '{_COMMERCE_MODE_TEXT}');
            const modern = [];
            for (const position of positionLeaves) {{
                for (const modeLeaf of modeLeaves) {{
                    const row = lowestCommonAncestor(position, modeLeaf);
                    if (!row || !visible(row)) continue;
                    const positionsInRow = leafText(row, '位置');
                    const modesInRow = leafText(row, '{_COMMERCE_MODE_TEXT}');
                    const fields = Array.from(row.querySelectorAll(
                        'input, textarea, [contenteditable="true"][role="textbox"]'
                    )).filter(editable);
                    const mode = interactive(modeLeaf);
                    if (positionsInRow.length === 1 && modesInRow.length === 1
                        && fields.length === 1 && mode && !modern.some(item => item.row === row)) {{
                        modern.push({{ row, mode, store: fields[0] }});
                    }}
                }}
            }}
            if (modern.length === 1) {{
                return mark(modern[0].mode, modern[0].store, 'modern-position-mode-input');
            }}
            if (modern.length > 1) {{
                return {{ count: modern.length, surface: 'modern-position-mode-input' }};
            }}

            // 部分账号在首次选择“位置”后只用 CSS/data-code 展示位置类型，
            // “带货模式”则位于紧邻的独立行。只在唯一“添加标签”小节内接受：
            // 当前行恰有“位置类型下拉 + 可搜索地点输入”，后续兄弟行恰有一个
            // “单下拉且无输入框”的模式候选；共创行含两个下拉和输入框，会被
            // 排除。不能把当前位置类型下拉误标成带货模式。
            const structural = [];
            for (const addLeaf of leafText(document, '添加标签')) {{
                for (let row = addLeaf.parentElement, depth = 0;
                    row && row !== document.body && depth < 6;
                    row = row.parentElement, depth += 1) {{
                    const rowClass = String(row.className || '');
                    if (!/(?:^|[-_\\s])new-layout(?:$|[-_\\s])/i.test(rowClass)) {{
                        continue;
                    }}
                    const contentChildren = Array.from(row.children)
                        .filter(visible)
                        .filter(node => /(?:^|[-_\\s])content-child(?:$|[-_\\s])/i
                            .test(String(node.className || '')));
                    if (contentChildren.length !== 1) break;
                    const content = contentChildren[0];
                    const selects = Array.from(content.querySelectorAll('.semi-select'))
                        .filter(visible)
                        .filter(node => !node.parentElement?.closest('.semi-select'));
                    const fields = Array.from(content.querySelectorAll(
                        'input, textarea, [contenteditable="true"][role="textbox"]'
                    )).filter(editable);
                    const positionTypeCandidates = selects.filter(node =>
                        !node.classList.contains('semi-select-filterable')
                        && !node.querySelector('input, textarea, [contenteditable="true"]'));
                    const searchSelects = selects.filter(node =>
                        node.classList.contains('semi-select-filterable')
                        && fields.some(field => node.contains(field)));
                    if (positionTypeCandidates.length !== 1 || searchSelects.length !== 1
                        || fields.length !== 1) {{
                        break;
                    }}
                    const adjacentModeCandidates = [];
                    for (let sibling = row.nextElementSibling;
                        sibling; sibling = sibling.nextElementSibling) {{
                        if (!visible(sibling)) continue;
                        const siblingClass = String(sibling.className || '');
                        if (!/(?:^|[-_\\s])new-layout(?:$|[-_\\s])/i.test(siblingClass)) {{
                            continue;
                        }}
                        const siblingSelects = Array.from(
                            sibling.querySelectorAll('.semi-select')
                        ).filter(visible).filter(node =>
                            !node.parentElement?.closest('.semi-select'));
                        const siblingFields = Array.from(sibling.querySelectorAll(
                            'input, textarea, [contenteditable="true"][role="textbox"]'
                        )).filter(editable);
                        if (siblingSelects.length === 1 && siblingFields.length === 0) {{
                            adjacentModeCandidates.push(siblingSelects[0]);
                        }}
                    }}
                    if (adjacentModeCandidates.length === 1) {{
                        structural.push({{
                            row,
                            mode: adjacentModeCandidates[0],
                            store: fields[0],
                        }});
                    }}
                    break;
                }}
            }}
            if (structural.length === 1) {{
                const matched = mark(
                    structural[0].mode,
                    structural[0].store,
                    'modern-add-tag-structure'
                );
                if (!matched.mode) matched.mode = '{_UNSELECTED_MODE_VALUE}';
                return matched;
            }}
            if (structural.length > 1) {{
                return {{ count: structural.length, surface: 'modern-add-tag-structure' }};
            }}
            return {{ count: modern.length, surface: 'modern-position-mode-input' }};
        }}"""
    )
    if not isinstance(result, Mapping) or int(result.get("count") or 0) != 1:
        count = int(result.get("count") or 0) if isinstance(result, Mapping) else 0
        raise DouyinCommerceError(
            f"抖音页面未找到唯一可用的带货模式控件组（实际 {count} 个），已安全停止"
        )
    return (
        page.locator('[data-oneclick-commerce-mode="active"]'),
        page.locator('[data-oneclick-commerce-store="active"]'),
        _normalized(result.get("mode")),
        _normalized(result.get("store")),
    )


async def _mark_unique_position_tag_select(page) -> Any | None:
    """标记“添加标签”行中唯一的“位置”入口。

    只在带货控件组尚未出现时使用。这个控件的当前文案、可见性和唯一性都由
    页面回读；新版不保证入口根节点的全文恰好等于“位置”，因此从精确文字
    叶节点向上收敛到可点击控件，并要求同一行内同时有唯一“带货模式”。若
    页面上存在多个候选，宁可停下也不按序号猜测。
    """

    result = await page.evaluate(
        """() => {
            const visible = node => {
                if (!(node instanceof HTMLElement)) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const normalize = value => String(value || '')
                .replace(/[\u200b\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
            const text = node => normalize(node.innerText || node.textContent);
            const leaves = (root, label) => Array.from(root.querySelectorAll('*'))
                .filter(visible)
                .filter(node => text(node) === label)
                .filter(node => !Array.from(node.children)
                    .some(child => visible(child) && text(child) === label));
            const interactive = node => {
                for (let current = node, depth = 0;
                    current && current !== document.body && depth < 6;
                    current = current.parentElement, depth += 1) {
                    const role = String(current.getAttribute?.('role') || '').toLowerCase();
                    const className = String(current.className || '');
                    if (current.classList?.contains('semi-select') || role === 'button'
                        || current.tagName === 'BUTTON' || Number(current.tabIndex) >= 0
                        || /(?:^|[-_\\s])(?:select|dropdown|item|option)(?:$|[-_\\s])/i.test(className)) {
                        return current;
                    }
                }
                return null;
            };
            const candidates = [];
            for (const leaf of leaves(document, '位置')) {
                const control = interactive(leaf);
                if (!control || candidates.includes(control)) continue;
                let row = control.parentElement;
                let supported = false;
                for (let depth = 0; row && row !== document.body && depth < 5;
                    row = row.parentElement, depth += 1) {
                    if (leaves(row, '位置').length === 1
                        && leaves(row, '带货模式').length === 1) {
                        supported = true;
                        break;
                    }
                }
                if (supported) candidates.push(control);
            }
            // 选择音乐后 React 可能重建“添加标签”行，并把标签类型错误保留为
            // “游戏手柄”。此时不能再依赖当前文案必须是“位置”；只在唯一
            // “添加标签”new-layout 行的右侧 content-child 内，接受唯一一个
            // 非筛选型标签类型下拉。地点搜索下拉含输入框，会被明确排除。
            if (candidates.length === 0) {
                const structural = [];
                for (const addLeaf of leaves(document, '添加标签')) {
                    for (let row = addLeaf.parentElement, depth = 0;
                        row && row !== document.body && depth < 6;
                        row = row.parentElement, depth += 1) {
                        const rowClass = String(row.className || '');
                        if (!/(?:^|[-_\\s])new-layout(?:$|[-_\\s])/i.test(rowClass)) {
                            continue;
                        }
                        const contentChildren = Array.from(row.children)
                            .filter(visible)
                            .filter(node => /(?:^|[-_\\s])content-child(?:$|[-_\\s])/i
                                .test(String(node.className || '')));
                        if (contentChildren.length !== 1) break;
                        const content = contentChildren[0];
                        const selector = '.semi-select, [role="combobox"], '
                            + '[aria-haspopup="listbox"], [aria-haspopup="menu"]';
                        const typeSelects = Array.from(content.querySelectorAll(selector))
                            .filter(visible)
                            .filter(node => !node.parentElement?.closest(selector))
                            .filter(node => !node.classList?.contains('semi-select-filterable'))
                            .filter(node => !node.querySelector(
                                'input, textarea, [contenteditable="true"]'
                            ));
                        if (typeSelects.length === 1
                            && !structural.includes(typeSelects[0])) {
                            structural.push(typeSelects[0]);
                        }
                        break;
                    }
                }
                structural.forEach(node => candidates.push(node));
            }
            const controls = candidates;
            if (controls.length !== 1) return { count: controls.length };
            controls[0].dataset.oneclickCommercePositionTag = 'active';
            return { count: 1, current: text(controls[0]) };
        }"""
    )
    if isinstance(result, Mapping) and int(result.get("count") or 0) == 1:
        current = _normalized(result.get("current"))
        if current and current != "位置":
            douyin_logger.warning(
                f"抖音添加标签当前显示“{current}”，将只通过精确菜单项改回“位置”"
            )
        return page.locator('[data-oneclick-commerce-position-tag="active"]')
    return None


async def _open_unique_add_tag(page) -> bool:
    """仅在页面存在唯一可见“添加标签”入口时打开其菜单。

    页面存在两种结构：精确文字位于可点击祖先内，或“添加标签”只是左侧
    静态标题、真正下拉位于同一行右侧兄弟区域。两条路径都必须收敛为唯一
    可见控件；不能因容器类名包含笼统的 ``item`` 就把整行误判为入口。
    """

    result = await page.evaluate(
        """() => {
            const visible = node => {
                if (!(node instanceof HTMLElement)) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const normalize = value => String(value || '')
                .replace(/[\u200b\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
            const text = node => normalize(node.innerText || node.textContent);
            const leaves = Array.from(document.querySelectorAll('*'))
                .filter(visible)
                .filter(node => text(node) === '添加标签')
                .filter(node => !Array.from(node.children)
                    .some(child => visible(child) && text(child) === '添加标签'));
            const interactive = node => {
                for (let current = node, depth = 0;
                    current && current !== document.body && depth < 6;
                    current = current.parentElement, depth += 1) {
                    const role = String(current.getAttribute?.('role') || '').toLowerCase();
                    const className = String(current.className || '');
                    if (current.classList?.contains('semi-select') || role === 'button'
                        || current.tagName === 'BUTTON' || Number(current.tabIndex) >= 0
                        || /(?:^|[-_\\s])(?:select|dropdown|trigger)(?:$|[-_\\s])/i
                            .test(className)) {
                        return current;
                    }
                }
                return null;
            };
            const nodes = [];
            for (const leaf of leaves) {
                const control = interactive(leaf);
                if (control && !nodes.includes(control)) nodes.push(control);
            }
            if (nodes.length > 1) {
                return { count: nodes.length, leaves: leaves.length, route: 'ancestor' };
            }
            if (nodes.length === 0) {
                const siblingNodes = [];
                const add = node => {
                    if (node && visible(node) && !siblingNodes.includes(node)) {
                        siblingNodes.push(node);
                    }
                };
                // 2026-08 新版在选择音乐后会重渲染并移除原位置标签。此时
                // “添加标签”右侧入口退化为无 role/tabindex 的 content-child
                // 容器。只接受精确标题所在 new-layout 行的唯一右侧直属容器；
                // 不在全页搜索 content-child，也不按同类节点序号猜测。该精确
                // 结构必须优先于旧版向后查找，否则后续声明区按钮会形成假歧义。
                const modernNodes = [];
                for (const leaf of leaves) {
                    for (let row = leaf.parentElement, depth = 0;
                        row && row !== document.body && depth < 6;
                        row = row.parentElement, depth += 1) {
                        const rowClass = String(row.className || '');
                        if (!/(?:^|[-_\\s])new-layout(?:$|[-_\\s])/i.test(rowClass)) {
                            continue;
                        }
                        const children = Array.from(row.children).filter(visible);
                        const titleBranch = children.find(child => child.contains(leaf));
                        if (!titleBranch) continue;
                        const titleRect = titleBranch.getBoundingClientRect();
                        const matches = children.filter(child => {
                            if (child === titleBranch) return false;
                            const className = String(child.className || '');
                            const rect = child.getBoundingClientRect();
                            const verticalOverlap = Math.min(titleRect.bottom, rect.bottom)
                                - Math.max(titleRect.top, rect.top);
                            return /(?:^|[-_\\s])content-child(?:$|[-_\\s])/i.test(className)
                                && rect.left >= titleRect.right
                                && verticalOverlap > 0;
                        });
                        matches.forEach(node => {
                            if (!modernNodes.includes(node)) modernNodes.push(node);
                        });
                        break;
                    }
                }
                if (modernNodes.length > 0) {
                    modernNodes.forEach(add);
                } else {
                    for (const leaf of leaves) {
                        for (let branch = leaf, depth = 0;
                            branch && branch !== document.body && depth < 5;
                            branch = branch.parentElement, depth += 1) {
                            for (let sibling = branch.nextElementSibling;
                                sibling; sibling = sibling.nextElementSibling) {
                                if (sibling.matches?.(
                                    '.semi-select-single, .semi-select, button, [role="button"], [tabindex]'
                                )) add(sibling);
                                sibling.querySelectorAll?.(
                                    '.semi-select-single, .semi-select, button, [role="button"], [tabindex]'
                                ).forEach(add);
                            }
                        }
                    }
                }
                if (siblingNodes.length !== 1) {
                    return {
                        count: siblingNodes.length,
                        leaves: leaves.length,
                        route: 'following-sibling',
                    };
                }
                nodes.push(siblingNodes[0]);
            }
            document.querySelectorAll('[data-oneclick-commerce-add-tag]')
                .forEach(node => node.removeAttribute('data-oneclick-commerce-add-tag'));
            nodes[0].dataset.oneclickCommerceAddTag = 'active';
            return { count: 1 };
        }"""
    )
    if not isinstance(result, Mapping) or int(result.get("count") or 0) != 1:
        return False
    control = page.locator('[data-oneclick-commerce-add-tag="active"]')
    try:
        await control.scroll_into_view_if_needed(timeout=5_000)
        await control.click(timeout=5_000)
    except Exception as exc:
        raise DouyinCommerceError("抖音“添加标签”入口无法安全打开") from exc
    return True


async def _select_unique_position_tag_option(
    page,
    *,
    attempts: int = 20,
    interval_ms: int = 200,
) -> None:
    """在同一 DOM 回合中校验并点击唯一“位置”选项。

    不能先给菜单项写标记、再通过异步 Locator 点击：React 可能在两步之间
    复用该节点并把文案改成“游戏手柄”。这里的精确文本回读与 ``click()``
    在同一个浏览器脚本中完成，节点一旦不是“位置”就不会发生点击。
    """

    max_attempts = max(1, int(attempts))
    for attempt in range(max_attempts):
        result = await page.evaluate(
            """() => {
                const visible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const normalize = value => String(value || '')
                    .replace(/[\u200b\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
                const options = Array.from(document.querySelectorAll(
                    '[role="option"], [role="menuitem"], .semi-select-option'
                ))
                    .filter(visible)
                    .filter(node => normalize(node.innerText || node.textContent) === '位置');
                if (options.length !== 1) {
                    return { count: options.length, clicked: false };
                }
                const option = options[0];
                // 最后一刻再次读取同一节点；若框架同步重绘或复用了节点，
                // 此处不再满足精确文案，也就绝不会退化成点击第一项。
                if (!visible(option)
                    || normalize(option.innerText || option.textContent) !== '位置') {
                    return { count: 0, clicked: false };
                }
                option.click();
                return { count: 1, clicked: true };
            }"""
        )
        if isinstance(result, Mapping) and result.get("clicked") is True:
            return
        count = int(result.get("count") or 0) if isinstance(result, Mapping) else 0
        if count > 1:
            raise DouyinCommerceError(
                f"抖音“位置”标签选项出现多个（实际 {count} 个），已安全停止"
            )
        if attempt + 1 < max_attempts:
            await page.wait_for_timeout(interval_ms)
    raise DouyinCommerceError("抖音“位置”标签选项未能唯一显示，已安全停止")


async def _wait_unique_position_tag_select(
    page,
    *,
    attempts: int = 4,
    interval_ms: int = 150,
) -> Any | None:
    """短时等待动态位置入口，只返回页面唯一确认的控件。"""

    max_attempts = max(1, attempts)
    for attempt in range(max_attempts):
        tag_select = await _mark_unique_position_tag_select(page)
        if tag_select is not None:
            return tag_select
        if attempt + 1 < max_attempts:
            await page.wait_for_timeout(interval_ms)
    return None


def _diagnostic_text(value: object, *, limit: int = 160) -> str:
    """收敛诊断元数据；禁止把长正文或控件值混入快照。"""

    return _normalized(value)[:limit]


def _sanitize_location_diagnostic_node(value: object) -> dict[str, Any]:
    """只保留控件结构白名单，不接受页面返回的任意附加字段。"""

    if not isinstance(value, Mapping):
        return {}
    classes = [
        _diagnostic_text(item, limit=96)
        for item in value.get("classes", [])
        if _diagnostic_text(item, limit=96)
    ][:16]
    data_attributes = [
        _diagnostic_text(item, limit=80)
        for item in value.get("dataAttributes", [])
        if _diagnostic_text(item, limit=80).startswith("data-")
    ][:24]
    rect_raw = value.get("rect") if isinstance(value.get("rect"), Mapping) else {}
    rect: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        try:
            rect[key] = round(float(rect_raw.get(key) or 0), 2)
        except (TypeError, ValueError):
            rect[key] = 0.0
    state_raw = value.get("state") if isinstance(value.get("state"), Mapping) else {}
    state = {
        key: _diagnostic_text(state_raw.get(key), limit=24)
        for key in (
            "ariaExpanded",
            "ariaSelected",
            "ariaChecked",
            "ariaCurrent",
            "dataState",
        )
        if _diagnostic_text(state_raw.get(key), limit=24)
    }
    try:
        tab_index = int(value.get("tabIndex", -1))
    except (TypeError, ValueError):
        tab_index = -1
    return {
        "tag": _diagnostic_text(value.get("tag"), limit=24).lower(),
        "role": _diagnostic_text(value.get("role"), limit=48).lower(),
        "classes": classes,
        "ariaLabel": _diagnostic_text(value.get("ariaLabel"), limit=120),
        "placeholder": _diagnostic_text(value.get("placeholder"), limit=120),
        "rect": rect,
        "tabIndex": tab_index,
        "disabled": bool(value.get("disabled")),
        "readOnly": bool(value.get("readOnly")),
        "contentEditable": bool(value.get("contentEditable")),
        "dataAttributes": data_attributes,
        "state": state,
    }


def _sanitize_location_entry_diagnostic(value: object) -> dict[str, Any]:
    """对浏览器返回结果做第二层白名单过滤，敏感字段即使出现也不落盘。"""

    raw = value if isinstance(value, Mapping) else {}
    allowed_labels = set(_LOCATION_DIAGNOSTIC_LABELS)
    counts_raw = raw.get("labelCounts") if isinstance(raw.get("labelCounts"), Mapping) else {}
    label_counts: dict[str, int] = {}
    for label in _LOCATION_DIAGNOSTIC_LABELS:
        try:
            count = max(0, int(counts_raw.get(label) or 0))
        except (TypeError, ValueError):
            count = 0
        if count:
            label_counts[label] = count

    landmarks: list[dict[str, Any]] = []
    for item in raw.get("landmarks", []):
        if not isinstance(item, Mapping):
            continue
        label = _diagnostic_text(item.get("label"), limit=24)
        if label not in allowed_labels:
            continue
        leaf = _sanitize_location_diagnostic_node(item.get("leaf"))
        ancestors = [
            cleaned
            for node in item.get("ancestors", [])
            if (cleaned := _sanitize_location_diagnostic_node(node))
        ][:8]
        siblings = [
            cleaned
            for node in item.get("siblings", [])
            if (cleaned := _sanitize_location_diagnostic_node(node))
        ][:48]
        landmarks.append(
            {
                "label": label,
                "leaf": leaf,
                "ancestors": ancestors,
                "siblings": siblings,
            }
        )

    editable_nodes = [
        cleaned
        for node in raw.get("editableNodes", [])
        if (cleaned := _sanitize_location_diagnostic_node(node))
    ][:32]
    marked_entry_descendants = [
        cleaned
        for node in raw.get("markedEntryDescendants", [])
        if (cleaned := _sanitize_location_diagnostic_node(node))
    ][:48]
    overlay_nodes = [
        cleaned
        for node in raw.get("overlayNodes", [])
        if (cleaned := _sanitize_location_diagnostic_node(node))
    ][:64]
    modern_row_nodes = [
        cleaned
        for node in raw.get("modernRowNodes", [])
        if (cleaned := _sanitize_location_diagnostic_node(node))
    ][:96]
    location_section_nodes = [
        cleaned
        for node in raw.get("locationSectionNodes", [])
        if (cleaned := _sanitize_location_diagnostic_node(node))
    ][:160]
    return {
        "schemaVersion": 1,
        "kind": "douyin-location-entry-structure",
        "capturedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "labelCounts": label_counts,
        "landmarks": landmarks[:32],
        "editableNodes": editable_nodes,
        "markedEntryDescendants": marked_entry_descendants,
        "overlayNodes": overlay_nodes,
        "modernRowNodes": modern_row_nodes,
        "locationSectionNodes": location_section_nodes,
    }


async def _capture_location_entry_diagnostic(page, output_dir: Path) -> Path:
    """保存最小脱敏 DOM 结构；不读取正文、输入值、Cookie、HTML 或请求。"""

    raw = await page.evaluate(
        f"""() => {{
            const labels = {json.dumps(list(_LOCATION_DIAGNOSTIC_LABELS), ensure_ascii=False)};
            const visible = node => {{
                if (!(node instanceof HTMLElement)) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            }};
            const normalize = value => String(value || '')
                .replace(/[\\u200b\\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
            const nodeText = node => normalize(node.innerText || node.textContent);
            const descriptor = node => {{
                const rect = node.getBoundingClientRect();
                const classText = typeof node.className === 'string' ? node.className : '';
                return {{
                    tag: String(node.tagName || '').toLowerCase(),
                    role: String(node.getAttribute?.('role') || ''),
                    classes: classText.split(/\\s+/).filter(Boolean).slice(0, 16),
                    ariaLabel: String(node.getAttribute?.('aria-label') || ''),
                    placeholder: String(node.getAttribute?.('placeholder') || ''),
                    rect: {{ x: rect.x, y: rect.y, width: rect.width, height: rect.height }},
                    tabIndex: Number(node.tabIndex ?? -1),
                    disabled: Boolean(node.disabled),
                    readOnly: Boolean(node.readOnly),
                    contentEditable: node.getAttribute?.('contenteditable') === 'true',
                    dataAttributes: Array.from(node.attributes || [])
                        .map(attribute => attribute.name)
                        .filter(name => name.startsWith('data-')).slice(0, 24),
                    state: {{
                        ariaExpanded: String(node.getAttribute?.('aria-expanded') || ''),
                        ariaSelected: String(node.getAttribute?.('aria-selected') || ''),
                        ariaChecked: String(node.getAttribute?.('aria-checked') || ''),
                        ariaCurrent: String(node.getAttribute?.('aria-current') || ''),
                        dataState: String(node.getAttribute?.('data-state') || ''),
                    }},
                }};
            }};
            const leaves = label => Array.from(document.querySelectorAll('*'))
                .filter(visible)
                .filter(node => nodeText(node) === label)
                .filter(node => !Array.from(node.children)
                    .some(child => visible(child) && nodeText(child) === label));
            const labelCounts = Object.fromEntries(labels.map(label => [label, leaves(label).length]));
            const landmarks = [];
            const neighborhoods = new Set();
            for (const label of labels) {{
                for (const leaf of leaves(label).slice(0, 8)) {{
                    const ancestors = [];
                    const siblings = [];
                    for (let current = leaf.parentElement, depth = 0;
                        current && current !== document.body && depth < 7;
                        current = current.parentElement, depth += 1) {{
                        neighborhoods.add(current);
                        ancestors.push(descriptor(current));
                        Array.from(current.children).filter(visible).slice(0, 12)
                            .forEach(node => siblings.push(descriptor(node)));
                    }}
                    landmarks.push({{
                        label,
                        leaf: descriptor(leaf),
                        ancestors,
                        siblings: siblings.slice(0, 48),
                    }});
                }}
            }}
            const editableSelector = 'input, textarea, [contenteditable="true"]';
            const editableNodes = Array.from(document.querySelectorAll(editableSelector))
                .filter(visible)
                .filter(node => {{
                    const placeholder = normalize(node.getAttribute?.('placeholder'));
                    if (/(?:位置|地点|商户)/.test(placeholder)) return true;
                    for (let current = node.parentElement, depth = 0;
                        current && current !== document.body && depth < 7;
                        current = current.parentElement, depth += 1) {{
                        if (neighborhoods.has(current)) return true;
                    }}
                    return false;
                }})
                .slice(0, 32).map(descriptor);
            const markedEntry = document.querySelector(
                '[data-oneclick-commerce-add-tag="active"]'
            );
            const markedEntryDescendants = markedEntry
                ? [markedEntry, ...Array.from(markedEntry.querySelectorAll('*'))]
                    .filter(visible).slice(0, 48).map(descriptor)
                : [];
            const overlayNodes = Array.from(document.querySelectorAll('*'))
                .filter(visible)
                .filter(node => {{
                    const role = String(node.getAttribute?.('role') || '').toLowerCase();
                    const classText = typeof node.className === 'string' ? node.className : '';
                    return ['listbox', 'menu', 'option', 'menuitem'].includes(role)
                        || node.getAttribute?.('aria-expanded') === 'true'
                        || /(?:dropdown|popover|popup|select-option|menu)/i.test(classText);
                }})
                .slice(0, 64).map(descriptor);
            const modernRowNodes = Array.from(document.querySelectorAll('*'))
                .filter(visible)
                .filter(node => /(?:^|[-_\\s])new-layout(?:$|[-_\\s])/i
                    .test(typeof node.className === 'string' ? node.className : ''))
                .flatMap(row => [row, ...Array.from(row.querySelectorAll('*')).filter(visible)])
                .slice(0, 96).map(descriptor);
            const locationSectionNodes = [];
            for (const addLeaf of leaves('添加标签').slice(0, 2)) {{
                let addRow = addLeaf.parentElement;
                for (let depth = 0;
                    addRow && addRow !== document.body && depth < 6;
                    addRow = addRow.parentElement, depth += 1) {{
                    if (/(?:^|[-_\\s])new-layout(?:$|[-_\\s])/i
                        .test(typeof addRow.className === 'string' ? addRow.className : '')) {{
                        break;
                    }}
                }}
                const section = addRow?.parentElement;
                if (!section) continue;
                [section, ...Array.from(section.querySelectorAll('*')).filter(visible)]
                    .slice(0, 160).forEach(node => locationSectionNodes.push(descriptor(node)));
            }}
            return {{
                schemaVersion: 1,
                kind: 'douyin-location-entry-structure',
                labelCounts,
                landmarks,
                editableNodes,
                markedEntryDescendants,
                overlayNodes,
                modernRowNodes,
                locationSectionNodes,
            }};
        }}"""
    )
    payload = _sanitize_location_entry_diagnostic(raw)
    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = target_dir / f"douyin-location-entry-{timestamp}.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


async def _capture_configured_location_entry_diagnostic(page) -> Path | None:
    """仅在开发者显式设置目录时取证；失败不得掩盖原平台安全停止原因。"""

    output_dir = _normalized(os.environ.get(_LOCATION_DIAGNOSTIC_ENV))
    if not output_dir:
        return None
    try:
        return await _capture_location_entry_diagnostic(page, Path(output_dir))
    except Exception:
        return None


async def _ensure_position_tag(page) -> None:
    """确保进入视频中实际的“位置 → 带货模式 → 输入地点”控件组。"""

    try:
        await _anchor_controls(page)
        return
    except DouyinCommerceError as exc:
        # 控件组本身若已出现多个，不应通过再次加标签去掩盖页面结构变化。
        if "实际 0 个" not in str(exc):
            raise

    tag_select = await _wait_unique_position_tag_select(page)
    if tag_select is None:
        opened = await _open_unique_add_tag(page)
        if not opened:
            diagnostic = await _capture_configured_location_entry_diagnostic(page)
            suffix = f"；脱敏诊断已保存：{diagnostic.name}" if diagnostic else ""
            raise DouyinCommerceError(
                f"抖音页面未找到唯一可用的“位置”标签入口，已安全停止{suffix}"
            )

        # 部分账号点击“添加标签”后不会再弹出“位置”选项，而是直接把位置
        # 类型和可搜索输入框渲染到同一行。先短时回读完整控件组；只有结构仍
        # 为 0 个才继续兼容旧菜单，多个候选仍立即安全停止。
        for attempt in range(5):
            try:
                await _anchor_controls(page)
                return
            except DouyinCommerceError as exc:
                if "实际 0 个" not in str(exc):
                    raise
            if attempt < 4:
                await page.wait_for_timeout(200)

        tag_select = await _wait_unique_position_tag_select(
            page,
            attempts=5,
            interval_ms=200,
        )
    if tag_select is None:
        diagnostic = await _capture_configured_location_entry_diagnostic(page)
        suffix = f"；脱敏诊断已保存：{diagnostic.name}" if diagnostic else ""
        raise DouyinCommerceError(
            f"抖音“添加标签”后未出现唯一“位置”控件，已安全停止{suffix}"
        )

    await _open_exact_select(page, tag_select, "位置标签")
    await _select_unique_position_tag_option(page)
    await page.wait_for_timeout(350)
    # 只有锚点结构明确出现，才允许进入后续带货模式和搜索输入。
    await _anchor_controls(page)


async def _open_exact_select(page, control, purpose: str) -> None:
    """打开已唯一确认的 Semi Select，处理已知的共创说明悬浮提示。

    先走正常用户可见点击。当前抖音会在自动鼠标悬停时短暂显示“添加共创”
    说明并遮住下拉；只有错误中同时出现该精确说明和 pointer interception 时，
    才对同一个已确认元素派发原生鼠标事件。该回退只打开下拉，绝不选择选项。
    """

    if await _is_direct_location_entry(control):
        try:
            await control.scroll_into_view_if_needed(timeout=5_000)
            await control.click(timeout=3_000)
            return
        except Exception as exc:
            raise DouyinCommerceError(f"抖音{purpose}控件无法安全打开") from exc

    selection = control.locator("> .semi-select-selection")
    if await selection.count() != 1:
        raise DouyinCommerceError(f"抖音{purpose}控件结构已变化，已安全停止")
    try:
        await selection.scroll_into_view_if_needed(timeout=5_000)
        await selection.click(timeout=3_000)
        return
    except Exception as exc:
        text = _normalized(str(exc))
        known_hover_tip = (
            "subtree intercepts pointer events" in text
            and "共创" in text
            and "公开账号" in text
        )
        if not known_hover_tip:
            raise DouyinCommerceError(f"抖音{purpose}控件无法安全打开") from exc
    try:
        # 只向已经唯一确认的下拉根发送与常规点击等价的事件，不用坐标或
        # force click 穿透未知遮罩。随后仍必须由实际菜单和选中值回读确认。
        await selection.dispatch_event("mousedown")
        await selection.dispatch_event("mouseup")
        await selection.dispatch_event("click")
    except Exception as exc:
        raise DouyinCommerceError(f"抖音{purpose}被共创说明遮挡且无法安全打开") from exc


async def _is_direct_location_entry(control) -> bool:
    """判断已唯一标记控件是否为新版原生地点输入入口。"""

    try:
        return bool(
            await control.evaluate(
                "node => node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement"
            )
        )
    except Exception:
        return False


async def _wait_mode_options(page) -> list[Any]:
    """等待且唯一确认“带货模式/打卡模式”菜单的两个官方选项。"""

    for _ in range(20):
        result = await page.evaluate(
            f"""() => {{
                const visible = node => {{
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                }};
                const normalize = value => String(value || '')
                    .replace(/[\\u200b\\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
                const lists = Array.from(document.querySelectorAll('[role="listbox"]'))
                    .filter(visible)
                    .filter(list => {{
                        const values = Array.from(list.querySelectorAll(':scope > [role="option"]'))
                            .filter(visible)
                            .map(node => normalize(node.innerText || node.textContent));
                        return values.length === 2
                            && values.includes('{_COMMERCE_MODE_TEXT}')
                            && values.includes('{_CHECKIN_MODE_TEXT}');
                    }});
                if (lists.length !== 1) return {{ count: lists.length }};
                const options = Array.from(lists[0].querySelectorAll(':scope > [role="option"]'))
                    .filter(visible);
                options.forEach((node, index) => node.dataset.oneclickCommerceModeOption = String(index));
                return {{ count: 1, options: options.map(node => normalize(node.innerText || node.textContent)) }};
            }}"""
        )
        if isinstance(result, Mapping) and int(result.get("count") or 0) == 1:
            options = page.locator('[data-oneclick-commerce-mode-option]')
            visible: list[Any] = []
            for index in range(await options.count()):
                node = options.nth(index)
                if await node.is_visible():
                    visible.append(node)
            if len(visible) == 2:
                return visible
        await page.wait_for_timeout(200)
    diagnostic = await _capture_configured_location_entry_diagnostic(page)
    suffix = f"；脱敏诊断已保存：{diagnostic.name}" if diagnostic else ""
    raise DouyinCommerceError(f"抖音带货模式菜单未能唯一显示，已安全停止{suffix}")


async def _ensure_local_group_buy_mode(page):
    """确认或切换到页面实际回读的“带货模式”，并返回关联地点下拉。"""

    mode_control, store_control, mode_value, _ = await _anchor_controls(page)
    # 当前新版页面会在“添加标签”的位置行直接提供原生地点输入框，同时在
    # 相邻行保留另一个空的筛选下拉。该相邻下拉展开后可能包含大量业务候选，
    # 并不是旧版只有“带货模式/打卡模式”两项的模式菜单。已有唯一原生地点
    # 输入时直接复用它，避免误开相邻下拉并污染后续国内/本地搜索。
    if await _is_direct_location_entry(store_control):
        return store_control
    if mode_value == _COMMERCE_MODE_TEXT:
        return store_control
    if mode_value not in {_CHECKIN_MODE_TEXT, _UNSELECTED_MODE_VALUE}:
        diagnostic = await _capture_configured_location_entry_diagnostic(page)
        suffix = f"；脱敏诊断已保存：{diagnostic.name}" if diagnostic else ""
        raise DouyinCommerceError(f"抖音带货模式当前值无法识别，已安全停止{suffix}")

    await _open_exact_select(page, mode_control, "带货模式")
    options = await _wait_mode_options(page)
    matches = [
        option
        for option in options
        if _normalized(await option.inner_text()) == _COMMERCE_MODE_TEXT
    ]
    if len(matches) != 1:
        raise DouyinCommerceError("抖音带货模式菜单中没有唯一“带货模式”选项，已安全停止")
    await matches[0].click(timeout=8_000)
    await page.wait_for_timeout(300)
    _, store_control, mode_value, _ = await _anchor_controls(page)
    if mode_value != _COMMERCE_MODE_TEXT:
        raise DouyinCommerceError("抖音带货模式选择后未能页面回读确认，已安全停止")
    return store_control


async def _visible_store_listbox(page) -> Any | None:
    """只读定位当前已展开的唯一带货位置/门店候选列表。

    新版页面会把“名称、完整地址、商品/返佣摘要”直接放在同一个结果项中，
    不保证 class 名称稳定。本地范围还可能在同一列表中附带“无地理位置”等
    无地址辅助项，因此只要求列表至少存在一个完整地点；后续逐项规范化仍会
    丢弃无地址项。标签菜单等只有短文本的下拉仍不会匹配。
    """

    result = await page.evaluate(
        """() => {
"""
        + _STORE_EFFECTIVE_VISIBILITY_JS
        + """
                const normalize = value => String(value || '')
                    .replace(/[\u200b\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
                const looksAddress = value => {
                    const text = normalize(value);
                    return text.length >= 6
                        && !/(?:商品|返佣|佣金|团购|套餐|券|专区)/.test(text)
                        && /(?:自治区|省|市|区|县|镇|乡|街|路|大道|巷|号|楼|村)/.test(text);
                };
                const descriptor = option => {
                    const rawText = String(option.innerText || option.textContent || '')
                        .replace(/[\u200b\u00a0]/g, ' ');
                    const text = normalize(rawText);
                    const lines = rawText.split(/\\n+/).map(normalize).filter(Boolean);
                    const nameNode = option.querySelector('[data-store-name], [class*="name-"], [class*="name_"], [class*="title-"]');
                    const addressNode = option.querySelector('[data-store-address], [class*="address-"], [class*="address_"], [class*="addr"]');
                    const name = normalize(nameNode && (nameNode.innerText || nameNode.textContent)) || lines[0] || '';
                    const address = normalize(addressNode && (addressNode.innerText || addressNode.textContent))
                        || lines.find(line => looksAddress(line)) || '';
                    return { name, address };
                };
                const ownedOptions = list => Array.from(
                    list.querySelectorAll('[role="option"]')
                ).filter(option => option.closest('[role="listbox"]') === list);
                const labelLeaves = (root, label) => Array.from(
                    root.querySelectorAll('*')
                ).filter(isEffectivelyVisible)
                    .filter(node => normalize(node.innerText || node.textContent) === label)
                    .filter(node => !Array.from(node.children).some(child =>
                        isEffectivelyVisible(child)
                        && normalize(child.innerText || child.textContent) === label));
                const editableSelector =
                    'input, textarea, [contenteditable="true"][role="textbox"]';
                const editableFields = root => Array.from(
                    root.querySelectorAll(editableSelector)
                ).filter(isEffectivelyVisible).filter(node =>
                    !node.disabled && !node.readOnly
                    && String(node.type || '').toLowerCase() !== 'hidden');
                const lists = Array.from(document.querySelectorAll('[role="listbox"]'))
                    .filter(isEffectivelyVisible)
                    .filter(list => !list.parentElement?.closest('[role="listbox"]'))
                    .filter(list => {
                        const options = ownedOptions(list).filter(isEffectivelyVisible);
                        return options.length > 0 && options.some(option => {
                            const row = descriptor(option);
                            return row.name && row.address && looksAddress(row.address);
                        });
                    });
                if (lists.length !== 1) return { count: lists.length };
                const list = lists[0];
                let currentPanel = null;
                let currentInput = null;
                for (let current = list.parentElement, depth = 0;
                    current && current !== document.body && depth < 10;
                    current = current.parentElement, depth += 1) {
                    if (labelLeaves(current, '本地').length !== 1
                        || labelLeaves(current, '国内').length !== 1) {
                        continue;
                    }
                    const fields = editableFields(current);
                    if (fields.length === 1) {
                        currentPanel = current;
                        currentInput = fields[0];
                    }
                    break;
                }
                // 平台加载新一批候选时可能替换整个地点 portal，旧的 input/panel
                // marker 会随分离节点消失。候选列表本身已通过唯一完整地点校验，
                // 这里从同一列表反向重建当前面板 marker，供分页入口继续定位。
                if (currentPanel && currentInput) {
                    document.querySelectorAll(
                        '[data-oneclick-commerce-search-input]'
                    ).forEach(node => node.removeAttribute(
                        'data-oneclick-commerce-search-input'
                    ));
                    document.querySelectorAll(
                        '[data-oneclick-commerce-location-panel]'
                    ).forEach(node => node.removeAttribute(
                        'data-oneclick-commerce-location-panel'
                    ));
                    currentInput.dataset.oneclickCommerceSearchInput = 'active';
                    currentPanel.dataset.oneclickCommerceLocationPanel = 'active';
                }
                document.querySelectorAll('[data-oneclick-commerce-store-list]')
                    .forEach(node => node.removeAttribute(
                        'data-oneclick-commerce-store-list'
                    ));
                list.dataset.oneclickCommerceStoreList = 'active';
                return { count: 1 };
            }"""
    )
    if isinstance(result, Mapping) and int(result.get("count") or 0) == 1:
        return page.locator('[data-oneclick-commerce-store-list="active"]')
    return None


async def _wait_store_listbox(page) -> Any:
    """等待右侧带货地点下拉对应的唯一结构化候选列表。"""

    for _ in range(20):
        listbox = await _visible_store_listbox(page)
        if listbox is not None:
            return listbox
        await page.wait_for_timeout(200)
    raise DouyinCommerceError("抖音可带货地点列表未能唯一显示，已安全停止")


async def _visible_commerce_location_overlay(page) -> Any | None:
    """定位当前地点输入面板内的候选浮层，包括空结果和辅助项列表。

    读取地点候选仍只接受含完整地址的列表；关闭动作则必须识别同一面板内
    的所有结果列表，否则“未找到相关地点”等辅助列表会残留到下一次搜索。
    """

    result = await page.evaluate(
        """() => {
            const visible = node => {
                if (!(node instanceof HTMLElement)) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const normalize = value => String(value || '')
                .replace(/[\u200b\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
            const text = node => normalize(node.innerText || node.textContent);
            const labelLeaves = (root, label) => Array.from(root.querySelectorAll('*'))
                .filter(visible)
                .filter(node => text(node) === label)
                .filter(node => !Array.from(node.children)
                    .some(child => visible(child) && text(child) === label));
            document.querySelectorAll('[data-oneclick-commerce-location-overlay]')
                .forEach(node => node.removeAttribute(
                    'data-oneclick-commerce-location-overlay'
                ));
            const input = document.querySelector(
                '[data-oneclick-commerce-search-input="active"], '
                + '[data-oneclick-commerce-store="active"]'
            );
            if (!input || !visible(input)) return { count: 0 };
            let panel = null;
            for (let current = input.parentElement, depth = 0;
                current && current !== document.body && depth < 10;
                current = current.parentElement, depth += 1) {
                if (labelLeaves(current, '本地').length === 1
                    && labelLeaves(current, '国内').length === 1) {
                    panel = current;
                    break;
                }
            }
            if (!panel) return { count: 0 };
            const lists = Array.from(panel.querySelectorAll('[role="listbox"]'))
                .filter(visible)
                .filter(list => !list.parentElement?.closest('[role="listbox"]'))
                .filter(list => {
                    const options = Array.from(
                        list.querySelectorAll('[role="option"]')
                    ).filter(option => option.closest('[role="listbox"]') === list)
                        .filter(visible);
                    if (options.length === 0) return false;
                    const values = options.map(text);
                    return !(values.length === 2
                        && values.includes('带货模式')
                        && values.includes('打卡模式'));
                });
            if (lists.length !== 1) return { count: lists.length };
            lists[0].dataset.oneclickCommerceLocationOverlay = 'active';
            return { count: 1 };
        }"""
    )
    count = int(result.get("count") or 0) if isinstance(result, Mapping) else 0
    if count > 1:
        raise DouyinCommerceError(
            f"抖音当前地点面板出现 {count} 个候选浮层，无法安全关闭"
        )
    if count == 1:
        return page.locator('[data-oneclick-commerce-location-overlay="active"]')
    return None


async def _open_store_selector(page, store_control) -> Any:
    # 读取候选后，页面会保留已打开的下拉列表供本次会话选择。再次点击会把
    # 菜单收起，导致后续“唯一列表不存在”的假失败；先复用已确认的列表。
    existing = await _visible_store_listbox(page)
    if existing is not None:
        return existing
    # 新版位置控件本身就是可编辑输入框。它不是 semi-select，若继续按旧结构
    # 查找 ``.semi-select-selection`` 会造成“无法安全打开”的假失败。
    try:
        is_direct_input = bool(
            await store_control.evaluate(
                "node => node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement"
            )
        )
    except Exception:
        is_direct_input = False
    if is_direct_input:
        try:
            await store_control.click(timeout=5_000)
        except Exception as exc:
            raise DouyinCommerceError("抖音带货位置输入框无法安全打开，已停止") from exc
        return await _wait_store_listbox(page)
    await _open_exact_select(page, store_control, "可绑定门店")
    return await _wait_store_listbox(page)


async def _visible_commerce_search_input(page) -> Any:
    """定位已展开的“位置 + 带货模式”组合中的唯一地点输入框。

    抖音新版不会在页面初始状态就渲染实际 ``input``：用户先点击“输入
    地理位置”入口后，才会在同一个带货控件组中创建文本框和候选面板。这里
    只读取已经可见的输入框；入口展开由 ``_open_commerce_search_input`` 负责，
    避免把页面上其他标题、文案或共创输入框误认成地点输入框。
    """

    result = await page.evaluate(
        """() => {
            const visible = node => {
                if (!(node instanceof HTMLElement)) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const normalize = value => String(value || '')
                .replace(/[\\u200b\\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
            const text = node => normalize(node.innerText || node.textContent);
            const editable = node => visible(node)
                && !node.disabled
                && !node.readOnly
                && String(node.type || '').toLowerCase() !== 'hidden';
            const labelLeaves = (root, label) => Array.from(root.querySelectorAll('*'))
                .filter(visible)
                .filter(node => text(node) === label)
                .filter(node => !Array.from(node.children)
                    .some(child => visible(child) && text(child) === label));
            const root = document.querySelector('[data-oneclick-commerce-store="active"]');
            if (!root || !visible(root)) return { count: 0 };

            const editableSelector =
                'input, textarea, [contenteditable="true"][role="textbox"]';
            const fieldsWithin = panel => Array.from(
                panel.querySelectorAll(editableSelector)
            ).filter(editable);
            const lowestCommonAncestor = (left, right) => {
                const ancestors = new Set();
                for (let current = left;
                    current && current !== document.body;
                    current = current.parentElement) {
                    ancestors.add(current);
                }
                for (let current = right;
                    current && current !== document.body;
                    current = current.parentElement) {
                    if (ancestors.has(current)) return current;
                }
                return null;
            };
            const clearMarkers = () => {
                document.querySelectorAll('[data-oneclick-commerce-search-input]')
                    .forEach(node => node.removeAttribute(
                        'data-oneclick-commerce-search-input'
                    ));
                document.querySelectorAll('[data-oneclick-commerce-location-panel]')
                    .forEach(node => node.removeAttribute(
                        'data-oneclick-commerce-location-panel'
                    ));
            };
            clearMarkers();

            // 现代页面有时已把唯一地点控件直接标记为输入框，无需再搜索 portal。
            if (root.matches(editableSelector) && editable(root)) {
                root.dataset.oneclickCommerceSearchInput = 'active';
                return { count: 1, source: 'direct' };
            }

            // 新版抖音把“输入地理位置”渲染在 dy-creator-content-portal 一类的
            // 独立浮层，已不再是带货 .semi-select 的子节点。不能为了兼容 portal
            // 退化为全页任意 input。先从“本地/国内”标签对确定最小地点面板，
            // 再要求该面板内只有一个输入字段，避免共享大祖先时把整页字段算入。
            const portalMatches = [];
            let observedPortalFieldCount = 0;
            for (const local of labelLeaves(document, '本地')) {
                for (const domestic of labelLeaves(document, '国内')) {
                    let panel = lowestCommonAncestor(local, domestic);
                    for (let depth = 0;
                        panel && panel !== document.body && depth < 8;
                        panel = panel.parentElement, depth += 1) {
                        if (labelLeaves(panel, '本地').length !== 1
                            || labelLeaves(panel, '国内').length !== 1) {
                            continue;
                        }
                        const fields = fieldsWithin(panel);
                        if (fields.length > 0 && observedPortalFieldCount === 0) {
                            observedPortalFieldCount = fields.length;
                        }
                        if (fields.length === 1) {
                            if (!portalMatches.some(item => item.field === fields[0])) {
                                portalMatches.push({ field: fields[0], panel });
                            }
                            break;
                        }
                        if (fields.length > 1) break;
                    }
                }
            }
            if (portalMatches.length > 1) return { count: portalMatches.length };
            let matches = portalMatches;
            if (matches.length === 0 && observedPortalFieldCount > 0) {
                return { count: observedPortalFieldCount };
            }

            // 保留旧版同组结构作为后备：少数账号仍把输入框渲染在 anchor-item 内，
            // 但它没有 portal 面板时才使用此分支，避免与当前浮层重复计数。
            if (matches.length === 0) {
                const group = root.closest('[class*="anchor-item"]') || root.parentElement;
                const scopes = [root];
                if (group && group !== root) scopes.push(group);
                const seen = new Set();
                matches = scopes.flatMap(scope => Array.from(
                    scope.querySelectorAll('input, textarea, [contenteditable="true"][role="textbox"]')
                )).filter(field => {
                    if (seen.has(field) || !editable(field)) return false;
                    seen.add(field);
                    return true;
                }).map(field => ({ field, panel: null }));
            }
            if (matches.length !== 1) return { count: matches.length };
            matches[0].field.dataset.oneclickCommerceSearchInput = 'active';
            if (matches[0].panel) {
                matches[0].panel.dataset.oneclickCommerceLocationPanel = 'active';
            }
            return { count: 1, source: matches[0].panel ? 'portal' : 'anchor' };
        }"""
    )
    if not isinstance(result, Mapping) or int(result.get("count") or 0) != 1:
        count = int(result.get("count") or 0) if isinstance(result, Mapping) else 0
        raise DouyinCommerceError(
            f"抖音带货位置输入框未能唯一显示（实际 {count} 个），已安全停止"
        )
    return page.locator('[data-oneclick-commerce-search-input="active"]')


async def _open_commerce_search_input(page, store_control) -> Any:
    """安全展开位置入口，并等待该入口实际创建唯一输入框。

    这个动作只打开用户已经选择的“带货模式”下的位置搜索入口；不填关键词、
    不选择候选，也不修改任何发布设置。已经展开时直接复用，避免二次点击把
    平台下拉收起。
    """

    try:
        return await _visible_commerce_search_input(page)
    except DouyinCommerceError as exc:
        # 只有“尚未创建输入框”的确定状态才允许打开入口。多个输入框或其他
        # 页面结构异常仍须立即停止，不能通过重新点击掩盖歧义。
        if "实际 0 个" not in str(exc):
            raise

    await _open_exact_select(page, store_control, "带货位置")
    for _ in range(20):
        try:
            return await _visible_commerce_search_input(page)
        except DouyinCommerceError as exc:
            if "实际 0 个" not in str(exc):
                raise
        await page.wait_for_timeout(200)
    raise DouyinCommerceError("抖音带货位置输入框打开后未能唯一显示，已安全停止")


async def set_commerce_location_scope(page, scope: object) -> str:
    """按用户明确选择切换并回读新版地点搜索范围。

    抖音编辑页把“本地/国内”放在地点搜索面板内。范围标签的文字节点经常嵌套
    在包含多个标签的父节点中，因此不能先提升到父节点、再拿父节点全文和“本地”
    /“国内”做相等比较；那会把一个正常的标签组误判成 0 个。这里始终以地点输入
    框所在面板内的两个可见文字叶节点为锚点，再从各自叶节点向上寻找可点击节点。

    只有同一地点面板中恰好存在一对“本地/国内”时才会继续。点击后仍须通过
    aria、data-state 或平台的 active/selected/current 状态回读，不能把一次 click
    当成范围已切换。
    """

    normalized_scope = normalize_commerce_location_scope(scope)
    expected_label = LOCATION_SCOPE_LABELS[normalized_scope]
    # 抖音切换标签后会异步更新下拉结果。至少连续两次看到“目标已选、另一项
    # 未选”才算切换完成，避免刚点击就把旧的“本地”请求结果当成“国内”。
    stable_selected_reads = 0
    last_missing_scope_pair = False
    # 连续切换或平台侧限速时，这组标签实测可能在输入后的 3 秒以后才挂载。
    # 以控件真实出现为完成条件，最多等待约 6 秒；不使用固定成功休眠，也不
    # 放宽唯一性和选中态回读要求。
    for _ in range(30):
        # 范围点击会替换整个 portal，旧输入框上的临时 marker
        # 会随 DOM 一起消失。每次回读前都重新限定当前地点面板；
        # 短暂的 0 个是重绘中，多个仍然立即安全停止。
        try:
            await _visible_commerce_search_input(page)
        except DouyinCommerceError as exc:
            if "实际 0 个" in str(exc):
                await page.wait_for_timeout(200)
                continue
            raise
        result = await page.evaluate(
            """() => {
                const visible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const normalize = value => String(value || '')
                    .replace(/[\u200b\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
                const text = node => normalize(node.innerText || node.textContent);
                const labelLeaves = (root, label) => Array.from(root.querySelectorAll('*'))
                    .filter(visible)
                    .filter(node => text(node) === label)
                    .filter(node => !Array.from(node.children).some(child => visible(child) && text(child) === label));
                const lowestCommonAncestor = (left, right, boundary) => {
                    const leftAncestors = new Set();
                    for (let current = left; current; current = current.parentElement) {
                        leftAncestors.add(current);
                        if (current === boundary) break;
                    }
                    for (let current = right; current; current = current.parentElement) {
                        if (leftAncestors.has(current)) return current;
                        if (current === boundary) break;
                    }
                    return null;
                };
                const interactive = (node, boundary) => {
                    let current = node;
                    for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {
                        const role = String(current.getAttribute?.('role') || '').toLowerCase();
                        const tag = String(current.tagName || '').toLowerCase();
                        const className = String(current.className || '');
                        if (role === 'tab' || tag === 'button'
                            || Number(current.tabIndex) >= 0
                            || /(?:^|[-_\\s])(?:tab|option|item|select)(?:$|[-_\\s])/i.test(className)) {
                            return current;
                        }
                        if (current === boundary) break;
                    }
                    return node;
                };
                const isMarkedSelected = (node, boundary) => {
                    let current = node;
                    // boundary 是“本地/国内”两项的公共容器，不是任一范围自身。
                    // 它可能因整个地点面板被展开而带有 active 类；若把它纳入
                    // 判断，两个范围都会被误判为已选，客户端便会显示“国内”但
                    // 平台其实仍停留在“本地”。
                    for (let depth = 0; current && current !== boundary && depth < 5; depth += 1, current = current.parentElement) {
                        const className = String(current.className || '');
                        const state = String(current.getAttribute?.('data-state') || '').toLowerCase();
                        const ariaCurrent = String(current.getAttribute?.('aria-current') || '').toLowerCase();
                        if (current.getAttribute?.('aria-selected') === 'true'
                            || current.getAttribute?.('aria-checked') === 'true'
                            || ariaCurrent === 'true' || ariaCurrent === 'page'
                            || /^(?:active|selected|current|checked)$/.test(state)
                            || /(?:^|[-_\\s])(?:active|selected|current|checked|is-active|is-selected)(?:$|[-_\\s])/i.test(className)) {
                            return true;
                        }
                        if (current === boundary) break;
                    }
                    return false;
                };
                const input = document.querySelector('[data-oneclick-commerce-search-input="active"]');
                if (!input || !visible(input)) return { state: 'ambiguous', reason: 'search-input-not-visible', local: 0, domestic: 0 };
                const panels = [];
                for (let current = input.parentElement, depth = 0;
                    current && current !== document.body && depth < 10;
                    current = current.parentElement, depth += 1) {
                    const local = labelLeaves(current, '本地');
                    const domestic = labelLeaves(current, '国内');
                    if (local.length === 1 && domestic.length === 1) {
                        panels.push({ root: current, local: local[0], domestic: domestic[0], depth });
                    }
                }
                if (panels.length === 0) {
                    const local = labelLeaves(document.body, '本地').length;
                    const domestic = labelLeaves(document.body, '国内').length;
                    return { state: 'ambiguous', reason: 'pair-not-in-search-panel', local, domestic };
                }
                // 选择包含输入框且层级最小的面板，避免落入同页其他隐藏/无关标签组。
                const panel = panels[0];
                // 选中态只能在“本地/国内”这对标签自身的最小公共容器中回读。
                // 不能一路查到整个地点面板：地点面板中其他控件的 selected/active
                // class 会导致“国内”被误判已选，从而从未真正切换平台范围。
                const scopeGroup = lowestCommonAncestor(panel.local, panel.domestic, panel.root);
                if (!scopeGroup || !visible(scopeGroup)) {
                    return { state: 'ambiguous', reason: 'scope-group-not-visible', local: 0, domestic: 0 };
                }
                const localTarget = interactive(panel.local, scopeGroup);
                const domesticTarget = interactive(panel.domestic, scopeGroup);
                if (!visible(localTarget) || !visible(domesticTarget)) {
                    return { state: 'ambiguous', reason: 'scope-target-not-visible', local: 0, domestic: 0 };
                }
                const target = '%s' === '本地' ? localTarget : domesticTarget;
                const targetLabel = '%s' === '本地' ? panel.local : panel.domestic;
                const localSelected = isMarkedSelected(panel.local, scopeGroup)
                    || isMarkedSelected(localTarget, scopeGroup);
                const domesticSelected = isMarkedSelected(panel.domestic, scopeGroup)
                    || isMarkedSelected(domesticTarget, scopeGroup);
                const expectedSelected = '%s' === '本地' ? localSelected : domesticSelected;
                const otherSelected = '%s' === '本地' ? domesticSelected : localSelected;
                if (expectedSelected && otherSelected) {
                    return {
                        state: 'ambiguous', reason: 'scope-selection-not-exclusive',
                        local: 1, domestic: 1, panelDepth: panel.depth,
                    };
                }
                document.querySelectorAll('[data-oneclick-commerce-location-scope]')
                    .forEach(node => node.removeAttribute('data-oneclick-commerce-location-scope'));
                target.dataset.oneclickCommerceLocationScope = 'active';
                return {
                    state: expectedSelected ? 'selected' : 'ready',
                    local: 1,
                    domestic: 1,
                    panelDepth: panel.depth,
                };
            }"""
            % (expected_label, expected_label, expected_label, expected_label)
        )
        if not isinstance(result, Mapping):
            await page.wait_for_timeout(200)
            continue
        state = _normalized(result.get("state"))
        if state == "ambiguous":
            local_count = int(result.get("local") or 0)
            domestic_count = int(result.get("domestic") or 0)
            reason = _normalized(result.get("reason"))
            if (
                local_count == 0
                and domestic_count == 0
                and reason == "pair-not-in-search-panel"
            ):
                # 输入或范围切换后 portal 会先替换输入框，再补上范围标签；
                # 这一小段 0/0 是已知重绘过渡态，给页面受限时间收敛。
                last_missing_scope_pair = True
                await page.wait_for_timeout(200)
                continue
            suffix = f"；{reason}" if reason else ""
            diagnostic = await _capture_configured_location_entry_diagnostic(page)
            diagnostic_suffix = (
                f"；脱敏诊断已保存：{diagnostic.name}" if diagnostic else ""
            )
            raise DouyinCommerceError(
                "抖音位置搜索范围“本地/国内”控件未能唯一显示"
                f"（当前地点面板内本地 {local_count} 个、国内 {domestic_count} 个{suffix}），已安全停止"
                f"{diagnostic_suffix}"
            )
        if state == "selected":
            stable_selected_reads += 1
            if stable_selected_reads >= 2:
                return expected_label
            await page.wait_for_timeout(150)
            continue
        if state == "ready":
            stable_selected_reads = 0
            control = page.locator('[data-oneclick-commerce-location-scope="active"]')
            try:
                await control.click(timeout=5_000)
            except Exception as exc:
                raise DouyinCommerceError(f"抖音位置搜索范围“{expected_label}”无法安全选择") from exc
            await page.wait_for_timeout(250)
            continue
        await page.wait_for_timeout(200)
    if last_missing_scope_pair:
        diagnostic = await _capture_configured_location_entry_diagnostic(page)
        diagnostic_suffix = (
            f"；脱敏诊断已保存：{diagnostic.name}" if diagnostic else ""
        )
        raise DouyinCommerceError(
            "抖音位置搜索范围“本地/国内”控件未能唯一显示"
            "（当前地点面板内本地 0 个、国内 0 个；pair-not-in-search-panel），已安全停止"
            f"{diagnostic_suffix}"
        )
    raise DouyinCommerceError(
        f"抖音位置搜索范围“本地/国内”未能确认“{expected_label}”，已安全停止"
    )


def _location_result_signature(rows: list[Mapping[str, Any]]) -> str:
    """返回可见地点列表的最小快照，用于排除输入前的旧候选。"""

    signatures: list[str] = []
    for row in rows:
        if not (_normalized(row.get("name")) or _normalized(row.get("address"))):
            continue
        commission = normalize_candidate_commission_fields(row)

        def count_text(field: str) -> str:
            value = commission.get(field)
            return str(value) if type(value) is int and value >= 0 else ""

        signatures.append(
            "\u241f".join(
                (
                    _normalized(row.get("name")),
                    _normalized(row.get("address")),
                    _normalized(row.get("distance")),
                    _normalized(commission.get("commissionType")),
                    count_text("productCount"),
                    count_text("commissionProductCount"),
                )
            )
        )
    return "\n".join(signatures)


def _location_rows_match_keyword(rows: list[Mapping[str, Any]], keyword: str) -> bool:
    """判断可见候选是否至少含有搜索词的一个有效片段。

    该校验不是本地 POI 搜索，也不替代平台排序；它只用于拒绝“输入遂宁夜南香，
    却立即回显北海旧本地候选”这种明显陈旧的列表。中文搜索词以相邻双字片段
    作为最小匹配单元，英文/数字则以两个字符以上的词作为单元。
    """

    compact = "".join(_normalized(keyword).casefold().split())
    if not compact:
        return False
    tokens = {compact}
    chinese = [char for char in compact if "\u4e00" <= char <= "\u9fff"]
    tokens.update("".join(chinese)[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    current = ""
    for char in compact:
        if char.isascii() and char.isalnum():
            current += char
        else:
            if len(current) >= 2:
                tokens.add(current)
            current = ""
    if len(current) >= 2:
        tokens.add(current)
    tokens = {item for item in tokens if len(item) >= 2}
    if not tokens:
        return True
    for row in rows:
        visible = "".join(
            _normalized(row.get(field)).casefold()
            for field in ("name", "address", "distance")
        )
        if any(token in visible for token in tokens):
            return True
    return False


async def _visible_commerce_location_result_snapshot(
    page,
) -> tuple[Any | None, list[dict[str, str]], str]:
    """读取当前可见地点列表及快照，不把列表存在误作本次检索完成。"""

    listbox = await _visible_store_listbox(page)
    if listbox is None:
        return None, [], ""
    rows = await _store_option_descriptors(listbox)
    return listbox, rows, _location_result_signature(rows)


async def _wait_for_fresh_commerce_location_results(
    page,
    *,
    baseline_signature: str,
    keyword: str,
    allow_stable_baseline_match: bool = False,
    expected_location: Mapping[str, Any] | None = None,
    commission_filter: object = "all",
    allow_filtered_empty: bool = False,
    timeout_ms: int = _LOCATION_RESULT_WAIT_TIMEOUT_MS,
    stable_reads_required: int = _LOCATION_RESULT_STABLE_READS,
    deadline: float | None = None,
) -> tuple[Any, list[dict[str, str]]]:
    """等待本次关键词对应的完整稳定候选，过滤空列表与陈旧下拉。

    ``allow_stable_baseline_match`` 仅供已经完成“清空再填回关键词”的范围
    切换流程使用。抖音会在范围切换时自动查询一次原关键词，此时最终的本地
    结果可能与清空后的快照完全相同；连续两次回读一致且匹配关键词时，可将其
    视为已稳定的当前范围结果，而不是误报为旧候选。

    正式发布可传入 ``expected_location`` 和 ``commission_filter``。慢网络下
    即使先出现空列表、非目标候选或分批渲染的候选，也必须等符合返佣要求的
    目标 POI 出现且列表连续稳定后才返回。
    """

    selected_commission_filter = normalize_commission_filter(
        commission_filter,
        default="all",
    )
    normalized_timeout_ms = max(
        _LOCATION_RESULT_POLL_INTERVAL_MS,
        int(timeout_ms),
    )
    required_reads = max(2, int(stable_reads_required))
    max_reads = max(
        required_reads,
        normalized_timeout_ms // _LOCATION_RESULT_POLL_INTERVAL_MS + 1,
    )
    expected = (
        normalize_commerce_location_candidate(expected_location)
        if isinstance(expected_location, Mapping)
        else None
    )
    started_at = monotonic()
    last_signature = ""
    stable_signature = ""
    stable_reads = 0
    first_complete_logged = False
    target_seen_logged = False
    commission_mismatch_seen = False
    douyin_logger.info(
        f"抖音地点候选开始等待：关键词={keyword}，最长等待="
        f"{normalized_timeout_ms / 1000:.1f} 秒，稳定要求={required_reads} 次"
    )
    for _ in range(max_reads):
        listbox, rows, signature = await _await_publish_location_dom_action(
            lambda _timeout: _visible_commerce_location_result_snapshot(page),
            deadline=deadline,
        )
        unfiltered_candidates = [
            candidate
            for row in rows
            if (candidate := normalize_commerce_location_candidate(row))
        ]
        candidates = normalize_commerce_location_candidates(
            rows,
            commission_filter=selected_commission_filter,
        )
        if expected is not None and selected_commission_filter != "all":
            try:
                match_location_preset(expected, unfiltered_candidates)
                unfiltered_target_ready = True
            except DouyinLocationPresetError as exc:
                unfiltered_target_ready = "存在多个" in str(exc)
            try:
                match_location_preset(expected, candidates)
                filtered_target_ready = True
            except DouyinLocationPresetError as exc:
                filtered_target_ready = "存在多个" in str(exc)
            if unfiltered_target_ready and not filtered_target_ready:
                commission_mismatch_seen = True
        stable_candidates = candidates
        if (
            allow_filtered_empty
            and not candidates
            and expected is None
            and unfiltered_candidates
        ):
            stable_candidates = unfiltered_candidates
        if listbox is not None and stable_candidates:
            last_signature = signature
            if not first_complete_logged:
                first_complete_logged = True
                douyin_logger.info(
                    f"抖音地点候选首次返回完整数据：关键词={keyword}，"
                    f"候选数={len(stable_candidates)}，继续等待目标和列表稳定"
                )
            matches_keyword = _location_rows_match_keyword(
                stable_candidates,
                keyword,
            )
            is_fresh = (
                signature != baseline_signature
                or allow_stable_baseline_match
            )
            target_ready = expected is None
            if expected is not None:
                try:
                    match_location_preset(expected, candidates)
                    target_ready = True
                except DouyinLocationPresetError as exc:
                    # 多个同身份候选也要先等列表稳定，再由调用方以明确的
                    # ambiguous 错误安全停止；目标尚未出现时则继续等待。
                    target_ready = "存在多个" in str(exc)
            eligible = matches_keyword and is_fresh and target_ready
            if eligible:
                if expected is not None and not target_seen_logged:
                    target_seen_logged = True
                    douyin_logger.info(
                        f"抖音地点目标候选已出现：关键词={keyword}，"
                        "继续确认候选列表稳定"
                    )
                if signature == stable_signature:
                    stable_reads += 1
                else:
                    stable_signature = signature
                    stable_reads = 1
                if stable_reads >= required_reads:
                    elapsed = monotonic() - started_at
                    douyin_logger.info(
                        f"抖音地点候选已稳定：关键词={keyword}，"
                        f"候选数={len(stable_candidates)}，耗时={elapsed:.1f} 秒"
                    )
                    # 保持既有内部契约：等待器返回页面原始描述，统一由搜索
                    # 出口归一化；这里只用归一化候选判断完整性与目标身份。
                    return listbox, rows
            else:
                stable_signature = ""
                stable_reads = 0
        else:
            # 空列表只表示平台仍在加载，不能作为搜索完成或可点击状态。
            stable_signature = ""
            stable_reads = 0
        await _wait_publish_location_timeout(
            page,
            _LOCATION_RESULT_POLL_INTERVAL_MS,
            deadline=deadline,
        )
    elapsed = monotonic() - started_at
    douyin_logger.warning(
        f"抖音地点候选等待超时：关键词={keyword}，耗时={elapsed:.1f} 秒，"
        f"是否出现完整候选={'是' if first_complete_logged else '否'}，"
        f"是否出现目标={'是' if target_seen_logged else '否'}"
    )
    if deadline is not None:
        raise DouyinCommerceError(_PUBLISH_LOCATION_LIMIT_CODE) from None
    if commission_mismatch_seen:
        raise DouyinCommerceError("publish_location_commission_mismatch")
    if last_signature and last_signature == baseline_signature:
        raise DouyinCommerceError(
            f"抖音“{keyword}”地点结果仍是切换范围前的旧候选，未展示错误地址，请重试"
        )
    raise DouyinCommerceError(
        f"抖音未返回与“{keyword}”相符的最新完整发布定位，已安全停止"
    )


async def search_commerce_location_store_candidates(
    page,
    keyword: object,
    *,
    scope: object,
    expected_location: Mapping[str, Any] | None = None,
    commission_filter: object = "all",
    include_metadata: bool = False,
    timeout_ms: int = _LOCATION_RESULT_WAIT_TIMEOUT_MS,
    deadline: float | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """按“本地/国内”范围和返佣要求读取发布定位候选。

    此步骤只会展开“添加标签 → 位置 → 带货模式”后对应的输入框，填入关键词
    并读取结果；不会选择结果、绑定门店、保存草稿或提交发布。
    默认保持旧的候选列表回包；显式启用元数据时，另返回过滤前
    可见有效候选数，候选本身仍只包含过滤后的公开结构字段。
    """

    selected_commission_filter = normalize_commission_filter(
        commission_filter,
        default="all",
    )
    normalized_keyword = normalize_location_keyword(keyword)
    await _await_publish_location_dom_action(
        lambda _timeout: _ensure_position_tag(page),
        deadline=deadline,
    )
    store_control = await _await_publish_location_dom_action(
        lambda _timeout: _ensure_local_group_buy_mode(page),
        deadline=deadline,
    )
    input_control = await _await_publish_location_dom_action(
        lambda _timeout: _open_commerce_search_input(page, store_control),
        deadline=deadline,
    )
    try:
        await _await_publish_location_dom_action(
            lambda action_timeout: input_control.scroll_into_view_if_needed(
                timeout=action_timeout
            ),
            deadline=deadline,
            timeout_ms=5_000,
        )
        await _await_publish_location_dom_action(
            lambda action_timeout: input_control.click(timeout=action_timeout),
            deadline=deadline,
            timeout_ms=5_000,
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError("抖音带货位置输入框无法安全打开，已停止") from exc
    except Exception as exc:
        raise DouyinCommerceError("抖音带货位置输入框无法安全打开，已停止") from exc
    # 抖音会在关键词输入后立即按当前范围请求候选；必须先把页面实际范围切成
    # 用户所选“本地/国内”，再填写关键词，避免客户端默认“国内”但平台仍以
    # 初始“本地”返回候选。
    try:
        await _await_publish_location_dom_action(
            lambda _timeout: set_commerce_location_scope(page, scope),
            deadline=deadline,
        )
    except DouyinCommerceError as exc:
        message = str(exc)
        delayed_scope_panel = (
            "本地 0 个、国内 0 个" in message
            and "pair-not-in-search-panel" in message
        )
        if not delayed_scope_panel:
            raise
        # 当前新版在空输入框获得焦点时仍不渲染“本地/国内”，只有本次关键
        # 词触发首轮候选后才创建范围标签。该首轮结果只负责唤起面板，不会
        # 返回客户端；随后仍会切换目标范围、清空旧词并重新检索与回读。
        try:
            # 重复同词搜索时输入框仍保留旧值；直接 fill 同样文字不会触发
            # input 事件，也就不会重新创建范围面板。先清空再写回，确保每次
            # 都产生一轮新的、可验证的页面请求。
            await _await_publish_location_dom_action(
                lambda action_timeout: input_control.fill(
                    "", timeout=action_timeout
                ),
                deadline=deadline,
                timeout_ms=8_000,
            )
            await _wait_publish_location_timeout(page, 120, deadline=deadline)
            await _await_publish_location_dom_action(
                lambda action_timeout: input_control.click(timeout=action_timeout),
                deadline=deadline,
                timeout_ms=5_000,
            )
            await _await_publish_location_dom_action(
                lambda action_timeout: input_control.fill(
                    normalized_keyword, timeout=action_timeout
                ),
                deadline=deadline,
                timeout_ms=8_000,
            )
        except DouyinCommerceError as fill_exc:
            if str(fill_exc) == _PUBLISH_LOCATION_LIMIT_CODE:
                raise
            raise DouyinCommerceError(
                "抖音地点范围面板未显示且无法用本次关键词安全唤起"
            ) from fill_exc
        except Exception as fill_exc:
            raise DouyinCommerceError(
                "抖音地点范围面板未显示且无法用本次关键词安全唤起"
            ) from fill_exc
        await _wait_publish_location_timeout(page, 900, deadline=deadline)
        await _await_publish_location_dom_action(
            lambda _timeout: set_commerce_location_scope(page, scope),
            deadline=deadline,
        )
    # 切换范围会重绘输入框和候选面板，不能继续使用切换前
    # 取得的 locator。重新回读带货模式与唯一输入框，同时兼容
    # 平台在切换后直接收起浮层的情况。
    store_control = await _await_publish_location_dom_action(
        lambda _timeout: _ensure_local_group_buy_mode(page),
        deadline=deadline,
    )
    input_control = await _await_publish_location_dom_action(
        lambda _timeout: _open_commerce_search_input(page, store_control),
        deadline=deadline,
    )
    try:
        await _await_publish_location_dom_action(
            lambda action_timeout: input_control.scroll_into_view_if_needed(
                timeout=action_timeout
            ),
            deadline=deadline,
            timeout_ms=5_000,
        )
        await _await_publish_location_dom_action(
            lambda action_timeout: input_control.click(timeout=action_timeout),
            deadline=deadline,
            timeout_ms=5_000,
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError(
            "抖音范围切换后的位置输入框无法安全打开，已停止"
        ) from exc
    except Exception as exc:
        raise DouyinCommerceError(
            "抖音范围切换后的位置输入框无法安全打开，已停止"
        ) from exc
    # 范围切换后若输入框保留着同一个关键词，fill(同样文本) 不会触发 input
    # 事件，抖音便继续展示切换前的旧候选。先清空再重新填写，强制触发当前
    # 范围的一次新检索；后续仍严格校验候选的完整地址和关键词匹配。
    try:
        await _await_publish_location_dom_action(
            lambda action_timeout: input_control.fill("", timeout=action_timeout),
            deadline=deadline,
            timeout_ms=8_000,
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError("抖音带货位置输入框无法重置关键词，已安全停止") from exc
    except Exception as exc:
        raise DouyinCommerceError("抖音带货位置输入框无法重置关键词，已安全停止") from exc
    try:
        cleared_keyword = _normalized(
            await _await_publish_location_dom_action(
                lambda _timeout: input_control.evaluate(
                    """node => node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement
                        ? node.value : (node.innerText || node.textContent || '')"""
                ),
                deadline=deadline,
            )
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError("抖音带货位置输入框清空后无法回读，已安全停止") from exc
    except Exception as exc:
        raise DouyinCommerceError("抖音带货位置输入框清空后无法回读，已安全停止") from exc
    if cleared_keyword:
        raise DouyinCommerceError("抖音带货位置输入框未能清空旧关键词，已安全停止")
    # 输入框首次展开或清空时，平台可能仍在回填初始“本地”推荐；先给范围切换
    # 与清空关键词触发的请求一个受限的收敛时间，并保留快照。后续必须等待
    # 列表真正变化，不能像此前那样只要看到 listbox 就立刻把旧结果返回客户端。
    await _wait_publish_location_timeout(page, 450, deadline=deadline)
    _, _, baseline_signature = await _await_publish_location_dom_action(
        lambda _timeout: _visible_commerce_location_result_snapshot(page),
        deadline=deadline,
    )
    # 清空关键词本身也可能让抖音替换整个搜索组件。上面的 locator 即使刚刚
    # 成功清空，也可能在 450ms 收敛期间失效；重新从唯一“添加标签”行定位
    # 当前输入框，再执行本次真正检索，避免向已分离节点写词。
    store_control = await _await_publish_location_dom_action(
        lambda _timeout: _ensure_local_group_buy_mode(page),
        deadline=deadline,
    )
    input_control = await _await_publish_location_dom_action(
        lambda _timeout: _open_commerce_search_input(page, store_control),
        deadline=deadline,
    )
    try:
        await _await_publish_location_dom_action(
            lambda action_timeout: input_control.scroll_into_view_if_needed(
                timeout=action_timeout
            ),
            deadline=deadline,
            timeout_ms=5_000,
        )
        await _await_publish_location_dom_action(
            lambda action_timeout: input_control.click(timeout=action_timeout),
            deadline=deadline,
            timeout_ms=5_000,
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError(
            "抖音关键词重置后的位置输入框无法安全打开，已停止"
        ) from exc
    except Exception as exc:
        raise DouyinCommerceError(
            "抖音关键词重置后的位置输入框无法安全打开，已停止"
        ) from exc
    try:
        await _await_publish_location_dom_action(
            lambda action_timeout: input_control.fill(
                normalized_keyword, timeout=action_timeout
            ),
            deadline=deadline,
            timeout_ms=8_000,
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError("抖音带货位置输入框无法填写关键词，已安全停止") from exc
    except Exception as exc:
        raise DouyinCommerceError("抖音带货位置输入框无法填写关键词，已安全停止") from exc
    try:
        written_keyword = _normalized(
            await _await_publish_location_dom_action(
                lambda _timeout: input_control.evaluate(
                    """node => node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement
                        ? node.value : (node.innerText || node.textContent || '')"""
                ),
                deadline=deadline,
            )
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError("抖音带货位置关键词写入后无法回读，已安全停止") from exc
    except Exception as exc:
        raise DouyinCommerceError("抖音带货位置关键词写入后无法回读，已安全停止") from exc
    if written_keyword != normalized_keyword:
        raise DouyinCommerceError("抖音带货位置关键词写入后回读不一致，已安全停止")
    # 不使用固定“成功等待”。只接受本次关键词触发、且相对于输入前已变化的
    # 结构化候选列表。
    listbox, rows = await _wait_for_fresh_commerce_location_results(
        page,
        baseline_signature=baseline_signature,
        keyword=normalized_keyword,
        allow_stable_baseline_match=True,
        expected_location=expected_location,
        commission_filter=selected_commission_filter,
        allow_filtered_empty=include_metadata is True,
        timeout_ms=timeout_ms,
        deadline=deadline,
    )
    platform_result_count = sum(
        1
        for row in rows
        if normalize_commerce_location_candidate(row) is not None
    )
    candidates = normalize_commerce_location_candidates(
        rows,
        commission_filter=selected_commission_filter,
    )
    if not candidates and not (
        include_metadata is True and platform_result_count > 0
    ):
        raise DouyinCommerceError(
            f"抖音未返回“{normalized_keyword}”的完整可选发布定位"
        )
    if include_metadata is True:
        return {
            "platformResultCount": platform_result_count,
            "candidates": [dict(item) for item in candidates],
        }
    return candidates


async def _unique_visible_load_more_control(page) -> Any | None:
    """返回当前地点面板唯一可点击的“加载更多”控件。"""

    listbox = await _visible_store_listbox(page)
    if listbox is None:
        return None
    # 平台只在地址选择框滚到底后展示/激活分页行。每次查找分页入口前都
    # 明确滚动当前唯一地点 listbox，并派发 scroll 让 React 完成本批渲染；
    # 点击新增一批后，下次调用会再次滚到新的底部。
    await listbox.evaluate(
        """node => {
            node.scrollTop = Math.max(0, node.scrollHeight - node.clientHeight);
            node.dispatchEvent(new Event('scroll', { bubbles: true }));
        }"""
    )
    await page.wait_for_timeout(150)

    result = await page.evaluate(
        """() => {
"""
        + _STORE_EFFECTIVE_VISIBILITY_JS
        + """
                const normalize = value => String(value || '')
                    .replace(/[\u200b\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
                const listbox = document.querySelector(
                    '[data-oneclick-commerce-store-list="active"]'
                );
                if (!listbox || !isEffectivelyVisible(listbox)) {
                    return { count: 0 };
                }
                const panelSelector = [
                    '[data-oneclick-commerce-location-panel]',
                    '[id*="location-panel"]',
                    '[id*="location_panel"]',
                    '[id*="poi-panel"]',
                    '[class*="location-panel"]',
                    '[class*="location_panel"]',
                    '[class*="poi-panel"]'
                ].join(', ');
                const explicitPanel = listbox.closest(panelSelector);
                const controlledAnchors = Array.from(new Set(Array.from(
                    document.querySelectorAll(
                        '[data-oneclick-commerce-search-input="active"], '
                        + '[data-oneclick-commerce-store="active"]'
                    )
                ).filter(isEffectivelyVisible)));
                const commonAncestor = (left, right) => {
                    const ancestors = new Set();
                    for (let current = left; current && current !== document.body;
                        current = current.parentElement) ancestors.add(current);
                    for (let current = right; current && current !== document.body;
                        current = current.parentElement) {
                        if (ancestors.has(current)) return current;
                    }
                    return null;
                };
                const boundedPanel = controlledAnchors.length === 1
                    ? commonAncestor(controlledAnchors[0], listbox) : null;
                const genericOwnerSelector = [
                    '[role="dialog"]',
                    '[contenteditable="true"]',
                    '[data-oneclick-commerce-editor]',
                    '[id*="editor"]',
                    '[class*="editor"]'
                ].join(', ');
                const boundedGenericOwner = boundedPanel
                    ? boundedPanel.closest(genericOwnerSelector) : null;
                const isExactLoadMoreNode = node => {
                    if (!isEffectivelyVisible(node)) return false;
                    const text = normalize(node instanceof HTMLInputElement
                        ? node.value : (node.innerText || node.textContent));
                    return text === '点击加载更多';
                };
                // 真实页面把地址下拉渲染在独立 portal 中，搜索输入与
                // listbox 没有共同面板；分页行是该唯一 listbox 的末尾
                // 子节点。它由当前唯一完整地点列表直接拥有，不再依赖输入框
                // 祖先关系。后续 Playwright scroll_into_view 会先把列表滚到底。
                const listboxExactLoadMoreTextNodes = Array.from(
                    listbox.querySelectorAll('*')
                ).filter(isExactLoadMoreNode);
                const exactLoadMoreTextNodes = boundedPanel
                    ? Array.from(boundedPanel.querySelectorAll('*')).filter(node => {
                        return isExactLoadMoreNode(node)
                            && Boolean(listbox.compareDocumentPosition(node)
                                & Node.DOCUMENT_POSITION_FOLLOWING);
                    }) : [];
                const boundedExactLoadMore = exactLoadMoreTextNodes.length > 0;
                const boundedLocationRegion = boundedPanel
                    && (!boundedGenericOwner || boundedExactLoadMore)
                    ? boundedPanel : null;
                const panel = listboxExactLoadMoreTextNodes.length > 0
                    ? listbox
                    : explicitPanel && explicitPanel !== listbox
                    ? explicitPanel : boundedLocationRegion;
                if (!panel || panel === document.body
                    || !isEffectivelyVisible(panel)) return { count: 0 };
                const interactiveCandidates = Array.from(panel.querySelectorAll(
                    'button, input[type="button"], input[type="submit"], '
                    + 'a[href], [role="button"], [tabindex], [onclick]'
                )).filter(isEffectivelyVisible).filter(node => {
                    if (listbox.contains(node)) return false;
                    const ownerPanel = node.closest(panelSelector);
                    if (ownerPanel && ownerPanel !== panel) return false;
                    if (node.matches('[disabled], [aria-disabled="true"]')
                        || node.closest('[disabled], [aria-disabled="true"], [inert]')) return false;
                    const view = node.ownerDocument?.defaultView;
                    for (let current = node; current; current = current.parentElement) {
                        if (view?.getComputedStyle(current)?.pointerEvents === 'none') return false;
                    }
                    const text = normalize(node instanceof HTMLInputElement
                        ? node.value : (node.innerText || node.textContent));
                    return text.includes('点击加载更多') || text.includes('加载更多');
                });
                // 抖音实页有时把分页入口渲染在 listbox 的末尾，使用普通
                // div/span 并由 React 在上层统一代理 click，DOM 上没有
                // role/onclick。精确文案节点已经受当前唯一地点面板、唯一
                // listbox 和完整地点候选约束，因此允许它位于 listbox 内；
                // 普通“加载更多”仍只接受列表外的可交互控件。
                const candidates = Array.from(new Set([
                    ...interactiveCandidates,
                    ...listboxExactLoadMoreTextNodes,
                    ...exactLoadMoreTextNodes,
                ])).filter(node => panel.contains(node));
                const controls = candidates.filter(node => !candidates.some(other =>
                    other !== node && other.contains(node)));
                document.querySelectorAll('[data-oneclick-commerce-load-more="active"]')
                    .forEach(node => node.removeAttribute(
                        'data-oneclick-commerce-load-more'
                    ));
                if (controls.length !== 1) return { count: controls.length };
                controls[0].dataset.oneclickCommerceLoadMore = 'active';
                return { count: 1 };
            }"""
    )
    count = int(result.get("count") or 0) if isinstance(result, Mapping) else 0
    if count > 1:
        raise DouyinCommerceError("publish_location_load_more_failed")
    if count == 1:
        return page.locator('[data-oneclick-commerce-load-more="active"]')
    return None


def _commerce_location_candidate_identity(
    candidate: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    """返回地点候选的完整公开身份，保留同 POI 的返佣变体。"""

    return (
        _normalized(candidate.get("poiId")),
        _normalized(candidate.get("name")),
        _normalized(candidate.get("address")),
        _normalized(candidate.get("commissionType")),
    )


def _commerce_location_candidates_signature(rows: list[Mapping[str, Any]]) -> str:
    """将完整公开身份收敛为可比较的稳定快照。"""

    return "\n".join(
        "\u241f".join(_commerce_location_candidate_identity(row)) for row in rows
    )


def _admit_publish_location_candidates(
    rows: list[dict[str, Any]],
    *,
    seen_identities: set[tuple[str, str, str, str]],
    max_candidates: int,
) -> list[dict[str, Any]]:
    """按平台稳定顺序只接纳全流程候选上限内的地点。"""

    admitted: list[dict[str, Any]] = []
    for candidate in rows:
        identity = _commerce_location_candidate_identity(candidate)
        if identity not in seen_identities:
            if len(seen_identities) >= max_candidates:
                continue
            seen_identities.add(identity)
        admitted.append(candidate)
    return admitted


def _dedupe_public_commerce_location_candidates(
    rows: object,
    *,
    commission_filter: str,
) -> list[dict[str, Any]]:
    """按可见地点身份保留去重后的公开候选，不把 DOM 细节带出页面层。"""

    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        candidate = normalize_commerce_location_candidate(row)
        if candidate is None:
            continue
        filtered = filter_location_candidates([candidate], commission_filter)
        if not filtered:
            continue
        public_candidate = dict(filtered[0])
        candidate_identity = _commerce_location_candidate_identity(public_candidate)
        if not candidate_identity[0] or candidate_identity in seen:
            continue
        seen.add(candidate_identity)
        result.append(public_candidate)
    return result


async def _commerce_location_candidates_snapshot(
    page,
    *,
    commission_filter: str,
) -> tuple[int, list[dict[str, Any]]]:
    """读取当前可见地点的公开候选及平台有效候选数。"""

    listbox = await _visible_store_listbox(page)
    if listbox is None:
        return 0, []
    rows = await _store_option_descriptors(listbox)
    platform_candidates = _dedupe_public_commerce_location_candidates(
        rows,
        commission_filter="all",
    )
    candidates = _dedupe_public_commerce_location_candidates(
        rows,
        commission_filter=commission_filter,
    )
    return len(platform_candidates), candidates


async def _load_more_control_or_fail(page) -> Any | None:
    """把加载更多控件读取失败统一收敛为公开固定错误。"""

    try:
        return await _unique_visible_load_more_control(page)
    except Exception:
        raise DouyinCommerceError("publish_location_load_more_failed") from None


async def _load_more_candidates_snapshot_or_fail(
    page,
    *,
    commission_filter: str,
) -> tuple[int, list[dict[str, Any]]]:
    """把候选快照读取失败统一收敛为公开固定错误。"""

    try:
        return await _commerce_location_candidates_snapshot(
            page,
            commission_filter=commission_filter,
        )
    except Exception:
        raise DouyinCommerceError("publish_location_load_more_failed") from None


async def _wait_for_reappearing_load_more_control(
    page,
    *,
    deadline: float,
) -> bool:
    """候选增长后等待平台重建下一批入口。

    抖音会在加载期间短暂移除按钮；只读一次会把“暂时消失”
    误判成“已经到底”。这里只在已成功读到新候选后给一个有界重现窗口。
    """

    checks = max(
        1,
        _LOCATION_LOAD_MORE_REAPPEAR_GRACE_MS
        // _LOCATION_RESULT_POLL_INTERVAL_MS,
    )
    for index in range(checks):
        control = await _await_publish_location_dom_action(
            lambda _timeout: _load_more_control_or_fail(page),
            deadline=deadline,
        )
        if control is not None:
            return True
        if index + 1 < checks:
            await _wait_publish_location_timeout(
                page,
                _LOCATION_RESULT_POLL_INTERVAL_MS,
                deadline=deadline,
            )
    return False


async def _load_more_commerce_location_candidates_impl(
    page,
    *,
    previous_candidates: object,
    commission_filter: object = "all",
    timeout_ms: int = _LOCATION_RESULT_WAIT_TIMEOUT_MS,
    deadline: float | None = None,
) -> dict[str, object]:
    """单击唯一可见的地点“加载更多”，等候候选增长稳定后返回公开元数据。"""

    try:
        selected_filter = normalize_commission_filter(commission_filter, default="all")
        normalized_timeout_ms = max(_LOCATION_RESULT_POLL_INTERVAL_MS, int(timeout_ms))
        if deadline is None:
            deadline = monotonic() + normalized_timeout_ms / 1000
        normalized_timeout_ms = _publish_location_remaining_ms(
            deadline,
            normalized_timeout_ms,
        )
    except (TypeError, ValueError):
        raise DouyinCommerceError("publish_location_load_more_failed") from None

    previous = _dedupe_public_commerce_location_candidates(
        previous_candidates,
        commission_filter=selected_filter,
    )
    previous_identities = {
        _commerce_location_candidate_identity(candidate) for candidate in previous
    }
    control = await _await_publish_location_dom_action(
        lambda _timeout: _load_more_control_or_fail(page),
        deadline=deadline,
    )
    if control is None:
        platform_result_count, current = await _await_publish_location_dom_action(
            lambda _timeout: _load_more_candidates_snapshot_or_fail(
                page,
                commission_filter="all",
            ),
            deadline=deadline,
        )
        candidates = _dedupe_public_commerce_location_candidates(
            filter_location_candidates(current, selected_filter) + previous,
            commission_filter=selected_filter,
        )
        return {
            "platformResultCount": platform_result_count,
            "candidates": candidates,
            "newCandidateCount": 0,
            "hasMore": False,
            "stopReason": "no_visible_load_more_control",
            "clickPerformed": False,
        }
    baseline_platform_result_count, baseline_candidates = (
        await _await_publish_location_dom_action(
            lambda _timeout: _load_more_candidates_snapshot_or_fail(
                page,
                commission_filter="all",
            ),
            deadline=deadline,
        )
    )
    baseline_identities = {
        _commerce_location_candidate_identity(candidate) for candidate in baseline_candidates
    }
    known_identities = previous_identities | baseline_identities
    try:
        await _await_publish_location_dom_action(
            lambda action_timeout: control.scroll_into_view_if_needed(
                timeout=action_timeout
            ),
            deadline=deadline,
            timeout_ms=5_000,
        )
        await _await_publish_location_dom_action(
            lambda action_timeout: control.click(timeout=action_timeout),
            deadline=deadline,
            timeout_ms=5_000,
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError("publish_location_load_more_failed") from None
    except Exception:
        raise DouyinCommerceError("publish_location_load_more_failed") from None

    max_reads = max(
        _LOCATION_RESULT_STABLE_READS,
        normalized_timeout_ms // _LOCATION_RESULT_POLL_INTERVAL_MS + 1,
    )
    stable_signature = ""
    stable_reads = 0
    zero_growth_signature = ""
    zero_growth_stable_reads = 0
    platform_result_count = 0
    candidates: list[dict[str, Any]] = list(previous)
    new_identity_seen = False
    for read_index in range(max_reads):
        try:
            platform_result_count, current = await _await_publish_location_dom_action(
                lambda _timeout: _load_more_candidates_snapshot_or_fail(
                    page,
                    commission_filter="all",
                ),
                deadline=deadline,
            )
        except DouyinCommerceError as exc:
            exc.click_performed = True
            raise
        current_signature = _commerce_location_candidates_signature(current)
        current_identities = {
            _commerce_location_candidate_identity(candidate) for candidate in current
        }
        candidates = _dedupe_public_commerce_location_candidates(
            filter_location_candidates(current, selected_filter) + previous,
            commission_filter=selected_filter,
        )
        if not current_identities - known_identities:
            stable_signature = ""
            stable_reads = 0
            if platform_result_count > baseline_platform_result_count:
                if current_signature == zero_growth_signature:
                    zero_growth_stable_reads += 1
                else:
                    zero_growth_signature = current_signature
                    zero_growth_stable_reads = 1
                if zero_growth_stable_reads >= _LOCATION_RESULT_STABLE_READS:
                    try:
                        has_more = await _wait_for_reappearing_load_more_control(
                            page,
                            deadline=deadline,
                        )
                    except DouyinCommerceError as exc:
                        exc.click_performed = True
                        raise
                    return {
                        "platformResultCount": platform_result_count,
                        "candidates": candidates,
                        "newCandidateCount": 0,
                        "hasMore": has_more,
                        "stopReason": "no_new_candidates",
                        "clickPerformed": True,
                    }
            else:
                zero_growth_signature = ""
                zero_growth_stable_reads = 0
        elif current_signature == stable_signature:
            new_identity_seen = True
            stable_reads += 1
        else:
            new_identity_seen = True
            stable_signature = current_signature
            stable_reads = 1
        if new_identity_seen and stable_reads >= _LOCATION_RESULT_STABLE_READS:
            try:
                has_more = await _wait_for_reappearing_load_more_control(
                    page,
                    deadline=deadline,
                )
            except DouyinCommerceError as exc:
                exc.click_performed = True
                raise
            new_candidate_count = sum(
                _commerce_location_candidate_identity(candidate) not in previous_identities
                for candidate in candidates
            )
            return {
                "platformResultCount": platform_result_count,
                "candidates": candidates,
                "newCandidateCount": new_candidate_count,
                "hasMore": has_more,
                "stopReason": "loaded" if new_candidate_count else "no_new_candidates",
                "clickPerformed": True,
            }
        if read_index + 1 < max_reads:
            try:
                await _wait_publish_location_timeout(
                    page,
                    _LOCATION_RESULT_POLL_INTERVAL_MS,
                    deadline=deadline,
                )
            except DouyinCommerceError as exc:
                exc.click_performed = True
                raise
    if not new_identity_seen:
        try:
            has_more = await _wait_for_reappearing_load_more_control(
                page,
                deadline=deadline,
            )
        except DouyinCommerceError as exc:
            exc.click_performed = True
            raise
        if not has_more:
            return {
                "platformResultCount": platform_result_count,
                "candidates": candidates,
                "newCandidateCount": 0,
                "hasMore": False,
                "stopReason": "no_visible_load_more_control",
                "clickPerformed": True,
            }
    if deadline is not None:
        raise DouyinCommerceError(
            _PUBLISH_LOCATION_LIMIT_CODE,
            click_performed=True,
        ) from None
    raise DouyinCommerceError(
        "publish_location_load_more_failed",
        click_performed=True,
    )


async def load_more_commerce_location_candidates(
    page,
    *,
    previous_candidates: object,
    commission_filter: object = "all",
    timeout_ms: int = _LOCATION_RESULT_WAIT_TIMEOUT_MS,
    deadline: float | None = None,
) -> dict[str, object]:
    """公开加载更多入口：所有内部失败只暴露固定错误码。"""

    has_outer_deadline = deadline is not None
    try:
        return await _load_more_commerce_location_candidates_impl(
            page,
            previous_candidates=previous_candidates,
            commission_filter=commission_filter,
            timeout_ms=timeout_ms,
            deadline=deadline,
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE and has_outer_deadline:
            raise
        raise DouyinCommerceError(
            "publish_location_load_more_failed",
            click_performed=exc.click_performed,
        ) from None
    except Exception:
        raise DouyinCommerceError("publish_location_load_more_failed") from None


async def _owned_store_option_locator(listbox):
    """Mark one exact listbox and return all descendant option candidates."""

    await listbox.evaluate(
        """node => {
            document.querySelectorAll('[data-oneclick-commerce-owned-listbox]')
                .forEach(current => current.removeAttribute(
                    'data-oneclick-commerce-owned-listbox'
                ));
            node.dataset.oneclickCommerceOwnedListbox = 'active';
        }"""
    )
    return listbox.locator('[role="option"]')


async def _location_option_targets(
    listbox,
    location: Mapping[str, Any],
    *,
    commission_filter: object = "all",
    deadline: float | None = None,
) -> list[Any]:
    """返回名称、完整地址与返佣要求均精确匹配的可点击发布定位项。

    不以页面请求、隐藏属性或门店 ID 反推地点。列表中同名地点必须再用完整地址
    消歧；若仍不是唯一项，则由调用方安全停止。
    """

    selected_commission_filter = normalize_commission_filter(
        commission_filter,
        default="all",
    )
    expected_poi_id = _normalized(location.get("poiId"))
    expected_name = _normalized(location.get("name"))
    expected_address = _normalized(location.get("address"))
    expected_commission_type = _normalized(location.get("commissionType"))
    if (
        not expected_poi_id
        or not expected_name
        or not expected_address
        or not expected_commission_type
    ):
        return []
    options = await _await_publish_location_dom_action(
        lambda _timeout: _owned_store_option_locator(listbox),
        deadline=deadline,
    )
    targets: list[Any] = []
    option_count = await _await_publish_location_dom_action(
        lambda _timeout: options.count(),
        deadline=deadline,
    )
    for index in range(option_count):
        node = options.nth(index)
        try:
            if not await _await_publish_location_dom_action(
                lambda _timeout: node.is_visible(),
                deadline=deadline,
            ):
                continue
            actual = await _await_publish_location_dom_action(
                lambda _timeout: node.evaluate(
                    """node => {
"""
                + _STORE_EFFECTIVE_VISIBILITY_JS
                + """
                    if (!isEffectivelyVisible(node)) return null;
                    const owner = node.closest('[role="listbox"]');
                    if (!owner || owner.getAttribute(
                        'data-oneclick-commerce-owned-listbox'
                    ) !== 'active') return null;
                    const normalize = value => String(value || '')
                        .replace(/[\u200b\u00a0]/g, ' ').replace(/\\s+/g, ' ').trim();
                    const rawText = String(node.innerText || node.textContent || '')
                        .replace(/[\u200b\u00a0]/g, ' ');
                    const lines = rawText.split(/\\n+/).map(normalize).filter(Boolean);
                    const looksAddress = value => {
                        const text = normalize(value);
                        return text.length >= 6
                            && !/(?:商品|返佣|佣金|团购|套餐|券|专区)/.test(text)
                            && /(?:自治区|省|市|区|县|镇|乡|街|路|大道|巷|号|楼|村)/.test(text);
                    };
                    const looksCommerce = value => {
                        const text = normalize(value);
                        return /(?:^|[^\\d,，.+-])(?:\\d{1,3}(?:[,，]\\d{3})+|\\d+)(?![\\d,，.+-])\\s*件(?:商品|返佣)/.test(text)
                            || /(?:返佣|佣金)/.test(text);
                    };
                    const nameNode = node.querySelector('[data-store-name], [class*="name-"], [class*="name_"], [class*="title-"]');
                    const addressNode = node.querySelector('[data-store-address], [class*="address-"], [class*="address_"], [class*="addr"]');
                    const commerceNode = node.querySelector('[class*="cps-item"], [data-commerce-info], [class*="commission"], [class*="product"]');
                    const name = normalize(nameNode && (nameNode.innerText || nameNode.textContent)) || lines[0] || '';
                    const address = normalize(addressNode && (addressNode.innerText || addressNode.textContent))
                        || lines.find(line => looksAddress(line)) || '';
                    const isIdentityNode = candidate => Boolean(candidate && (
                        candidate === nameNode || candidate === addressNode
                        || nameNode?.contains(candidate) || addressNode?.contains(candidate)
                        || candidate.contains?.(nameNode) || candidate.contains?.(addressNode)
                    ));
                    const isVisibleCommerceNode = isEffectivelyVisible;
                    const visibleCommerceText = candidate => {
                        if (!isVisibleCommerceNode(candidate)) return '';
                        const clone = candidate.cloneNode(true);
                        const originals = Array.from(candidate.querySelectorAll('*'));
                        const clones = Array.from(clone.querySelectorAll('*'));
                        for (let index = originals.length - 1; index >= 0; index -= 1) {
                            if (!isVisibleCommerceNode(originals[index])) {
                                clones[index]?.remove();
                            }
                        }
                        return normalize(clone.textContent);
                    };
                    const commerceCandidates = [];
                    if (commerceNode && !isIdentityNode(commerceNode) && isVisibleCommerceNode(commerceNode)) {
                        commerceCandidates.push(commerceNode);
                    }
                    Array.from(node.querySelectorAll('*')).forEach(candidate => {
                        if (!isIdentityNode(candidate) && isVisibleCommerceNode(candidate)) {
                            commerceCandidates.push(candidate);
                        }
                    });
                    const commerceInfo = commerceCandidates
                        .map(visibleCommerceText)
                        .find(looksCommerce) || '';
                    return {
                        poiId: normalize(
                            node.getAttribute('data-poi-id')
                            || node.getAttribute('data-location-id')
                            || node.getAttribute('data-id')
                        ),
                        name,
                        address,
                        commerceInfo,
                    };
                }"""
                ),
                deadline=deadline,
            )
        except DouyinCommerceError as exc:
            if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
                raise
            continue
        except Exception:
            continue
        if not isinstance(actual, Mapping):
            continue
        normalized_actual = normalize_commerce_location_candidate(actual)
        if not normalized_actual or not filter_location_candidates(
            [normalized_actual],
            selected_commission_filter,
        ):
            continue
        if _commerce_location_candidate_identity(normalized_actual) == (
            expected_poi_id,
            expected_name,
            expected_address,
            expected_commission_type,
        ):
            targets.append(node)
    return targets


async def _apply_open_commerce_location_to_page(
    page,
    listbox,
    candidate: Mapping[str, Any],
    *,
    commission_filter: object = "all",
    deadline: float | None = None,
) -> dict[str, Any]:
    """从当前已打开的地点面板点击唯一候选并回读。"""

    selected_commission_filter = normalize_commission_filter(
        commission_filter,
        default="all",
    )
    normalized = normalize_commerce_location_candidate(candidate)
    if not normalized:
        raise DouyinCommerceError("publish_location_candidate_missing")
    location = {
        key: _normalized(normalized.get(key))
        for key in ("poiId", "name", "address", "distance", "commissionType")
    }
    visible_locations = normalize_commerce_location_candidates(
        await _await_publish_location_dom_action(
            lambda _timeout: _store_option_descriptors(listbox),
            deadline=deadline,
        ),
        commission_filter=selected_commission_filter,
    )
    matched_locations = [
        row
        for row in visible_locations
        if _normalized(row.get("poiId")) == location["poiId"]
        and _normalized(row.get("name")) == location["name"]
        and _normalized(row.get("address")) == location["address"]
        and _normalized(row.get("commissionType"))
        == _normalized(normalized.get("commissionType"))
    ]
    if len(matched_locations) != 1:
        code = (
            "publish_location_candidate_ambiguous"
            if len(matched_locations) > 1
            else "publish_location_candidate_missing"
        )
        raise DouyinCommerceError(code)
    targets = await _location_option_targets(
        listbox,
        location,
        commission_filter=selected_commission_filter,
        deadline=deadline,
    )
    if len(targets) != 1:
        raise DouyinCommerceError("publish_location_click_failed")
    try:
        _, _, before_mode_value, before_selected_name = (
            await _await_publish_location_dom_action(
                lambda _timeout: _anchor_controls(page),
                deadline=deadline,
            )
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        before_mode_value = ""
        before_selected_name = ""
    except Exception:
        before_mode_value = ""
        before_selected_name = ""
    try:
        await _await_publish_location_dom_action(
            lambda action_timeout: targets[0].scroll_into_view_if_needed(
                timeout=action_timeout
            ),
            deadline=deadline,
            timeout_ms=5_000,
        )
        await _await_publish_location_dom_action(
            lambda action_timeout: targets[0].click(timeout=action_timeout),
            deadline=deadline,
            timeout_ms=8_000,
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError("publish_location_click_failed") from None
    except Exception:
        raise DouyinCommerceError("publish_location_click_failed") from None
    try:
        await _wait_publish_location_timeout(page, 450, deadline=deadline)
        _, store_control, mode_value, selected_name = (
            await _await_publish_location_dom_action(
                lambda _timeout: _anchor_controls(page),
                deadline=deadline,
            )
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError("publish_location_readback_mismatch") from None
    except Exception:
        raise DouyinCommerceError("publish_location_readback_mismatch") from None
    if mode_value != _COMMERCE_MODE_TEXT or selected_name != location["name"]:
        raise DouyinCommerceError("publish_location_readback_mismatch")
    try:
        clicked_panel_closed = not await _await_publish_location_dom_action(
            lambda _timeout: listbox.is_visible(),
            deadline=deadline,
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        clicked_panel_closed = False
    except Exception:
        clicked_panel_closed = False
    if clicked_panel_closed:
        douyin_logger.info(
            "抖音发布定位已通过候选面板收口与控件目标值回读确认"
        )
        return {"location": dict(matched_locations[0])}
    try:
        verify_listbox = await _await_publish_location_dom_action(
            lambda _timeout: _open_store_selector(page, store_control),
            deadline=deadline,
        )
        verify_rows = await _await_publish_location_dom_action(
            lambda _timeout: _store_option_descriptors(verify_listbox),
            deadline=deadline,
        )
        selected_rows = [
            row
            for row in verify_rows
            if _normalized(row.get("selected")) == "true"
        ]
        reopened_locations = normalize_commerce_location_candidates(
            verify_rows,
            commission_filter=selected_commission_filter,
        )
        selected_locations = normalize_commerce_location_candidates(
            selected_rows,
            commission_filter=selected_commission_filter,
        )
    except DouyinCommerceError as exc:
        if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
            raise
        raise DouyinCommerceError("publish_location_readback_mismatch") from None
    except Exception:
        raise DouyinCommerceError("publish_location_readback_mismatch") from None
    verified = [
        row
        for row in selected_locations
        if _normalized(row.get("poiId")) == location["poiId"]
        and _normalized(row.get("name")) == location["name"]
        and _normalized(row.get("address")) == location["address"]
        and _normalized(row.get("commissionType"))
        == _normalized(normalized.get("commissionType"))
    ]
    if len(verified) == 1:
        return {"location": dict(verified[0])}

    reopened_verified = [
        row
        for row in reopened_locations
        if _normalized(row.get("poiId")) == location["poiId"]
        and _normalized(row.get("name")) == location["name"]
        and _normalized(row.get("address")) == location["address"]
        and _normalized(row.get("commissionType"))
        == _normalized(normalized.get("commissionType"))
    ]
    value_transition_proved = (
        len(selected_rows) == 0
        and before_mode_value == _COMMERCE_MODE_TEXT
        and before_selected_name != location["name"]
        and mode_value == _COMMERCE_MODE_TEXT
        and selected_name == location["name"]
        and len(reopened_verified) == 1
    )
    if value_transition_proved:
        douyin_logger.info(
            "抖音发布定位已通过点击前后值迁移与候选身份重读确认"
        )
        return {"location": dict(reopened_verified[0])}

    douyin_logger.warning(
        "抖音发布定位点击后严格回读不一致："
        f"选中候选数={len(selected_rows)}，"
        f"筛选后选中数={len(selected_locations)}，"
        f"身份匹配数={len(verified)}，"
        f"重读身份匹配数={len(reopened_verified)}，"
        f"值迁移证明={'是' if value_transition_proved else '否'}"
    )
    raise DouyinCommerceError("publish_location_readback_mismatch") from None


async def apply_commerce_location_to_page(
    page,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """只选择并回读发布定位，不读取或绑定团购门店。"""

    normalized = normalize_commerce_location_candidate(candidate)
    if not normalized:
        raise DouyinCommerceError("publish_location_candidate_missing")
    store_control = await _ensure_local_group_buy_mode(page)
    listbox = await _open_store_selector(page, store_control)
    return await _apply_open_commerce_location_to_page(page, listbox, normalized)


async def _close_commerce_store_selector_strict(
    page,
    *,
    deadline: float | None = None,
) -> None:
    """收口地点面板；失败时只暴露固定公开错误码。"""

    if deadline is None:
        deadline = monotonic() + _LOCATION_RESULT_WAIT_TIMEOUT_MS / 1000
    try:
        await close_commerce_store_selector(page, deadline=deadline)
    except Exception:
        raise DouyinCommerceError("publish_location_cleanup_incomplete") from None


async def apply_saved_commerce_location_to_page(
    page,
    preset: Mapping[str, Any],
    scope: object,
    keywords: list[str],
    commission_filter: object = "all",
    *,
    max_load_more_clicks: int = 10,
    max_candidates: int = 100,
) -> dict[str, Any]:
    """在同一地点面板中有界分页，命中完整身份后点击并回读。"""

    selected_scope = normalize_commerce_location_scope(scope)
    selected_commission_filter = normalize_commission_filter(
        commission_filter,
        default="all",
    )
    if (
        type(max_load_more_clicks) is not int
        or max_load_more_clicks < 1
        or type(max_candidates) is not int
        or max_candidates < 1
    ):
        raise DouyinCommerceError("publish_location_load_more_limit") from None
    bounded_keywords = list(
        dict.fromkeys(
            normalized
            for value in keywords
            if (normalized := _normalized(value))
        )
    )[:3]
    if not bounded_keywords:
        raise DouyinCommerceError("publish_location_candidate_missing")

    scope_label = location_scope_label(selected_scope)
    expected = normalize_commerce_location_candidate(preset) or {}
    observed_commission_type = _normalized(
        preset.get("commissionType") or preset.get("observedCommissionType")
    )
    if observed_commission_type not in {
        "commission",
        "no_commission",
        "unknown",
    }:
        observed_commission_type = ""
    if observed_commission_type == "unknown" and selected_commission_filter != "all":
        observed_commission_type = selected_commission_filter
    if observed_commission_type:
        expected["commissionType"] = observed_commission_type
    expected_identity = _commerce_location_candidate_identity(expected)
    if not all(expected_identity):
        raise DouyinCommerceError("publish_location_candidate_missing") from None
    target_text = (
        f"{_normalized(expected.get('name'))} · {_normalized(expected.get('address'))}"
    ).strip(" ·")
    any_search_succeeded = False
    total_click_count = 0
    seen_candidate_identities: set[tuple[str, str, str, str]] = set()
    commission_mismatch_seen = False

    for attempt, keyword in enumerate(bounded_keywords, start=1):
        douyin_logger.info(
            f"抖音发布定位第 {attempt}/{len(bounded_keywords)} 次搜索："
            f"范围={scope_label}，关键词={keyword}，目标={target_text}"
        )
        pre_cleanup_deadline = (
            monotonic() + _LOCATION_RESULT_WAIT_TIMEOUT_MS / 1000
        )
        await _close_commerce_store_selector_strict(
            page,
            deadline=pre_cleanup_deadline,
        )
        panel_may_be_open = False
        active_error: BaseException | None = None
        try:
            panel_may_be_open = True
            try:
                search_deadline = (
                    monotonic() + _LOCATION_RESULT_WAIT_TIMEOUT_MS / 1000
                )
                candidates = await search_commerce_location_store_candidates(
                    page,
                    keyword,
                    scope=selected_scope,
                    commission_filter="all",
                    timeout_ms=_LOCATION_RESULT_WAIT_TIMEOUT_MS,
                    deadline=search_deadline,
                )
            except DouyinCommerceError as exc:
                if str(exc) in {
                    "publish_location_commission_mismatch",
                }:
                    raise
                if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
                    raise _location_failure(
                        "publish_location_action_timeout",
                        stage="search",
                        keyword=keyword,
                        scope=selected_scope,
                        clicks=total_click_count,
                        candidates=len(seen_candidate_identities),
                        max_candidates=max_candidates,
                        max_load_more_clicks=max_load_more_clicks,
                    ) from None
                douyin_logger.warning(
                    f"抖音发布定位关键词“{keyword}”搜索失败："
                    f"{_normalized(str(exc))[:220] or type(exc).__name__}；"
                    "将尝试下一关键词"
                )
                continue
            any_search_succeeded = True
            candidates = [
                candidate
                for row in candidates
                if (
                    candidate := normalize_commerce_location_candidate(row)
                ) is not None
            ]
            candidate_summary = [
                (
                    f"{_normalized(row.get('name'))} · "
                    f"{_normalized(row.get('address'))}"
                ).strip(" ·")
                for row in candidates[:5]
                if isinstance(row, Mapping)
            ]
            douyin_logger.info(
                f"抖音发布定位关键词“{keyword}”返回 {len(candidates)} 个完整候选："
                f"{candidate_summary or ['无']}"
            )
            candidates = _admit_publish_location_candidates(
                candidates,
                seen_identities=seen_candidate_identities,
                max_candidates=max_candidates,
            )
            exhausted = False
            matched_candidates: list[dict[str, Any]] = []
            while True:
                matched_candidates = [
                    row
                    for row in candidates
                    if _commerce_location_candidate_identity(row)
                    == expected_identity
                ]
                if len(matched_candidates) > 1:
                    raise DouyinCommerceError(
                        "publish_location_candidate_ambiguous"
                    ) from None
                if len(matched_candidates) == 1:
                    break
                commission_mismatch_seen = commission_mismatch_seen or any(
                    _commerce_location_candidate_identity(row)[:3]
                    == expected_identity[:3]
                    and _commerce_location_candidate_identity(row)[3]
                    != expected_identity[3]
                    for row in candidates
                )
                if len(seen_candidate_identities) >= max_candidates:
                    raise _location_failure(
                        "publish_location_candidate_limit",
                        stage="load_more",
                        keyword=keyword,
                        scope=selected_scope,
                        clicks=total_click_count,
                        candidates=len(seen_candidate_identities),
                        max_candidates=max_candidates,
                        max_load_more_clicks=max_load_more_clicks,
                    ) from None
                if total_click_count >= max_load_more_clicks:
                    raise _location_failure(
                        "publish_location_click_limit",
                        stage="load_more",
                        keyword=keyword,
                        scope=selected_scope,
                        clicks=total_click_count,
                        candidates=len(seen_candidate_identities),
                        max_candidates=max_candidates,
                        max_load_more_clicks=max_load_more_clicks,
                    ) from None
                if exhausted:
                    break
                try:
                    load_deadline = (
                        monotonic() + _LOCATION_RESULT_WAIT_TIMEOUT_MS / 1000
                    )
                    load_result = await load_more_commerce_location_candidates(
                        page,
                        previous_candidates=candidates,
                        commission_filter="all",
                        timeout_ms=_LOCATION_RESULT_WAIT_TIMEOUT_MS,
                        deadline=load_deadline,
                    )
                    raw_candidates = load_result.get("candidates")
                    new_candidate_count = load_result.get("newCandidateCount")
                    has_more = load_result.get("hasMore")
                    click_performed = load_result.get("clickPerformed")
                    if type(click_performed) is not bool:
                        click_performed = (
                            load_result.get("stopReason")
                            != "no_visible_load_more_control"
                        )
                    if (
                        not isinstance(raw_candidates, list)
                        or type(new_candidate_count) is not int
                        or new_candidate_count < 0
                        or type(has_more) is not bool
                    ):
                        raise TypeError("publish_location_load_more_failed")
                except DouyinCommerceError as exc:
                    if getattr(exc, "click_performed", False) is True:
                        total_click_count += 1
                    if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
                        raise _location_failure(
                            "publish_location_action_timeout",
                            stage="load_more",
                            keyword=keyword,
                            scope=selected_scope,
                            clicks=total_click_count,
                            candidates=len(seen_candidate_identities),
                            max_candidates=max_candidates,
                            max_load_more_clicks=max_load_more_clicks,
                        ) from None
                    raise DouyinCommerceError(
                        "publish_location_load_more_failed"
                    ) from None
                except Exception:
                    raise DouyinCommerceError(
                        "publish_location_load_more_failed"
                    ) from None
                if click_performed:
                    total_click_count += 1
                candidates = [
                    candidate
                    for row in raw_candidates
                    if (
                        candidate := normalize_commerce_location_candidate(row)
                    ) is not None
                ]
                candidates = _admit_publish_location_candidates(
                    candidates,
                    seen_identities=seen_candidate_identities,
                    max_candidates=max_candidates,
                )
                douyin_logger.info(
                    f"抖音发布定位关键词“{keyword}”累计加载第 {total_click_count} 次："
                    f"全关键词累计有效候选={len(seen_candidate_identities)}，"
                    f"本轮新增={new_candidate_count}"
                )
                exhausted = not has_more
            if not matched_candidates:
                douyin_logger.warning(
                    f"抖音发布定位关键词“{keyword}”已读完可用批次，"
                    f"累计 {len(candidates)} 个候选均未匹配目标“{target_text}”；"
                    "将尝试下一关键词"
                )
                continue
            matched_candidate = dict(matched_candidates[0])
            readback_deadline = (
                monotonic() + _LOCATION_RESULT_WAIT_TIMEOUT_MS / 1000
            )
            try:
                listbox = await _await_publish_location_dom_action(
                    lambda _timeout: _visible_store_listbox(page),
                    deadline=readback_deadline,
                )
            except DouyinCommerceError as exc:
                if str(exc) != _PUBLISH_LOCATION_LIMIT_CODE:
                    raise
                raise _location_failure(
                    "publish_location_action_timeout",
                    stage="readback",
                    keyword=keyword,
                    scope=selected_scope,
                    clicks=total_click_count,
                    candidates=len(seen_candidate_identities),
                    max_candidates=max_candidates,
                    max_load_more_clicks=max_load_more_clicks,
                ) from None
            if listbox is None:
                douyin_logger.warning(
                    f"抖音发布定位关键词“{keyword}”已匹配目标，但候选面板已消失"
                )
                raise DouyinCommerceError("publish_location_click_failed")
            try:
                if selected_commission_filter == "all":
                    result = await _apply_open_commerce_location_to_page(
                        page,
                        listbox,
                        matched_candidate,
                        deadline=readback_deadline,
                    )
                else:
                    result = await _apply_open_commerce_location_to_page(
                        page,
                        listbox,
                        matched_candidate,
                        commission_filter=selected_commission_filter,
                        deadline=readback_deadline,
                    )
            except DouyinCommerceError as exc:
                if str(exc) != _PUBLISH_LOCATION_LIMIT_CODE:
                    raise
                raise _location_failure(
                    "publish_location_action_timeout",
                    stage="readback",
                    keyword=keyword,
                    scope=selected_scope,
                    clicks=total_click_count,
                    candidates=len(seen_candidate_identities),
                    max_candidates=max_candidates,
                    max_load_more_clicks=max_load_more_clicks,
                ) from None
            douyin_logger.success(
                f"抖音发布定位关键词“{keyword}”已命中并回读目标：{target_text}"
            )
            return {**result, "matchedKeyword": keyword}
        except DouyinCommerceError as exc:
            active_error = exc
            raise
        except Exception:
            public_error = DouyinCommerceError("publish_location_click_failed")
            active_error = public_error
            raise public_error from None
        except BaseException as exc:
            active_error = exc
            raise
        finally:
            if panel_may_be_open:
                try:
                    final_cleanup_deadline = (
                        monotonic() + _LOCATION_RESULT_WAIT_TIMEOUT_MS / 1000
                    )
                    await _close_commerce_store_selector_strict(
                        page,
                        deadline=final_cleanup_deadline,
                    )
                except Exception:
                    if active_error is None:
                        raise
                    douyin_logger.warning(
                        "抖音发布定位原失败处理期间面板清理失败："
                        "publish_location_cleanup_incomplete"
                    )

    douyin_logger.warning(
        f"抖音发布定位恢复结束：范围={scope_label}，目标={target_text}，"
        f"全部关键词均未返回唯一匹配，已尝试={bounded_keywords}"
    )
    if commission_mismatch_seen:
        raise DouyinCommerceError(
            "publish_location_commission_mismatch"
        ) from None
    if any_search_succeeded:
        raise _location_failure(
            "publish_location_not_found_after_all_pages",
            stage="all_pages",
            keyword=bounded_keywords[-1],
            scope=selected_scope,
            clicks=total_click_count,
            candidates=len(seen_candidate_identities),
            max_candidates=max_candidates,
            max_load_more_clicks=max_load_more_clicks,
        ) from None
    raise DouyinCommerceError("publish_location_candidate_missing") from None


async def apply_commerce_location_store_to_page(
    page,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """选择一个联合候选，并同时回读位置、带货模式与门店身份。"""

    normalized = normalize_commerce_location_store_candidate(candidate)
    if not normalized:
        raise DouyinCommerceError("待选择的位置/门店候选缺少完整地址或稳定身份")
    location = {
        key: _normalized(normalized.get(key))
        for key in ("poiId", "name", "address", "distance")
    }
    store = normalized.get("commerceStore")
    if not isinstance(store, Mapping):
        raise DouyinCommerceError("待选择的位置没有可绑定门店身份")
    selected_store = await apply_commerce_store_to_page(page, store, location)
    if (
        _normalized(selected_store.get("name")) != location["name"]
        or _normalized(selected_store.get("address")) != location["address"]
        or _normalized(selected_store.get("poiId")) != location["poiId"]
    ):
        raise DouyinCommerceError("抖音位置与绑定门店回读不一致，已安全停止")
    return {"location": location, "commerceStore": dict(selected_store)}


async def close_commerce_store_selector(
    page,
    *,
    deadline: float | None = None,
) -> None:
    """关闭当前地点候选浮层，不选择地点或门店。"""

    if deadline is None:
        deadline = monotonic() + _LOCATION_RESULT_WAIT_TIMEOUT_MS / 1000
    try:
        input_control = await _await_publish_location_dom_action(
            lambda _timeout: _visible_commerce_search_input(page),
            deadline=deadline,
        )
    except DouyinCommerceError as exc:
        if "实际 0 个" not in str(exc):
            raise
        input_control = None
    if input_control is not None:
        overlay = await _await_publish_location_dom_action(
            lambda _timeout: _visible_commerce_location_overlay(page),
            deadline=deadline,
        )
        if overlay is not None:
            try:
                await _await_publish_location_dom_action(
                    lambda action_timeout: input_control.press(
                        "Escape",
                        timeout=action_timeout,
                    ),
                    deadline=deadline,
                    timeout_ms=5_000,
                )
            except DouyinCommerceError as exc:
                if str(exc) == _PUBLISH_LOCATION_LIMIT_CODE:
                    raise
                raise DouyinCommerceError(
                    "抖音地点候选浮层无法安全关闭，已停止后续设置"
                ) from exc
            except Exception as exc:
                raise DouyinCommerceError(
                    "抖音地点候选浮层无法安全关闭，已停止后续设置"
                ) from exc
            for _ in range(10):
                current_overlay = await _await_publish_location_dom_action(
                    lambda _timeout: _visible_commerce_location_overlay(page),
                    deadline=deadline,
                )
                if current_overlay is None:
                    return
                await _wait_publish_location_timeout(
                    page,
                    150,
                    deadline=deadline,
                )
            raise DouyinCommerceError(
                "抖音地点候选浮层无法安全关闭，已停止后续设置"
            )

    listbox = await _await_publish_location_dom_action(
        lambda _timeout: _visible_store_listbox(page),
        deadline=deadline,
    )
    if listbox is None:
        return
    _, store_control, _, _ = await _await_publish_location_dom_action(
        lambda _timeout: _anchor_controls(page),
        deadline=deadline,
    )
    await _await_publish_location_dom_action(
        lambda _timeout: _open_exact_select(page, store_control, "可绑定门店"),
        deadline=deadline,
    )
    for _ in range(10):
        current_listbox = await _await_publish_location_dom_action(
            lambda _timeout: _visible_store_listbox(page),
            deadline=deadline,
        )
        if current_listbox is None:
            return
        await _wait_publish_location_timeout(
            page,
            150,
            deadline=deadline,
        )
    raise DouyinCommerceError("抖音带货门店下拉无法安全关闭，已停止后续设置")


async def _store_option_descriptors(listbox) -> list[dict[str, str]]:
    """只抽取页面上可见的门店标识、名称和地址，不保存原始 DOM。"""

    rows: list[dict[str, str]] = []
    locator = await _owned_store_option_locator(listbox)
    for index in range(await locator.count()):
        node = locator.nth(index)
        try:
            if not await node.is_visible():
                continue
            descriptor = await node.evaluate(
                """node => {
"""
                + _STORE_EFFECTIVE_VISIBILITY_JS
                + """
                    if (!isEffectivelyVisible(node)) return null;
                    const owner = node.closest('[role="listbox"]');
                    if (!owner || owner.getAttribute(
                        'data-oneclick-commerce-owned-listbox'
                    ) !== 'active') return null;
                    const normalize = value => String(value || '').replace(/\\u200b/g, ' ').replace(/\\s+/g, ' ').trim();
                    const attr = name => normalize(node.getAttribute(name));
                    const rawText = String(node.innerText || node.textContent || '')
                        .replace(/[\u200b\u00a0]/g, ' ');
                    const text = normalize(rawText);
                    const lines = rawText.split(/\\n+/).map(normalize).filter(Boolean);
                    const looksAddress = value => {
                        const text = normalize(value);
                        return text.length >= 6
                            && !/(?:商品|返佣|佣金|团购|套餐|券|专区)/.test(text)
                            && /(?:自治区|省|市|区|县|镇|乡|街|路|大道|巷|号|楼|村)/.test(text);
                    };
                    const looksCommerce = value => {
                        const text = normalize(value);
                        return /(?:^|[^\\d,，.+-])(?:\\d{1,3}(?:[,，]\\d{3})+|\\d+)(?![\\d,，.+-])\\s*件(?:商品|返佣)/.test(text)
                            || /(?:返佣|佣金)/.test(text);
                    };
                    const nameNode = node.querySelector('[data-store-name], [class*="name-"], [class*="name_"], [class*="title-"]');
                    const addressNode = node.querySelector('[data-store-address], [class*="address-"], [class*="address_"], [class*="addr"]');
                    const commerceNode = node.querySelector('[class*="cps-item"], [data-commerce-info], [class*="commission"], [class*="product"]');
                    const name = normalize(nameNode && (nameNode.innerText || nameNode.textContent)) || lines[0] || '';
                    const address = normalize(addressNode && (addressNode.innerText || addressNode.textContent))
                        || lines.find(line => looksAddress(line)) || '';
                    const isIdentityNode = candidate => Boolean(candidate && (
                        candidate === nameNode || candidate === addressNode
                        || nameNode?.contains(candidate) || addressNode?.contains(candidate)
                        || candidate.contains?.(nameNode) || candidate.contains?.(addressNode)
                    ));
                    const isVisibleCommerceNode = isEffectivelyVisible;
                    const visibleCommerceText = candidate => {
                        if (!isVisibleCommerceNode(candidate)) return '';
                        const clone = candidate.cloneNode(true);
                        const originals = Array.from(candidate.querySelectorAll('*'));
                        const clones = Array.from(clone.querySelectorAll('*'));
                        for (let index = originals.length - 1; index >= 0; index -= 1) {
                            if (!isVisibleCommerceNode(originals[index])) {
                                clones[index]?.remove();
                            }
                        }
                        return normalize(clone.textContent);
                    };
                    const commerceCandidates = [];
                    if (commerceNode && !isIdentityNode(commerceNode) && isVisibleCommerceNode(commerceNode)) {
                        commerceCandidates.push(commerceNode);
                    }
                    Array.from(node.querySelectorAll('*')).forEach(candidate => {
                        if (!isIdentityNode(candidate) && isVisibleCommerceNode(candidate)) {
                            commerceCandidates.push(candidate);
                        }
                    });
                    const commerceInfo = commerceCandidates
                        .map(visibleCommerceText)
                        .find(looksCommerce) || '';
                    const ariaSelected = node.getAttribute('aria-selected');
                    const selectionClassTokens = Array.from(node.classList || [])
                        .filter(token => /selected|chosen/i.test(token));
                    const explicitSelectedClass = selectionClassTokens.length > 0
                        && selectionClassTokens.every(
                            token => /^(?:selected|chosen)$/i.test(token)
                        );
                    const explicitSemiSelectedClass = Array.from(node.classList || [])
                        .some(token => token === 'semi-select-option-selected');
                    const selected = ariaSelected === 'true'
                        || (ariaSelected === null
                            && (explicitSelectedClass || explicitSemiSelectedClass));
                    return {
                        storeId: attr('data-store-id') || attr('data-shop-id') || attr('data-id'),
                        poiId: attr('data-poi-id') || attr('data-location-id') || attr('data-id'),
                        name,
                        address,
                        commerceInfo,
                        selected: selected ? 'true' : 'false',
                        text,
                    };
                }"""
            )
        except Exception:
            continue
        if not isinstance(descriptor, dict):
            continue
        store_id = _normalized(descriptor.get("storeId"))
        visible_identity = _visible_store_identity(
            _normalized(descriptor.get("name")),
            _normalized(descriptor.get("address")),
        )
        # 每个可见 option 都是当前平台面板的独立证据。没有可证明的
        # 虚拟列表克隆规则时绝不在 DOM 层去重；返佣筛选后仍重复由
        # 归一化边界以明确错误安全停止。
        descriptor["storeId"] = store_id or visible_identity
        rows.append({str(key): _normalized(value) for key, value in descriptor.items()})
    return rows


async def read_commerce_store_candidates(page, location_poi: Mapping[str, Any]) -> list[dict[str, str]]:
    """在已打开的官方编辑页读取可绑定门店，缺少唯一控件即安全停止。"""

    _ = normalize_location_candidate(location_poi)
    if not _:
        raise DouyinCommerceError("读取带货门店前必须先选择官方发布定位")
    store_control = await _ensure_local_group_buy_mode(page)
    listbox = await _open_store_selector(page, store_control)
    raw_rows = await _store_option_descriptors(listbox)
    linked_rows = []
    for row in raw_rows:
        if not _is_location_linked_store(row, location_poi):
            continue
        linked = dict(row)
        linked["poiId"] = _normalized(location_poi.get("poiId") or location_poi.get("poi_id"))
        linked_rows.append(linked)
    candidates = normalize_commerce_store_candidates(
        linked_rows,
        location_poi=location_poi,
    )
    if not candidates:
        raise DouyinCommerceError(
            "抖音当前账号或地点没有可唯一回读的团购门店，已停止绑定"
        )
    return candidates


async def apply_commerce_store_to_page(
    page,
    store: Mapping[str, Any],
    location_poi: Mapping[str, Any],
) -> dict[str, str]:
    """开启带货模式、唯一选择已指定门店并由页面回读确认。"""

    normalized_store = normalize_commerce_store(store, location_poi=location_poi)
    if not normalized_store:
        raise DouyinCommerceError("待绑定门店缺少稳定身份或与发布定位不一致")
    store_control = await _ensure_local_group_buy_mode(page)
    listbox = await _open_store_selector(page, store_control)
    raw_rows = await _store_option_descriptors(listbox)
    linked_rows = []
    for row in raw_rows:
        if _is_location_linked_store(row, location_poi):
            linked = dict(row)
            linked["poiId"] = _normalized(location_poi.get("poiId") or location_poi.get("poi_id"))
            linked_rows.append(linked)
    candidates = normalize_commerce_store_candidates(linked_rows, location_poi=location_poi)
    matched = [row for row in candidates if row["storeId"] == normalized_store["storeId"]]
    if len(matched) != 1:
        raise DouyinCommerceError("抖音页面未找到唯一匹配的目标门店，已安全停止")

    store_id = normalized_store["storeId"]
    selected = [row for row in raw_rows if _normalized(row.get("selected")) == "true"]
    selected_linked = [
        row
        for row in selected
        if _is_location_linked_store(row, location_poi)
        and _normalized(row.get("storeId")) == store_id
    ]
    if len(selected_linked) == 1:
        return dict(normalized_store)

    options = await _owned_store_option_locator(listbox)
    targets: list[Any] = []
    for index in range(await options.count()):
        node = options.nth(index)
        try:
            if not await node.is_visible():
                continue
            actual = await node.evaluate(
                """node => {
                    const owner = node.closest('[role="listbox"]');
                    if (!owner || owner.getAttribute(
                        'data-oneclick-commerce-owned-listbox'
                    ) !== 'active') return null;
                    const normalize = value => String(value || '').replace(/\\u200b/g, ' ').replace(/\\s+/g, ' ').trim();
                    const rawText = String(node.innerText || node.textContent || '').replace(/[\\u200b\\u00a0]/g, ' ');
                    const lines = rawText.split(/\\n+/).map(normalize).filter(Boolean);
                    const looksAddress = value => {
                        const text = normalize(value);
                        return text.length >= 6
                            && !/(?:商品|返佣|佣金|团购|套餐|券|专区)/.test(text)
                            && /(?:自治区|省|市|区|县|镇|乡|街|路|大道|巷|号|楼|村)/.test(text);
                    };
                    const nameNode = node.querySelector('[data-store-name], [class*="name-"], [class*="name_"], [class*="title-"]');
                    const addressNode = node.querySelector('[data-store-address], [class*="address-"], [class*="address_"], [class*="addr"]');
                    const name = normalize(nameNode?.innerText || nameNode?.textContent) || lines[0] || '';
                    const address = normalize(addressNode?.innerText || addressNode?.textContent)
                        || lines.find(line => looksAddress(line)) || '';
                    return { name, address, direct: normalize(node.getAttribute('data-store-id') || node.getAttribute('data-shop-id') || node.getAttribute('data-id')) };
                }"""
            )
        except Exception:
            continue
        if not isinstance(actual, Mapping):
            continue
        actual_id = _normalized(actual.get("direct")) or _visible_store_identity(
            _normalized(actual.get("name")), _normalized(actual.get("address"))
        )
        if actual_id == store_id:
            targets.append(node)
    if len(targets) != 1:
        raise DouyinCommerceError("目标门店在当前页面不是唯一可点击项，已安全停止")
    await targets[0].scroll_into_view_if_needed(timeout=5_000)
    await targets[0].click(timeout=8_000)
    await page.wait_for_timeout(450)
    _, store_control, mode_value, selected_name = await _anchor_controls(page)
    if mode_value != _COMMERCE_MODE_TEXT or selected_name != normalized_store["name"]:
        raise DouyinCommerceError("抖音门店选择后未能回读带货模式和门店名称，已安全停止")
    verify_listbox = await _open_store_selector(page, store_control)
    verified = await _store_option_descriptors(verify_listbox)
    selected_verified = [
        row
        for row in verified
        if _normalized(row.get("selected")) == "true"
        and _is_location_linked_store(row, location_poi)
        and _normalized(row.get("storeId")) == store_id
    ]
    if len(selected_verified) != 1:
        raise DouyinCommerceError("抖音门店选择后未能回读稳定门店身份，已安全停止")
    return dict(normalized_store)
