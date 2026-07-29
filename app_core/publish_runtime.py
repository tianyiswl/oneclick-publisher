# -*- coding: utf-8 -*-
"""桌面端发布运行时。

这里保留原项目已经验证过的平台上传函数，供 PyQt6 桌面端直接调用。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from myUtils.auth import check_cookie
from myUtils.postVideo import (
    post_video_DouYin,
    post_video_batch_draft_tabs,
    post_video_batch_dry_run_tabs,
    post_video_batch_tabs,
    post_video_bilibili,
    post_video_facebook,
    post_video_instagram,
    post_video_ks,
    post_video_tiktok,
    post_video_tencent,
    post_video_xhs,
    post_video_youtube,
)
from utils.publish_limits import get_publish_tag_limit, normalize_publish_tags
from utils.publish_observer import publish_context
from utils.publish_tasks import (
    complete_task,
    create_publish_task,
    fail_task,
    mark_platform_results,
    mark_task_running,
    record_task_event,
)

from .account_service import (
    DRAFT_SUPPORTED_PLATFORM_TYPES,
    DRAFT_UNSUPPORTED_PLATFORM_MESSAGES,
    PLATFORMS,
)
from .paths import COOKIE_DIR, VIDEO_DIR


# 公众号是国内第六个平台；图文、文字任务需要与其它国内平台一同排序。
PUBLISH_PLATFORM_ORDER = [3, 2, 5, 1, 4, 10, 6, 7, 8, 9]
PUBLISH_PLATFORM_ORDER_INDEX = {platform_type: index for index, platform_type in enumerate(PUBLISH_PLATFORM_ORDER)}
OVERSEAS_PLATFORM_TYPES = {6, 7, 8, 9}
OVERSEAS_PREFLIGHT_ONLY_MESSAGE = "海外平台当前仅开放预发布检查，正式发布按钮尚未解锁"
OVERSEAS_DRAFT_DISABLED_MESSAGE = "海外平台登录已暂缓，当前不执行平台草稿保存"
PUBLISH_RUNTIME_MODES = {"preflight", "draft", "publish"}


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def request_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def publish_runtime_mode(data: dict[str, Any]) -> str:
    mode = str(data.get("runtimeMode") or "").strip().lower()
    if mode in PUBLISH_RUNTIME_MODES:
        return mode
    if request_bool(data.get("saveDraftOnly"), False):
        return "draft"
    return "preflight" if request_bool(data.get("debugDryRun"), False) else "publish"


def publish_platform_sort_key(item: dict[str, Any]) -> int:
    if not isinstance(item, dict):
        return 999
    try:
        platform_type = int(item.get("type"))
    except (TypeError, ValueError):
        return 999
    return PUBLISH_PLATFORM_ORDER_INDEX.get(platform_type, 999)


def platform_draft_disabled_message(platform_type: int) -> str | None:
    """返回平台草稿禁用原因；当前仅视频号和 B 站开放此能力。"""

    if platform_type in DRAFT_SUPPORTED_PLATFORM_TYPES:
        return None
    if platform_type in OVERSEAS_PLATFORM_TYPES:
        return OVERSEAS_DRAFT_DISABLED_MESSAGE
    return DRAFT_UNSUPPORTED_PLATFORM_MESSAGES.get(
        platform_type,
        "当前平台尚未接入可验证的草稿保存；目前仅视频号和B站支持",
    )


def safe_storage_path(base_dir: Path, stored_name: str | None) -> Path | None:
    if not stored_name:
        return None
    try:
        candidate = (base_dir / Path(str(stored_name)).name).resolve()
        base = base_dir.resolve()
        if base == candidate or base in candidate.parents:
            return candidate
    except Exception:
        return None
    return None


def validate_publish_payload(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["发布参数格式错误"]

    try:
        platform_type = int(data.get("type"))
    except (TypeError, ValueError):
        platform_type = 0
    if platform_type not in PLATFORMS:
        errors.append("请选择有效的发布平台")
    runtime_mode = publish_runtime_mode(data)
    if runtime_mode == "draft":
        draft_disabled_message = platform_draft_disabled_message(platform_type)
        if draft_disabled_message:
            errors.append(draft_disabled_message)
    elif platform_type in OVERSEAS_PLATFORM_TYPES and runtime_mode == "publish":
        errors.append(OVERSEAS_PREFLIGHT_ONLY_MESSAGE)
    if runtime_mode == "draft" and request_bool(data.get("debugDryRun"), False):
        errors.append("保存平台草稿与预发布检查不能同时开启")
    if (
        runtime_mode == "draft"
        and platform_type not in {2, 5}
        and request_bool(data.get("enableTimer"), False)
    ):
        errors.append(
            "保存平台草稿不能设置定时发布；"
            "如需核对定时时间，请改用预发布检查或正式发布"
        )
    if (
        runtime_mode == "draft"
        and platform_type == 2
        and request_bool(data.get("enableTimer"), False)
    ):
        errors.append(
            "视频号设置定时发布后无法保存草稿，请取消视频号定时发布；"
            "如需保留定时设置，请改用预发布检查或正式发布"
        )
    if (
        runtime_mode == "draft"
        and platform_type == 5
        and request_bool(data.get("enableTimer"), False)
    ):
        errors.append(
            "B站平台草稿不会保留定时发布时间，请取消B站定时发布；"
            "如需保留定时设置，请改用预发布检查或正式发布"
        )

    if request_bool(data.get("enableTimer"), False):
        schedule_time = data.get("scheduleTime")
        if schedule_time:
            try:
                datetime.fromisoformat(str(schedule_time).replace("T", " "))
            except ValueError:
                errors.append("定时发布时间格式应为 YYYY-MM-DD HH:mm")
        else:
            try:
                jitter_minutes = int(data.get("timeJitterMinutes", 0) or 0)
                if jitter_minutes < 0 or jitter_minutes > 120:
                    errors.append("随机浮动需在 0-120 分钟之间")
            except (TypeError, ValueError):
                errors.append("随机浮动需在 0-120 分钟之间")

    visibility = str(data.get("visibility") or "public")
    if visibility not in {"public", "private", "unlisted"}:
        errors.append("可见范围设置不正确")
    if (
        visibility == "private"
        and platform_type != 7
        and runtime_mode == "publish"
    ):
        errors.append(f"{PLATFORMS.get(platform_type, '当前平台')}的私密发布尚未接入自动执行，请先使用预发布检查")

    title = (data.get("title") or data.get("biliTitle") or "").strip()
    if platform_type == 5 and not title:
        errors.append("请填写 B站稿件标题")
    if platform_type == 7 and not title:
        errors.append("请填写 YouTube 视频标题")

    file_list = data.get("fileList", [])
    if not isinstance(file_list, list) or not file_list:
        errors.append("请至少选择一个视频文件")
    else:
        for file_name in file_list:
            path = safe_storage_path(VIDEO_DIR, file_name)
            if not path or not path.exists():
                errors.append(f"视频文件不存在：{file_name}")

    account_list = data.get("accountList", [])
    if not isinstance(account_list, list) or not account_list:
        errors.append("请至少选择一个发布账号")
    else:
        for account_file in account_list:
            path = safe_storage_path(COOKIE_DIR, account_file)
            if not path or not path.exists():
                errors.append(f"账号登录文件不存在：{account_file}")

    cover_path = data.get("coverPath")
    if cover_path:
        path = safe_storage_path(VIDEO_DIR, cover_path)
        if not path or not path.exists():
            errors.append(f"封面文件不存在：{cover_path}")

    cover_paths = data.get("coverPaths")
    if cover_paths is not None:
        if not isinstance(cover_paths, dict):
            errors.append("封面规格数据格式不正确")
        else:
            for ratio, file_name in cover_paths.items():
                if not file_name:
                    continue
                path = safe_storage_path(VIDEO_DIR, file_name)
                if not path or not path.exists():
                    errors.append(f"{ratio} 封面文件不存在：{file_name}")

    return errors


async def check_publish_account_states(data_list: list[dict[str, Any]]) -> list[str]:
    from .database import connect

    account_items = []
    seen_keys = set()
    for data in data_list:
        if not isinstance(data, dict):
            continue
        if data.get("skipAccountCheck") in (1, "1", True, "true", "True", "yes"):
            continue
        try:
            platform_type = int(data.get("type"))
        except (TypeError, ValueError):
            continue
        for account_file in data.get("accountList") or []:
            key = (platform_type, account_file)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            account_items.append({"type": platform_type, "filePath": account_file})

    if not account_items:
        return []

    with connect() as conn:
        file_paths = [item["filePath"] for item in account_items]
        placeholders = ",".join(["?"] * len(file_paths))
        rows = conn.execute(
            f"""
            SELECT id, type, filePath, userName, status, profileName
            FROM user_info
            WHERE filePath IN ({placeholders})
            """,
            file_paths,
        ).fetchall()
        rows_by_key = {(int(row["type"]), row["filePath"]): row for row in rows}

    failures: list[str] = []
    status_updates: list[tuple[int, str, int]] = []

    for item in account_items:
        platform_type = item["type"]
        file_path = item["filePath"]
        row = rows_by_key.get((platform_type, file_path))
        display_name = row["userName"] if row else file_path
        profile_name = row["profileName"] if row else None
        label_name = display_name or profile_name or file_path
        platform_name = PLATFORMS.get(platform_type, "未知平台")

        cookie_path = COOKIE_DIR / Path(file_path).name
        if not cookie_path.exists():
            failures.append(f"{platform_name}「{label_name}」登录文件不存在，请重新登录")
            if row:
                status_updates.append(
                    (0, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row["id"])
                )
            continue

        try:
            is_valid = await check_cookie(platform_type, file_path)
        except Exception:
            is_valid = False

        if row:
            status_updates.append(
                (
                    1 if is_valid else 0,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    row["id"],
                )
            )
        if not is_valid:
            failures.append(f"{platform_name}「{label_name}」登录已失效，请先重新登录后再发布")

    if status_updates:
        with connect() as conn:
            conn.executemany(
                "UPDATE user_info SET status = ?, lastCheckedAt = ? WHERE id = ?",
                status_updates,
            )
            conn.commit()

    return failures


def validate_publish_accounts_before_run(data_list: list[dict[str, Any]]) -> list[str]:
    failures = run_async(check_publish_account_states(data_list))
    if not failures:
        return []
    return ["发布前账号检查未通过"] + failures


def run_with_publish_context(
    task: dict[str, Any],
    mode: str,
    callback,
    *args,
    background_mode: bool = False,
    **kwargs,
):
    with publish_context(
        task_id=task["id"],
        task_no=task.get("taskNo"),
        mode=mode,
        background_mode=bool(background_mode),
    ):
        return callback(*args, **kwargs)


def execute_single_publish(data: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    platform_type = int(data.get("type"))
    runtime_mode = publish_runtime_mode(data)
    if platform_type in OVERSEAS_PLATFORM_TYPES and runtime_mode == "publish":
        fail_task(task["id"], OVERSEAS_PREFLIGHT_ONLY_MESSAGE)
        return {"code": 403, "msg": OVERSEAS_PREFLIGHT_ONLY_MESSAGE, "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}
    if runtime_mode == "draft":
        draft_disabled_message = platform_draft_disabled_message(platform_type)
        if draft_disabled_message:
            fail_task(task["id"], draft_disabled_message)
            return {"code": 403, "msg": draft_disabled_message, "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}
    mark_task_running(task["id"], "正在检查账号登录状态")
    account_errors = validate_publish_accounts_before_run([data])
    if account_errors:
        fail_task(task["id"], "；".join(account_errors))
        return {"code": 409, "msg": "；".join(account_errors), "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}
    record_task_event(task["id"], "account_check_ok", "账号登录状态检查通过")

    file_list = data.get("fileList", [])
    account_list = data.get("accountList", [])
    title = data.get("title") or data.get("biliTitle") or ""
    tags = normalize_publish_tags(data.get("tags"), max_count=get_publish_tag_limit(platform_type))
    category = data.get("category")
    enable_timer = data.get("enableTimer")
    cover_path = data.get("coverPath")
    cover_paths = data.get("coverPaths") if isinstance(data.get("coverPaths"), dict) else {}
    dry_run = request_bool(data.get("debugDryRun"), False)
    save_draft_only = runtime_mode == "draft"
    dry_run_hold_browser = request_bool(data.get("debugDryRunHoldBrowser"), True)
    background_mode = request_bool(data.get("backgroundMode"), True)
    videos_per_day = data.get("videosPerDay")
    daily_times = data.get("dailyTimes")
    start_days = data.get("startDays")
    jitter_minutes = data.get("timeJitterMinutes", 0)
    if category == 0:
        category = None

    execution_label = "后台" if background_mode else "前台"
    operation_label = {
        "preflight": "预发布检查",
        "draft": "平台草稿保存",
        "publish": "正式发布",
    }[runtime_mode]
    mark_task_running(task["id"], f"桌面端{execution_label}{operation_label}任务开始执行")
    record_task_event(
        task["id"],
        "browser_mode_selected",
        "发布流程将在无窗口后台运行" if background_mode else "发布浏览器将显示在前台",
    )
    try:
        if platform_type == 1:
            run_with_publish_context(task, "single", post_video_xhs, title, file_list, tags, account_list, category, enable_timer, videos_per_day, daily_times, start_days, cover_path=cover_path, cover_paths=cover_paths, schedule_time=data.get("scheduleTime"), jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, save_draft_only=save_draft_only, background_mode=background_mode)
        elif platform_type == 2:
            run_with_publish_context(task, "single", post_video_tencent, title, file_list, tags, account_list, category, enable_timer, videos_per_day, daily_times, start_days, cover_path=cover_path, cover_paths=cover_paths, schedule_time=data.get("scheduleTime"), jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, save_draft_only=save_draft_only, background_mode=background_mode)
        elif platform_type == 3:
            run_with_publish_context(task, "single", post_video_DouYin, title, file_list, tags, account_list, category, enable_timer, videos_per_day, daily_times, start_days, cover_path=cover_path, cover_paths=cover_paths, schedule_time=data.get("scheduleTime"), jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, save_draft_only=save_draft_only, background_mode=background_mode)
        elif platform_type == 4:
            run_with_publish_context(task, "single", post_video_ks, title, file_list, tags, account_list, category, enable_timer, videos_per_day, daily_times, start_days, cover_path=cover_path, cover_paths=cover_paths, schedule_time=data.get("scheduleTime"), jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, save_draft_only=save_draft_only, background_mode=background_mode)
        elif platform_type == 5:
            run_with_publish_context(task, "single", post_video_bilibili, title, file_list, tags, account_list, category, enable_timer, videos_per_day, daily_times, start_days, desc=data.get("biliDesc"), bili_type=data.get("biliType"), bili_partition=data.get("biliPartition"), cover_path=cover_path, cover_paths=cover_paths, schedule_time=data.get("scheduleTime"), jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, save_draft_only=save_draft_only, background_mode=background_mode)
            if save_draft_only and data.get("biliPartition"):
                record_task_event(
                    task["id"],
                    "bilibili_draft_partition_not_persisted",
                    "B站平台草稿已保存，但平台不会保留新版分区值；"
                    "重新打开可能显示“影视”。正式发布时桌面端会重新选择"
                    f"“{data.get('biliPartition')}”并校验后再投稿。",
                    level="warning",
                )
        elif platform_type == 6:
            run_with_publish_context(task, "single", post_video_tiktok, title, file_list, tags, account_list, category, enable_timer, videos_per_day, daily_times, start_days, description=data.get("description"), cover_path=cover_path, cover_paths=cover_paths, schedule_time=data.get("scheduleTime"), jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, background_mode=background_mode)
        elif platform_type == 7:
            run_with_publish_context(task, "single", post_video_youtube, title, file_list, tags, account_list, category, enable_timer, videos_per_day, daily_times, start_days, description=data.get("description"), cover_path=cover_path, cover_paths=cover_paths, schedule_time=data.get("scheduleTime"), jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, background_mode=background_mode)
        elif platform_type == 8:
            run_with_publish_context(task, "single", post_video_instagram, title, file_list, tags, account_list, category, enable_timer, videos_per_day, daily_times, start_days, description=data.get("description"), cover_path=cover_path, cover_paths=cover_paths, schedule_time=data.get("scheduleTime"), jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, background_mode=background_mode)
        elif platform_type == 9:
            run_with_publish_context(task, "single", post_video_facebook, title, file_list, tags, account_list, category, enable_timer, videos_per_day, daily_times, start_days, description=data.get("description"), cover_path=cover_path, cover_paths=cover_paths, schedule_time=data.get("scheduleTime"), jitter_minutes=jitter_minutes, dry_run=dry_run, dry_run_hold_browser=dry_run_hold_browser, background_mode=background_mode)
        else:
            fail_task(task["id"], "不支持的发布平台")
            return {"code": 400, "msg": "unsupported platform", "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}
    except Exception as exc:
        fail_task(task["id"], f"发布失败：{exc}")
        return {"code": 500, "msg": f"发布失败：{exc}", "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}

    complete_task(
        task["id"],
        {
            "preflight": "预发布检查完成",
            "draft": "平台草稿保存完成",
            "publish": "发布流程已完成",
        }[runtime_mode],
    )
    return {"code": 200, "msg": None, "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}


def execute_batch_publish(data_list: list[dict[str, Any]], task: dict[str, Any]) -> dict[str, Any]:
    formal_overseas = [
        data for data in data_list
        if int(data.get("type", 0) or 0) in OVERSEAS_PLATFORM_TYPES
        and publish_runtime_mode(data) == "publish"
    ]
    if formal_overseas:
        fail_task(task["id"], OVERSEAS_PREFLIGHT_ONLY_MESSAGE)
        return {"code": 403, "msg": OVERSEAS_PREFLIGHT_ONLY_MESSAGE, "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}
    draft_disabled_messages = list(
        dict.fromkeys(
            message
            for data in data_list
            if publish_runtime_mode(data) == "draft"
            for message in (
                platform_draft_disabled_message(
                    int(data.get("type", 0) or 0)
                ),
            )
            if message
        )
    )
    if draft_disabled_messages:
        message = "；".join(draft_disabled_messages)
        fail_task(task["id"], message)
        return {"code": 403, "msg": message, "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}
    mark_task_running(task["id"], "正在检查账号登录状态")
    account_errors = validate_publish_accounts_before_run(data_list)
    if account_errors:
        fail_task(task["id"], "；".join(account_errors))
        return {"code": 409, "msg": "；".join(account_errors), "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}
    record_task_event(task["id"], "account_check_ok", "账号登录状态检查通过")

    all_dry_run = all(request_bool(data.get("debugDryRun"), False) for data in data_list)
    all_save_draft = all(
        publish_runtime_mode(data) == "draft"
        for data in data_list
    )
    background_mode = all(request_bool(data.get("backgroundMode"), True) for data in data_list)
    execution_label = "后台" if background_mode else "前台"
    batch_label = (
        "预发布检查"
        if all_dry_run
        else "平台草稿保存"
        if all_save_draft
        else "正式发布"
    )
    mark_task_running(task["id"], f"桌面端{execution_label}批量{batch_label}任务开始执行")
    record_task_event(
        task["id"],
        "browser_mode_selected",
        "发布流程将在无窗口后台运行" if background_mode else "发布浏览器将显示在前台",
    )
    if all_dry_run:
        try:
            results = run_with_publish_context(
                task,
                "batch",
                post_video_batch_dry_run_tabs,
                data_list,
                background_mode=background_mode,
            )
        except Exception as exc:
            fail_task(task["id"], f"预发布检查失败：{exc}")
            return {"code": 500, "msg": f"预发布检查失败：{exc}", "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}
        mark_platform_results(task["id"], results, "预发布检查完成")
        return {"code": 200, "msg": None, "data": {"results": results, "taskId": task["id"], "taskNo": task["taskNo"]}}

    if all_save_draft:
        try:
            results = run_with_publish_context(
                task,
                "batch",
                post_video_batch_draft_tabs,
                data_list,
                background_mode=background_mode,
            )
        except Exception as exc:
            fail_task(task["id"], f"保存平台草稿失败：{exc}")
            return {"code": 500, "msg": f"保存平台草稿失败：{exc}", "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}
        mark_platform_results(task["id"], results, "平台草稿保存完成")
        return {"code": 200, "msg": None, "data": {"results": results, "taskId": task["id"], "taskNo": task["taskNo"]}}

    try:
        results = run_with_publish_context(
            task,
            "batch",
            post_video_batch_tabs,
            data_list,
            dry_run=False,
            background_mode=background_mode,
        )
    except Exception as exc:
        fail_task(task["id"], f"发布失败：{exc}")
        return {"code": 500, "msg": f"发布失败：{exc}", "data": {"taskId": task["id"], "taskNo": task["taskNo"]}}

    mark_platform_results(task["id"], results, "发布流程已完成")
    return {"code": 200, "msg": None, "data": {"results": results, "taskId": task["id"], "taskNo": task["taskNo"]}}
