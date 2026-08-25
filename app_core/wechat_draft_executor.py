# -*- coding: utf-8 -*-
"""公众号保存草稿执行器的硬边界。

本模块与正式发表执行器分离：它不导入正式发表策略，也不接受任何发表、
群发或定时字段。浏览器动作接入前，所有调用方必须先通过这里的载荷校验。
"""

from __future__ import annotations

from typing import Any


class WechatDraftError(RuntimeError):
    """公众号草稿任务没有满足只保存草稿的边界。"""


_FORBIDDEN_PUBLISH_KEYS = (
    "wechatGroupNotification",
    "enableTimer",
    "scheduleTime",
)


def validate_wechat_draft_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """拒绝任何可进入发表路径的公众号草稿载荷。"""
    checked = dict(payload)
    if int(checked.get("type") or 0) != 10:
        raise WechatDraftError("公众号草稿执行器只接受公众号账号")
    if checked.get("runtimeMode") != "wechat_draft":
        raise WechatDraftError("公众号草稿执行器只接受 runtimeMode=wechat_draft")
    if checked.get("debugDryRun") is not True:
        raise WechatDraftError("公众号草稿执行器要求 debugDryRun=true")
    if checked.get("wechatGroupNotification") is not False:
        raise WechatDraftError("公众号草稿不得开启群发通知")
    if checked.get("enableTimer") is not False:
        raise WechatDraftError("公众号草稿不得开启定时发表")
    if checked.get("scheduleTime") is not None:
        raise WechatDraftError("公众号草稿不得携带定时时间")
    if checked.get("originalDeclaration") is not True:
        raise WechatDraftError("公众号草稿必须显式声明原创状态")
    title = str(checked.get("title") or "").strip()
    if not title:
        raise WechatDraftError("公众号草稿标题不能为空")
    content_html = str(checked.get("contentHtml") or "").strip()
    if not content_html:
        raise WechatDraftError("公众号草稿缺少冻结 HTML 正文")
    for key in _FORBIDDEN_PUBLISH_KEYS:
        if key not in checked:
            raise WechatDraftError(f"公众号草稿必须显式关闭 {key}")
    return checked


def run_wechat_draft_sync(payload: dict[str, Any], *, task_id: int) -> dict[str, Any]:
    """同步入口预留给桌面任务；实际浏览器写入必须后续逐控件实现。"""
    del task_id
    validate_wechat_draft_payload(payload)
    raise WechatDraftError("公众号草稿浏览器执行器尚未接入")
