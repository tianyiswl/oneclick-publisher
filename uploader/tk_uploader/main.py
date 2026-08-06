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


UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
FORMAL_LOCK_MESSAGE = "TikTok 正式发布尚未解锁；当前只允许停在最终发布前"


async def _body_text(page) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return ""


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

    async def upload(self, playwright: Playwright) -> None:
        if not self.dry_run:
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
            tiktok_logger.info(f"[tiktok] 开始预发布上传：{Path(self.file_path).name}")
            await self._upload_file(page, base)
            await self._fill_caption(page, base)
            await self._wait_until_ready(base)
            await save_context_storage_state(context, self.account_file)
            tiktok_logger.success("[tiktok] 已停在最终发布前，未点击 Post")

            if owns_browser:
                await keep_browser_open_for_dry_run(
                    page,
                    context,
                    browser,
                    account_file=self.account_file,
                    logger=tiktok_logger,
                    platform_name="TikTok",
                    block_until_close=self.dry_run_hold_browser,
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
