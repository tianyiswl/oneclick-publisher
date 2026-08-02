# -*- coding: utf-8 -*-
"""YouTube Studio 浏览器预发布上传器。"""

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
from utils.log import youtube_logger


UPLOAD_URL = "https://www.youtube.com/upload"
FORMAL_LOCK_MESSAGE = "YouTube 正式发布尚未解锁；当前只允许停在最终发布前"


async def _click_if_present(page, selector: str, timeout: int = 5000) -> bool:
    try:
        element = page.locator(selector).first
        await element.wait_for(state="visible", timeout=timeout)
        await element.click()
        return True
    except Exception:
        return False


async def _fill_editable(page, selector: str, text: str) -> None:
    box = page.locator(selector).first
    await box.wait_for(state="visible", timeout=30000)
    await box.click()
    await page.keyboard.press("ControlOrMeta+A")
    await page.keyboard.press("Backspace")
    try:
        await box.fill(text)
    except Exception:
        await page.keyboard.insert_text(text)
    await page.wait_for_timeout(300)
    try:
        await page.evaluate("() => document.activeElement && document.activeElement.blur()")
    except Exception:
        pass


async def _wait_upload_complete(page, max_polls: int = 240) -> None:
    for _ in range(max_polls):
        texts = []
        for selector in (".progress-label", "span.progress-label", "ytcp-video-upload-progress"):
            locator = page.locator(selector).first
            try:
                if await locator.count():
                    texts.append((await locator.inner_text()).strip().lower())
            except Exception:
                pass
        status = " ".join(texts)
        if status and any(
            marker in status
            for marker in ("upload complete", "finished", "checks", "processing", "上传完成", "检查", "处理")
        ):
            return
        if await page.locator("#done-button").count():
            done = page.locator("#done-button").first
            try:
                if await done.is_visible() and await done.is_enabled():
                    return
            except Exception:
                pass
        await asyncio.sleep(3)
    raise RuntimeError("YouTube 视频上传或处理超时，未进入可保存状态")


