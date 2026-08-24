# -*- coding: utf-8 -*-
"""一键发内置的小红书图文/视频正式执行器。

本模块只处理已由用户明确授权的正式任务。账号会话、素材、字段、立即/定时发布
以及平台回执均在一键发进程内完成；不启动蚁小二客户端、不调用 yxer、蚁小二
网关或远程签名服务。只有读到小红书成功页或明确成功提示后才返回成功。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import re

from . import account_service, oneclick_preflight, task_service
from .xhs_native_adapter import XhsNativeAdapter, build_native_contract


class XhsPublishError(RuntimeError):
    """小红书正式任务未获得可验证平台回执。"""


def _normalized(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", "").split())


def _schedule_time(payload: dict) -> datetime | None:
    if payload.get("enableTimer") is not True:
        if str(payload.get("scheduleTime") or "").strip():
            raise XhsPublishError("未开启定时发布，但载荷中存在定时时间")
        return None
    raw = str(payload.get("scheduleTime") or "").strip().replace("T", " ")
    try:
        target = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise XhsPublishError("小红书定时发布时间必须为 YYYY-MM-DD HH:mm") from exc
    if target <= datetime.now():
        raise XhsPublishError("小红书定时发布时间必须晚于当前本地时间")
    return target


def _validate_payload(payload: dict) -> None:
    if int(payload.get("type") or 0) != 1:
        raise XhsPublishError("小红书执行器只接受平台类型 1")
    if str(payload.get("contentType") or "") not in {"article", "video"}:
        raise XhsPublishError("小红书正式执行器只支持图文或视频")
    if str(payload.get("runtimeMode") or "") != "publish":
        raise XhsPublishError("小红书正式执行器要求 runtimeMode=publish")
    if payload.get("debugDryRun") is not False:
        raise XhsPublishError("小红书正式执行器要求 debugDryRun=false")
    try:
        build_native_contract(payload)
    except Exception as exc:
        raise XhsPublishError(str(exc)) from exc
    _validate_declaration_policy(payload)
    _schedule_time(payload)


def _validate_declaration_policy(payload: dict) -> None:
    """声明只能来自内容包明确授权或用户亲自确认。"""

    if not isinstance(payload.get("originalDeclaration", False), bool):
        raise XhsPublishError("小红书原创声明必须是明确布尔值")

    disclosure = payload.get("aiDisclosure") or {}
    if not isinstance(disclosure, dict):
        raise XhsPublishError("小红书 AI 声明元数据必须是对象")
    contains_ai = disclosure.get("containsAiGeneratedContent") is True
    allows_auto = disclosure.get("allowPlatformAutoDeclaration") is True
    enabled = payload.get("aiGenerated") is True
    explicitly_confirmed = (
        payload.get("aiDeclarationExplicitlyConfirmed") is True
    )

    if allows_auto and not contains_ai:
        raise XhsPublishError("AI 声明授权与内容依据不一致，已安全停止")
    if contains_ai and not enabled:
        raise XhsPublishError(
            "内容包明确含 AI 内容，但尚未确认小红书 AI 声明"
        )
    if enabled and not (allows_auto or explicitly_confirmed):
        raise XhsPublishError(
            "内容包未允许自动 AI 声明，且用户未在本次任务中明确确认"
        )


def _identity_name_from_response(value: object) -> str | None:
    """从小红书官方身份回执中提取昵称，不保留回执原文。"""

    wanted = {"nickname", "nick_name", "user_name", "username", "account_name", "name"}
    if isinstance(value, dict):
        for key, candidate in value.items():
            if str(key).lower() in wanted and _normalized(candidate):
                text = _normalized(candidate)
                if 2 <= len(text) <= 40:
                    return text
        for candidate in value.values():
            found = _identity_name_from_response(candidate)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _identity_name_from_response(candidate)
            if found:
                return found
    return None


async def _detect_xhs_account(page) -> str | None:
    """优先读取官方身份接口，页面身份节点只作为兼容兜底。"""

    try:
        data = await page.evaluate(
            """async () => {
                const response = await fetch(
                  '/api/galaxy/creator/home/personal_info',
                  { credentials: 'include' }
                );
                if (!response.ok) return null;
                return await response.json();
            }"""
        )
        identity = _identity_name_from_response(data)
        if identity:
            return identity
    except Exception:
        pass
    return await account_service._detect_display_name(page, 1)


def _publish_button_label(info: object) -> str:
    """兼容普通按钮文本和新版 xhs-publish-btn 的 submit-text 属性。"""

    if not isinstance(info, dict):
        return ""
    text = _normalized(info.get("text"))
    attrs = info.get("attrs")
    submit_text = (
        _normalized(attrs.get("submitText"))
        if isinstance(attrs, dict)
        else ""
    )
    return submit_text or text


async def _fill_article(page, payload: dict) -> dict:
    files = [Path(str(item)).resolve() for item in payload.get("fileList") or []]
    title = " ".join(str(payload.get("title") or "").split())[:20]
    description = str(payload.get("description") or "").strip()
    tags = [
        str(tag).strip().lstrip("#")
        for tag in payload.get("tags") or []
        if str(tag).strip().lstrip("#")
    ]
    await page.goto(
        oneclick_preflight._XHS_PUBLISH_URL,
        wait_until="domcontentloaded",
        timeout=45_000,
    )
    await page.wait_for_timeout(900)
    tab = page.locator(".creator-tab[data-hp-kind='creator-tab-上传图文']").first
    if not await tab.count():
        tab = page.locator(".creator-tab").filter(has_text="上传图文").last
    await tab.click(timeout=8_000, force=True)
    await page.wait_for_timeout(600)
    upload = page.locator("input[type=file]").first
    await upload.set_input_files([str(path) for path in files])
    await page.wait_for_timeout(7_000)

    title_input = page.locator('input[placeholder="填写标题会有更多赞哦"]').first
    await title_input.wait_for(state="visible", timeout=15_000)
    await oneclick_preflight._set_dom_value(title_input, title)
    editor = page.locator(".tiptap.ProseMirror").first
    await editor.wait_for(state="visible", timeout=8_000)
    await editor.evaluate(
        """(element, value) => {
            element.textContent = value;
            element.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                inputType: 'insertText',
                data: value,
            }));
        }""",
        description,
    )
    title_readback = await title_input.input_value()
    body_readback = await editor.inner_text()
    if _normalized(title_readback) != _normalized(title):
        raise XhsPublishError("小红书标题写入后回读不一致")
    if _normalized(description) not in _normalized(body_readback):
        raise XhsPublishError("小红书正文写入后回读不一致")
    return {
        "title": title_readback.strip(),
        "body": body_readback.strip(),
        "imageCount": len(files),
        "tags": tags,
    }


async def _wait_for_platform_result(
    page,
    *,
    task_id: int,
    schedule_text: str | None,
    scheduled: bool,
    timeout_seconds: int = 600,
) -> dict:
    """等待前台最终操作后的平台回执；不点击任何弹窗或确认按钮。"""

    reported_markers: set[str] = set()
    for _ in range(timeout_seconds * 2):
        if page.is_closed():
            raise XhsPublishError("小红书发布页已关闭，未获得平台成功回执")
        url = str(page.url or "")
        body = ""
        try:
            body = await page.locator("body").inner_text(timeout=1_500)
        except Exception:
            pass
        normalized_body = _normalized(body)
        if "creator.xiaohongshu.com/publish/success" in url or any(
            marker in normalized_body
            for marker in ("定时发布成功", "发布成功", "定时发布已提交")
        ):
            return {
                "ok": True,
                "scheduled": scheduled,
                "scheduledAt": schedule_text,
                "receiptUrl": url,
            }

        marker = ""
        if re.search(r"扫码|二维码|登录", normalized_body):
            marker = "检测到登录或扫码提示，等待用户处理"
        else:
            dialogs = page.locator(
                '[role="dialog"], [aria-modal="true"], '
                '[class*="modal"], [class*="dialog"], [class*="popup"]'
            )
            for index in range(await dialogs.count() - 1, -1, -1):
                dialog = dialogs.nth(index)
                try:
                    if not await dialog.is_visible(timeout=200):
                        continue
                    text = _normalized(await dialog.inner_text(timeout=700))
                    if text:
                        marker = "平台出现可见确认：" + text[:600]
                        break
                except Exception:
                    continue
        if marker and marker not in reported_markers:
            reported_markers.add(marker)
            task_service.record_task_event(
                task_id,
                "xhs_user_action_required",
                marker,
                level="warning",
            )
            await page.bring_to_front()
            if marker.startswith("平台出现可见确认："):
                raise XhsPublishError(
                    "小红书出现未授权的平台提示，已保持页面并安全停止："
                    + marker[:700]
                )
        await page.wait_for_timeout(500)
    raise XhsPublishError("等待小红书平台成功回执超时，页面已保留至超时结束")


async def run_xhs_publish(payload: dict, *, task_id: int) -> dict:
    _validate_payload(payload)
    account = oneclick_preflight._account_for_payload(payload)
    try:
        oneclick_preflight._validate_xhs_account_id(payload, account)
    except oneclick_preflight.PreflightError as exc:
        raise XhsPublishError(str(exc)) from exc
    expected_account = _normalized(account.get("userName"))
    target_schedule = _schedule_time(payload)
    schedule_text = (
        target_schedule.strftime("%Y-%m-%d %H:%M")
        if target_schedule is not None
        else None
    )
    publish_label = "定时发布" if target_schedule is not None else "发布"

    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = None
    context = None
    try:
        # 正式任务强制显示平台页，未知提示必须由用户看见并处理。
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(
            storage_state=str(oneclick_preflight._storage_state(account))
        )
        page = await context.new_page()
        await page.goto(
            "https://creator.xiaohongshu.com/",
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        # 创作中心身份区和 personal_info 回执均为异步加载，不能在
        # domcontentloaded 后立即判定账号不可用。
        await page.wait_for_timeout(3_500)
        detected_account = await _detect_xhs_account(page)
        if not detected_account:
            raise XhsPublishError("小红书平台页面未能回读当前账号昵称")
        if _normalized(detected_account) != expected_account:
            raise XhsPublishError(
                f"小红书账号回读不一致：本地={expected_account}，平台={detected_account}"
            )

        helper = XhsNativeAdapter(payload)
        readback = await helper.fill_content(page)
        location_readback = await helper.apply_location(page)
        await helper.fill_official_topics(page)
        topic_nodes = page.locator(".tiptap.ProseMirror a.tiptap-topic")
        topic_node_texts = [
            _normalized(await topic_nodes.nth(index).inner_text())
            for index in range(await topic_nodes.count())
        ]
        if len(topic_node_texts) < len(readback["tags"]):
            raise XhsPublishError(
                "小红书官方话题节点回读不足，已停止发布"
            )
        await helper.set_declarations(page)
        task_service.record_task_event(
            task_id,
            "xhs_declarations_readback",
            (
                f"AI声明={'已回读' if helper.ai_generated else '未启用'}；"
                f"原创声明={'已回读' if helper.original_declaration else '未启用'}；"
                f"官方话题节点={len(topic_node_texts)}。"
            ),
        )
        if target_schedule is not None:
            await helper.set_schedule(page, target_schedule)
        else:
            await helper.verify_immediate_publish(page)
        await page.bring_to_front()

        task_service.record_task_event(
            task_id,
            "xhs_ready_for_final",
            (
                f"小红书已完成发布前回读：账号={detected_account}；"
                f"类型={'图文' if readback['contentType'] == 'article' else '视频'}；"
                f"标题={readback['title']}；素材={readback['mediaCount']}；"
                f"标签={len(readback['tags'])}；"
                f"地点={location_readback['editorNameReadback'] if location_readback else '未设置'}；"
                f"发布方式={schedule_text or '立即发布'}。"
                "平台页已置于前台，等待点击最终发布按钮。"
            ),
        )
        publish_button = await helper.final_button(page, publish_label)
        button_info = await helper.read_final_button(publish_button)
        button_text = _publish_button_label(button_info)
        if button_text != publish_label:
            raise XhsPublishError(
                f"小红书最终按钮与任务不一致："
                f"期望={publish_label}，实际={button_text or '空'}"
            )
        await helper.submit_final_button(publish_button)
        task_service.record_task_event(
            task_id,
            "xhs_final_clicked",
            (
                f"已点击小红书明确的“{publish_label}”按钮，"
                f"目标时间={schedule_text or '立即'}。"
            ),
        )
        receipt = await _wait_for_platform_result(
            page,
            task_id=task_id,
            schedule_text=schedule_text,
            scheduled=target_schedule is not None,
        )
        receipt.update(
            {
                "account": detected_account,
                "title": readback["title"],
                "contentType": readback["contentType"],
                "mediaCount": readback["mediaCount"],
                "imageCount": readback["imageCount"],
                "videoCount": readback["videoCount"],
                "tagCount": len(readback["tags"]),
                "executionBackend": readback["executionBackend"],
                "location": location_readback,
            }
        )
        return receipt
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        await playwright.stop()


def run_xhs_publish_sync(payload: dict, *, task_id: int) -> dict:
    return asyncio.run(run_xhs_publish(payload, task_id=task_id))
