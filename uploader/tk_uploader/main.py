# -*- coding: utf-8 -*-
"""TikTok Studio 浏览器预发布上传器。"""

from __future__ import annotations

import asyncio
import unicodedata
from pathlib import Path
from typing import Any, Sequence

from playwright.async_api import Playwright, async_playwright

from app_core.overseas_tiktok_publish import (
    TikTokPublishError,
    compose_tiktok_caption,
)
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

CAPTION_EDITOR_SELECTORS = (
    '[data-e2e="caption-editor"] [contenteditable="true"]',
    '[contenteditable="true"][role="textbox"]',
    'div.public-DraftEditor-content',
)
TOPIC_CANDIDATE_SELECTORS = (
    '[data-e2e*="hashtag" i] [role="option"]',
    '[data-e2e*="suggest" i] [role="option"]',
    '[role="listbox"] [role="option"]',
)
TOPIC_ENTITY_SELECTORS = (
    '[data-e2e*="hashtag" i]',
    '[data-type="hashtag"]',
    '[data-hashtag-name]',
    'a[href^="/tag/"]',
)
TOPIC_CANDIDATE_STABLE_READS = 2
TOPIC_CANDIDATE_POLL_ATTEMPTS = 20
TOPIC_ENTITY_STABLE_READS = 3
TOPIC_ENTITY_POLL_ATTEMPTS = 20


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
        expected_account_reference: str = "",
        execution_mode: str | None = None,
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
        self.expected_account_reference = str(expected_account_reference or "")
        self.execution_mode = str(
            execution_mode or ("preflight" if self.dry_run else "formal")
        )
        self.publish_confirmed = False
        self._submit_consumed = False
        self.external_page = None
        self.external_context = None
        self.external_browser = None

    def _caption(self) -> str:
        return compose_tiktok_caption(
            self.title.strip(),
            self.description.strip(),
            self._topics(),
        )

    def _topics(self) -> list[str]:
        topics: list[str] = []
        for value in self.tags:
            normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
            normalized = "".join(normalized.lstrip("#").strip().split())
            if normalized:
                topics.append(normalized)
        return topics

    def _plain_caption(self) -> str:
        return compose_tiktok_caption(
            self.title.strip(),
            self.description.strip(),
            (),
        )

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

    @staticmethod
    def _normalize_caption_text(value: object) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        return text.replace("\r\n", "\n").replace("\r", "\n").strip()

    @staticmethod
    def _normalize_topic_label(value: object) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).strip()
        return "".join(text.lstrip("#").strip().split())

    async def _read_editor_text(self, editor) -> str:
        try:
            return str(await editor.inner_text(timeout=2000) or "")
        except Exception:
            try:
                return str(await editor.input_value(timeout=2000) or "")
            except Exception as exc:
                raise TikTokPublishError(
                    "tiktok_caption_readback_failed",
                    "TikTok 文案框无法回读，已停止在最终发布前",
                ) from exc

    async def _resolve_caption_editor(self, base):
        for selector in CAPTION_EDITOR_SELECTORS:
            try:
                candidates = base.locator(selector)
                count = await candidates.count()
            except Exception as exc:
                raise TikTokPublishError(
                    "tiktok_caption_editor_invalid",
                    "TikTok 文案框选择器命中后无法安全检查",
                ) from exc
            if count == 0:
                continue
            visible_editors = []
            for index in range(count):
                try:
                    candidate = candidates.nth(index)
                    if not await candidate.is_visible():
                        continue
                    is_editable = getattr(candidate, "is_editable", None)
                    if is_editable is None or not await is_editable():
                        continue
                except Exception as exc:
                    raise TikTokPublishError(
                        "tiktok_caption_editor_invalid",
                        "TikTok 文案框选择器命中后无法安全检查",
                    ) from exc
                visible_editors.append(candidate)
            if not visible_editors:
                raise TikTokPublishError(
                    "tiktok_caption_editor_invalid",
                    "TikTok 文案框选择器命中，但没有唯一可见可编辑对象",
                )
            if len(visible_editors) != 1:
                raise TikTokPublishError(
                    "tiktok_caption_editor_ambiguous",
                    "TikTok 上传页出现多个可编辑文案框，无法安全继续",
                )
            return visible_editors[0]
        raise TikTokPublishError(
            "tiktok_caption_editor_missing",
            "TikTok 上传页未找到唯一可编辑文案框",
        )

    async def _fill_plain_caption(self, page, base, plain_caption: str):
        editor = await self._resolve_caption_editor(base)
        await editor.click()
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.press("Backspace")
        cleared = self._normalize_caption_text(await self._read_editor_text(editor))
        if cleared:
            raise TikTokPublishError(
                "tiktok_caption_clear_failed",
                "TikTok 文案框未能完全清空",
            )
        await page.keyboard.insert_text(plain_caption)
        actual = self._normalize_caption_text(await self._read_editor_text(editor))
        expected = self._normalize_caption_text(plain_caption)
        if actual != expected:
            raise TikTokPublishError(
                "tiktok_caption_readback_mismatch",
                "TikTok 正文写入后回读不一致",
            )
        return editor

    async def _stable_topic_candidates(self, page, base):
        previous: tuple[str, tuple[str, ...]] | None = None
        stable_reads = 0
        for _ in range(TOPIC_CANDIDATE_POLL_ATTEMPTS):
            await self._wait_for_manual_intervention(page)
            current: tuple[str, tuple[str, ...]] | None = None
            current_items: list[object] = []
            for selector in TOPIC_CANDIDATE_SELECTORS:
                candidates = base.locator(selector)
                try:
                    count = await candidates.count()
                except Exception:
                    continue
                labels: list[str] = []
                items: list[object] = []
                for index in range(count):
                    candidate = candidates.nth(index)
                    try:
                        if not await candidate.is_visible():
                            continue
                        label = self._normalize_topic_label(
                            await candidate.inner_text(timeout=1000)
                        )
                    except Exception:
                        continue
                    if label:
                        labels.append(label)
                        items.append(candidate)
                if items:
                    current = (selector, tuple(labels))
                    current_items = items
                    break
            if current is not None and current == previous:
                stable_reads += 1
                if stable_reads >= TOPIC_CANDIDATE_STABLE_READS - 1:
                    return current_items, list(current[1])
            else:
                previous = current
                stable_reads = 0
            await page.wait_for_timeout(100)
        return [], []

    async def _read_topic_entities(self, editor) -> list[str]:
        for selector in TOPIC_ENTITY_SELECTORS:
            nodes = editor.locator(selector)
            try:
                count = await nodes.count()
            except Exception:
                continue
            entities: list[str] = []
            for index in range(count):
                node = nodes.nth(index)
                try:
                    if not await node.is_visible():
                        continue
                    value = self._normalize_topic_label(
                        await node.inner_text(timeout=1000)
                    )
                except Exception:
                    continue
                if value:
                    entities.append(value)
            if entities:
                return entities
        return []

    @staticmethod
    def _topics_equal(actual: Sequence[str], expected: Sequence[str]) -> bool:
        return [value.casefold() for value in actual] == [
            value.casefold() for value in expected
        ]

    async def _wait_for_expected_topic_entities(
        self,
        page,
        editor,
        expected: Sequence[str],
        topic: str,
    ) -> list[str]:
        stable_reads = 0
        saw_target = False
        for _ in range(TOPIC_ENTITY_POLL_ATTEMPTS):
            await self._wait_for_manual_intervention(page)
            entities = await self._read_topic_entities(editor)
            saw_target = saw_target or any(
                value.casefold() == topic.casefold() for value in entities
            )
            if self._topics_equal(entities, expected):
                stable_reads += 1
                if stable_reads >= TOPIC_ENTITY_STABLE_READS:
                    return entities
            else:
                stable_reads = 0
            await page.wait_for_timeout(100)

        if not saw_target:
            raise TikTokPublishError(
                "tiktok_topic_entity_missing",
                f"TikTok 话题 {topic} 没有稳定回读为平台实体",
            )
        raise TikTokPublishError(
            "tiktok_topic_entity_mismatch",
            "TikTok 话题实体重复或顺序与结构化输入不一致",
        )

    async def _append_official_topics(
        self,
        page,
        base,
        editor,
        topics: Sequence[str],
    ) -> list[str]:
        expected: list[str] = []
        for topic in topics:
            await editor.click()
            await page.keyboard.press("ControlOrMeta+End")
            current_text = self._normalize_caption_text(
                await self._read_editor_text(editor)
            )
            if current_text:
                await page.keyboard.insert_text(" ")
            await page.keyboard.insert_text(f"#{topic}")
            candidates, labels = await self._stable_topic_candidates(page, base)
            exact = [
                candidate
                for candidate, label in zip(candidates, labels)
                if label.casefold() == topic.casefold()
            ]
            if not exact:
                raise TikTokPublishError(
                    "tiktok_topic_candidate_missing",
                    f"TikTok 未返回话题 {topic} 的唯一精确官方候选",
                )
            if len(exact) != 1:
                raise TikTokPublishError(
                    "tiktok_topic_candidate_ambiguous",
                    f"TikTok 话题 {topic} 出现多个精确官方候选",
                )
            await exact[0].click()
            expected.append(topic)
            await self._wait_for_expected_topic_entities(
                page,
                editor,
                expected,
                topic,
            )
        return await self._read_topic_entities(editor)

    async def _verify_form_snapshot(self, page, base) -> dict[str, Any]:
        editor = await self._resolve_caption_editor(base)
        entities = await self._read_topic_entities(editor)
        topics = self._topics()
        if not self._topics_equal(entities, topics):
            if topics and not entities:
                raise TikTokPublishError(
                    "tiktok_topic_entity_missing",
                    "TikTok 结构化话题没有回读为平台实体",
                )
            raise TikTokPublishError(
                "tiktok_topic_entity_mismatch",
                "TikTok 话题实体顺序或数量不一致",
            )
        actual_caption = self._normalize_caption_text(
            await self._read_editor_text(editor)
        )
        expected_caption = self._normalize_caption_text(self._caption())
        if actual_caption != expected_caption:
            raise TikTokPublishError(
                "tiktok_caption_final_mismatch",
                "TikTok 最终文案回读与唯一组合结果不一致",
            )
        visibility = await self._ensure_public_visibility(page, base)
        if visibility != "public":
            raise TikTokPublishError(
                "tiktok_visibility_readback_mismatch",
                "TikTok 可见性回读不是公开",
            )
        button = await self._post_button(base)
        if button is None:
            raise TikTokPublishError(
                "tiktok_final_action_unavailable",
                "TikTok Post 按钮尚不可用",
            )
        return {
            "plainCaption": self._plain_caption(),
            "topicEntities": entities,
            "visibility": "public",
            "finalCaption": self._caption(),
            "finalActionReady": True,
        }

    async def prepare_form(self, page, base) -> dict[str, Any]:
        await self._upload_file(page, base)
        await self._wait_for_manual_intervention(page)
        plain_caption = self._plain_caption()
        editor = await self._fill_plain_caption(page, base, plain_caption)
        await self._append_official_topics(
            page,
            base,
            editor,
            self._topics(),
        )
        await self._ensure_public_visibility(page, base)
        await self._wait_until_ready(page, base)
        return await self._verify_form_snapshot(page, base)

    async def _wait_until_ready(self, page, base) -> None:
        for _ in range(180):
            await self._wait_for_manual_intervention(page)
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

    async def _ensure_public_visibility(self, page, base) -> str:
        """选择“所有人/公开”并从同一控件回读，找不到控件时禁止发布。"""

        await self._wait_for_manual_intervention(page)
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
                        return "public"
                    break
            if trigger is not None:
                break
        if trigger is None:
            raise RuntimeError("TikTok 未找到可回读的可见性控件，未执行最终发布")

        await trigger.click()
        await self._wait_for_manual_intervention(page)
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
        await self._wait_for_manual_intervention(page)
        try:
            actual = " ".join((await trigger.inner_text()).lower().split())
        except Exception as exc:
            raise RuntimeError("TikTok 可见性设置后无法回读，未执行最终发布") from exc
        if not any(marker in actual for marker in public_markers):
            raise RuntimeError("TikTok 可见性设置后回读不是公开，未执行最终发布")
        tiktok_logger.info("[tiktok] 可见性已设置并回读：公开")
        return "public"

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

    async def submit_once(self, page, base) -> dict[str, Any]:
        if self.execution_mode != "formal" or not self.publish_confirmed:
            raise RuntimeError(FORMAL_LOCK_MESSAGE)
        if self._submit_consumed:
            raise TikTokPublishError(
                "tiktok_final_action_already_consumed",
                "TikTok 最终动作已在本会话消费，禁止再次调用",
            )
        await self._wait_for_manual_intervention(page)
        snapshot = await self._verify_form_snapshot(page, base)
        button = await self._post_button(base)
        if button is None:
            raise RuntimeError("TikTok 最终发布按钮不可用，未执行发布")
        if self._submit_consumed:
            raise TikTokPublishError(
                "tiktok_final_action_already_consumed",
                "TikTok 最终动作已在本会话消费，禁止再次调用",
            )
        self._submit_consumed = True
        publish_event("tiktok_final_click", "TikTok 已确认，正在点击 Post")
        await button.click()
        signal = await self._wait_for_publish_result(page)
        publish_event(
            "tiktok_publish_verified",
            "TikTok 发布结果已回读",
            reference=signal,
        )
        return {
            "status": "published",
            "evidence": signal,
            "formSnapshot": snapshot,
        }

    async def _publish_formally(self, page, base) -> dict[str, Any]:
        return await self.submit_once(page, base)

    async def upload(self, playwright: Playwright) -> dict[str, Any] | None:
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
            await page.wait_for_timeout(2500)
            await self._wait_for_manual_intervention(page)
            text = await _body_text(page)
            if "/login" in (page.url or "").lower() or "log in to tiktok" in text:
                raise RuntimeError("TikTok 登录已失效，请先在账号管理中重新登录")

            base = await self._base(page)
            mode_text = "预发布" if self.dry_run else "正式发布"
            tiktok_logger.info(f"[tiktok] 开始{mode_text}上传：{Path(self.file_path).name}")
            result: dict[str, Any] | None = await self.prepare_form(page, base)
            if not self.dry_run:
                result = await self.submit_once(page, base)
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
