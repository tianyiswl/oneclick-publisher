# -*- coding: utf-8 -*-
"""Instagram/Facebook Reels 的 Meta Business Suite 预发布上传器。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from playwright.async_api import Playwright, async_playwright

from utils.base_social_media import (
    keep_browser_open_for_dry_run,
    launch_publish_browser,
    new_publish_context,
    reveal_page_window,
    save_context_storage_state,
    set_init_script,
)
from utils.log import meta_logger
from utils.publish_observer import publish_event


COMPOSER_URL = "https://business.facebook.com/latest/composer/"
FORMAL_LOCK_MESSAGE = "Meta 浏览器正式发布缺少桌面端显式确认"
MANUAL_INTERVENTION_TIMEOUT_SECONDS = 600
PUBLISH_RESULT_TIMEOUT_SECONDS = 120


class MetaManualInterventionRequired(RuntimeError):
    """Meta 要求验证码、双重验证或其他真人安全确认。"""


class MetaPublishResultUnverified(RuntimeError):
    """最终按钮已经点击，但平台没有返回足够的成功证据。"""


def meta_security_intervention_reason(url: str, body_text: str) -> str | None:
    """识别需要真人处理的登录或安全检查，不记录页面敏感内容。"""

    normalized_url = str(url or "").lower()
    text = str(body_text or "").lower()
    if "checkpoint" in normalized_url:
        return "Meta 要求完成账号安全检查"
    if "facebook.com/login" in normalized_url:
        return "Meta 登录状态已失效，请重新登录"
    markers = (
        "enter the code we sent",
        "two-factor authentication",
        "confirm it's you",
        "confirm your identity",
        "unusual activity",
        "security check",
        "log in to facebook",
        "输入我们发送的验证码",
        "双重验证",
        "确认是你本人",
        "确认你的身份",
        "异常活动",
        "安全检查",
    )
    if any(marker in text for marker in markers):
        return "Meta 要求完成验证码或安全确认"
    return None


def meta_publish_success_signal(
    *,
    url: str,
    feedback_text: str,
    scheduled: bool,
) -> str | None:
    """只接受明确的平台提示或离开编辑器进入内容页作为成功证据。"""

    text = str(feedback_text or "").lower()
    markers = (
        (
            "your reel is scheduled",
            "your post is scheduled",
            "scheduled successfully",
            "reel scheduled",
            "post scheduled",
            "定时发布成功",
            "已安排发布",
        )
        if scheduled
        else (
            "your reel was published",
            "your post was published",
            "published successfully",
            "reel published",
            "post published",
            "发布成功",
            "已成功发布",
        )
    )
    matched = next((marker for marker in markers if marker in text), None)
    if matched:
        return f"platform_feedback:{matched}"

    normalized_url = str(url or "").lower()
    content_routes = (
        "/latest/content",
        "/content_management",
        "/posts_and_stories",
    )
    if "business.facebook.com" in normalized_url and any(
        route in normalized_url for route in content_routes
    ):
        return "platform_content_route"
    return None


async def _body_text(page) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return ""


async def _feedback_text(page) -> str:
    parts = []
    for selector in ('[role="alert"]', '[role="status"]'):
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), 12)
        except Exception:
            continue
        for index in range(count):
            try:
                text = (await locator.nth(index).inner_text(timeout=1000)).strip()
            except Exception:
                continue
            if text:
                parts.append(text)
    return "\n".join(parts)


class MetaReelVideo:
    def __init__(
        self,
        title,
        file_path,
        tags,
        account_file,
        *,
        target_platform,
        description="",
        thumbnail_path=None,
        thumbnail_paths=None,
        dry_run=True,
        dry_run_hold_browser=True,
        publish_confirmed=False,
        automation_acknowledged=False,
    ):
        if target_platform not in {"instagram", "facebook"}:
            raise ValueError(f"不支持的 Meta 发布目标：{target_platform}")
        self.title = str(title or "")
        self.description = str(description or "")
        self.file_path = str(file_path)
        self.tags = list(tags or [])
        self.account_file = str(account_file)
        self.target_platform = target_platform
        self.thumbnail_path = str(thumbnail_path) if thumbnail_path else None
        self.thumbnail_paths = dict(thumbnail_paths or {})
        self.dry_run = bool(dry_run)
        self.dry_run_hold_browser = bool(dry_run_hold_browser)
        self.publish_confirmed = bool(publish_confirmed)
        self.automation_acknowledged = bool(automation_acknowledged)
        self.publish_date = 0
        self.external_page = None
        self.external_context = None
        self.external_browser = None

    def _caption(self) -> str:
        parts = [part.strip() for part in (self.title, self.description) if part and part.strip()]
        if self.tags:
            parts.append(" ".join(f"#{str(tag).lstrip('#')}" for tag in self.tags))
        return "\n\n".join(parts)

    async def _find_caption_field(self, page):
        selectors = (
            '[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"]',
            'textarea[placeholder*="caption" i]',
        )
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=6000)
                return locator
            except Exception:
                continue
        for name in ("Caption", "Write", "Text", "What’s on your mind", "写点什么", "正文"):
            locator = page.get_by_role("textbox", name=name, exact=False).first
            try:
                await locator.wait_for(state="visible", timeout=3000)
                return locator
            except Exception:
                continue
        raise RuntimeError("Meta 发布页未找到文案输入框，页面结构可能已变化")

    async def _set_destination(self, page) -> None:
        checkboxes = page.get_by_role("checkbox")
        count = await checkboxes.count()
        found_target = False
        for index in range(count):
            checkbox = checkboxes.nth(index)
            label_parts = []
            try:
                label_parts.append(await checkbox.get_attribute("aria-label") or "")
            except Exception:
                pass
            try:
                label_parts.append(await checkbox.locator("xpath=..").inner_text(timeout=1000))
            except Exception:
                pass
            label = " ".join(label_parts).lower()
            destination = None
            if "instagram" in label:
                destination = "instagram"
            elif "facebook" in label:
                destination = "facebook"
            if destination is None:
                continue
            should_check = destination == self.target_platform
            found_target = found_target or should_check
            try:
                if should_check and not await checkbox.is_checked():
                    await checkbox.check()
                elif not should_check and await checkbox.is_checked():
                    await checkbox.uncheck()
            except Exception as exc:
                raise RuntimeError(f"Meta 无法单独选择 {self.target_platform} 发布目标：{exc}") from exc
        if not found_target:
            raise RuntimeError(
                f"Meta 发布页未检测到 {self.target_platform} 目标；请确认 Page 与 Instagram 专业账号已连接"
            )

    async def _upload_video(self, page) -> None:
        inputs = page.locator('input[type="file"]')
        for index in range(await inputs.count()):
            try:
                await inputs.nth(index).set_input_files(self.file_path)
                return
            except Exception:
                continue

        for label in ("Add video", "添加视频", "Photo/video", "照片/视频"):
            button = page.get_by_role("button", name=label, exact=False).first
            if not await button.count():
                continue
            await button.click()
            await page.wait_for_timeout(600)
            inputs = page.locator('input[type="file"]')
            for index in range(await inputs.count()):
                try:
                    await inputs.nth(index).set_input_files(self.file_path)
                    return
                except Exception:
                    continue
            upload_item = page.get_by_text("Upload from desktop", exact=False).first
            if await upload_item.count():
                async with page.expect_file_chooser(timeout=15000) as chooser_info:
                    await upload_item.click()
                chooser = await chooser_info.value
                await chooser.set_files(self.file_path)
                return
        raise RuntimeError("Meta 发布页未找到视频上传入口，页面结构可能已变化")

    async def _wait_for_upload_start(self, page) -> None:
        for _ in range(60):
            text = await _body_text(page)
            if any(
                marker in text
                for marker in (
                    "processing",
                    "uploading",
                    "video details",
                    "share",
                    "处理中",
                    "上传中",
                    "视频详情",
                    "分享",
                )
            ):
                return
            await asyncio.sleep(2)
        raise RuntimeError("Meta 未确认视频已进入上传流程")

    async def _wait_for_manual_intervention(self, page) -> None:
        reason = meta_security_intervention_reason(page.url, await _body_text(page))
        if not reason:
            return
        await reveal_page_window(page)
        publish_event(
            "meta_manual_intervention",
            f"{reason}；请在当前浏览器完成，程序会自动继续",
            level="warning",
        )
        meta_logger.warning(f"[meta] {reason}；等待用户在可见浏览器完成")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MANUAL_INTERVENTION_TIMEOUT_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(1)
            reason = meta_security_intervention_reason(
                page.url,
                await _body_text(page),
            )
            if not reason:
                publish_event(
                    "meta_manual_intervention_resolved",
                    "Meta 真人安全确认已完成，自动发布继续执行",
                )
                return
        raise MetaManualInterventionRequired(
            "Meta 真人安全确认等待超时；登录态已保留，请重新启动前台发布"
        )

    async def _action_button(self, page, names: tuple[str, ...]):
        for name in names:
            locator = page.get_by_role("button", name=name, exact=True).first
            try:
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    async def _wait_for_action_button(self, page, names: tuple[str, ...]):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 240
        while loop.time() < deadline:
            await self._wait_for_manual_intervention(page)
            text = await _body_text(page)
            if any(
                marker in text
                for marker in (
                    "video upload failed",
                    "couldn't upload",
                    "上传视频失败",
                    "无法上传",
                )
            ):
                raise RuntimeError("Meta 页面提示视频上传失败")
            button = await self._action_button(page, names)
            if button is not None:
                try:
                    if await button.is_enabled():
                        return button
                except Exception:
                    pass
            await asyncio.sleep(2)
        raise RuntimeError("Meta 视频处理超时，最终发布按钮仍不可用")

    async def _visible_input(self, page, selectors: tuple[str, ...]):
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    async def _configure_schedule(self, page, publish_date: datetime) -> None:
        if publish_date <= datetime.now():
            raise ValueError("Meta 定时发布时间必须晚于当前时间")

        date_selectors = (
            'input[type="date"]',
            'input[aria-label*="date" i]',
            'input[placeholder*="date" i]',
            'input[aria-label*="日期"]',
        )
        time_selectors = (
            'input[type="time"]',
            'input[aria-label*="time" i]',
            'input[placeholder*="time" i]',
            'input[aria-label*="时间"]',
        )
        date_input = await self._visible_input(page, date_selectors)
        time_input = await self._visible_input(page, time_selectors)

        if date_input is None or time_input is None:
            for name in ("Scheduling options", "排期选项", "定时发布设置"):
                trigger = page.get_by_role("button", name=name, exact=False).first
                try:
                    if await trigger.count() and await trigger.is_visible():
                        await trigger.click()
                        break
                except Exception:
                    continue

            for name in ("Schedule", "定时发布", "安排发布"):
                radio = page.get_by_role("radio", name=name, exact=False).first
                try:
                    if await radio.count() and await radio.is_visible():
                        await radio.check()
                        break
                except Exception:
                    continue
            await page.wait_for_timeout(500)
            date_input = await self._visible_input(page, date_selectors)
            time_input = await self._visible_input(page, time_selectors)

        if date_input is None or time_input is None:
            raise RuntimeError(
                "Meta 未找到可验证的定时日期和时间输入框；为避免误发，未点击最终按钮"
            )

        expected_date = publish_date.strftime("%Y-%m-%d")
        expected_time = publish_date.strftime("%H:%M")
        await date_input.fill(expected_date)
        await time_input.fill(expected_time)
        date_value = await date_input.input_value()
        time_value = await time_input.input_value()
        if expected_date not in date_value or expected_time not in time_value:
            raise RuntimeError("Meta 定时发布时间回读不一致；为避免误发，未点击最终按钮")
        publish_event(
            "meta_schedule_verified",
            f"Meta 定时发布时间已填写并回读：{expected_date} {expected_time}",
        )

    async def _wait_for_publish_result(self, page, *, scheduled: bool) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PUBLISH_RESULT_TIMEOUT_SECONDS
        while loop.time() < deadline:
            await self._wait_for_manual_intervention(page)
            feedback = await _feedback_text(page)
            signal = meta_publish_success_signal(
                url=page.url,
                feedback_text=feedback,
                scheduled=scheduled,
            )
            if signal:
                return signal
            combined = f"{feedback}\n{await _body_text(page)}".lower()
            if any(
                marker in combined
                for marker in (
                    "couldn't publish",
                    "could not publish",
                    "publishing failed",
                    "couldn't schedule",
                    "发布失败",
                    "定时发布失败",
                )
            ):
                raise RuntimeError("Meta 页面提示最终发布失败")
            await asyncio.sleep(2)
        raise MetaPublishResultUnverified(
            "Meta 未返回可验证的发布成功结果；本次不会记为成功，请到内容列表人工核对"
        )

    async def _publish_formally(self, page) -> dict[str, str]:
        if not self.publish_confirmed or not self.automation_acknowledged:
            raise RuntimeError(FORMAL_LOCK_MESSAGE)
        await self._wait_for_manual_intervention(page)
        scheduled = isinstance(self.publish_date, datetime)
        if scheduled:
            await self._configure_schedule(page, self.publish_date)
            names = ("Schedule", "定时发布", "安排发布")
        else:
            names = (
                "Publish",
                "Publish now",
                "Share",
                "Share now",
                "Post",
                "发布",
                "立即发布",
                "分享",
                "立即分享",
            )
        button = await self._wait_for_action_button(page, names)
        await self._wait_for_manual_intervention(page)
        publish_event(
            "meta_final_click",
            "Meta 已通过桌面端确认，正在点击定时发布" if scheduled else "Meta 已通过桌面端确认，正在点击立即发布",
        )
        await button.click()
        signal = await self._wait_for_publish_result(page, scheduled=scheduled)
        mode = "scheduled" if scheduled else "published"
        publish_event(
            "meta_publish_verified",
            "Meta 定时发布结果已回读" if scheduled else "Meta 公开发布结果已回读",
            reference=signal,
        )
        return {"status": mode, "evidence": signal}

    async def upload(self, playwright: Playwright) -> dict[str, str] | None:
        if not self.dry_run and (
            not self.publish_confirmed or not self.automation_acknowledged
        ):
            raise RuntimeError(FORMAL_LOCK_MESSAGE)
        if not Path(self.file_path).is_file():
            raise RuntimeError(f"Meta 视频文件不存在：{self.file_path}")

        owns_browser = self.external_browser is None
        browser = self.external_browser or await launch_publish_browser(playwright)
        context = self.external_context
        page = self.external_page
        if context is None:
            context = await new_publish_context(browser, storage_state=self.account_file)
            context = await set_init_script(context)
        if page is None:
            page = await context.new_page()

        try:
            if "business.facebook.com/latest/composer" not in (page.url or ""):
                await page.goto(COMPOSER_URL, wait_until="domcontentloaded", timeout=60000)
            await reveal_page_window(page)
            await page.wait_for_timeout(3000)
            await self._wait_for_manual_intervention(page)
            if "business.facebook.com/latest/composer" not in (page.url or ""):
                await page.goto(
                    COMPOSER_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                await page.wait_for_timeout(2000)
                await self._wait_for_manual_intervention(page)
            text = await _body_text(page)
            denied_markers = (
                "无法用这个账户访问 meta business suite",
                "can't access meta business suite",
                "cannot access meta business suite",
                "you don't have access to meta business suite",
            )
            if any(marker in text for marker in denied_markers):
                raise RuntimeError("Meta Business Suite 无访问权限，请先完成 Page 与 Instagram 专业账号配置")

            await self._set_destination(page)
            caption = await self._find_caption_field(page)
            await caption.click()
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.insert_text(self._caption())
            await self._upload_video(page)
            await self._wait_for_upload_start(page)
            result = None
            if not self.dry_run:
                result = await self._publish_formally(page)
            await save_context_storage_state(context, self.account_file, include_indexed_db=True)
            platform_name = "Instagram Reels" if self.target_platform == "instagram" else "Facebook Reels"
            if self.dry_run:
                meta_logger.success(f"[meta] {platform_name} 已填入发布编辑页，未点击 Share/Schedule")
            else:
                meta_logger.success(f"[meta] {platform_name} 已获得平台发布结果：{result['status']}")

            if owns_browser:
                if self.dry_run:
                    await keep_browser_open_for_dry_run(
                        page,
                        context,
                        browser,
                        account_file=self.account_file,
                        logger=meta_logger,
                        platform_name=platform_name,
                        block_until_close=self.dry_run_hold_browser,
                        include_indexed_db=True,
                    )
                else:
                    await context.close()
                    await browser.close()
            return result
        except Exception:
            if owns_browser:
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass
            raise

    async def main(self):
        async with async_playwright() as playwright:
            return await self.upload(playwright)