class YouTubeVideo:
    def __init__(
        self,
        title,
        file_path,
        tags,
        account_file,
        *,
        description="",
        thumbnail_path=None,
        thumbnail_paths=None,
        visibility="private",
        dry_run=True,
        dry_run_hold_browser=True,
    ):
        self.title = str(title or "")
        self.file_path = str(file_path)
        self.tags = list(tags or [])
        self.account_file = str(account_file)
        self.description = str(description or "")
        self.thumbnail_path = str(thumbnail_path) if thumbnail_path else None
        self.thumbnail_paths = dict(thumbnail_paths or {})
        self.visibility = visibility if visibility in {"public", "private", "unlisted"} else "private"
        self.dry_run = bool(dry_run)
        self.dry_run_hold_browser = bool(dry_run_hold_browser)
        self.external_page = None
        self.external_context = None
        self.external_browser = None

    async def select_playlist(self, page) -> None:
        collection_name = str(getattr(self, "collection_name", "") or "").strip()
        if not collection_name:
            return
        trigger = page.locator(
            "ytcp-video-metadata-playlists #container, "
            "ytcp-video-metadata-playlists #dropdown-trigger"
        ).first
        try:
            await trigger.wait_for(state="visible", timeout=10000)
            await trigger.click()
            option = page.locator(
                "ytcp-checkbox-lit, tp-yt-paper-checkbox"
            ).filter(has_text=collection_name).first
            await option.wait_for(state="visible", timeout=10000)
            await option.click()
            await _click_if_present(
                page,
                "ytcp-button#done-button, ytcp-button:has-text('Done'), ytcp-button:has-text('完成')",
                5000,
            )
            youtube_logger.info(f"[youtube] 播放列表已选择：{collection_name}")
        except Exception as exc:
            raise RuntimeError(f"YouTube 未找到播放列表“{collection_name}”") from exc

    async def set_ai_generated_declaration(self, page) -> None:
        ai_generated = bool(getattr(self, "ai_generated", False))
        await _click_if_present(page, "#toggle-button", 5000)
        selector = (
            "tp-yt-paper-radio-button[name='VIDEO_HAS_ALTERED_CONTENT']"
            if ai_generated
            else "tp-yt-paper-radio-button[name='VIDEO_DOES_NOT_HAVE_ALTERED_CONTENT']"
        )
        if await _click_if_present(page, selector, 5000):
            youtube_logger.info(
                f"[youtube] AI/合成内容声明已设置：{'包含' if ai_generated else '不包含'}"
            )
            return
        text_selector = (
            "tp-yt-paper-radio-button:has-text('Yes'), tp-yt-paper-radio-button:has-text('是')"
            if ai_generated
            else "tp-yt-paper-radio-button:has-text('No'), tp-yt-paper-radio-button:has-text('否')"
        )
        if not await _click_if_present(page, text_selector, 3000):
            youtube_logger.warning("[youtube] 未识别到 AI/合成内容声明控件，请在预发布页人工确认")

    async def upload(self, playwright: Playwright) -> None:
        if not self.dry_run:
            raise RuntimeError(FORMAL_LOCK_MESSAGE)
        if not Path(self.file_path).is_file():
            raise RuntimeError(f"YouTube 视频文件不存在：{self.file_path}")

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
            if "youtube.com/upload" not in (page.url or ""):
                await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            await reveal_page_window(page)
            await page.wait_for_timeout(2500)
            url = (page.url or "").lower()
            if "accounts.google.com" in url or "/signin" in url:
                raise RuntimeError("YouTube 登录已失效，请先在账号管理中重新登录")

            youtube_logger.info(f"[youtube] 开始预发布上传：{Path(self.file_path).name}")
            file_input = page.locator('input[type="file"]').first
            await file_input.wait_for(state="attached", timeout=60000)
            await file_input.set_input_files(self.file_path)
            await page.locator("#title-textarea").wait_for(state="visible", timeout=120000)
            await _fill_editable(page, "#title-textarea #textbox", self.title[:100])
            if self.description.strip():
                await _fill_editable(page, "#description-textarea #textbox", self.description)
            await self.select_playlist(page)

            if self.thumbnail_path and Path(self.thumbnail_path).is_file():
                try:
                    thumb = page.locator(
                        "#file-loader input[type='file'], ytcp-thumbnail-uploader input[type='file']"
                    ).first
                    await thumb.wait_for(state="attached", timeout=20000)
                    await thumb.set_input_files(self.thumbnail_path)
                except Exception as exc:
                    youtube_logger.warning(f"[youtube] 封面暂未自动填入，可在预发布页手动确认：{exc}")

            if not await _click_if_present(
                page,
                "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
                10000,
            ):
                await _click_if_present(
                    page,
                    "tp-yt-paper-radio-button:has-text('not made for kids'), "
                    "tp-yt-paper-radio-button:has-text('不是面向儿童')",
                    5000,
                )

            if self.tags:
                try:
                    await _click_if_present(page, "#toggle-button", 5000)
                    tag_input = page.locator(
                        "#tags-container #text-input, ytcp-form-input-container#tags-container input"
                    ).first
                    await tag_input.wait_for(state="visible", timeout=8000)
                    await tag_input.fill(",".join(self.tags)[:490])
                except Exception as exc:
                    youtube_logger.warning(f"[youtube] 标签暂未自动填入，可在预发布页手动确认：{exc}")
            await self.set_ai_generated_declaration(page)

            for _ in range(5):
                visibility = page.locator("tp-yt-paper-radio-button[name='PRIVATE']").first
                if await visibility.count() and await visibility.is_visible():
                    break
                await _click_if_present(page, "#next-button", 8000)
                await page.wait_for_timeout(800)
            visibility_name = {
                "public": "PUBLIC",
                "unlisted": "UNLISTED",
                "private": "PRIVATE",
            }[self.visibility]
            if not await _click_if_present(
                page,
                f"tp-yt-paper-radio-button[name='{visibility_name}']",
                10000,
            ):
                raise RuntimeError(f"YouTube 未能设置{self.visibility}可见性，已停止在发布前")

            await _wait_upload_complete(page)
            await save_context_storage_state(context, self.account_file, include_indexed_db=True)
            youtube_logger.success(f"[youtube] 已设置为{self.visibility}并停在最终保存前，未点击 Done")

            if owns_browser:
                await keep_browser_open_for_dry_run(
                    page,
                    context,
                    browser,
                    account_file=self.account_file,
                    logger=youtube_logger,
                    platform_name="YouTube",
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
