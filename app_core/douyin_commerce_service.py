# -*- coding: utf-8 -*-
"""抖音本地团购带货的本地契约与受控页面适配。

本模块不调用官方 API、签名接口或第三方服务。抖音本地团购的新版编辑器把
“位置 → 带货模式 → 地点输入”做成同一条可见交互；一键发将发布定位与作品
内容声明作为独立、可回读的步骤。候选及声明只从一键发受控浏览器内的可见页面
读取；任何控件、候选或回读不唯一时都会中止，而不会猜测点击。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

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


class DouyinCommerceError(RuntimeError):
    """抖音带货流程不能安全继续时抛出。"""


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


def normalize_commerce_location_candidate(value: object) -> dict[str, str] | None:
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
    location = normalize_location_candidate(
        {
            "poiId": _visible_location_identity(name, address),
            "name": name,
            "address": address,
            "distance": "",
        }
    )
    if not location:
        return None
    return {
        **location,
        "source": "douyin-visible-commerce-location",
    }


def normalize_commerce_location_candidates(rows: object) -> list[dict[str, Any]]:
    """去重并保留新版编辑页可见的发布定位候选。

    即使抖音结果项同时展示商品/返佣信息，也不把它提取为门店候选或任务字段。
    首版只验证发布定位；门店绑定已从一键发流程中移除。
    """

    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    seen_locations: set[str] = set()
    for row in rows:
        location = normalize_commerce_location_candidate(row)
        if not location:
            continue
        location_id = _normalized(location.get("poiId"))
        if location_id in seen_locations:
            raise DouyinCommerceError("抖音发布定位候选出现重复完整地址，无法安全选择")
        seen_locations.add(location_id)
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
    seen_locations: set[str] = set()
    seen_stores: set[str] = set()
    for row in rows:
        candidate = normalize_commerce_location_store_candidate(row)
        if not candidate:
            continue
        location = candidate
        store = candidate["commerceStore"]
        location_id = _normalized(location.get("poiId"))
        store_id = _normalized(store.get("storeId"))
        if location_id in seen_locations or store_id in seen_stores:
            raise DouyinCommerceError("抖音带货候选出现重复可见身份，无法安全选择")
        seen_locations.add(location_id)
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
        _normalized(item) for item in checked.get("fileList") or [] if _normalized(item)
    ]
    if len(files) != 1 or not Path(files[0]).is_file():
        raise DouyinCommerceError("抖音带货需要且只允许一条可读取的视频素材")
    checked["fileList"] = files

    if not _normalized(checked.get("description")):
        raise DouyinCommerceError(f"{action}缺少作品描述")
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
        _normalized(item)
        for item in checked.get("fileList") or []
        if _normalized(item)
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
            const controls = candidates;
            if (controls.length !== 1) return { count: controls.length };
            controls[0].dataset.oneclickCommercePositionTag = 'active';
            return { count: 1 };
        }"""
    )
    if isinstance(result, Mapping) and int(result.get("count") or 0) == 1:
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


