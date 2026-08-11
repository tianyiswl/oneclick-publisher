# -*- coding: utf-8 -*-
"""TikTok Studio 浏览器预发布上传器。"""

from __future__ import annotations

import asyncio
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
from utils.log import tiktok_logger
from utils.publish_observer import publish_event


UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
FORMAL_LOCK_MESSAGE = "TikTok 正式发布缺少桌面端确认"
MANUAL_INTERVENTION_TIMEOUT_SECONDS = 600
PUBLISH_RESULT_TIMEOUT_SECONDS = 120


class TikTokManualInterventionRequired(RuntimeError):
    """TikTok 要求验证码、扫码或其他真人安全确认。"""


class TikTokPublishResultUnverified(RuntimeError):
    """最终按钮已点击，但平台没有返回足够的成功证据。"""


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
                value = (await locator.nth(index).inner_text(timeout=1000)).strip()
            except RuntimeError:
                raise
            except Exception:
                continue
            if value:
                parts.append(value)
    return "\n".join(parts)


def tiktok_security_intervention_reason(url: str, body_text: str) -> str | None:
    """识别登录失效、验证码与风控页面，不采集页面中的敏感值。"""

    normalized_url = str(url or "").lower()
    text = str(body_text or "").lower()
    if "/login" in normalized_url:
        return "TikTok 登录状态已失效，请重新登录"
    if any(marker in normalized_url for marker in ("/challenge", "/verify", "captcha")):
        return "TikTok 要求完成账号安全验证"
    markers = (
        "verify to continue",
        "security verification",
        "enter verification code",
        "two-step verification",
        "scan qr code",
        "confirm it's you",
        "captcha",
        "请完成验证",
        "安全验证",
        "输入验证码",
        "两步验证",
        "扫描二维码",
        "确认是你本人",
    )
    if any(marker in text for marker in markers):
        return "TikTok 要求完成验证码、扫码或安全确认"
    return None


def tiktok_publish_success_signal(*, url: str, feedback_text: str) -> str | None:
    """只接受明确成功提示或进入 TikTok Studio 内容页作为成功证据。"""

    text = str(feedback_text or "").lower()
    markers = (
        "your video has been uploaded",
        "video posted successfully",
        "post published",
        "posted successfully",
        "upload successful",
        "视频已发布",
        "发布成功",
        "上传成功",
    )
    matched = next((marker for marker in markers if marker in text), None)
    if matched:
        return f"platform_feedback:{matched}"
    normalized_url = str(url or "").lower()
    content_routes = (
        "/tiktokstudio/content",
        "/tiktokstudio/posts",
        "/creator-center/content",
    )
    if "tiktok.com" in normalized_url and any(route in normalized_url for route in content_routes):
        return "platform_content_route"
    return None


