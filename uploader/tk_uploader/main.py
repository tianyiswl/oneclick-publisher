# -*- coding: utf-8 -*-
"""TikTok Studio 浏览器预发布上传器。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.async_api import Error as PlaywrightError, Playwright, async_playwright

from app_core.overseas_tiktok_publish import (
    TikTokPublishError,
    compose_tiktok_caption,
)
from app_core.overseas_tiktok_session_scope import (
    load_sanitized_tiktok_storage_state_file,
    replace_tiktok_storage_state_file,
)
from utils.base_social_media import (
    keep_browser_open_for_dry_run,
    launch_publish_browser,
    new_publish_context,
    reveal_page_window,
    set_init_script,
)
from utils.log import tiktok_logger
from utils.publish_observer import publish_event
from uploader.tk_uploader.schedule_form import (
    TikTokScheduleAcceptance,
    TikTokScheduleForm,
    TikTokScheduledContentExpectation,
    TikTokScheduleTarget,
    canonicalize_tiktok_caption,
)


UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
FORMAL_LOCK_MESSAGE = "TikTok 正式发布缺少桌面端确认"
MANUAL_INTERVENTION_TIMEOUT_SECONDS = 600
PUBLISH_RESULT_TIMEOUT_SECONDS = 120
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _uploader_shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)

CAPTION_EDITOR_SELECTORS = (
    '[data-e2e="caption-editor"] [contenteditable="true"]',
    '[contenteditable="true"][role="textbox"]',
    'div.public-DraftEditor-content',
)
TOPIC_CANDIDATE_SELECTORS = (
    '[data-e2e*="hashtag" i] [role="option"]',
    '[data-e2e*="suggest" i] [role="option"]',
    '[role="listbox"] [role="option"]',
    '[role="option"]',
)
TUTORIAL_DISMISS_LABELS = (
    "知道了",
    "Got it",
    "Got It",
)
COOKIE_REJECT_LABELS = (
    "拒绝可选 Cookie",
    "Reject optional cookies",
    "Decline optional cookies",
)
CAPTION_READBACK_POLL_ATTEMPTS = 50
CAPTION_READBACK_POLL_INTERVAL_MS = 100
TOPIC_ENTITY_SELECTORS = (
    '[data-e2e*="hashtag" i]',
    '[data-type="hashtag"]',
    '[data-hashtag-name]',
    'a[href^="/tag/"]',
    'span.mention',
)
TOPIC_CANDIDATE_STABLE_READS = 2
TOPIC_CANDIDATE_POLL_ATTEMPTS = 300
TOPIC_ENTITY_STABLE_READS = 3
TOPIC_ENTITY_POLL_ATTEMPTS = 300
MUSIC_COPYRIGHT_CHECK_POLL_ATTEMPTS = 90
MUSIC_COPYRIGHT_CHECK_POLL_INTERVAL_MS = 2_000
PENDING_CHECKS_CONFIRM_POLL_ATTEMPTS = 20
PENDING_CHECKS_CONFIRM_POLL_INTERVAL_MS = 500
UPLOAD_ENTRY_POLL_INTERVAL_MS = 1_000
UPLOAD_ENTRY_TIMEOUT_SECONDS = 120.0
CAPTION_EDITOR_POLL_ATTEMPTS = 180
CAPTION_EDITOR_POLL_INTERVAL_MS = 1_000


class TikTokManualInterventionRequired(RuntimeError):
    """TikTok 要求验证码、扫码或其他真人安全确认。"""


class TikTokPublishResultUnverified(RuntimeError):
    """最终按钮已点击，但平台没有返回足够的成功证据。"""


async def _body_text(page) -> str:
    try:
        body = page.locator("body")
        if inspect.isawaitable(body):
            body = await body
        value = await body.inner_text(timeout=3000)
        return value.lower() if isinstance(value, str) else ""
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
        schedule_timezone: str = "Asia/Shanghai",
    ):
        self.title = str(title or "")
        self.description = str(description or "")
        self.file_path = str(file_path)
        self.tags = list(tags or [])
        if publish_date is None or (type(publish_date) is int and publish_date == 0):
            self.publish_date = None
        elif type(publish_date) is str and re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", publish_date
        ):
            try:
                parsed_publish_date = datetime.strptime(
                    publish_date, "%Y-%m-%d %H:%M"
                )
            except ValueError as exc:
                raise TikTokPublishError(
                    "tiktok_schedule_invalid",
                    "TikTok 排期必须是有效的北京时间",
                ) from exc
            if parsed_publish_date.strftime("%Y-%m-%d %H:%M") != publish_date:
                raise TikTokPublishError(
                    "tiktok_schedule_invalid",
                    "TikTok 排期必须是精确到分钟的北京时间字符串",
                )
            self.publish_date = publish_date
        else:
            raise TikTokPublishError(
                "tiktok_schedule_invalid",
                "TikTok 排期必须是精确到分钟的北京时间字符串",
            )
        self._requested_schedule_at = self.publish_date
        self.schedule_timezone = str(schedule_timezone)
        if self._requested_schedule_at is not None and self.schedule_timezone != "Asia/Shanghai":
            raise TikTokPublishError(
                "tiktok_schedule_invalid",
                "TikTok 定时只支持 Asia/Shanghai 时区",
            )
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
        self.form_stage_observer = None
        self._schedule_form: TikTokScheduleForm | None = None
        self._schedule_target: TikTokScheduleTarget | None = None
        self.authorized_snapshot_validator = None
        self.schedule_checkpoint_observer = None
        self.final_action_observer = None

    def _emit_form_stage(self, stage: str) -> None:
        observer = self.form_stage_observer
        if not callable(observer):
            return
        try:
            observer(str(stage))
        except Exception:
            # 诊断事件不能改变发布表单本身的控制流。
            return

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
        del base
        loop = asyncio.get_running_loop()
        deadline = loop.time() + UPLOAD_ENTRY_TIMEOUT_SECONDS
        while loop.time() < deadline:
            await self._wait_for_manual_intervention(page)
            if loop.time() >= deadline:
                break
            current_base = await self._base(page)
            file_input = current_base.locator('input[type="file"]').first
            try:
                if await file_input.count():
                    await file_input.set_input_files(self.file_path)
                    return
            except TikTokPublishError:
                raise
            except PlaywrightError as exc:
                if "detached" not in str(exc).lower():
                    raise
                # TikTok 慢加载时上传组件会重挂载；只在该已知情形重新解析入口。
                continue
            for label in ("Select video", "Select file", "Upload"):
                button = current_base.get_by_role(
                    "button",
                    name=label,
                    exact=False,
                ).first
                try:
                    if not await button.count():
                        continue
                    remaining_ms = int((deadline - loop.time()) * 1000)
                    if remaining_ms <= 0:
                        break
                    async with page.expect_file_chooser(timeout=remaining_ms) as chooser_info:
                        remaining_ms = int((deadline - loop.time()) * 1000)
                        if remaining_ms <= 0:
                            break
                        await button.click(timeout=remaining_ms)
                    chooser = await chooser_info.value
                    await chooser.set_files(self.file_path)
                    return
                except TikTokPublishError:
                    raise
                except PlaywrightError as exc:
                    if "detached" not in str(exc).lower():
                        raise
                    # 入口可能正从占位按钮切换为真实文件控件，重新解析页面。
                    break
            remaining_ms = int((deadline - loop.time()) * 1000)
            if remaining_ms <= 0:
                break
            await page.wait_for_timeout(
                min(UPLOAD_ENTRY_POLL_INTERVAL_MS, remaining_ms)
            )
        raise TikTokPublishError(
            "tiktok_upload_entry_timeout",
            "TikTok 上传页在等待时间内没有出现视频选择入口",
        )

    @staticmethod
    def _normalize_caption_text(value: object) -> str:
        return canonicalize_tiktok_caption(value)

    @staticmethod
    def _log_caption_mismatch(stage: str, actual: str, expected: str) -> None:
        first_difference = next(
            (
                index
                for index, (actual_char, expected_char) in enumerate(
                    zip(actual, expected)
                )
                if actual_char != expected_char
            ),
            min(len(actual), len(expected)),
        )
        actual_codes = ",".join(
            f"U+{ord(char):04X}"
            for char in actual[first_difference : first_difference + 8]
        )
        expected_codes = ",".join(
            f"U+{ord(char):04X}"
            for char in expected[first_difference : first_difference + 8]
        )
        tiktok_logger.error(
            f"[tiktok-controlled] {stage} mismatch "
            f"actual_len={len(actual)} expected_len={len(expected)} "
            f"first_difference={first_difference} "
            f"actual_codes={actual_codes or '-'} "
            f"expected_codes={expected_codes or '-'}"
        )

    @staticmethod
    def _normalize_topic_label(value: object) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).strip()
        leading = re.match(r"^#\s*([^\s#]+)", text)
        if leading is not None:
            return leading.group(1)
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

    async def _wait_for_caption_editor(self, page, base):
        current_base = base
        for attempt in range(CAPTION_EDITOR_POLL_ATTEMPTS):
            await self._wait_for_manual_intervention(page)
            if attempt:
                current_base = await self._base(page)
            try:
                editor = await self._resolve_caption_editor(current_base)
                return current_base, editor
            except TikTokPublishError as exc:
                if exc.error_code not in {
                    "tiktok_caption_editor_missing",
                    "tiktok_caption_editor_invalid",
                }:
                    raise
            await page.wait_for_timeout(CAPTION_EDITOR_POLL_INTERVAL_MS)
        raise TikTokPublishError(
            "tiktok_caption_editor_timeout",
            "TikTok 视频已选择，但等待文案编辑区域超时",
        )

    async def _fill_plain_caption(
        self,
        page,
        base,
        plain_caption: str,
        *,
        editor=None,
    ):
        if editor is None:
            editor = await self._resolve_caption_editor(base)
        expected = self._normalize_caption_text(plain_caption)
        actual = ""
        for write_attempt in range(2):
            await self._focus_caption_editor(editor)
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("Backspace")
            cleared = self._normalize_caption_text(
                await self._read_editor_text(editor)
            )
            if cleared:
                raise TikTokPublishError(
                    "tiktok_caption_clear_failed",
                    "TikTok 文案框未能完全清空",
                )
            await page.keyboard.insert_text(plain_caption)
            for attempt in range(CAPTION_READBACK_POLL_ATTEMPTS):
                actual = self._normalize_caption_text(
                    await self._read_editor_text(editor)
                )
                if actual == expected:
                    return editor
                if attempt < CAPTION_READBACK_POLL_ATTEMPTS - 1:
                    await page.wait_for_timeout(CAPTION_READBACK_POLL_INTERVAL_MS)
            if write_attempt == 0 and not actual:
                # TikTok may remount the editor while the selected video is
                # still being prepared.  Only an entirely blank readback is
                # safe to rebuild automatically; partial or foreign text must
                # remain a hard stop to avoid duplicated captions.
                self._emit_form_stage("caption_editor_recovered")
                tiktok_logger.warning(
                    "[tiktok] 正文编辑器发生空白重挂载，正在安全重写一次"
                )
                await page.wait_for_timeout(500)
                editor = await self._resolve_caption_editor(base)
                continue
            break
        self._log_caption_mismatch("caption write readback", actual, expected)
        raise TikTokPublishError(
            "tiktok_caption_readback_mismatch",
            "TikTok 正文写入后回读不一致",
        )

    async def _focus_caption_editor(self, editor) -> None:
        focus = getattr(editor, "focus", None)
        if not callable(focus):
            raise TikTokPublishError(
                "tiktok_caption_focus_failed",
                "TikTok 文案框无法获得输入焦点",
            )
        try:
            await focus()
        except Exception as exc:
            raise TikTokPublishError(
                "tiktok_caption_focus_failed",
                "TikTok 文案框无法获得输入焦点",
            ) from exc

    async def _place_caption_caret_at_end(self, editor) -> None:
        """Put the live contenteditable caret at its real DOM end.

        ``Meta+End`` is not deterministic in TikTok Studio on macOS: when the
        editor remounts, the shortcut can move the document instead of the
        contenteditable caret and the next official topic is inserted before
        the plain caption.  A collapsed DOM range gives us an exact, readable
        placement contract before typing any hashtag.
        """

        await self._focus_caption_editor(editor)
        evaluate = getattr(editor, "evaluate", None)
        if not callable(evaluate):
            raise TikTokPublishError(
                "tiktok_caption_caret_failed",
                "TikTok 文案框无法确认话题插入位置",
            )
        try:
            placed = await evaluate(
                """element => {
                    element.focus();
                    const selection = element.ownerDocument.defaultView.getSelection();
                    if (!selection) return false;
                    const range = element.ownerDocument.createRange();
                    range.selectNodeContents(element);
                    range.collapse(false);
                    selection.removeAllRanges();
                    selection.addRange(range);
                    return selection.rangeCount === 1
                        && selection.isCollapsed
                        && element.contains(selection.anchorNode);
                }"""
            )
        except Exception as exc:
            raise TikTokPublishError(
                "tiktok_caption_caret_failed",
                "TikTok 文案框无法确认话题插入位置",
            ) from exc
        if placed is not True:
            raise TikTokPublishError(
                "tiktok_caption_caret_failed",
                "TikTok 文案框无法确认话题插入位置",
            )

    async def _stable_topic_candidates(
        self,
        page,
        base,
        *,
        topic: str | None = None,
    ):
        previous: tuple[str, tuple[str, ...]] | None = None
        stable_reads = 0
        for _ in range(TOPIC_CANDIDATE_POLL_ATTEMPTS):
            await self._wait_for_manual_intervention(page)
            current: tuple[str, tuple[str, ...]] | None = None
            current_items: list[object] = []
            scopes = (base,)
            if base is not page and callable(getattr(page, "locator", None)):
                scopes = (base, page)
            for scope_index, scope in enumerate(scopes):
                for selector in TOPIC_CANDIDATE_SELECTORS:
                    candidates = scope.locator(selector)
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
                    if topic is not None:
                        exact_pairs = [
                            (item, label)
                            for item, label in zip(items, labels)
                            if label.casefold() == topic.casefold()
                        ]
                        items = [item for item, _label in exact_pairs]
                        labels = [label for _item, label in exact_pairs]
                    if items:
                        if topic is not None:
                            # TikTok 的建议下拉层可能挂在 iframe 外层；目标候选
                            # 点击后仍须经过平台话题实体的连续稳定回读。
                            return items, labels
                        current = (f"{scope_index}:{selector}", tuple(labels))
                        current_items = items
                        break
                if current is not None:
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

    async def _exact_topic_candidate(
        self,
        candidates: Sequence[object],
        labels: Sequence[str],
        topic: str,
    ):
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
        if len(exact) == 1:
            return exact[0]

        focused = []
        for candidate in exact:
            try:
                class_name = str(
                    await candidate.get_attribute("class") or ""
                ).casefold()
                aria_selected = str(
                    await candidate.get_attribute("aria-selected") or ""
                ).casefold()
            except Exception:
                continue
            class_tokens = set(class_name.split())
            if "focused" in class_tokens or aria_selected == "true":
                focused.append(candidate)
        if len(focused) == 1:
            return focused[0]
        raise TikTokPublishError(
            "tiktok_topic_candidate_ambiguous",
            f"TikTok 话题 {topic} 出现多个精确官方候选",
        )

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
            await self._place_caption_caret_at_end(editor)
            current_text = await self._read_editor_text(editor)
            if current_text and not current_text[-1].isspace():
                await page.keyboard.insert_text(" ")
            await page.keyboard.insert_text(f"#{topic}")
            candidates, labels = await self._stable_topic_candidates(
                page,
                base,
                topic=topic,
            )
            exact = await self._exact_topic_candidate(
                candidates,
                labels,
                topic,
            )
            await exact.click()
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
            self._log_caption_mismatch(
                "caption snapshot",
                actual_caption,
                expected_caption,
            )
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
        button = await self._final_action_button(base)
        if button is None:
            raise TikTokPublishError(
                "tiktok_final_action_unavailable",
                "TikTok 最终动作按钮尚不可用",
            )
        result = {
            "plainCaption": self._plain_caption(),
            "topicEntities": entities,
            "visibility": "public",
            "finalCaption": self._caption(),
            "finalActionReady": True,
        }
        if self._schedule_form is not None and self._schedule_target is not None:
            verified_schedule = await self._schedule_form.verify(
                self._schedule_target
            )
            result.update(verified_schedule.as_dict())
        else:
            result["finalActionLabel"] = "Post"
        return result

    async def prepare_form(self, page, base) -> dict[str, Any]:
        self._emit_form_stage("upload_entry_waiting")
        await self._upload_file(page, base)
        self._emit_form_stage("video_selected")
        self._emit_form_stage("caption_editor_waiting")
        base, _editor = await self._wait_for_caption_editor(page, base)
        self._emit_form_stage("caption_editor_ready")
        plain_caption = self._plain_caption()
        self._emit_form_stage("caption_write_started")
        editor = await self._fill_plain_caption(page, base, plain_caption)
        self._emit_form_stage("caption_write_verified")
        self._emit_form_stage("topics_started")
        await self._dismiss_known_tutorial_overlay(page, base)
        await self._neutralize_cookie_banner(page, base)
        try:
            await self._append_official_topics(
                page,
                base,
                editor,
                self._topics(),
            )
        except TikTokPublishError as exc:
            if exc.error_code != "tiktok_topic_candidate_missing":
                raise
            # A freshly remounted editor can accept the raw hashtag before its
            # official suggestion component is ready.  Rebuild the complete
            # structured caption once, still before any final action.  The
            # second miss remains terminal.
            self._emit_form_stage("topic_candidates_recovered")
            tiktok_logger.warning(
                "[tiktok] 话题候选组件暂未就绪，正在安全重建一次"
            )
            await page.wait_for_timeout(1000)
            base = await self._base(page)
            base, editor = await self._wait_for_caption_editor(page, base)
            editor = await self._fill_plain_caption(
                page,
                base,
                plain_caption,
                editor=editor,
            )
            await self._dismiss_known_tutorial_overlay(page, base)
            await self._neutralize_cookie_banner(page, base)
            await self._append_official_topics(
                page,
                base,
                editor,
                self._topics(),
            )
        self._emit_form_stage("topics_verified")
        self._emit_form_stage("visibility_started")
        await self._ensure_public_visibility(page, base)
        self._emit_form_stage("visibility_verified")
        if self._requested_schedule_at is not None:
            self._schedule_form = TikTokScheduleForm(
                page,
                resolve_base=lambda: self._base(page),
                wait_for_manual_intervention=self._wait_for_manual_intervention,
            )
            self._schedule_target = TikTokScheduleTarget(
                str(self._requested_schedule_at),
                self.schedule_timezone,
            )
            await self._schedule_form.configure(self._schedule_target)
        self._emit_form_stage("post_ready_waiting")
        await self._wait_until_ready(page, base)
        self._emit_form_stage("form_snapshot_started")
        try:
            receipt = await self._verify_form_snapshot(page, base)
        except TikTokPublishError as exc:
            if exc.error_code != "tiktok_caption_final_mismatch":
                raise
            current_editor = await self._resolve_caption_editor(base)
            actual_caption = self._normalize_caption_text(
                await self._read_editor_text(current_editor)
            )
            topic_only_caption = self._normalize_caption_text(
                " ".join(f"#{topic}" for topic in self._topics())
            )
            if actual_caption != topic_only_caption:
                # Extra, reordered or otherwise unexpected text is not the
                # known editor-remount race and must remain a hard failure.
                raise
            # The upload editor can be remounted once while TikTok finishes
            # processing the selected video.  In that race the platform keeps
            # the topic chips but drops the plain caption.  Re-resolve the live
            # editor and rebuild the entire structured caption once, still
            # before any final action.  A second mismatch remains terminal.
            tiktok_logger.warning(
                "[tiktok] 发布页文案状态发生重挂载，正在最终动作前重建一次"
            )
            base = await self._base(page)
            base, editor = await self._wait_for_caption_editor(page, base)
            editor = await self._fill_plain_caption(
                page,
                base,
                plain_caption,
                editor=editor,
            )
            await self._append_official_topics(
                page,
                base,
                editor,
                self._topics(),
            )
            await self._ensure_public_visibility(page, base)
            await self._wait_until_ready(page, base)
            receipt = await self._verify_form_snapshot(page, base)
        self._emit_form_stage("form_snapshot_verified")
        return receipt

    async def _dismiss_known_tutorial_overlay(self, page, base) -> bool:
        get_by_role = getattr(base, "get_by_role", None)
        if not callable(get_by_role):
            return False
        for label in TUTORIAL_DISMISS_LABELS:
            try:
                button = get_by_role("button", name=label, exact=True).first
                if (
                    not await button.count()
                    or not await button.is_visible()
                    or not await button.is_enabled()
                ):
                    continue
                await button.click(timeout=3000)
                await page.wait_for_timeout(300)
                self._emit_form_stage("tutorial_dismissed")
                tiktok_logger.info("[tiktok] 已关闭阻挡发布按钮的新手引导")
                return True
            except Exception:
                continue
        return False

    async def _neutralize_cookie_banner(self, page, base) -> bool:
        get_by_role = getattr(base, "get_by_role", None)
        if not callable(get_by_role):
            return False
        for label in COOKIE_REJECT_LABELS:
            try:
                button = get_by_role("button", name=label, exact=True).first
                if not await button.count() or not await button.is_visible():
                    continue
                if await button.is_enabled():
                    await button.click(timeout=3000)
                else:
                    neutralized = await button.evaluate(
                        """
                        (button) => {
                          let node = button;
                          while (node && node !== document.body) {
                            const text = String(node.innerText || '');
                            const rect = node.getBoundingClientRect();
                            const style = window.getComputedStyle(node);
                            const fixed = style.position === 'fixed' || style.position === 'sticky';
                            const isCookieBanner = /Cookie|cookie/.test(text)
                              && fixed
                              && rect.width >= window.innerWidth * 0.7
                              && rect.height <= window.innerHeight * 0.5
                              && rect.bottom >= window.innerHeight - 4;
                            if (isCookieBanner) {
                              node.style.setProperty('pointer-events', 'none', 'important');
                              node.style.setProperty('visibility', 'hidden', 'important');
                              node.setAttribute('data-oneclick-neutralized', 'cookie-banner');
                              return true;
                            }
                            node = node.parentElement;
                          }
                          return false;
                        }
                        """
                    )
                    if neutralized is not True:
                        continue
                await page.wait_for_timeout(200)
                self._emit_form_stage("cookie_banner_neutralized")
                tiktok_logger.info("[tiktok] 已解除底部 Cookie 提示条对发布按钮的遮挡")
                return True
            except Exception:
                continue
        return False

    @staticmethod
    async def _center_action_button(button) -> None:
        evaluate = getattr(button, "evaluate", None)
        if callable(evaluate):
            await evaluate(
                "(element) => element.scrollIntoView({block: 'center', inline: 'center'})"
            )

    async def _wait_until_ready(self, page, base) -> None:
        for _ in range(180):
            await self._wait_for_manual_intervention(page)
            await self._dismiss_known_tutorial_overlay(page, base)
            await self._neutralize_cookie_banner(page, base)
            try:
                button = await self._final_action_button(base)
                if button is not None:
                    # TikTok can expose an enabled Post button while the video
                    # processing overlay still intercepts pointer events.  A
                    # trial click runs Playwright's full actionability checks
                    # without dispatching the irreversible action.
                    await self._center_action_button(button)
                    await button.click(trial=True, timeout=2000)
                    self._emit_form_stage("post_actionability_verified")
                    return
            except TikTokPublishError as exc:
                if (
                    self._schedule_target is None
                    or exc.error_code
                    != "tiktok_schedule_final_action_unavailable"
                ):
                    raise
            except Exception:
                # The visible/enabled state is not sufficient while an upload
                # overlay, animation or remount is still covering the button.
                # Keep waiting within the existing bounded readiness window.
                pass
            await asyncio.sleep(2)
        raise TikTokPublishError(
            "tiktok_post_ready_timeout",
            "TikTok 视频上传或处理超时，最终按钮未进入可用状态",
        )

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

    @staticmethod
    def _music_copyright_check_state(body_text: object) -> str:
        text = unicodedata.normalize("NFKC", str(body_text or ""))
        lowered = text.casefold()
        starts = [
            index
            for marker in ("音乐版权检查", "music copyright check")
            if (index := lowered.find(marker.casefold())) >= 0
        ]
        if not starts:
            return "not_present"
        start = min(starts)
        ends = [
            index
            for marker in (
                "内容快速检查",
                "content check lite",
                "content check",
            )
            if (index := lowered.find(marker.casefold(), start + 1)) >= 0
        ]
        segment = lowered[start : min(ends) if ends else start + 500]
        if any(
            marker in segment
            for marker in (
                "未发现问题",
                "no issues found",
                "check complete",
                "check completed",
                "passed",
            )
        ):
            return "passed"
        if any(
            marker in segment
            for marker in (
                "正在检查",
                "checking",
                "in progress",
            )
        ):
            return "pending"
        if any(
            marker in segment
            for marker in (
                "发现问题",
                "issues found",
                "copyright issue",
                "failed",
            )
        ):
            return "failed"
        return "unknown"

    async def _wait_for_music_copyright_check(self, page) -> str:
        announced = False
        for attempt in range(MUSIC_COPYRIGHT_CHECK_POLL_ATTEMPTS):
            await self._wait_for_manual_intervention(page)
            state = self._music_copyright_check_state(await _body_text(page))
            if state == "not_present":
                return state
            if state == "passed":
                self._emit_form_stage("copyright_check_verified")
                return state
            if state == "failed":
                raise TikTokPublishError(
                    "tiktok_copyright_check_failed",
                    "TikTok 音乐版权检查发现问题，已停止在发布前",
                )
            if state == "unknown":
                raise TikTokPublishError(
                    "tiktok_copyright_check_unreadable",
                    "TikTok 音乐版权检查状态无法安全回读，已停止在发布前",
                )
            if not announced:
                announced = True
                self._emit_form_stage("copyright_check_waiting")
            if attempt < MUSIC_COPYRIGHT_CHECK_POLL_ATTEMPTS - 1:
                await page.wait_for_timeout(MUSIC_COPYRIGHT_CHECK_POLL_INTERVAL_MS)
        raise TikTokPublishError(
            "tiktok_copyright_check_timeout",
            "TikTok 音乐版权检查在安全等待时间内没有完成，已停止在发布前",
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

    async def _final_action_button(self, base):
        if self._schedule_form is not None and self._schedule_target is not None:
            return await self._schedule_form.final_button(self._schedule_target)
        return await self._post_button(base)

    async def _click_immediate_post(self, page, button) -> None:
        observer = self.final_action_observer
        if not callable(observer):
            # Compatibility path for isolated/legacy callers.  The controlled
            # service always binds an observer and therefore uses the verified
            # mouse-dispatch path below.
            await button.click(no_wait_after=True)
            return
        await button.scroll_into_view_if_needed(timeout=5000)
        await self._center_action_button(button)
        # Re-check actionability immediately before consuming the one-time
        # authorization.  This is non-mutating and catches a late overlay or
        # remount between the final snapshot and the actual click.
        await button.click(trial=True, timeout=5000)
        observer()
        # The final control currently lives inside TikTok Studio's upload
        # frame.  A page-level coordinate click can land in the top-level
        # document even when the locator trial succeeds, producing no DOM
        # event and no platform response.  Dispatch through the same verified
        # locator so Playwright preserves the correct frame and element.
        await button.click(no_wait_after=True, timeout=5000)
        self._emit_form_stage("final_click_dispatched")

    @staticmethod
    def _is_pending_checks_confirmation(text: object) -> bool:
        normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
        has_continue_prompt = any(
            marker in normalized
            for marker in (
                "继续发布",
                "仍要发布",
                "continue posting",
                "still post",
            )
        )
        has_pending_check = any(
            marker in normalized
            for marker in (
                "版权检查未完成",
                "检查尚未完成",
                "copyright check hasn't finished",
                "copyright check has not finished",
                "check is not complete",
                "checks are not complete",
            )
        )
        return has_continue_prompt and has_pending_check

    async def _confirm_pending_checks_dialog(self, page, base) -> bool:
        """Confirm TikTok's known post-check warning exactly once.

        TikTok can keep the longer content check running after the music
        copyright check has passed.  The first Post click then opens a second
        confirmation instead of submitting.  Only that specifically identified
        warning is accepted; unrelated dialogs remain untouched.
        """

        dialog_seen = False
        confirm_labels = (
            "立即发布",
            "Post now",
            "Post Now",
            "Post anyway",
            "Continue posting",
        )
        for attempt in range(PENDING_CHECKS_CONFIRM_POLL_ATTEMPTS):
            await self._wait_for_manual_intervention(page)
            roots = [base]
            if page is not base:
                roots.append(page)
            matches = []
            for root in roots:
                text = await _body_text(root)
                if not self._is_pending_checks_confirmation(text):
                    continue
                dialog_seen = True
                get_by_role = getattr(root, "get_by_role", None)
                if not callable(get_by_role):
                    continue
                for label in confirm_labels:
                    candidates = get_by_role("button", name=label, exact=True)
                    try:
                        count = min(await candidates.count(), 3)
                    except Exception:
                        continue
                    for index in range(count):
                        candidate = candidates.nth(index) if count > 1 else candidates
                        try:
                            if await candidate.is_visible() and await candidate.is_enabled():
                                matches.append(candidate)
                        except Exception:
                            continue
            if len(matches) > 1:
                raise TikTokPublishError(
                    "tiktok_pending_checks_confirmation_ambiguous",
                    "TikTok 发布后的继续确认入口不唯一，结果需要人工核对",
                    outcome_ambiguous=True,
                )
            if matches:
                confirm = matches[0]
                await confirm.click(trial=True, timeout=5000)
                self._emit_form_stage("pending_checks_confirmed")
                await confirm.click(no_wait_after=True, timeout=5000)
                return True
            if tiktok_publish_success_signal(
                url=getattr(page, "url", ""),
                feedback_text="",
            ):
                return False
            if attempt < PENDING_CHECKS_CONFIRM_POLL_ATTEMPTS - 1:
                wait_for_timeout = getattr(page, "wait_for_timeout", None)
                if not callable(wait_for_timeout):
                    return False
                await wait_for_timeout(PENDING_CHECKS_CONFIRM_POLL_INTERVAL_MS)
        if dialog_seen:
            raise TikTokPublishError(
                "tiktok_pending_checks_confirmation_missing",
                "TikTok 已显示继续发布确认，但无法唯一点击“立即发布”，结果需要人工核对",
                outcome_ambiguous=True,
            )
        return False

    def _emit_schedule_checkpoint(self, stage: str) -> None:
        observer = self.schedule_checkpoint_observer
        if not callable(observer):
            raise TikTokPublishError(
                "tiktok_schedule_outcome_unknown",
                "TikTok 排期受理状态无法持久化",
                outcome_ambiguous=True,
            )
        observer(stage)

    async def _public_content_list_receipt(
        self,
        page,
    ) -> dict[str, str] | None:
        """Read one exact public Studio row after TikTok accepts the post."""

        normalized_url = str(getattr(page, "url", "") or "").casefold()
        if not any(
            route in normalized_url
            for route in (
                "/tiktokstudio/content",
                "/tiktokstudio/posts",
                "/creator-center/content",
            )
        ):
            return None
        expected_caption = " ".join(
            canonicalize_tiktok_caption(self._caption()).split()
        )
        expected_handle = str(self.expected_account_reference or "").strip()
        expected_handle = expected_handle.lstrip("@").casefold()
        if not expected_caption or not expected_handle:
            return None
        anchors = page.locator('a[href*="/video/"]')
        try:
            count = min(await anchors.count(), 100)
        except Exception:
            return None
        exact: list[dict[str, str]] = []
        for index in range(count):
            anchor = anchors.nth(index)
            try:
                if not await anchor.is_visible():
                    continue
                caption = " ".join(
                    canonicalize_tiktok_caption(await anchor.inner_text()).split()
                )
                if caption != expected_caption:
                    continue
                href = str(await anchor.get_attribute("href") or "").strip()
                parsed = urlsplit(href)
                match = re.fullmatch(
                    r"/@([^/]+)/video/([0-9]{5,})",
                    parsed.path.rstrip("/"),
                )
                if (
                    parsed.scheme.casefold() != "https"
                    or (parsed.hostname or "").casefold()
                    not in {"tiktok.com", "www.tiktok.com"}
                    or match is None
                    or match.group(1).casefold() != expected_handle
                ):
                    continue
                row_text = str(
                    await anchor.evaluate(
                        """
                        element => {
                          let node = element;
                          let best = '';
                          while (node && node.parentElement) {
                            node = node.parentElement;
                            const links = node.querySelectorAll('a[href*="/video/"]');
                            if (links.length > 1) break;
                            if (links.length === 1) best = node.innerText || best;
                          }
                          return best;
                        }
                        """
                    )
                    or ""
                )
                row_lines = {
                    unicodedata.normalize("NFKC", line).strip().casefold()
                    for line in row_text.splitlines()
                    if line.strip()
                }
                if not row_lines.intersection({"所有人", "公开", "everyone", "public"}):
                    continue
            except Exception:
                continue
            content_id = match.group(2)
            exact.append(
                {
                    "contentId": content_id,
                    "contentUrl": (
                        f"https://www.tiktok.com/@{expected_handle}/video/{content_id}"
                    ),
                    "publishedAt": _uploader_shanghai_now().isoformat(),
                    "visibility": "public",
                    "evidence": "tiktok_studio_public_content_exact_match",
                }
            )
        if len(exact) == 1:
            return exact[0]
        return None

    async def _wait_for_publish_result(self, page) -> str | dict[str, str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PUBLISH_RESULT_TIMEOUT_SECONDS
        while loop.time() < deadline:
            await self._wait_for_manual_intervention(page)
            # TikTok may navigate to the content list while the old upload DOM is
            # being torn down.  Reading its alerts can then time out even though
            # the URL already provides the platform acceptance signal.
            signal = tiktok_publish_success_signal(
                url=page.url,
                feedback_text="",
            )
            if signal == "platform_content_route":
                receipt = await self._public_content_list_receipt(page)
                if receipt is not None:
                    return receipt
            elif signal:
                return signal
            try:
                feedback = await _feedback_text(page)
            except Exception:
                # One unreadable feedback frame is transient evidence, not a
                # terminal outcome.  Keep polling until the bounded deadline.
                feedback = ""
            signal = tiktok_publish_success_signal(
                url=page.url,
                feedback_text=feedback,
            )
            if signal == "platform_content_route":
                receipt = await self._public_content_list_receipt(page)
                if receipt is not None:
                    return receipt
            elif signal:
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
        await self._wait_for_music_copyright_check(page)
        snapshot = await self._verify_form_snapshot(page, base)
        if self._schedule_target is not None:
            validator = self.authorized_snapshot_validator
            if not callable(validator):
                raise TikTokPublishError(
                    "tiktok_form_snapshot_mismatch",
                    "TikTok 定时表单缺少授权快照校验",
                )
            validator(snapshot)
        button = await self._final_action_button(base)
        if button is None:
            raise RuntimeError("TikTok 最终发布按钮不可用，未执行发布")
        if self._submit_consumed:
            raise TikTokPublishError(
                "tiktok_final_action_already_consumed",
                "TikTok 最终动作已在本会话消费，禁止再次调用",
            )
        if self._schedule_form is not None and self._schedule_target is not None:
            expected_caption = canonicalize_tiktok_caption(self._caption())
            baseline = (
                await self._schedule_form.capture_scheduled_content_baseline(
                    self.expected_account_reference
                )
            )
            submitted_after = _uploader_shanghai_now()
            expectation = TikTokScheduledContentExpectation(
                account_reference=self.expected_account_reference,
                expected_caption=expected_caption,
                caption_sha256=hashlib.sha256(
                    expected_caption.encode("utf-8")
                ).hexdigest(),
                target=self._schedule_target,
                submitted_after=submitted_after,
                baseline_row_keys=baseline.row_keys,
            )
            self._submit_consumed = True
            publish_event("tiktok_final_click", "TikTok 已确认，正在点击 Schedule")
            try:
                await button.click()
                acceptance = await self._schedule_form.wait_for_acceptance(
                    self._schedule_target
                )
                self._emit_schedule_checkpoint("scheduled_accepted")
                readback = await self._schedule_form.readback_scheduled_content(
                    expectation
                )
                if readback is None:
                    raise TikTokPublishError(
                        "tiktok_schedule_outcome_unknown",
                        "TikTok 定时提交后未能唯一回读内容",
                        outcome_ambiguous=True,
                    )
            except Exception as exc:
                if isinstance(exc, TikTokPublishError) and exc.error_code in {
                    "tiktok_publish_rejected",
                    "tiktok_schedule_out_of_range",
                }:
                    raise
                if isinstance(exc, TikTokPublishError) and exc.outcome_ambiguous:
                    raise
                raise TikTokPublishError(
                    "tiktok_schedule_outcome_unknown",
                    "TikTok 定时最终动作后结果无法确认",
                    outcome_ambiguous=True,
                ) from None
            return {
                "status": "scheduled",
                "phase": "scheduled_readback_confirmed",
                "evidence": readback.evidence,
                "formSnapshot": snapshot,
                "receipt": {
                    "scheduleMode": "platform_native",
                    "scheduledAt": readback.scheduled_at,
                    "scheduleTimezone": readback.schedule_timezone,
                    "platformAccepted": isinstance(
                        acceptance, TikTokScheduleAcceptance
                    ),
                    "scheduledReadbackConfirmed": True,
                    "contentId": readback.content_id,
                    "contentUrl": readback.content_url,
                    "publishedAt": None,
                },
            }
        self._submit_consumed = True
        publish_event("tiktok_final_click", "TikTok 已确认，正在点击 Post")
        await self._click_immediate_post(page, button)
        await self._confirm_pending_checks_dialog(page, base)
        signal = await self._wait_for_publish_result(page)
        if isinstance(signal, dict):
            evidence = str(signal.get("evidence") or "")
            result = {
                "status": "published",
                "evidence": evidence,
                "formSnapshot": snapshot,
                "receipt": dict(signal),
            }
        else:
            evidence = signal
            result = {
                "status": "published",
                "evidence": evidence,
                "formSnapshot": snapshot,
            }
        publish_event(
            "tiktok_publish_verified",
            "TikTok 发布结果已回读",
            reference=evidence,
        )
        return result

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
            storage_state = load_sanitized_tiktok_storage_state_file(
                Path(self.account_file)
            )
            context = await new_publish_context(
                browser,
                storage_state=storage_state,
            )
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
            replace_tiktok_storage_state_file(
                Path(self.account_file),
                await context.storage_state(),
            )
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
                        account_file=None,
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
