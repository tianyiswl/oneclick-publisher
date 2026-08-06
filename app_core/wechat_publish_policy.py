# -*- coding: utf-8 -*-
"""公众号正式发表阶段的安全决策规则。

只允许内容包已显式标记、且平台弹窗仅声明相符 AI 来源时继续。所有其他
弹窗、二维码、风控、群发或定时分支都交由用户处理。
"""

from __future__ import annotations

from datetime import datetime, tzinfo
import os
from pathlib import Path
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_AI_DIALOG_TITLE = "创作来源声明提醒"
_AI_IMAGE_DECLARATION = "以下内容含有AI生成图片，平台将自动展示AI声明"
_ALLOWED_AI_DIALOG_ACTIONS = {"查看", "继续发表"}
_GROUP_SCOPE_TITLE = "发表"
_GROUP_SCOPE_BODY = (
    "已开启群发通知"
    "开启群发通知的内容将展示在用户的公众号列表和公众号主页。"
    "若允许平台推荐，内容有可能被推荐至看一看或其他推荐场景。"
)
_GROUP_SCOPE_ACTIONS = {"查看详情", "继续发表", "取消"}
_FORBIDDEN_SPECIFIC_MARKERS = {
    "国内外时事",
    "公共政策",
    "社会事件",
    "风控",
    "违规",
    "安全验证",
    "管理员验证",
    "定时",
    "群发",
}


