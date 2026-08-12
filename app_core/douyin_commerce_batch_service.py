# -*- coding: utf-8 -*-
"""抖音带货批量任务的纯本地契约与排期。

本模块只收敛用户已经选择的批次配置，不会读取浏览器状态、Cookie、验证码，
也不会上传、创建草稿或发布。批次信封和传给既有单视频会话的内部载荷使用
不同工作流标识，避免批次任务被旧校验器误判为单条任务。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .douyin_commerce_service import (
    LOCAL_GROUP_BUY_MODE,
    normalize_commerce_location_scope,
    normalize_content_declaration,
)
from .douyin_commerce_location_commission import (
    normalize_commission_filter,
    normalize_observed_commission_type,
)
from .douyin_location_service import (
    DouyinLocationSearchError,
    normalize_location_candidate,
    normalize_location_keyword,
)
from .douyin_music_service import FAVORITE_MANUAL_MUSIC_MODE, normalize_music_readback


BATCH_WORKFLOW = "douyin-commerce-batch"
SINGLE_ITEM_WORKFLOW = "douyin-commerce"
SHANGHAI_TIMEZONE = "Asia/Shanghai"
_SHANGHAI = ZoneInfo(SHANGHAI_TIMEZONE)
_DATETIME_FORMAT = "%Y-%m-%d %H:%M"


class DouyinCommerceBatchError(ValueError):
    """批量带货任务缺少安全执行所需的本地字段。"""


def current_shanghai_time() -> datetime:
    """返回批量任务入口唯一可传递的北京时间时钟值。

    调用方应在一次“创建任务 + 启动执行”的入口只取一次该值，并将同一个值
    传给本模块。这样任务明细、排期和执行器不会因分别读取机器时钟而产生不一致。
    """

    return datetime.now(_SHANGHAI).replace(second=0, microsecond=0)


def _text(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def _tags(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DouyinCommerceBatchError("抖音带货批量话题必须是列表")
    result: list[str] = []
    for raw in value:
        tag = _text(raw).lstrip("#").strip()
        if tag and tag not in result:
            result.append(tag)
    return result


def _parse_shanghai_time(value: object, *, field_name: str) -> datetime:
    raw = _text(value)
    if not raw:
        raise DouyinCommerceBatchError(f"{field_name}不能为空")
    try:
        return datetime.strptime(raw, _DATETIME_FORMAT).replace(tzinfo=_SHANGHAI)
    except ValueError as exc:
        raise DouyinCommerceBatchError(
            f"{field_name}必须为北京时间 YYYY-MM-DD HH:MM"
        ) from exc


def _future_shanghai_time(value: object, *, now: datetime, field_name: str) -> str:
    scheduled = _parse_shanghai_time(value, field_name=field_name)
    reference = _as_shanghai(now)
    if scheduled <= reference:
        raise DouyinCommerceBatchError(f"{field_name}必须晚于当前北京时间")
    return scheduled.strftime(_DATETIME_FORMAT)


def _as_shanghai(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise DouyinCommerceBatchError("当前时间必须是 datetime")
    # 没有时区的输入只在离线界面中按北京时间理解；服务永远返回带时区的比较值。
    return value.replace(tzinfo=_SHANGHAI) if value.tzinfo is None else value.astimezone(_SHANGHAI)


def _account_file(payload: Mapping[str, Any]) -> str:
    accounts = payload.get("accountList")
    if accounts is None:
        account_file = _text(payload.get("accountFile"))
        if account_file:
            return account_file
    if not isinstance(accounts, list):
        raise DouyinCommerceBatchError("抖音带货批量一次只能选择一个已登录账号")
    normalized = [_text(item) for item in accounts if _text(item)]
    if len(normalized) != 1:
        raise DouyinCommerceBatchError("抖音带货批量一次只能选择一个已登录账号")
    return normalized[0]


def _shared(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DouyinCommerceBatchError("抖音带货批量缺少共享内容")
    description = str(value.get("description") or "").strip()
    if not description:
        raise DouyinCommerceBatchError("抖音带货批量缺少作品描述")
    music = normalize_music_readback(value.get("selectedMusic"))
    if not music:
        raise DouyinCommerceBatchError("抖音带货批量需要当前收藏音乐的可验证回读")
    try:
        declaration = normalize_content_declaration(value.get("contentDeclaration"))
    except Exception as exc:
        raise DouyinCommerceBatchError(str(exc)) from exc
    return {
        "title": _text(value.get("title")),
        "description": description,
        "tags": _tags(value.get("tags")),
        "selectedMusic": music,
        "contentDeclaration": declaration,
    }


def _location_preset(value: object, *, index: int) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    location = normalize_location_candidate(value)
    if not location or not location["address"]:
        raise DouyinCommerceBatchError(f"第 {index} 条视频必须选择带完整地址的官方地点预设")
    try:
        scope = normalize_commerce_location_scope(
            raw.get("scope")
        )
    except Exception as exc:
        raise DouyinCommerceBatchError(f"第 {index} 条视频地点预设范围无效：{exc}") from exc
    result = {**location, "scope": scope}
    result["commissionFilter"] = normalize_commission_filter(
        raw.get("commissionFilter"), default="all"
    )
    result["observedCommissionType"] = normalize_observed_commission_type(
        raw.get("observedCommissionType"), default="unknown"
    )
    for field in ("productCount", "commissionProductCount"):
        count = raw.get(field)
        result[field] = count if type(count) is int and count >= 0 else None
    search_keyword = _text(
        raw.get("searchKeyword")
    )
    if search_keyword:
        try:
            result["searchKeyword"] = normalize_location_keyword(search_keyword)
        except DouyinLocationSearchError as exc:
            raise DouyinCommerceBatchError(
                f"第 {index} 条视频地点原始搜索词无效：{exc}"
            ) from exc
    return result


def _items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise DouyinCommerceBatchError("抖音带货批量条目数量必须在 1 至 20 之间")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            raise DouyinCommerceBatchError(f"第 {index} 条视频格式无效")
        media_path = _text(raw.get("mediaPath"))
        if not media_path or not Path(media_path).is_file():
            raise DouyinCommerceBatchError(f"第 {index} 条视频缺少可读取的本地媒体")
        override = _text(raw.get("scheduleTimeOverride"))
        # 覆盖时间的有效性会在取得明确的 now 后由 apply_interval_schedule 校验。
        if override:
            _parse_shanghai_time(override, field_name=f"第 {index} 条视频覆盖发布时间")
        result.append(
            {
                "mediaPath": media_path,
                "locationPreset": _location_preset(raw.get("locationPreset"), index=index),
                "scheduleTimeOverride": override,
            }
        )
    return result


def _schedule(value: object, *, publish_mode: str) -> dict[str, Any]:
    if publish_mode == "immediate":
        return {"timezone": SHANGHAI_TIMEZONE, "startTime": "", "intervalMinutes": 0}
    if publish_mode != "interval-schedule":
        raise DouyinCommerceBatchError("批量发布方式只能是 immediate 或 interval-schedule")
    if not isinstance(value, Mapping):
        raise DouyinCommerceBatchError("按间隔定时必须填写排期")
    timezone = _text(value.get("timezone") or SHANGHAI_TIMEZONE)
    if timezone != SHANGHAI_TIMEZONE:
        raise DouyinCommerceBatchError("批量定时仅支持 Asia/Shanghai")
    start_time = _parse_shanghai_time(value.get("startTime"), field_name="定时起始时间")
    try:
        interval_minutes = int(value.get("intervalMinutes"))
    except (TypeError, ValueError) as exc:
        raise DouyinCommerceBatchError("定时间隔必须是正整数分钟") from exc
    if interval_minutes <= 0:
        raise DouyinCommerceBatchError("定时间隔必须是正整数分钟")
    return {
        "timezone": SHANGHAI_TIMEZONE,
        "startTime": start_time.strftime(_DATETIME_FORMAT),
        "intervalMinutes": interval_minutes,
    }


def _validate_future_schedule(batch: Mapping[str, Any], *, now: datetime) -> None:
    """以调用方明确提供的北京时间校验定时与覆盖时间。"""

    if batch["publishMode"] != "interval-schedule":
        return
    current = _as_shanghai(now)
    start = _parse_shanghai_time(batch["schedule"]["startTime"], field_name="定时起始时间")
    interval = timedelta(minutes=batch["schedule"]["intervalMinutes"])
    for index, item in enumerate(batch["items"]):
        candidate = item["scheduleTimeOverride"] or (start + interval * index).strftime(
            _DATETIME_FORMAT
        )
        _future_shanghai_time(candidate, now=current, field_name=f"第 {index + 1} 条视频发布时间")


def validate_batch_payload(
    payload: Mapping[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """验证批次信封并仅返回白名单字段，不执行任何平台动作。

    ``now`` 仅由需要判定“未来”的调用方显式传入，避免本地契约校验依赖机器
    时钟；排期生成会把自己的时钟原样传入。
    """

    if not isinstance(payload, Mapping):
        raise DouyinCommerceBatchError("抖音带货批量任务格式无效")
    try:
        platform_type = int(payload.get("type") or 0)
    except (TypeError, ValueError) as exc:
        raise DouyinCommerceBatchError("抖音带货批量只能选择抖音平台") from exc
    if platform_type != 3:
        raise DouyinCommerceBatchError("抖音带货批量只能选择抖音平台")
    if _text(payload.get("workflow")) != BATCH_WORKFLOW:
        raise DouyinCommerceBatchError("抖音带货批量缺少 workflow=douyin-commerce-batch")
    if _text(payload.get("commerceMode")) != LOCAL_GROUP_BUY_MODE:
        raise DouyinCommerceBatchError("抖音带货批量仅支持本地团购模式")
    if _text(payload.get("contentType")) != "video":
        raise DouyinCommerceBatchError("抖音带货批量仅支持视频")
    publish_mode = _text(payload.get("publishMode") or "immediate")
    checked = {
        "type": 3,
        "workflow": BATCH_WORKFLOW,
        "commerceMode": LOCAL_GROUP_BUY_MODE,
        "contentType": "video",
        "accountFile": _account_file(payload),
        # 后台运行默认开启；只有桌面端明确传入 false 才显示受控浏览器，
        # 方便用户观察平台 DOM 与风控页面。
        "backgroundMode": payload.get("backgroundMode") is not False,
        "shared": _shared(payload.get("shared")),
        "publishMode": publish_mode,
        "schedule": _schedule(payload.get("schedule"), publish_mode=publish_mode),
        "items": _items(payload.get("items")),
    }
    if now is not None:
        _validate_future_schedule(checked, now=now)
    return checked


def apply_interval_schedule(payload: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """以给定当前时间生成每条发布时间，并验证其均晚于北京时间当前时刻。"""

    batch = validate_batch_payload(payload, now=now)
    result = deepcopy(batch)
    if result["publishMode"] == "immediate":
        for item in result["items"]:
            item["enableTimer"] = False
            item.pop("scheduleTime", None)
        return result

    start = _parse_shanghai_time(result["schedule"]["startTime"], field_name="定时起始时间")
    interval = timedelta(minutes=result["schedule"]["intervalMinutes"])
    for index, item in enumerate(result["items"]):
        generated = start + interval * index
        candidate = item["scheduleTimeOverride"] or generated.strftime(_DATETIME_FORMAT)
        # validate_batch_payload 已使用同一个显式 now 完成未来时间校验；
        # 此处只写入已验证的排期，避免再次读取或推断机器时钟。
        item["scheduleTime"] = candidate
        item["enableTimer"] = True
    return result


def prepare_batch_for_execution(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """生成每条可直接建任务和执行的排期字段。

    ``validate_batch_payload`` 故意只保存用户配置，避免把过期的运行排期写入本地
    草稿；所有会创建任务的入口都必须经过这里，确保每项都具有显式
    ``enableTimer``，定时项还具有经过同一北京时间校验的 ``scheduleTime``。
    """

    controlled_now = current_shanghai_time() if now is None else _as_shanghai(now)
    return apply_interval_schedule(payload, now=controlled_now)


def item_publish_payload(batch: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    """为既有单视频会话生成最小内部操作载荷。"""

    schedule_time = _text(item.get("scheduleTime")) if item.get("enableTimer") is True else ""
    return {
        "type": 3,
        "workflow": SINGLE_ITEM_WORKFLOW,
        "batchWorkflow": BATCH_WORKFLOW,
        "commerceMode": LOCAL_GROUP_BUY_MODE,
        "contentType": "video",
        "accountList": [batch["accountFile"]],
        "fileList": [item["mediaPath"]],
        "title": batch["shared"]["title"],
        "description": batch["shared"]["description"],
        "tags": list(batch["shared"]["tags"]),
        "selectedMusic": dict(batch["shared"]["selectedMusic"]),
        # 批量任务共享的是用户已选择并回读的收藏音乐，不能退回历史首条策略。
        "musicMode": FAVORITE_MANUAL_MUSIC_MODE,
        "contentDeclaration": batch["shared"]["contentDeclaration"],
        "locationPoi": dict(item["locationPreset"]),
        "locationCommissionFilter": item["locationPreset"].get(
            "commissionFilter", "all"
        ),
        "locationKeyword": item["locationPreset"]["name"],
        "locationSearchKeyword": _text(
            item["locationPreset"].get("searchKeyword")
        ),
        "locationScope": item["locationPreset"]["scope"],
        "backgroundMode": batch.get("backgroundMode") is not False,
        "enableTimer": item["enableTimer"] is True,
        "scheduleTime": schedule_time,
    }