class TiktokVideo:
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date,
        account_file,
        *,
        description="",
        thumbnail_path=None,
        thumbnail_paths=None,
        dry_run=True,
        dry_run_hold_browser=True,
    ):
        self.title = str(title or "")
        self.description = str(description or "")
        self.file_path = str(file_path)
        self.tags = list(tags or [])
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.thumbnail_path = str(thumbnail_path) if thumbnail_path else None
        self.thumbnail_paths = dict(thumbnail_paths or {})
        self.dry_run = bool(dry_run)
        self.dry_run_hold_browser = bool(dry_run_hold_browser)
        self.publish_confirmed = False
        self.external_page = None
        self.external_context = None
        self.external_browser = None

    def _caption(self) -> str:
        parts = [part.strip() for part in (self.title, self.description) if part and part.strip()]
        if self.tags:
            parts.append(" ".join(f"#{str(tag).lstrip('#')}" for tag in self.tags))
        return "\n\n".join(parts)[:2200]

    async def _base(self, page):
        if await page.locator('iframe[data-tt="Upload_index_iframe"]').count():
            return page.frame_locator('iframe[data-tt="Upload_index_iframe"]')
        return page

    async def _upload_file(self, page, base) -> None:
        file_input = base.locator('input[type="file"]').first
        if await file_input.count():
            await file_input.set_input_files(self.file_path)
            return
        for label in ("Select video", "Select file", "Upload"):
            button = base.get_by_role("button", name=label, exact=False).first
            if not await button.count():
                continue
            async with page.expect_file_chooser(timeout=15000) as chooser_info:
                await button.click()
            chooser = await chooser_info.value
            await chooser.set_files(self.file_path)
            return
        raise RuntimeError("TikTok 上传页未找到视频选择入口，页面结构可能已变化")

    async def _fill_caption(self, page, base) -> None:
        caption = self._caption()
        selectors = (
            '[contenteditable="true"][role="textbox"]',
            'div.public-DraftEditor-content',
            '[data-e2e="caption-editor"] [contenteditable="true"]',
        )
        for selector in selectors:
            editor = base.locator(selector).first
            try:
                await editor.wait_for(state="visible", timeout=8000)
                await editor.click()
                await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.insert_text(caption)
                try:
                    actual = await editor.input_value()
                except Exception:
                    actual = await editor.inner_text()
                expected_normalized = " ".join(caption.split())
                actual_normalized = " ".join(str(actual or "").split())
                if actual_normalized != expected_normalized:
                    raise RuntimeError(
                        "TikTok 文案写入后回读不一致，已停止在最终发布前"
                    )
                return
            except Exception:
                continue
        raise RuntimeError("TikTok 上传页未找到文案输入框，页面结构可能已变化")

    async def _wait_until_ready(self, base) -> None:
        for _ in range(180):
            for selector in (
                'button:has-text("Post")',
                'div.button-group > button:has-text("Post")',
                'div.btn-post > button',
            ):
                button = base.locator(selector).first
                try:
                    if await button.count() and await button.is_visible() and await button.is_enabled():
                        return
                except Exception:
                    pass
            await asyncio.sleep(2)
        raise RuntimeError("TikTok 视频上传或处理超时，未进入可发布状态")

    async def _wait_for_manual_intervention(self, page) -> None:
        reason = tiktok_security_intervention_reason(page.url, await _body_text(page))
        if not reason:
            return
        await reveal_page_window(page)
        publish_event(
            "tiktok_manual_intervention",
            f"{reason}；请在当前浏览器完成，程序会自动继续",
            level="warning",
        )
        tiktok_logger.warning(f"[tiktok] {reason}；等待用户在可见浏览器完成")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MANUAL_INTERVENTION_TIMEOUT_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(1)
            reason = tiktok_security_intervention_reason(
                page.url,
                await _body_text(page),
            )
            if not reason:
                publish_event(
                    "tiktok_manual_intervention_resolved",
                    "TikTok 真人安全确认已完成，自动发布继续执行",
                )
                return
        raise TikTokManualInterventionRequired(
            "TikTok 真人安全确认等待超时；登录态已保留，请重新启动前台发布"
        )

    async def _ensure_public_visibility(self, page, base) -> None:
        """选择“所有人/公开”并从同一控件回读，找不到控件时禁止发布。"""

        public_markers = ("everyone", "public", "所有人", "公开")
        privacy_markers = public_markers + ("friends", "only you", "好友", "仅自己")
        selectors = (
            '[data-e2e*="privacy" i] [role="combobox"]',
            '[data-e2e*="privacy" i] button',
            'button[aria-label*="privacy" i]',
            'button[aria-label*="watch" i]',
            '[role="combobox"]',
        )
        trigger = None
        for selector in selectors:
            candidates = base.locator(selector)
            try:
                count = min(await candidates.count(), 12)
            except Exception:
                continue
            for index in range(count):
                candidate = candidates.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    text = " ".join((await candidate.inner_text()).lower().split())
                    aria = str(await candidate.get_attribute("aria-label") or "").lower()
                except Exception:
                    continue
                if any(marker in f"{text} {aria}" for marker in privacy_markers + ("privacy", "watch", "可见")):
                    trigger = candidate
                    if any(marker in text for marker in public_markers):
                        tiktok_logger.info("[tiktok] 可见性已回读：公开")
                        return
                    break
            if trigger is not None:
                break
        if trigger is None:
            raise RuntimeError("TikTok 未找到可回读的可见性控件，未执行最终发布")

        await trigger.click()
        option = None
        for name in ("Everyone", "Public", "所有人", "公开"):
            for finder in (
                base.get_by_role("option", name=name, exact=False).first,
                base.get_by_text(name, exact=True).first,
            ):
                try:
                    if await finder.count() and await finder.is_visible():
                        option = finder
                        break
                except Exception:
                    continue
            if option is not None:
                break
        if option is None:
            raise RuntimeError("TikTok 可见性列表中未找到“所有人/公开”，未执行最终发布")
        await option.click()
        await page.wait_for_timeout(300)
        try:
            actual = " ".join((await trigger.inner_text()).lower().split())
        except Exception as exc:
            raise RuntimeError("TikTok 可见性设置后无法回读，未执行最终发布") from exc
        if not any(marker in actual for marker in public_markers):
            raise RuntimeError("TikTok 可见性设置后回读不是公开，未执行最终发布")
        tiktok_logger.info("[tiktok] 可见性已设置并回读：公开")

    async def _post_button(self, base):
        for selector in (
            'button:has-text("Post")',
            'div.button-group > button:has-text("Post")',
            'div.btn-post > button',
            'button:has-text("发布")',
        ):
            button = base.locator(selector).first
            try:
                if await button.count() and await button.is_visible() and await button.is_enabled():
                    return button
            except Exception:
                continue
        return None

    async def _wait_for_publish_result(self, page) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PUBLISH_RESULT_TIMEOUT_SECONDS
        while loop.time() < deadline:
            await self._wait_for_manual_intervention(page)
            feedback = await _feedback_text(page)
            signal = tiktok_publish_success_signal(
                url=page.url,
                feedback_text=feedback,
            )
            if signal:
                return signal
            combined = f"{feedback}\n{await _body_text(page)}".lower()
            if any(
                marker in combined
                for marker in (
                    "couldn't post",
                    "could not post",
                    "post failed",
                    "upload failed",
                    "无法发布",
                    "发布失败",
                    "上传失败",
                )
            ):
                raise RuntimeError("TikTok 页面提示最终发布失败")
            await asyncio.sleep(2)
        raise TikTokPublishResultUnverified(
            "TikTok 未返回可验证的发布成功结果；本次不会记为成功，请到内容列表人工核对"
        )

    async def _publish_formally(self, page, base) -> dict[str, str]:
        if not self.publish_confirmed:
            raise RuntimeError(FORMAL_LOCK_MESSAGE)
        await self._wait_for_manual_intervention(page)
        await self._ensure_public_visibility(page, base)
        button = await self._post_button(base)
        if button is None:
            raise RuntimeError("TikTok 最终发布按钮不可用，未执行发布")
        publish_event("tiktok_final_click", "TikTok 已确认，正在点击 Post")
        await button.click()
        signal = await self._wait_for_publish_result(page)
        publish_event(
            "tiktok_publish_verified",
            "TikTok 发布结果已回读",
            reference=signal,
        )
        return {"status": "published", "evidence": signal}

    async def upload(self, playwright: Playwright) -> dict[str, str] | None:
        if not self.dry_run and not self.publish_confirmed:
            raise RuntimeError(FORMAL_LOCK_MESSAGE)
        if not Path(self.file_path).is_file():
            raise RuntimeError(f"TikTok 视频文件不存在：{self.file_path}")

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
            if "tiktokstudio/upload" not in (page.url or ""):
                await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            await reveal_page_window(page)
            await page.wait_for_timeout(2500)
            text = await _body_text(page)
            if "/login" in (page.url or "").lower() or "log in to tiktok" in text:
                raise RuntimeError("TikTok 登录已失效，请先在账号管理中重新登录")

            base = await self._base(page)
            mode_text = "预发布" if self.dry_run else "正式发布"
            tiktok_logger.info(f"[tiktok] 开始{mode_text}上传：{Path(self.file_path).name}")
            await self._upload_file(page, base)
            await self._fill_caption(page, base)
            await self._wait_until_ready(base)
            result = None
            if not self.dry_run:
                result = await self._publish_formally(page, base)
            await save_context_storage_state(context, self.account_file)
            if self.dry_run:
                tiktok_logger.success("[tiktok] 已停在最终发布前，未点击 Post")
            else:
                tiktok_logger.success("[tiktok] 已获得平台发布成功回执")

            if owns_browser:
                if self.dry_run:
                    await keep_browser_open_for_dry_run(
                        page,
                        context,
                        browser,
                        account_file=self.account_file,
                        logger=tiktok_logger,
                        platform_name="TikTok",
                        block_until_close=self.dry_run_hold_browser,
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
