# -*- coding: utf-8 -*-
"""公众号保存草稿执行器的硬边界。

本模块与正式发表执行器分离：它不导入正式发表策略，也不接受任何发表、
群发或定时字段。浏览器动作接入前，所有调用方必须先通过这里的载荷校验。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import re
from typing import Any

from . import account_service, task_service
from .oneclick_preflight import (
    PreflightError,
    _account_for_payload,
    _storage_state,
    _wechat_preflight,
)
from .wechat_verification import (
    validate_qr_image_bytes,
    verification_broker,
)


class WechatDraftError(RuntimeError):
    """公众号草稿任务没有满足只保存草稿的边界。"""

    def __init__(self, message: str, *, error_code: str = "field_readback_mismatch") -> None:
        super().__init__(message)
        self.error_code = error_code


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


async def _draft_verification_state(page) -> dict[str, Any]:
    """只识别当前可见扫码控件，不读取或持久化二维码内容。"""

    return await page.evaluate(
        r"""() => {
          const visible = node => { const r = node.getBoundingClientRect();
            const s = getComputedStyle(node); return r.width > 0 && r.height > 0
              && s.display !== 'none' && s.visibility !== 'hidden'; };
          const nodes = Array.from(document.querySelectorAll(
            'img,canvas,svg,[class*="qr"],[id*="qr"],[class*="scan"],[id*="scan"]'
          )).filter(node => {
            if (!visible(node)) return false;
            const meta = [node.id || '', node.className || '',
              node.getAttribute?.('src') || '', node.getAttribute?.('alt') || '',
              node.getAttribute?.('aria-label') || ''].join(' ').toLowerCase();
            return /(qrcode|qr_code|qr-|_qr|扫码|二维码|scan)/.test(meta);
          });
          nodes.forEach((node, index) =>
            node.setAttribute('data-oneclick-wechat-draft-qr', String(index))
          );
          const containers = Array.from(document.querySelectorAll(
            '[role="dialog"],.weui-desktop-dialog,.weui-desktop-dialog__wrp'
          )).filter(visible);
          const text = containers.map(node => String(node.innerText || ''))
            .join(' ').replace(/\s+/g, ' ').trim();
          return {
            qrCount: nodes.length,
            qrText: ['扫码', '二维码', '微信扫一扫', '身份验证']
              .filter(marker => text.includes(marker)),
            textTail: text.slice(-500),
          };
        }"""
    )


async def _capture_draft_qr_image(page) -> bytes:
    candidates: list[tuple[int, bytes]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for selector in (
        "[data-oneclick-wechat-draft-qr] img",
        "[data-oneclick-wechat-draft-qr] canvas",
        "[data-oneclick-wechat-draft-qr] svg",
        "[data-oneclick-wechat-draft-qr] *",
        "[data-oneclick-wechat-draft-qr]",
        "img",
        "canvas",
        "svg",
    ):
        locator = page.locator(selector)
        for index in range(min(await locator.count(), 160)):
            node = locator.nth(index)
            try:
                if not await node.is_visible():
                    continue
                box = await node.bounding_box()
                if not box:
                    continue
                width = float(box.get("width") or 0)
                height = float(box.get("height") or 0)
                if min(width, height) < 120 or max(width, height) > 900:
                    continue
                if not 0.70 <= width / max(1.0, height) <= 1.42:
                    continue
                key = (
                    round(float(box.get("x") or 0)),
                    round(float(box.get("y") or 0)),
                    round(width),
                    round(height),
                )
                if key in seen:
                    continue
                seen.add(key)
                image = await node.screenshot(type="png")
                validate_qr_image_bytes(image)
                candidates.append((round(width * height), image))
            except Exception:
                continue
    if not candidates:
        raise WechatDraftError(
            "公众号保存草稿需要扫码，但没有找到可扫码二维码",
            error_code="login_required",
        )
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


async def _wait_for_draft_qr_image(page, *, timeout_seconds: float = 15) -> bytes:
    deadline = asyncio.get_running_loop().time() + float(timeout_seconds)
    last_error: WechatDraftError | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            return await _capture_draft_qr_image(page)
        except WechatDraftError as exc:
            last_error = exc
            await asyncio.sleep(0.4)
    raise last_error or WechatDraftError(
        "公众号保存草稿二维码加载超时",
        error_code="login_required",
    )


async def _handle_draft_qr_verification(page, task_id: int) -> None:
    """在原生窗口等待扫码，并在同一浏览器会话中继续保存草稿。"""

    qr_image = await _wait_for_draft_qr_image(page)
    loop = asyncio.get_running_loop()

    def refresh_qr() -> bytes:
        future = asyncio.run_coroutine_threadsafe(
            _wait_for_draft_qr_image(page, timeout_seconds=12),
            loop,
        )
        return future.result(timeout=15)

    def open_verification_page() -> None:
        future = asyncio.run_coroutine_threadsafe(page.bring_to_front(), loop)
        future.result(timeout=5)

    request_id = verification_broker.create(
        task_id=int(task_id),
        qr_image=qr_image,
        expires_in_seconds=120,
        refresh_callback=refresh_qr,
        open_page_callback=open_verification_page,
    )
    task_service.record_task_event(
        int(task_id),
        "wechat_verification_required",
        "公众号保存草稿需要微信验证，请在一键发客户端扫码",
        level="warning",
    )
    try:
        deadline = asyncio.get_running_loop().time() + 600
        while asyncio.get_running_loop().time() < deadline:
            state = verification_broker.snapshot(request_id)
            if state["state"] in {"cancelled", "failed"}:
                raise WechatDraftError(
                    str(state["message"]),
                    error_code="login_required",
                )
            await asyncio.sleep(0.8)
            try:
                page_state = await _draft_verification_state(page)
            except Exception:
                continue
            if page_state.get("qrCount") or page_state.get("qrText"):
                if "已扫码" in str(page_state.get("textTail") or ""):
                    verification_broker.mark_verifying(request_id)
                continue
            verification_broker.succeed(request_id)
            task_service.record_task_event(
                int(task_id),
                "wechat_verification_succeeded",
                "微信验证成功，继续同一公众号保存草稿任务",
            )
            await asyncio.sleep(1)
            return
        verification_broker.fail(request_id, "等待微信验证超时，保存草稿已安全停止")
        raise WechatDraftError(
            "等待微信验证超时，保存草稿已安全停止",
            error_code="login_required",
        )
    finally:
        verification_broker.clear(request_id)


async def _handle_draft_verification_if_present(page, task_id: int) -> None:
    """给平台少量时间呈现验证层；没有验证时继续草稿回读。"""

    for _ in range(12):
        await asyncio.sleep(0.25)
        try:
            state = await _draft_verification_state(page)
        except Exception:
            continue
        if state.get("qrCount") or state.get("qrText"):
            await _handle_draft_qr_verification(page, task_id)
            return


async def _open_draft_list(page) -> None:
    """保存后只点击唯一的草稿箱入口，不猜测后台 URL。"""
    current_url = str(getattr(page, "url", "") or "")
    if "action=list_card" in current_url or "action=list_ex" in current_url:
        return
    try:
        entry = await _unique_enabled_button(page, "草稿箱")
    except WechatDraftError as exc:
        raise WechatDraftError(
            "公众号草稿箱入口不是唯一可用控件",
            error_code="draft_readback_missing",
        ) from exc
    await entry.click(timeout=10_000)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception as exc:
        raise WechatDraftError(
            "公众号草稿列表未完成加载",
            error_code="draft_readback_missing",
        ) from exc


def _candidate_saved_at(candidate: dict[str, Any], started_at: datetime) -> datetime | None:
    raw = str(candidate.get("savedAt") or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and started_at.tzinfo is None:
                parsed = parsed.replace(tzinfo=None)
            elif parsed.tzinfo is None and started_at.tzinfo is not None:
                parsed = parsed.replace(tzinfo=started_at.tzinfo)
            return parsed
        except ValueError:
            pass
    text = " ".join(str(candidate.get("text") or "").split())
    if any(marker in text for marker in ("刚刚", "片刻前", "1分钟前")):
        return started_at
    time_match = re.search(r"(?:今天\s*)?(\d{1,2}):(\d{2})", text)
    if "昨天" not in text and time_match:
        return started_at.replace(
            hour=int(time_match.group(1)),
            minute=int(time_match.group(2)),
            second=0,
            microsecond=0,
        )
    date_match = re.search(
        r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?\s+(\d{1,2}):(\d{2})",
        text,
    )
    if date_match:
        try:
            return started_at.replace(
                year=int(date_match.group(1)),
                month=int(date_match.group(2)),
                day=int(date_match.group(3)),
                hour=int(date_match.group(4)),
                minute=int(date_match.group(5)),
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None
    return None


def _current_run_candidates(
    candidates: object,
    *,
    title: str,
    started_at: datetime,
) -> list[dict[str, Any]]:
    if not isinstance(candidates, list):
        return []
    earliest = started_at - timedelta(minutes=2)
    latest = started_at + timedelta(minutes=15)
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if " ".join(str(candidate.get("title") or "").split()) != title:
            continue
        if candidate.get("hasCover") is not True:
            continue
        saved_at = _candidate_saved_at(candidate, started_at)
        if saved_at is not None and earliest <= saved_at <= latest:
            matches.append(candidate)
    return matches


async def _readback_saved_draft(
    page,
    title: str,
    *,
    started_at: datetime,
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    """只把同标题且位于本次保存时间窗口的唯一草稿视为成功。"""
    deadline = asyncio.get_running_loop().time() + max(0.0, float(timeout_seconds))
    first_attempt = True
    while first_attempt or asyncio.get_running_loop().time() < deadline:
        first_attempt = False
        candidates = await page.evaluate(
            """expectedTitle => {
              const norm = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const visible = node => { const r = node.getBoundingClientRect();
                const s = getComputedStyle(node); return r.width > 0 && r.height > 0
                  && s.display !== 'none' && s.visibility !== 'hidden'; };
              const seen = new Set(); const rows = [];
              for (const node of document.querySelectorAll('a,p,span,div,h1,h2,h3')) {
                if (!visible(node) || norm(node.innerText) !== expectedTitle) continue;
                let parent = node; let found = null;
                for (let depth = 0; parent && depth < 8; depth += 1, parent = parent.parentElement) {
                  const text = norm(parent.innerText);
                  if (!text.includes('草稿')) continue;
                  const timeNode = parent.querySelector('time,[datetime],[data-time],[data-timestamp]');
                  found = {
                    title: expectedTitle,
                    text: text.slice(0, 500),
                    hasCover: Boolean(parent.querySelector('img')),
                    savedAt: timeNode?.getAttribute('datetime')
                      || timeNode?.getAttribute('data-time')
                      || timeNode?.getAttribute('data-timestamp') || '',
                  };
                  break;
                }
                if (found && !seen.has(found.text)) { seen.add(found.text); rows.push(found); }
              }
              return rows;
            }""",
            title,
        )
        matches = _current_run_candidates(
            candidates,
            title=title,
            started_at=started_at,
        )
        if len(matches) == 1:
            return {
                "ok": True,
                "message": "公众号草稿已由草稿列表回读",
                "draftTitle": title,
                "errorCode": None,
            }
        if len(matches) > 1:
            return {"ok": False, "message": "公众号草稿列表出现多个同标题记录", "draftTitle": None, "errorCode": "draft_readback_ambiguous"}
        if asyncio.get_running_loop().time() < deadline:
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
                raise WechatDraftError(
                    "公众号编辑器账号回读不一致",
                    error_code="account_mismatch",
                )
            started_at = datetime.now()
            try:
                save_button = await _unique_enabled_button(page, "保存草稿")
            except WechatDraftError as exc:
                raise WechatDraftError(
                    "公众号保存草稿控件不是唯一可用控件",
                    error_code="save_control_ambiguous",
                ) from exc
            await save_button.click(timeout=10_000)
            await _handle_draft_verification_if_present(page, task_id)
            await _open_draft_list(page)
            list_account = await account_service._detect_display_name(page, 10)
            if list_account != expected_account:
                raise WechatDraftError(
                    "公众号草稿列表账号回读不一致",
                    error_code="account_mismatch",
                )
            result = await _readback_saved_draft(
                page,
                checked["title"].strip(),
                started_at=started_at,
            )
            task_service.record_task_event(task_id, "wechat_draft_readback", result["message"], level="info" if result["ok"] else "warning")
            return result
        except PreflightError as exc:
            raise WechatDraftError(str(exc)) from exc
        finally:
            await context.close()
            await browser.close()
