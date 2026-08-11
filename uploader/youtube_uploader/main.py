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
from utils.publish_observer import publish_event


UPLOAD_URL = "https://www.youtube.com/upload"
FORMAL_LOCK_MESSAGE = "YouTube 正式发布缺少桌面端确认"
MANUAL_INTERVENTION_TIMEOUT_SECONDS = 600
PUBLISH_RESULT_TIMEOUT_SECONDS = 120


class YouTubeManualInterventionRequired(RuntimeError):
    """YouTube 要求登录、验证码或其他真人安全确认。"""


class YouTubePublishResultUnverified(RuntimeError):
    """最终保存按钮已点击，但平台没有返回足够的成功证据。"""


async def _click_if_present(page, selector: str, timeout: int = 5000) -> bool:
    try:
        element = page.locator(selector).first
        await element.wait_for(state="visible", timeout=timeout)
        await element.click()
        return True
    except Exception:
        return False


async def _body_text(page) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return ""


async def _feedback_text(page) -> str:
    parts = []
    for selector in ('[role="alert"]', '[role="status"]', 'ytcp-toast'):
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), 12)
        except Exception:
            continue
        for index in range(count):
            try:
                value = (await locator.nth(index).inner_text(timeout=1000)).strip()
            except Exception:
                continue
            if value:
                parts.append(value)
    return "\n".join(parts)


def youtube_security_intervention_reason(url: str, body_text: str) -> str | None:
    """识别 Google/YouTube 登录与安全验证页面。"""

    normalized_url = str(url or "").lower()
    text = str(body_text or "").lower()
    if "accounts.google.com" in normalized_url or "/signin" in normalized_url:
        return "YouTube 登录状态已失效，请重新登录"
    if any(marker in normalized_url for marker in ("/challenge/", "signin/v2/challenge")):
        return "Google 要求完成账号安全验证"
    markers = (
        "verify it's you",
        "2-step verification",
        "enter the code",
        "check your phone",
        "confirm your recovery",
        "验证您的身份",
        "两步验证",
        "输入验证码",
        "查看您的手机",
        "确认恢复",
    )
    if any(marker in text for marker in markers):
        return "Google 要求完成验证码或安全确认"
    return None