def _normalized_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _unique_semantic_dialogs(page_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    dialogs: list[dict[str, Any]] = []
    seen_dialogs: set[tuple[str, tuple[str, ...]]] = set()
    for raw_dialog in page_state.get("dialogs") or []:
        dialog = dict(raw_dialog or {})
        signature = (
            _normalized_text(dialog.get("text")),
            tuple(
                sorted(
                    _normalized_text(item)
                    for item in dialog.get("buttons") or []
                    if _normalized_text(item)
                )
            ),
        )
        if signature in seen_dialogs:
            continue
        seen_dialogs.add(signature)
        dialogs.append(dialog)
    return dialogs


def decide_ai_source_declaration(
    ai_disclosure: Mapping[str, Any] | None,
    page_state: Mapping[str, Any],
) -> dict[str, Any]:
    """判断当前页面是否只有与内容包相符的 AI 来源自动声明。"""

    policy = dict(ai_disclosure or {})
    if not policy.get("containsAiGeneratedContent"):
        return {"allowed": False, "reason": "内容包未明确标记含 AI 生成内容"}
    if not policy.get("allowPlatformAutoDeclaration"):
        return {"allowed": False, "reason": "内容包未允许平台自动展示 AI 来源声明"}
    kinds = {str(item).strip().lower() for item in policy.get("contentKinds") or []}
    if kinds != {"image"}:
        return {
            "allowed": False,
            "reason": "当前仅允许与 AI 生成图片完全匹配的自动声明",
        }
    if not policy.get("assetPaths"):
        return {"allowed": False, "reason": "内容包未列出 AI 图片素材"}
    if page_state.get("qrElements") or page_state.get("qrText"):
        return {"allowed": False, "reason": "页面出现二维码或身份验证提示"}
    if page_state.get("decisionMarkers"):
        return {"allowed": False, "reason": "页面出现其他确认、风控、定时或群发分支"}

    # 公众号会同时暴露 dialog 外层 wrapper 和内层主体；内容及操作完全
    # 相同时是同一个视觉弹窗，不应误判成两个独立确认。
    dialogs = _unique_semantic_dialogs(page_state)
    if len(dialogs) != 1:
        return {
            "allowed": False,
            "reason": f"平台可见弹窗数量不是 1（实际 {len(dialogs)}）",
        }
    dialog = dialogs[0]
    text = _normalized_text(dialog.get("text"))
    if _AI_DIALOG_TITLE not in text:
        return {"allowed": False, "reason": "当前弹窗不是创作来源声明提醒"}

    # 平台说明会概述时事/公共政策等规则；只检查“以下内容…”后的实际命中项。
    if "以下内容" not in text:
        return {"allowed": False, "reason": "平台声明缺少可核验的实际命中项"}
    specific = text.split("以下内容", 1)[1]
    for marker in _FORBIDDEN_SPECIFIC_MARKERS:
        if marker in specific:
            return {
                "allowed": False,
                "reason": f"实际命中项还包含非 AI 声明：{marker}",
            }
    if _AI_IMAGE_DECLARATION not in text:
        return {"allowed": False, "reason": "平台声明与 AI 图片元数据不匹配"}

    actions = {
        _normalized_text(item)
        for item in dialog.get("buttons") or []
        if _normalized_text(item)
    }
    if "继续发表" not in actions:
        return {"allowed": False, "reason": "AI 声明弹窗缺少继续发表控件"}
    unexpected_actions = actions - _ALLOWED_AI_DIALOG_ACTIONS
    if unexpected_actions:
        return {
            "allowed": False,
            "reason": "AI 声明弹窗包含未授权操作：" + "、".join(sorted(unexpected_actions)),
        }
    return {
        "allowed": True,
        "reason": "平台仅声明与内容包相符的 AI 生成图片来源",
        "expectedAction": "继续发表",
    }


def decide_group_notification_scope_confirmation(
    payload: Mapping[str, Any],
    page_state: Mapping[str, Any],
) -> dict[str, Any]:
    """仅自动接受用户已确认的群发展示范围/推荐场景说明。"""

    try:
        preferences = normalize_wechat_publish_preferences(payload)
    except ValueError as exc:
        return {"allowed": False, "reason": str(exc)}
    if not preferences["groupNotification"]:
        return {"allowed": False, "reason": "用户未开启群发通知"}
    if page_state.get("qrElements") or page_state.get("qrText") or page_state.get("qrCount"):
        return {"allowed": False, "reason": "页面出现二维码或身份验证提示"}
    dialogs = _unique_semantic_dialogs(page_state)
    if len(dialogs) != 1:
        return {
            "allowed": False,
            "reason": f"平台可见弹窗数量不是 1（实际 {len(dialogs)}）",
        }
    dialog = dialogs[0]
    title = _normalized_text(dialog.get("title"))
    body = _normalized_text(dialog.get("body") or dialog.get("text"))
    if title != _normalized_text(_GROUP_SCOPE_TITLE):
        return {"allowed": False, "reason": "当前弹窗标题不是已确认的群发提示"}
    if body != _normalized_text(_GROUP_SCOPE_BODY):
        return {"allowed": False, "reason": "群发展示范围提示文案与用户确认版本不一致"}
    forbidden = (
        "风控",
        "违规",
        "法律",
        "协议",
        "公共政策",
        "时事",
        "社会事件",
        "账号设置",
        "管理员验证",
        "安全验证",
    )
    if any(marker in body for marker in forbidden):
        return {"allowed": False, "reason": "群发提示还包含其他合规或风控内容"}
    actions = {
        _normalized_text(item)
        for item in dialog.get("buttons") or []
        if _normalized_text(item)
    }
    if actions != {_normalized_text(item) for item in _GROUP_SCOPE_ACTIONS}:
        return {"allowed": False, "reason": "群发提示按钮与用户确认版本不一致"}
    return {
        "allowed": True,
        "reason": "精确匹配用户已确认的群发展示范围与推荐场景说明",
        "expectedAction": "继续发表",
    }


def build_publish_execution_record(
    *,
    account_id: int,
    profile_name: str,
    storage_state: str | Path,
    context_type: str,
    editor_account_readback: str,
) -> dict[str, Any]:
    """生成不包含 Cookie、令牌或浏览器凭据的执行记录。"""

    state_path = Path(storage_state)
    return {
        "accountId": int(account_id),
        "profileName": str(profile_name),
        "sessionSource": state_path.name,
        "sessionSourceMtime": (
            int(state_path.stat().st_mtime) if state_path.is_file() else None
        ),
        "contextType": str(context_type),
        "editorAccountReadback": str(editor_account_readback),
    }


def local_timezone() -> tzinfo:
    """返回用户本机时区；优先保留 IANA 时区以正确处理未来夏令时。"""

    timezone_name = str(os.environ.get("TZ") or "").strip()
    if not timezone_name:
        try:
            localtime = Path("/etc/localtime").resolve()
            marker = "/zoneinfo/"
            if marker in str(localtime):
                timezone_name = str(localtime).split(marker, 1)[1]
        except OSError:
            timezone_name = ""
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


def local_timezone_name(timezone: tzinfo | None = None) -> str:
    """返回可随载荷审计、但不含凭据的本机时区名称。"""

    current = timezone or local_timezone()
    return str(getattr(current, "key", "") or current.tzname(datetime.now()) or "local")


def _strict_optional_bool(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是明确的布尔值")
    return value


def normalize_wechat_publish_preferences(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
    timezone: tzinfo | None = None,
) -> dict[str, Any]:
    """把一键发公众号设置标准化为唯一、可回读的发布意图。

    - 群发通知缺省开启，只有显式 ``false`` 才关闭。
    - 分组通知不由一键发独立猜选，遵从公众号对群发通知的依赖状态。
    - 只有 ``enableTimer=true`` 且给出未来时间时才进入定时发表。
    - 原创必须是明确布尔值，未勾选时作者流程完全跳过。
    """

    current_timezone = timezone or local_timezone()
    current_time = now or datetime.now(current_timezone)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=current_timezone)
    else:
        current_time = current_time.astimezone(current_timezone)

    group_notification = _strict_optional_bool(
        payload,
        "wechatGroupNotification",
        default=True,
    )
    original = _strict_optional_bool(
        payload,
        "originalDeclaration",
        default=False,
    )
    enable_timer = _strict_optional_bool(payload, "enableTimer", default=False)
    raw_schedule = str(payload.get("scheduleTime") or "").strip()
    if not enable_timer and raw_schedule:
        raise ValueError("未启用定时发表时不得携带 scheduleTime")

    schedule_at: datetime | None = None
    if enable_timer:
        if not raw_schedule:
            raise ValueError("已启用定时发表，但没有设置发布时间")
        try:
            schedule_at = datetime.fromisoformat(raw_schedule.replace("T", " "))
        except ValueError as exc:
            raise ValueError("公众号定时发表时间格式不正确") from exc
        if schedule_at.tzinfo is None:
            schedule_at = schedule_at.replace(tzinfo=current_timezone)
        else:
            schedule_at = schedule_at.astimezone(current_timezone)
        schedule_at = schedule_at.replace(second=0, microsecond=0)
        if schedule_at <= current_time:
            raise ValueError("公众号定时发表时间必须晚于当前本地时间")

    return {
        "groupNotification": group_notification,
        "groupedNotificationPolicy": (
            "platform-dependent" if group_notification else "disabled-by-parent"
        ),
        "scheduledPublish": schedule_at is not None,
        "publishMode": "scheduled" if schedule_at else "immediate",
        "scheduleLocal": (
            schedule_at.strftime("%Y-%m-%d %H:%M") if schedule_at else None
        ),
        "scheduleIso": schedule_at.isoformat(timespec="minutes") if schedule_at else None,
        "timezone": local_timezone_name(current_timezone),
        "originalDeclaration": original,
        "authorPolicy": "first-available" if original else "skip-completely",
    }


def _readback_option(
    options: Mapping[str, Any],
    name: str,
) -> tuple[bool, Mapping[str, Any] | None]:
    item = options.get(name)
    return isinstance(item, Mapping) and item.get("available") is True, (
        item if isinstance(item, Mapping) else None
    )


def decide_wechat_publish_options(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    now: datetime | None = None,
    timezone: tzinfo | None = None,
) -> dict[str, Any]:
    """核对群发、定时和最终按钮；任一回读不一致都安全停止。"""

    try:
        expected = normalize_wechat_publish_preferences(
            payload,
            now=now,
            timezone=timezone,
        )
    except ValueError as exc:
        return {"allowed": False, "reason": str(exc)}

    options = snapshot.get("options")
    if not isinstance(options, Mapping):
        return {"allowed": False, "reason": "发布方式状态无法回读"}

    for name, expected_enabled in (
        ("groupNotification", expected["groupNotification"]),
        ("scheduledPublish", expected["scheduledPublish"]),
    ):
        available, item = _readback_option(options, name)
        if not available or item is None:
            return {"allowed": False, "reason": f"发布选项无法唯一识别：{name}"}
        if item.get("enabled") is not expected_enabled:
            return {
                "allowed": False,
                "reason": f"发布选项回读不一致：{name}",
            }

    grouped = options.get("groupedNotification")
    if not isinstance(grouped, Mapping):
        return {"allowed": False, "reason": "分组通知状态无法回读"}
    if (
        not expected["groupNotification"]
        and grouped.get("available") is True
        and grouped.get("enabled") is not False
    ):
        return {"allowed": False, "reason": "关闭群发后分组通知仍处于开启状态"}
    if expected["scheduledPublish"]:
        actual_schedule = str(
            options["scheduledPublish"].get("time")
            or snapshot.get("scheduledTime")
            or ""
        ).strip()
        try:
            actual_preferences = normalize_wechat_publish_preferences(
                {
                    "enableTimer": True,
                    "scheduleTime": actual_schedule,
                    "wechatGroupNotification": expected["groupNotification"],
                },
                now=now,
                timezone=timezone,
            )
        except ValueError:
            return {"allowed": False, "reason": "平台定时发表时间无法回读"}
        if actual_preferences["scheduleIso"] != expected["scheduleIso"]:
            return {
                "allowed": False,
                "reason": "平台定时发表时间与用户设置不一致",
            }

    if int(snapshot.get("publishButtonCount") or 0) != 1:
        return {"allowed": False, "reason": "最终发表按钮不是唯一可见控件"}
    actions = {
        _normalized_text(item)
        for item in snapshot.get("buttons") or []
        if _normalized_text(item)
    }
    if actions != {"发表", "取消"}:
        return {"allowed": False, "reason": "最终弹窗包含未授权操作"}
    return {
        "allowed": True,
        "reason": (
            f"群发通知已{'开启' if expected['groupNotification'] else '关闭'}，"
            + (
                f"定时发表时间已回读为 {expected['scheduleLocal']}"
                if expected["scheduledPublish"]
                else "未设置定时，保持立即发表"
            )
        ),
        "expectedAction": "发表",
        "preferences": expected,
    }


def decide_immediate_publish_options(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """兼容旧调用：按新默认值核对“群发开启、立即发表”。"""

    return decide_wechat_publish_options({}, snapshot)


def decide_wechat_author_readback(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """核对原创与作者联动，未勾选原创时不允许触碰作者控件。"""

    try:
        expected = normalize_wechat_publish_preferences(payload)
    except ValueError as exc:
        return {"allowed": False, "reason": str(exc)}
    if not expected["originalDeclaration"]:
        if snapshot.get("authorControlTouched") is not False:
            return {"allowed": False, "reason": "未勾选原创，但作者控件被读取或操作"}
        return {
            "allowed": True,
            "reason": "未勾选原创，作者流程已完全跳过",
            "authorPolicy": "skip-completely",
        }

    if snapshot.get("authorControlTouched") is not True:
        return {"allowed": False, "reason": "已勾选原创，但作者控件未打开"}
    first_name = str(snapshot.get("firstAvailableName") or "").strip()
    selected_name = str(snapshot.get("selectedName") or "").strip()
    if not first_name:
        return {"allowed": False, "reason": "当前公众号账号没有可用作者"}
    if selected_name != first_name:
        return {"allowed": False, "reason": "作者回读与第一个可用作者不一致"}
    return {
        "allowed": True,
        "reason": f"原创作者已选择并回读：{selected_name}",
        "authorPolicy": "first-available",
        "authorName": selected_name,
    }


def decide_no_group_notification_confirmation(
    page_state: Mapping[str, Any],
) -> dict[str, Any]:
    """仅匹配用户已明确接受的“未开启群发通知”二次提示。"""

    if page_state.get("qrElements") or page_state.get("qrText"):
        return {"allowed": False, "reason": "页面出现二维码或身份验证提示"}
    if page_state.get("decisionMarkers"):
        return {"allowed": False, "reason": "页面出现其他确认、风控、定时或群发分支"}
    dialogs = _unique_semantic_dialogs(page_state)
    if len(dialogs) != 1:
        return {
            "allowed": False,
            "reason": f"平台可见弹窗数量不是 1（实际 {len(dialogs)}）",
        }
    dialog = dialogs[0]
    text = _normalized_text(dialog.get("text"))
    required = (
        "未开启群发通知",
        "内容将展示在公众号主页",
        "有可能被推荐至看一看或其他推荐场景",
    )
    if not all(marker in text for marker in required):
        return {"allowed": False, "reason": "当前弹窗不是已授权的未群发提示"}
    forbidden = (
        "管理员验证",
        "安全验证",
        "风险提示",
        "风控",
        "二维码",
        "扫码",
        "确认群发",
        "定时发布",
    )
    if any(marker in text for marker in forbidden):
        return {"allowed": False, "reason": "未群发提示同时包含其他阻断信息"}
    actions = {
        _normalized_text(item)
        for item in dialog.get("buttons") or []
        if _normalized_text(item)
    }
    if actions != {"查看详情", "继续发表", "取消"}:
        return {"allowed": False, "reason": "未群发提示包含未授权操作"}
    return {
        "allowed": True,
        "reason": "精确匹配用户已接受的未开启群发通知提示",
        "expectedAction": "继续发表",
    }