async def _wait_position_tag_option(page) -> Any:
    """等待唯一可见的“位置”标签选项；只选择这个精确文案。"""

    for _ in range(20):
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
                const options = Array.from(document.querySelectorAll('[role="option"]'))
                    .filter(visible)
                    .filter(node => normalize(node.innerText || node.textContent) === '位置');
                if (options.length !== 1) return { count: options.length };
                options[0].dataset.oneclickCommercePositionOption = 'active';
                return { count: 1 };
            }"""
        )
        if isinstance(result, Mapping) and int(result.get("count") or 0) == 1:
            return page.locator('[data-oneclick-commerce-position-option="active"]')
        await page.wait_for_timeout(200)
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
    position_option = await _wait_position_tag_option(page)
    await position_option.click(timeout=8_000)
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
                const visible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
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
                const lists = Array.from(document.querySelectorAll('[role="listbox"]'))
                    .filter(visible)
                    .filter(list => {
                        const options = Array.from(list.querySelectorAll(':scope > [role="option"]'))
                            .filter(visible);
                        return options.length > 0 && options.some(option => {
                            const row = descriptor(option);
                            return row.name && row.address && looksAddress(row.address);
                        });
                    });
                if (lists.length !== 1) return { count: lists.length };
                lists[0].dataset.oneclickCommerceStoreList = 'active';
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
                .filter(list => {
                    const options = Array.from(
                        list.querySelectorAll(':scope > [role="option"]')
                    ).filter(visible);
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

    return "\n".join(
        "\u241f".join(
            (
                _normalized(row.get("name")),
                _normalized(row.get("address")),
                _normalized(row.get("distance")),
            )
        )
        for row in rows
        if _normalized(row.get("name")) or _normalized(row.get("address"))
    )


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
) -> tuple[Any, list[dict[str, str]]]:
    """等待本次关键词对应的候选结果，过滤范围切换前的陈旧下拉。

    ``allow_stable_baseline_match`` 仅供已经完成“清空再填回关键词”的范围
    切换流程使用。抖音会在范围切换时自动查询一次原关键词，此时最终的本地
    结果可能与清空后的快照完全相同；连续两次回读一致且匹配关键词时，可将其
    视为已稳定的当前范围结果，而不是误报为旧候选。
    """

    last_signature = ""
    stable_matching_baseline_reads = 0
    for _ in range(30):
        listbox, rows, signature = await _visible_commerce_location_result_snapshot(page)
        if listbox is not None and rows:
            last_signature = signature
            matches_keyword = _location_rows_match_keyword(rows, keyword)
            # 新结果既应替换切换范围前的列表，也应至少与本次搜索词有关。
            # 若平台暂时仍返回旧本地候选，继续等待而不把错误地址展示给用户。
            if signature != baseline_signature and matches_keyword:
                return listbox, rows
            if (
                allow_stable_baseline_match
                and signature == baseline_signature
                and matches_keyword
            ):
                stable_matching_baseline_reads += 1
                if stable_matching_baseline_reads >= 2:
                    return listbox, rows
            else:
                stable_matching_baseline_reads = 0
        await page.wait_for_timeout(200)
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
) -> list[dict[str, Any]]:
    """按用户选择的“本地/国内”范围读取发布定位候选。

    此步骤只会展开“添加标签 → 位置 → 带货模式”后对应的输入框，填入关键词
    并读取结果；不会选择结果、绑定门店、保存草稿或提交发布。
    """

    normalized_keyword = normalize_location_keyword(keyword)
    await _ensure_position_tag(page)
    store_control = await _ensure_local_group_buy_mode(page)
    input_control = await _open_commerce_search_input(page, store_control)
    try:
        await input_control.scroll_into_view_if_needed(timeout=5_000)
        await input_control.click(timeout=5_000)
    except Exception as exc:
        raise DouyinCommerceError("抖音带货位置输入框无法安全打开，已停止") from exc
    # 抖音会在关键词输入后立即按当前范围请求候选；必须先把页面实际范围切成
    # 用户所选“本地/国内”，再填写关键词，避免客户端默认“国内”但平台仍以
    # 初始“本地”返回候选。
    try:
        await set_commerce_location_scope(page, scope)
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
            await input_control.fill("", timeout=8_000)
            await page.wait_for_timeout(120)
            await input_control.click(timeout=5_000)
            await input_control.fill(normalized_keyword, timeout=8_000)
        except Exception as fill_exc:
            raise DouyinCommerceError(
                "抖音地点范围面板未显示且无法用本次关键词安全唤起"
            ) from fill_exc
        await page.wait_for_timeout(900)
        await set_commerce_location_scope(page, scope)
    # 切换范围会重绘输入框和候选面板，不能继续使用切换前
    # 取得的 locator。重新回读带货模式与唯一输入框，同时兼容
    # 平台在切换后直接收起浮层的情况。
    store_control = await _ensure_local_group_buy_mode(page)
    input_control = await _open_commerce_search_input(page, store_control)
    try:
        await input_control.scroll_into_view_if_needed(timeout=5_000)
        await input_control.click(timeout=5_000)
    except Exception as exc:
        raise DouyinCommerceError(
            "抖音范围切换后的位置输入框无法安全打开，已停止"
        ) from exc
    # 范围切换后若输入框保留着同一个关键词，fill(同样文本) 不会触发 input
    # 事件，抖音便继续展示切换前的旧候选。先清空再重新填写，强制触发当前
    # 范围的一次新检索；后续仍严格校验候选的完整地址和关键词匹配。
    try:
        await input_control.fill("", timeout=8_000)
    except Exception as exc:
        raise DouyinCommerceError("抖音带货位置输入框无法重置关键词，已安全停止") from exc
    try:
        cleared_keyword = _normalized(
            await input_control.evaluate(
                """node => node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement
                    ? node.value : (node.innerText || node.textContent || '')"""
            )
        )
    except Exception as exc:
        raise DouyinCommerceError("抖音带货位置输入框清空后无法回读，已安全停止") from exc
    if cleared_keyword:
        raise DouyinCommerceError("抖音带货位置输入框未能清空旧关键词，已安全停止")
    # 输入框首次展开或清空时，平台可能仍在回填初始“本地”推荐；先给范围切换
    # 与清空关键词触发的请求一个受限的收敛时间，并保留快照。后续必须等待
    # 列表真正变化，不能像此前那样只要看到 listbox 就立刻把旧结果返回客户端。
    await page.wait_for_timeout(450)
    _, _, baseline_signature = await _visible_commerce_location_result_snapshot(page)
    # 清空关键词本身也可能让抖音替换整个搜索组件。上面的 locator 即使刚刚
    # 成功清空，也可能在 450ms 收敛期间失效；重新从唯一“添加标签”行定位
    # 当前输入框，再执行本次真正检索，避免向已分离节点写词。
    store_control = await _ensure_local_group_buy_mode(page)
    input_control = await _open_commerce_search_input(page, store_control)
    try:
        await input_control.scroll_into_view_if_needed(timeout=5_000)
        await input_control.click(timeout=5_000)
    except Exception as exc:
        raise DouyinCommerceError(
            "抖音关键词重置后的位置输入框无法安全打开，已停止"
        ) from exc
    try:
        await input_control.fill(normalized_keyword, timeout=8_000)
    except Exception as exc:
        raise DouyinCommerceError("抖音带货位置输入框无法填写关键词，已安全停止") from exc
    try:
        written_keyword = _normalized(
            await input_control.evaluate(
                """node => node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement
                    ? node.value : (node.innerText || node.textContent || '')"""
            )
        )
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
    )
    candidates = normalize_commerce_location_candidates(rows)
    if not candidates:
        raise DouyinCommerceError(
            f"抖音未返回“{normalized_keyword}”的完整可选发布定位"
        )
    return candidates


async def _location_option_targets(listbox, location: Mapping[str, Any]) -> list[Any]:
    """返回名称与完整地址均精确匹配的可点击发布定位项。

    不以页面请求、隐藏属性或门店 ID 反推地点。列表中同名地点必须再用完整地址
    消歧；若仍不是唯一项，则由调用方安全停止。
    """

    expected_name = _normalized(location.get("name"))
    expected_address = _normalized(location.get("address"))
    if not expected_name or not expected_address:
        return []
    options = listbox.locator(':scope > [role="option"]')
    targets: list[Any] = []
    for index in range(await options.count()):
        node = options.nth(index)
        try:
            if not await node.is_visible():
                continue
            actual = await node.evaluate(
                """node => {
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
                    const nameNode = node.querySelector('[data-store-name], [class*="name-"], [class*="name_"], [class*="title-"]');
                    const addressNode = node.querySelector('[data-store-address], [class*="address-"], [class*="address_"], [class*="addr"]');
                    const name = normalize(nameNode && (nameNode.innerText || nameNode.textContent)) || lines[0] || '';
                    const address = normalize(addressNode && (addressNode.innerText || addressNode.textContent))
                        || lines.find(line => looksAddress(line)) || '';
                    return { name, address };
                }"""
            )
        except Exception:
            continue
        if not isinstance(actual, Mapping):
            continue
        if (
            _normalized(actual.get("name")) == expected_name
            and _normalized(actual.get("address")) == expected_address
        ):
            targets.append(node)
    return targets


async def _apply_open_commerce_location_to_page(
    page,
    listbox,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """从当前已打开的地点面板点击唯一候选并回读。"""

    normalized = normalize_commerce_location_candidate(candidate)
    if not normalized:
        raise DouyinCommerceError("publish_location_candidate_missing")
    location = {
        key: _normalized(normalized.get(key))
        for key in ("poiId", "name", "address", "distance")
    }
    visible_locations = normalize_commerce_location_candidates(
        await _store_option_descriptors(listbox)
    )
    matched_locations = [
        row
        for row in visible_locations
        if _normalized(row.get("poiId")) == location["poiId"]
        and _normalized(row.get("name")) == location["name"]
        and _normalized(row.get("address")) == location["address"]
    ]
    if len(matched_locations) != 1:
        code = (
            "publish_location_candidate_ambiguous"
            if len(matched_locations) > 1
            else "publish_location_candidate_missing"
        )
        raise DouyinCommerceError(code)
    targets = await _location_option_targets(listbox, location)
    if len(targets) != 1:
        raise DouyinCommerceError("publish_location_click_failed")
    try:
        await targets[0].scroll_into_view_if_needed(timeout=5_000)
        await targets[0].click(timeout=8_000)
    except Exception:
        raise DouyinCommerceError("publish_location_click_failed") from None
    try:
        await page.wait_for_timeout(450)
        _, _, mode_value, selected_name = await _anchor_controls(page)
    except Exception:
        raise DouyinCommerceError("publish_location_readback_mismatch") from None
    if mode_value != _COMMERCE_MODE_TEXT or selected_name != location["name"]:
        raise DouyinCommerceError("publish_location_readback_mismatch")
    return {"location": location}


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


async def _close_commerce_store_selector_strict(page) -> None:
    """收口地点面板；失败时只暴露固定公开错误码。"""

    try:
        await close_commerce_store_selector(page)
    except Exception:
        raise DouyinCommerceError("publish_location_cleanup_incomplete") from None


async def apply_saved_commerce_location_to_page(
    page,
    preset: Mapping[str, Any],
    scope: object,
    keywords: list[str],
) -> dict[str, Any]:
    """在同一地点面板中完成搜索、唯一匹配、点击和回读。"""

    selected_scope = normalize_commerce_location_scope(scope)
    bounded_keywords = list(
        dict.fromkeys(
            normalized
            for value in keywords
            if (normalized := _normalized(value))
        )
    )[:3]
    if not bounded_keywords:
        raise DouyinCommerceError("publish_location_candidate_missing")

    for keyword in bounded_keywords:
        await _close_commerce_store_selector_strict(page)
        panel_may_be_open = False
        try:
            panel_may_be_open = True
            try:
                candidates = await search_commerce_location_store_candidates(
                    page,
                    keyword,
                    scope=selected_scope,
                )
            except DouyinCommerceError:
                continue
            try:
                matched = match_location_preset(preset, candidates)
            except DouyinLocationPresetError as exc:
                if "存在多个" in str(exc):
                    raise DouyinCommerceError(
                        "publish_location_candidate_ambiguous"
                    ) from None
                continue
            listbox = await _visible_store_listbox(page)
            if listbox is None:
                raise DouyinCommerceError("publish_location_click_failed")
            result = await _apply_open_commerce_location_to_page(
                page,
                listbox,
                matched,
            )
            return {**result, "matchedKeyword": keyword}
        except DouyinCommerceError:
            raise
        except Exception:
            raise DouyinCommerceError("publish_location_click_failed") from None
        finally:
            if panel_may_be_open:
                await _close_commerce_store_selector_strict(page)

    raise DouyinCommerceError("publish_location_candidate_missing")


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


async def close_commerce_store_selector(page) -> None:
    """关闭当前地点候选浮层，不选择地点或门店。"""

    try:
        input_control = await _visible_commerce_search_input(page)
    except DouyinCommerceError as exc:
        if "实际 0 个" not in str(exc):
            raise
        input_control = None
    if input_control is not None:
        overlay = await _visible_commerce_location_overlay(page)
        if overlay is not None:
            try:
                await input_control.press("Escape", timeout=5_000)
            except Exception as exc:
                raise DouyinCommerceError(
                    "抖音地点候选浮层无法安全关闭，已停止后续设置"
                ) from exc
            for _ in range(10):
                if await _visible_commerce_location_overlay(page) is None:
                    return
                await page.wait_for_timeout(150)
            raise DouyinCommerceError(
                "抖音地点候选浮层无法安全关闭，已停止后续设置"
            )

    listbox = await _visible_store_listbox(page)
    if listbox is None:
        return
    _, store_control, _, _ = await _anchor_controls(page)
    await _open_exact_select(page, store_control, "可绑定门店")
    for _ in range(10):
        if await _visible_store_listbox(page) is None:
            return
        await page.wait_for_timeout(150)
    raise DouyinCommerceError("抖音带货门店下拉无法安全关闭，已停止后续设置")


async def _store_option_descriptors(listbox) -> list[dict[str, str]]:
    """只抽取页面上可见的门店标识、名称和地址，不保存原始 DOM。"""

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    locator = listbox.locator(':scope > [role="option"]')
    for index in range(await locator.count()):
        node = locator.nth(index)
        try:
            if not await node.is_visible():
                continue
            descriptor = await node.evaluate(
                """node => {
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
                    const looksCommerce = value => /(?:商品|返佣|佣金|团购|套餐|券)/.test(value);
                    const nameNode = node.querySelector('[data-store-name], [class*="name-"], [class*="name_"], [class*="title-"]');
                    const addressNode = node.querySelector('[data-store-address], [class*="address-"], [class*="address_"], [class*="addr"]');
                    const commerceNode = node.querySelector('[class*="cps-item"], [data-commerce-info], [class*="commission"], [class*="product"]');
                    const name = normalize(nameNode && (nameNode.innerText || nameNode.textContent)) || lines[0] || '';
                    const address = normalize(addressNode && (addressNode.innerText || addressNode.textContent))
                        || lines.find(line => looksAddress(line)) || '';
                    const commerceInfo = normalize(commerceNode && (commerceNode.innerText || commerceNode.textContent))
                        || lines.find(line => looksCommerce(line)) || '';
                    const selected = node.getAttribute('aria-selected') === 'true'
                        || /(?:^|[-_\\s])(selected|chosen)(?:$|[-_\\s])/i.test(String(node.className || ''));
                    return {
                        storeId: attr('data-store-id') || attr('data-shop-id') || attr('data-id'),
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
        key = _normalized(descriptor.get("storeId")) or _visible_store_identity(
            _normalized(descriptor.get("name")), _normalized(descriptor.get("address"))
        )
        if not key or key in seen:
            continue
        seen.add(key)
        descriptor["storeId"] = key
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

    options = listbox.locator(':scope > [role="option"]')
    targets: list[Any] = []
    for index in range(await options.count()):
        node = options.nth(index)
        try:
            if not await node.is_visible():
                continue
            actual = await node.evaluate(
                """node => {
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