def youtube_publish_success_signal(
    *,
    url: str,
    feedback_text: str,
    upload_dialog_visible: bool,
    final_button_visible: bool,
    visibility: str,
) -> str | None:
    """接受平台明确提示，或 Studio 上传对话框在最终保存后关闭。"""

    text = str(feedback_text or "").lower()
    markers = (
        "video published",
        "published successfully",
        "video saved",
        "视频已发布",
        "发布成功",
        "视频已保存",
    )
    matched = next((marker for marker in markers if marker in text), None)
    if matched:
        return f"platform_feedback:{matched}"
    normalized_url = str(url or "").lower()
    if (
        "studio.youtube.com" in normalized_url
        and not upload_dialog_visible
        and not final_button_visible
    ):
        return f"studio_upload_dialog_closed:{visibility}"
    return None


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
    try:
        actual = await box.input_value()
    except Exception:
        actual = await box.inner_text()
    if " ".join(str(actual or "").split()) != " ".join(str(text or "").split()):
        raise RuntimeError("YouTube 字段写入后回读不一致，已停止在最终保存前")


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
        self.publish_confirmed = False
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

    async def set_ai_generated_declaration(self, page) -> bool:
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
            return True
        text_selector = (
            "tp-yt-paper-radio-button:has-text('Yes'), tp-yt-paper-radio-button:has-text('是')"
            if ai_generated
            else "tp-yt-paper-radio-button:has-text('No'), tp-yt-paper-radio-button:has-text('否')"
        )
        if not await _click_if_present(page, text_selector, 3000):
            youtube_logger.warning("[youtube] 未识别到 AI/合成内容声明控件，请在预发布页人工确认")
            return False
        return True

    async def set_audience(self, page) -> None:
        """按一键发设置选择 YouTube 受众，并从真实控件回读。"""

        made_for_kids = bool(getattr(self, "made_for_kids", False))
        selectors = (
            (
                "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_MFK']",
                "tp-yt-paper-radio-button:has-text(\"Yes, it's made for kids\")",
                "tp-yt-paper-radio-button:has-text('是的，此视频是面向儿童的')",
            )
            if made_for_kids
            else (
                "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
                "tp-yt-paper-radio-button:has-text(\"No, it's not made for kids\")",
                "tp-yt-paper-radio-button:has-text('不，此视频不是面向儿童的')",
            )
        )
        for selector in selectors:
            candidate = page.locator(selector).first
            try:
                if not await candidate.count() or not await candidate.is_visible():
                    continue
                await candidate.click()
                await page.wait_for_timeout(250)
                checked = await candidate.evaluate(
                    """element =>
                      element.getAttribute('aria-checked') === 'true'
                      || element.getAttribute('aria-selected') === 'true'
                      || element.hasAttribute('checked')
                      || element.checked === true
                    """
                )
                if checked:
                    youtube_logger.info(
                        "[youtube] 受众已设置并回读："
                        + ("面向儿童" if made_for_kids else "不面向儿童")
                    )
                    return
            except Exception:
                continue
        raise RuntimeError(
            "YouTube 受众设置后无法回读确认，已停止在最终保存前"
        )

    async def set_visibility(self, page) -> None:
        """选择可见性并回读选中状态。"""

        visibility_name = {
            "public": "PUBLIC",
            "unlisted": "UNLISTED",
            "private": "PRIVATE",
        }[self.visibility]
        target = page.locator(
            f"tp-yt-paper-radio-button[name='{visibility_name}']"
        ).first
        try:
            await target.wait_for(state="visible", timeout=10000)
            await target.click()
            await page.wait_for_timeout(250)
            checked = await target.evaluate(
                """element =>
                  element.getAttribute('aria-checked') === 'true'
                  || element.getAttribute('aria-selected') === 'true'
                  || element.hasAttribute('checked')
                  || element.checked === true
                """
            )
        except Exception as exc:
            raise RuntimeError(
                f"YouTube 未能设置{self.visibility}可见性，已停止在发布前"
            ) from exc
        if not checked:
            raise RuntimeError(
                f"YouTube {self.visibility}可见性设置后无法回读，已停止在发布前"
            )
        youtube_logger.info(f"[youtube] 可见性已设置并回读：{self.visibility}")

    async def _wait_for_manual_intervention(self, page) -> None:
        reason = youtube_security_intervention_reason(page.url, await _body_text(page))
        if not reason:
            return
        await reveal_page_window(page)
        publish_event(
            "youtube_manual_intervention",
            f"{reason}；请在当前浏览器完成，程序会自动继续",
            level="warning",
        )
        youtube_logger.warning(f"[youtube] {reason}；等待用户在可见浏览器完成")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MANUAL_INTERVENTION_TIMEOUT_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(1)
            reason = youtube_security_intervention_reason(
                page.url,
                await _body_text(page),
            )
            if not reason:
                publish_event(
                    "youtube_manual_intervention_resolved",
                    "YouTube 真人安全确认已完成，自动发布继续执行",
                )
                return
        raise YouTubeManualInterventionRequired(
            "YouTube 真人安全确认等待超时；登录态已保留，请重新启动前台发布"
        )

    async def _final_button(self, page):
        selectors = (
            "#done-button",
            "ytcp-button#done-button",
            "ytcp-button:has-text('Save')",
            "ytcp-button:has-text('Publish')",
            "ytcp-button:has-text('保存')",
            "ytcp-button:has-text('发布')",
        )
        for selector in selectors:
            button = page.locator(selector).first
            try:
                if await button.count() and await button.is_visible() and await button.is_enabled():
                    return button
            except Exception:
                continue
        return None

    async def _upload_dialog_visible(self, page) -> bool:
        for selector in (
            "ytcp-video-upload-dialog",
            "ytcp-uploads-dialog",
            "ytcp-dialog[aria-label*='upload' i]",
        ):
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _final_button_visible(self, page) -> bool:
        return await self._final_button(page) is not None

    async def _wait_for_publish_result(self, page) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PUBLISH_RESULT_TIMEOUT_SECONDS
        while loop.time() < deadline:
            await self._wait_for_manual_intervention(page)
            feedback = await _feedback_text(page)
            signal = youtube_publish_success_signal(
                url=page.url,
                feedback_text=feedback,
                upload_dialog_visible=await self._upload_dialog_visible(page),
                final_button_visible=await self._final_button_visible(page),
                visibility=self.visibility,
            )
            if signal:
                return signal
            combined = f"{feedback}\n{await _body_text(page)}".lower()
            if any(
                marker in combined
                for marker in (
                    "couldn't save",
                    "could not save",
                    "upload failed",
                    "processing abandoned",
                    "无法保存",
                    "上传失败",
                    "处理已中止",
                )
            ):
                raise RuntimeError("YouTube 页面提示最终保存失败")
            await asyncio.sleep(2)
        raise YouTubePublishResultUnverified(
            "YouTube 未返回可验证的保存或发布结果；本次不会记为成功，请到内容列表人工核对"
        )

    async def _publish_formally(self, page) -> dict[str, str]:
        if not self.publish_confirmed:
            raise RuntimeError(FORMAL_LOCK_MESSAGE)
        await self._wait_for_manual_intervention(page)
        button = await self._final_button(page)
        if button is None:
            raise RuntimeError("YouTube 最终 SAVE 按钮不可用，未执行发布")
        publish_event("youtube_final_click", "YouTube 已确认，正在点击 SAVE")
        await button.click()
        signal = await self._wait_for_publish_result(page)
        publish_event(
            "youtube_publish_verified",
            "YouTube 保存或发布结果已回读",
            reference=signal,
            visibility=self.visibility,
        )
        return {"status": "published", "evidence": signal}

    async def upload(self, playwright: Playwright) -> dict[str, str] | None:
        if not self.dry_run and not self.publish_confirmed:
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

            mode_text = "预发布" if self.dry_run else "正式发布"
            youtube_logger.info(f"[youtube] 开始{mode_text}上传：{Path(self.file_path).name}")
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
                    if self.dry_run:
                        youtube_logger.warning(f"[youtube] 封面暂未自动填入，可在预发布页手动确认：{exc}")
                    else:
                        raise RuntimeError(f"YouTube 封面写入失败：{exc}") from exc

            await self.set_audience(page)

            if self.tags:
                try:
                    await _click_if_present(page, "#toggle-button", 5000)
                    tag_input = page.locator(
                        "#tags-container #text-input, ytcp-form-input-container#tags-container input"
                    ).first
                    await tag_input.wait_for(state="visible", timeout=8000)
                    await tag_input.fill(",".join(self.tags)[:490])
                except Exception as exc:
                    if self.dry_run:
                        youtube_logger.warning(f"[youtube] 标签暂未自动填入，可在预发布页手动确认：{exc}")
                    else:
                        raise RuntimeError(f"YouTube 标签写入失败：{exc}") from exc
            ai_declaration_set = await self.set_ai_generated_declaration(page)
            if bool(getattr(self, "ai_generated", False)) and not ai_declaration_set and not self.dry_run:
                raise RuntimeError("YouTube 未能设置 AI/合成内容声明，未执行最终发布")

            for _ in range(5):
                visibility = page.locator("tp-yt-paper-radio-button[name='PRIVATE']").first
                if await visibility.count() and await visibility.is_visible():
                    break
                await _click_if_present(page, "#next-button", 8000)
                await page.wait_for_timeout(800)
            await self.set_visibility(page)

            await _wait_upload_complete(page)
            result = None
            if not self.dry_run:
                result = await self._publish_formally(page)
            await save_context_storage_state(context, self.account_file, include_indexed_db=True)
            if self.dry_run:
                youtube_logger.success(f"[youtube] 已设置为{self.visibility}并停在最终保存前，未点击 SAVE")
            else:
                youtube_logger.success(f"[youtube] 已获得平台结果：{self.visibility}")

            if owns_browser:
                if self.dry_run:
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
