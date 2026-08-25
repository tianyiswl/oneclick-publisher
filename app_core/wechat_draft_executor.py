# -*- coding: utf-8 -*-
"""公众号保存草稿执行器的硬边界。

本模块与正式发表执行器分离：它不导入正式发表策略，也不接受任何发表、
群发或定时字段。浏览器动作接入前，所有调用方必须先通过这里的载荷校验。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from . import account_service, task_service
from .oneclick_preflight import (
    PreflightError,
    _account_for_payload,
    _storage_state,
    _wechat_preflight,
)


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
    """桌面任务线程使用的同步入口。"""
    return asyncio.run(run_wechat_draft(dict(payload), task_id=int(task_id)))


async def _unique_enabled_button(page, text: str):
    locator = page.get_by_text(text, exact=True)
    visible = []
    for index in range(await locator.count()):
        node = locator.nth(index)
        if await node.is_visible() and await node.is_enabled():
            visible.append(node)
    if len(visible) != 1:
        raise WechatDraftError(f"{text} 不是唯一可用控件")
    return visible[0]


async def _readback_saved_draft(page, title: str, *, started_at: datetime) -> dict[str, Any]:
    """只把同标题且位于草稿上下文中的唯一可见记录视为成功。"""
    del started_at
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        matches = await page.evaluate(
            """expectedTitle => {
              const norm = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const visible = node => { const r = node.getBoundingClientRect();
                const s = getComputedStyle(node); return r.width > 0 && r.height > 0
                  && s.display !== 'none' && s.visibility !== 'hidden'; };
              const seen = new Set(); const rows = [];
              for (const node of document.querySelectorAll('a,p,span,div,h1,h2,h3')) {
                if (!visible(node) || norm(node.innerText) !== expectedTitle) continue;
                let parent = node; let found = '';
                for (let depth = 0; parent && depth < 8; depth += 1, parent = parent.parentElement) {
                  const text = norm(parent.innerText); if (text.includes('草稿')) { found = text; break; }
                }
                if (found && !seen.has(found)) { seen.add(found); rows.push(found.slice(0, 500)); }
              }
              return rows;
            }""",
            title,
        )
        if len(matches) == 1:
            return {"ok": True, "message": "公众号草稿已由草稿列表回读", "draftTitle": title, "errorCode": None}
        if len(matches) > 1:
            return {"ok": False, "message": "公众号草稿列表出现多个同标题记录", "draftTitle": None, "errorCode": "draft_readback_ambiguous"}
        await asyncio.sleep(0.5)
    return {"ok": False, "message": "公众号草稿列表未回读到当前标题", "draftTitle": None, "errorCode": "draft_readback_missing"}


async def run_wechat_draft(payload: dict[str, Any], *, task_id: int) -> dict[str, Any]:
    """填写编辑器、只点击保存草稿，并回读草稿列表。"""
    checked = validate_wechat_draft_payload(payload)
    account = _account_for_payload(checked)
    if int(account.get("type") or 0) != 10:
        raise WechatDraftError("公众号草稿执行器只接受公众号账号")
    expected_account = str(account.get("profileName") or account.get("userName") or "")
    if not expected_account:
        raise WechatDraftError("公众号账号显示名为空")
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=bool(checked.get("backgroundMode", False)))
        context = await browser.new_context(storage_state=str(_storage_state(account)), viewport={"width": 1440, "height": 1000})
        page = await context.new_page()
        try:
            preflight_payload = dict(checked)
            preflight_payload["runtimeMode"] = "preflight"
            await _wechat_preflight(page, preflight_payload, account=account)
            editor_account = await account_service._detect_display_name(page, 10)
            if editor_account != expected_account:
                raise WechatDraftError("公众号编辑器账号回读不一致")
            await (await _unique_enabled_button(page, "保存草稿")).click(timeout=10_000)
            result = await _readback_saved_draft(page, checked["title"].strip(), started_at=datetime.now())
            task_service.record_task_event(task_id, "wechat_draft_readback", result["message"], level="info" if result["ok"] else "warning")
            return result
        except PreflightError as exc:
            raise WechatDraftError(str(exc)) from exc
        finally:
            await context.close()
            await browser.close()
