# -*- coding: utf-8 -*-
"""Instagram/Facebook Reels 的 Meta Business Suite 预发布上传器。"""

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
from utils.log import meta_logger


COMPOSER_URL = "https://business.facebook.com/latest/composer/"
FORMAL_LOCK_MESSAGE = "Meta 正式发布尚未解锁；当前只允许停在发布编辑页"


async def _body_text(page) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return ""


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

    async def upload(self, playwright: Playwright) -> None:
        if not self.dry_run:
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
            await save_context_storage_state(context, self.account_file, include_indexed_db=True)
            platform_name = "Instagram Reels" if self.target_platform == "instagram" else "Facebook Reels"
            meta_logger.success(f"[meta] {platform_name} 已填入发布编辑页，未点击 Share/Schedule")

            if owns_browser:
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
            await self.upload(playwright)
