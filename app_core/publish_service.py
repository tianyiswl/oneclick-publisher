# -*- coding: utf-8 -*-
"""一键发桌面端发布任务服务。

恢复代码只作为能力参考；这里仅调度一键发自己的会话、任务表和执行器。
小红书图文/视频与公众号正式发表均使用一键发内置受控执行器；其他平台仍保持
既有安全边界。
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from myUtils.postVideo import post_video_batch_draft_tabs

from . import (
    douyin_commerce_batch_executor,
    douyin_commerce_batch_service,
    douyin_commerce_service,
    douyin_location_service,
    douyin_publish_executor,
    oneclick_capabilities,
    oneclick_preflight,
    overseas_browser_publish,
    overseas_video_publish,
    overseas_preflight,
    task_service,
    video_channel_location_service,
    wechat_location_service,
    wechat_publish_executor,
    wechat_publish_policy,
    xhs_location_service,
    xhs_publish_executor,
)
from .paths import VIDEO_DIR


_publish_lock = threading.Lock()
_active_threads: dict[int, threading.Thread] = {}


_PLATFORM_NAMES = {
    1: "小红书", 2: "视频号", 3: "抖音", 4: "快手", 5: "B站",
    6: "TikTok", 7: "YouTube", 8: "Instagram Reels", 9: "Facebook Reels",
    10: "公众号",
}


_DOUYIN_COMMERCE_BATCH_WORKFLOW = "douyin-commerce-batch"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _is_douyin_commerce_batch_payload(payload: object) -> bool:
    """判断是否为必须交给批量执行器的批次信封。

    ``batchWorkflow`` 只会出现在批次内部生成的单视频操作载荷中；它同样不能
    回流到通用发布服务，否则旧的按平台汇总结果会绕过逐视频最终回执。
    """

    return isinstance(payload, dict) and (
        str(payload.get("workflow") or "").strip()
        == _DOUYIN_COMMERCE_BATCH_WORKFLOW
        or str(payload.get("batchWorkflow") or "").strip()
        == _DOUYIN_COMMERCE_BATCH_WORKFLOW
    )


def _prepare_douyin_commerce_batch_request(
    payloads: list[dict[str, Any]],
    *,
    expected_mode: str,
    schedule_now: datetime | None = None,
) -> dict[str, Any]:
    """校验批量入口运行边界并返回白名单批次信封。

    批次执行器只消费 ``validate_batch_payload`` 返回的字段。运行模式与最终确认
    只在通用入口处作为门槛验证，不会混入持久化批次或下沉给单视频旧执行器。
    """

    if len(payloads) != 1 or not _is_douyin_commerce_batch_payload(payloads[0]):
        raise ValueError("抖音带货批量任务一次只能传入一个批次信封")
    raw = dict(payloads[0])
    workflow = str(raw.get("workflow") or "").strip()
    if workflow != _DOUYIN_COMMERCE_BATCH_WORKFLOW:
        raise ValueError("批次内部单视频载荷不能进入通用发布服务")

    runtime_mode = str(raw.get("runtimeMode") or "preflight").strip()
    if runtime_mode != expected_mode:
        raise ValueError(f"抖音带货批量{('预检' if expected_mode == 'preflight' else '发布')}必须明确 runtimeMode={expected_mode}")
    if expected_mode == "preflight":
        if raw.get("debugDryRun", True) is not True:
            raise ValueError("抖音带货批量预检必须明确 debugDryRun=true")
    else:
        if raw.get("debugDryRun") is not False:
            raise ValueError("抖音带货批量正式发布必须明确 debugDryRun=false")
        if raw.get("batchConfirmed") is not True:
            raise ValueError("抖音带货批量正式发布必须先完成批量确认")
    try:
        return douyin_commerce_batch_service.prepare_batch_for_execution(
            raw,
            now=(schedule_now or datetime.now(_SHANGHAI).replace(second=0, microsecond=0)),
        )
    except douyin_commerce_batch_service.DouyinCommerceBatchError as exc:
        raise ValueError(str(exc)) from exc


def _record_douyin_batch_progress(task_id: int, event: object) -> None:
    """记录不含浏览器状态的批次进度，供任务记录和客户端轮询使用。"""

    public = event.to_public_dict() if hasattr(event, "to_public_dict") else {}
    if not isinstance(public, dict):
        return
    index = int(public.get("index") or 0) + 1
    total = int(public.get("total") or 0)
    message = " ".join(str(public.get("message") or "").split())
    if message:
        task_service.record_task_event(
            int(task_id),
            "douyin_commerce_batch_progress",
            f"第 {index}/{total} 条：{message}",
        )


def _runtime_media_path(value: object) -> str:
    """把素材库中的安全文件名解析为运行时绝对路径。"""

    raw = str(value or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if path.is_file():
        return str(path.resolve())
    stored = VIDEO_DIR / path.name
    return str(stored.resolve()) if stored.is_file() else raw


def _validate_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not payloads:
        raise ValueError("没有可执行的预发布检查项")
    validated: list[dict[str, Any]] = []
    for raw in payloads:
        payload = dict(raw)
        if (
            str(payload.get("workflow") or "").strip() == "douyin-commerce-batch"
            or str(payload.get("batchWorkflow") or "").strip()
            == "douyin-commerce-batch"
        ):
            raise ValueError(
                "抖音带货批量任务必须使用批量执行器，不能进入通用发布服务"
            )
        payload["fileList"] = [
            _runtime_media_path(item)
            for item in payload.get("fileList") or []
        ]
        payload["coverPath"] = _runtime_media_path(payload.get("coverPath"))
        if isinstance(payload.get("coverPaths"), dict):
            payload["coverPaths"] = {
                str(ratio): _runtime_media_path(path)
                for ratio, path in payload["coverPaths"].items()
                if _runtime_media_path(path)
            }
        if isinstance(payload.get("imagePlacements"), list):
            payload["imagePlacements"] = [
                {
                    **dict(item),
                    "path": _runtime_media_path(dict(item).get("path")),
                }
                for item in payload["imagePlacements"]
                if isinstance(item, dict)
            ]
        runtime_mode = str(payload.get("runtimeMode") or "preflight")
        platform_type = int(payload.get("type") or 0)
        if platform_type == 1:
            content_type = str(payload.get("contentType") or "").strip()
            xhs_location_keys = (
                "xhsLocationKeyword",
                "xhsLocationScope",
                "xhsLocationPoi",
            )
            generic_location_keys = (
                "locationKeyword",
                "locationScope",
                "locationPoi",
            )

            def has_value(key: str) -> bool:
                value = payload.get(key)
                if isinstance(value, (dict, list, tuple, set)):
                    return bool(value)
                return bool(str(value or "").strip())

            if content_type != "video":
                if any(key in payload for key in xhs_location_keys):
                    raise ValueError("小红书地点字段仅支持视频发布")
                if any(has_value(key) for key in generic_location_keys):
                    raise ValueError(
                        "小红书非视频内容不得夹带通用地点字段"
                    )
            else:
                try:
                    location = xhs_location_service.normalize_location_selection(
                        payload
                    )
                except xhs_location_service.XhsLocationSearchError as exc:
                    raise ValueError(str(exc)) from exc
                if location is None:
                    payload["xhsLocationKeyword"] = ""
                    payload["xhsLocationScope"] = ""
                    payload["xhsLocationPoi"] = None
                else:
                    payload["xhsLocationKeyword"] = str(
                        location["searchKeyword"]
                    )
                    payload["xhsLocationScope"] = str(location["scope"])
                    payload["xhsLocationPoi"] = location
            for key in generic_location_keys:
                payload.pop(key, None)
        video_channel_location_keys = (
            "videoChannelLocationKeyword",
            "videoChannelLocationScope",
            "videoChannelLocationPoi",
        )
        if platform_type == 2:
            if str(payload.get("contentType") or "").strip() != "video":
                if any(bool(payload.get(key)) for key in video_channel_location_keys):
                    raise ValueError("视频号位置字段仅支持视频发布")
            else:
                try:
                    location = (
                        video_channel_location_service.normalize_location_selection(
                            payload
                        )
                    )
                except video_channel_location_service.VideoChannelLocationError as exc:
                    raise ValueError(str(exc)) from exc
                if location is None:
                    payload["videoChannelLocationKeyword"] = ""
                    payload["videoChannelLocationScope"] = ""
                    payload["videoChannelLocationPoi"] = None
                else:
                    payload["videoChannelLocationKeyword"] = str(
                        location["searchKeyword"]
                    )
                    payload["videoChannelLocationScope"] = str(location["scope"])
                    payload["videoChannelLocationPoi"] = location
        elif any(bool(payload.get(key)) for key in video_channel_location_keys):
            raise ValueError("视频号位置字段不能用于其他平台")
        if platform_type == 3:
            location_keyword = " ".join(
                str(payload.get("locationKeyword") or "").split()
            )
            is_commerce = (
                str(payload.get("workflow") or "").strip()
                == "douyin-commerce"
            )
            location_normalizer = (
                douyin_location_service.normalize_location_candidate
                if is_commerce
                else douyin_location_service.normalize_publish_location_candidate
            )
            location = location_normalizer(payload.get("locationPoi"))
            if location_keyword:
                if not location or location["name"] != location_keyword:
                    raise ValueError(
                        "抖音发布定位必须来自一键发官方地点候选，不能只传关键词"
                    )
                if (
                    not is_commerce
                    and str(payload.get("locationScope") or "").strip()
                    != "local"
                ):
                    raise ValueError(
                        "普通抖音发布定位仅支持账号本地地点，"
                        "请返回平台适配重新搜索并选择本地地点"
                    )
                payload["locationKeyword"] = location["name"]
                payload["locationPoi"] = location
                if not is_commerce:
                    payload["locationScope"] = "local"
            else:
                payload["locationKeyword"] = ""
                payload["locationPoi"] = {}
            if is_commerce:
                try:
                    payload.update(
                        douyin_commerce_service.validate_douyin_commerce_payload(payload)
                    )
                except douyin_commerce_service.DouyinCommerceError as exc:
                    raise ValueError(str(exc)) from exc
        wechat_location_keys = (
            "wechatLocationKeyword",
            "wechatLocationScope",
            "wechatLocationPoi",
        )
        if platform_type == 10:
            try:
                wechat_location = (
                    wechat_location_service.normalize_location_selection(payload)
                )
            except wechat_location_service.WechatLocationError as exc:
                raise ValueError(str(exc)) from exc
            if wechat_location is None:
                payload["wechatLocationKeyword"] = ""
                payload["wechatLocationScope"] = ""
                payload["wechatLocationPoi"] = None
            else:
                payload["wechatLocationKeyword"] = str(
                    wechat_location["searchKeyword"]
                )
                payload["wechatLocationScope"] = str(wechat_location["scope"])
                payload["wechatLocationPoi"] = wechat_location
        elif any(
            bool(payload.get(key))
            for key in wechat_location_keys
        ):
            raise ValueError("公众号正文地点字段不能用于其他平台")
        if runtime_mode == "preflight":
            if payload.get("debugDryRun") is not True:
                raise ValueError("预发布检查必须保持 debugDryRun=true")
        elif runtime_mode == "publish":
            if platform_type not in {1, 3, 6, 7, 8, 9, 10}:
                raise ValueError(
                    "当前正式发布执行器只开放小红书、抖音、公众号、TikTok、YouTube 和 Meta 浏览器通道"
                )
            if payload.get("debugDryRun") is not False:
                raise ValueError("正式发布必须明确 debugDryRun=false")
            if platform_type == 3:
                # 抖音正式发布强制前台并规范化结构化 POI，写回任务载荷，
                # 让任务记录、执行器与页面实际行为保持一致。
                payload.update(
                    douyin_publish_executor.validate_douyin_publish_payload(payload)
                )
            elif platform_type in {6, 7}:
                checked = overseas_video_publish.validate_overseas_video_publish_payload(
                    payload
                )
                if not checked["ok"]:
                    raise ValueError("；".join(checked["errors"]))
                # 海外账号风控与二次验证必须能在前台由用户处理。
                payload["backgroundMode"] = False
            elif platform_type in {8, 9}:
                checked = overseas_browser_publish.validate_meta_browser_publish_payload(
                    payload
                )
                if not checked["ok"]:
                    raise ValueError("；".join(checked["errors"]))
            elif platform_type == 1:
                if str(payload.get("contentType") or "") not in {"article", "video"}:
                    raise ValueError("小红书正式发布只支持图文或视频")
                if payload.get("enableTimer") is True and not payload.get("scheduleTime"):
                    raise ValueError("小红书已开启定时发布，但未设置发布时间")
            else:
                wechat_publish_policy.normalize_wechat_publish_preferences(payload)
        elif runtime_mode == "draft":
            if platform_type not in {2, 5}:
                raise ValueError(
                    "当前平台没有可验证的草稿保存通道；"
                    "目前仅视频号和B站支持"
                )
        else:
            raise ValueError("当前任务模式不支持")
        capability = oneclick_capabilities.validate_payload(
            {
                "platform": payload.get("type") and _PLATFORM_NAMES.get(int(payload["type"]), ""),
                "content_type": payload.get("contentType"),
                "title": payload.get("title"),
                "description": payload.get("description"),
                "fileList": payload.get("fileList") or [],
                "coverPath": payload.get("coverPath"),
            }
        )
        if not capability.get("valid"):
            raise ValueError("；".join(capability.get("errors") or ["发布内容未通过预检"]))
        missing = [str(path) for path in payload.get("fileList") or [] if not Path(str(path)).is_file()]
        if missing:
            raise ValueError("素材文件不存在，请重新选择素材")
        if (
            int(payload.get("type") or 0) in {3, 10}
            and str(payload.get("contentType") or "") == "text"
            and not Path(str(payload.get("coverPath") or "")).is_file()
        ):
            platform_name = _PLATFORM_NAMES[int(payload["type"])]
            raise ValueError(f"{platform_name}文字预检需要选择本地封面图片")
        validated.append(payload)
    return validated


def _run_douyin_commerce_batch_preflight(
    task: dict,
    payloads: list[dict[str, Any]],
    *,
    prepared_batch: dict | None = None,
) -> None:
    """执行批量 dry-run；此路径没有也不会调用最终提交。"""

    batch = prepared_batch or _prepare_douyin_commerce_batch_request(payloads, expected_mode="preflight")
    if not _publish_lock.acquire(blocking=False):
        task_service.record_task_event(
            int(task["id"]),
            "douyin_commerce_batch_busy",
            "已有发布或预检任务正在执行，批量预检未启动",
            level="warning",
        )
        _active_threads.pop(int(task["id"]), None)
        return
    try:
        task_service.mark_task_running(int(task["id"]), "抖音带货批量开始逐视频预检")
        results = douyin_commerce_batch_executor.batch_executor.run_preflight(
            batch,
            task_id=int(task["id"]),
            progress=lambda event: _record_douyin_batch_progress(int(task["id"]), event),
        )
        if not all(isinstance(item, dict) and item.get("status") == "preflighted" for item in results):
            task_service.record_task_event(
                int(task["id"]),
                "douyin_commerce_batch_preflight_incomplete",
                "批量预检未取得全部逐视频编辑页回读，未进入提交",
                level="warning",
            )
    except Exception as exc:
        task_service.record_task_event(
            int(task["id"]),
            "douyin_commerce_batch_preflight_failed",
            f"批量预检异常：{type(exc).__name__}：{exc}",
            level="error",
        )
    finally:
        _publish_lock.release()
        _active_threads.pop(int(task["id"]), None)


def _run_douyin_commerce_batch_publish(
    task: dict,
    payloads: list[dict[str, Any]],
    *,
    prepared_batch: dict | None = None,
) -> None:
    """执行已总确认的批量最终提交，成功状态只由批量执行器平台回执写入。"""

    batch = prepared_batch or _prepare_douyin_commerce_batch_request(payloads, expected_mode="publish")
    if not _publish_lock.acquire(blocking=False):
        task_service.record_task_event(
            int(task["id"]),
            "douyin_commerce_batch_busy",
            "已有发布或预检任务正在执行，批量提交未启动",
            level="warning",
        )
        _active_threads.pop(int(task["id"]), None)
        return
    try:
        task_service.mark_task_running(int(task["id"]), "抖音带货批量开始逐视频最终提交")
        douyin_commerce_batch_executor.batch_executor.run_publish(
            batch,
            task_id=int(task["id"]),
            confirmed=True,
            progress=lambda event: _record_douyin_batch_progress(int(task["id"]), event),
        )
    except Exception as exc:
        # 这里绝不调用 mark_platform_result：批量任务每条的成功只能由执行器在
        # 同一 submit 会话取得平台最终回读后写入。异常仅记录为任务事件。
        task_service.record_task_event(
            int(task["id"]),
            "douyin_commerce_batch_publish_failed",
            f"批量最终提交异常：{type(exc).__name__}：{exc}",
            level="error",
        )
    finally:
        _publish_lock.release()
        _active_threads.pop(int(task["id"]), None)


def _run_preflight(task: dict, payloads: list[dict[str, Any]]) -> None:
    if payloads and _is_douyin_commerce_batch_payload(payloads[0]):
        _run_douyin_commerce_batch_preflight(task, payloads)
        return
    if not _publish_lock.acquire(blocking=False):
        task_service.mark_platform_result(
            task["id"], int(payloads[0]["type"]), ok=False,
            message="已有预检任务正在执行，请稍后重试",
            content_type=str(payloads[0].get("contentType") or ""),
        )
        _active_threads.pop(int(task["id"]), None)
        return
    try:
        task_service.mark_task_running(task["id"], "一键发开始执行真实预发布检查")
        for payload in payloads:
            platform_type = int(payload["type"])
            task_service.record_task_event(task["id"], "platform_started", f"开始检查{platform_type}号平台的素材上传与表单填写")
            try:
                if (
                    platform_type == 3
                    and str(payload.get("workflow") or "") == "douyin-commerce"
                ):
                    result = douyin_publish_executor.run_douyin_commerce_preflight_sync(
                        payload,
                        task_id=int(task["id"]),
                    )
                elif platform_type in {6, 7, 8, 9}:
                    result = overseas_preflight.run_overseas_preflight_sync(payload)
                else:
                    result = oneclick_preflight.run_preflight_sync(payload)
                task_service.mark_platform_result(
                    task["id"],
                    platform_type,
                    ok=bool(result.get("ok")),
                    message=str(result.get("message") or "预发布检查结束"),
                    content_type=str(payload.get("contentType") or ""),
                )
            except Exception as exc:
                task_service.mark_platform_result(
                    task["id"],
                    platform_type,
                    ok=False,
                    message=f"预检任务异常：{type(exc).__name__}：{exc}",
                    content_type=str(payload.get("contentType") or ""),
                )
    except Exception as exc:
        task_service.mark_platform_result(
            task["id"], int(payloads[0]["type"]), ok=False,
            message=f"预检任务异常：{type(exc).__name__}：{exc}",
            content_type=str(payloads[0].get("contentType") or ""),
        )
    finally:
        _publish_lock.release()
        _active_threads.pop(int(task["id"]), None)


def _run_publish(task: dict, payloads: list[dict[str, Any]]) -> None:
    """执行已获用户确认的小红书图文/视频或公众号正式提交。"""

    if payloads and _is_douyin_commerce_batch_payload(payloads[0]):
        _run_douyin_commerce_batch_publish(task, payloads)
        return

    if not _publish_lock.acquire(blocking=False):
        task_service.mark_platform_result(
            task["id"],
            int(payloads[0]["type"]),
            ok=False,
            message="已有发布任务正在执行，请稍后重试",
            content_type=str(payloads[0].get("contentType") or ""),
            event_type="platform_publish",
        )
        _active_threads.pop(int(task["id"]), None)
        return
    try:
        task_service.mark_task_running(task["id"], "一键发开始执行受控正式发布")
        for payload in payloads:
            platform_type = int(payload["type"])
            platform_name = _PLATFORM_NAMES.get(platform_type, "当前平台")
            task_service.record_task_event(
                task["id"],
                "platform_publish_started",
                f"开始执行{platform_name}字段回读与正式提交",
            )
            try:
                if platform_type == 1:
                    result = xhs_publish_executor.run_xhs_publish_sync(
                        payload,
                        task_id=int(task["id"]),
                    )
                elif platform_type == 3:
                    result = douyin_publish_executor.run_douyin_publish_sync(
                        payload,
                        task_id=int(task["id"]),
                    )
                elif platform_type == 10:
                    result = wechat_publish_executor.run_wechat_publish_sync(
                        payload,
                        task_id=int(task["id"]),
                    )
                elif platform_type in {6, 7}:
                    result = overseas_video_publish.run_overseas_video_publish_sync(
                        payload
                    )
                elif platform_type in {8, 9}:
                    result = overseas_browser_publish.run_meta_browser_publish_sync(
                        payload
                    )
                else:
                    raise ValueError(f"{platform_name}尚未接入受控正式发布执行器")
                task_service.mark_platform_result(
                    task["id"],
                    platform_type,
                    ok=bool(result.get("ok")),
                    message=str(
                        result.get("message")
                        or (
                            (
                                f"小红书定时发布成功：{result.get('scheduledAt')}"
                                if platform_type == 1 and result.get("scheduled")
                                else f"{platform_name}正式提交结束"
                            )
                        )
                    ),
                    content_type=str(payload.get("contentType") or ""),
                    event_type="platform_publish",
                )
            except Exception as exc:
                task_service.mark_platform_result(
                    task["id"],
                    platform_type,
                    ok=False,
                    message=f"正式发布异常：{type(exc).__name__}：{exc}",
                    content_type=str(payload.get("contentType") or ""),
                    event_type="platform_publish",
                )
    except Exception as exc:
        task_service.mark_platform_result(
            task["id"],
            int(payloads[0]["type"]),
            ok=False,
            message=f"正式发布异常：{type(exc).__name__}：{exc}",
            content_type=str(payloads[0].get("contentType") or ""),
            event_type="platform_publish",
        )
    finally:
        _publish_lock.release()
        _active_threads.pop(int(task["id"]), None)


def _run_draft(task: dict, payloads: list[dict[str, Any]]) -> None:
    """执行已接入并可回读的国内平台草稿保存。"""

    if not _publish_lock.acquire(blocking=False):
        task_service.mark_platform_result(
            task["id"],
            int(payloads[0]["type"]),
            ok=False,
            message="已有发布任务正在执行，请稍后重试",
            content_type=str(payloads[0].get("contentType") or ""),
            event_type="platform_draft",
        )
        _active_threads.pop(int(task["id"]), None)
        return
    try:
        task_service.mark_task_running(
            task["id"], "一键发开始执行受控平台草稿保存"
        )
        results = post_video_batch_draft_tabs(payloads)
        by_platform: dict[int, list[dict[str, Any]]] = {}
        for result in results or []:
            try:
                result_type = int(result.get("type") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            by_platform.setdefault(result_type, []).append(result)
        for payload in payloads:
            platform_type = int(payload["type"])
            platform_results = by_platform.get(platform_type, [])
            failed = [
                item for item in platform_results if item.get("ok") is False
            ]
            ok = bool(platform_results) and not failed
            message = (
                str(failed[0].get("message") or "平台草稿保存失败")
                if failed
                else "平台已返回可验证的草稿保存结果"
                if ok
                else "平台草稿执行器未返回可验证结果"
            )
            task_service.mark_platform_result(
                task["id"],
                platform_type,
                ok=ok,
                message=message,
                content_type=str(payload.get("contentType") or ""),
                event_type="platform_draft",
            )
    except Exception as exc:
        task_service.mark_platform_result(
            task["id"],
            int(payloads[0]["type"]),
            ok=False,
            message=f"平台草稿异常：{type(exc).__name__}：{exc}",
            content_type=str(payloads[0].get("contentType") or ""),
            event_type="platform_draft",
        )
    finally:
        _publish_lock.release()
        _active_threads.pop(int(task["id"]), None)


def start_desktop_publish(payloads: list[dict[str, Any]]) -> dict:
    """按载荷启动预检或已确认的公众号正式发布任务。"""

    if payloads and _is_douyin_commerce_batch_payload(payloads[0]):
        raw_batch = dict(payloads[0])
        runtime_mode = str(raw_batch.get("runtimeMode") or "preflight").strip()
        if runtime_mode not in {"preflight", "publish"}:
            raise ValueError("抖音带货批量只支持预检或已确认的正式发布")
        controlled_now = datetime.now(_SHANGHAI).replace(second=0, microsecond=0)
        batch = _prepare_douyin_commerce_batch_request(
            [raw_batch], expected_mode=runtime_mode, schedule_now=controlled_now
        )
        task = task_service.create_douyin_batch_task(
            batch,
            mode="oneclick_publish" if runtime_mode == "publish" else "oneclick_preflight",
            schedule_now=controlled_now,
        )
        worker = threading.Thread(
            target=(
                _run_douyin_commerce_batch_publish
                if runtime_mode == "publish"
                else _run_douyin_commerce_batch_preflight
            ),
            args=(task, [raw_batch]),
            kwargs={"prepared_batch": batch},
            daemon=True,
            name=f"oneclick-douyin-commerce-batch-{runtime_mode}-{task['id']}",
        )
        _active_threads[int(task["id"])] = worker
        worker.start()
        return task

    prepared = _validate_payloads(payloads)
    runtime_mode = str(prepared[0].get("runtimeMode") or "preflight")
    if any(str(item.get("runtimeMode") or "preflight") != runtime_mode for item in prepared):
        raise ValueError("同一任务不能混合预检与正式发布")
    is_publish = runtime_mode == "publish"
    is_draft = runtime_mode == "draft"
    task = task_service.create_pending_task(
        prepared,
        mode=(
            "oneclick_publish"
            if is_publish
            else "oneclick_draft"
            if is_draft
            else "oneclick_preflight"
        ),
    )
    worker = threading.Thread(
        target=_run_publish if is_publish else _run_draft if is_draft else _run_preflight,
        args=(task, prepared),
        daemon=True,
        name=(
            f"oneclick-{'publish' if is_publish else 'draft' if is_draft else 'preflight'}-"
            f"{task['id']}"
        ),
    )
    _active_threads[int(task["id"])] = worker
    worker.start()
    return task


def is_task_running(task_id: int) -> bool:
    worker = _active_threads.get(int(task_id))
    return bool(worker and worker.is_alive())
