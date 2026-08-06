# -*- coding: utf-8 -*-
from datetime import datetime
from time import sleep
import hashlib
from io import BytesIO
import re
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Playwright, async_playwright, Page
import os
import asyncio
from PIL import Image

from conf import LOCAL_CHROME_PATH
from utils.base_social_media import (
    set_init_script,
    launch_publish_browser,
    new_publish_context,
    goto_and_reveal,
    keep_browser_open_for_dry_run,
    save_context_storage_state,
)
from utils.log import douyin_logger, DOUYIN_SCREENSHOT_DIR
from utils.platform_draft import record_draft_field_warning
from utils.publish_limits import normalize_publish_tags


class TopicCandidateUnavailable(RuntimeError):
    """抖音未返回某个话题的精确候选。"""


async def cookie_auth(account_file):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://creator.douyin.com/creator-micro/content/upload")
        try:
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=5000)
        except:
            print("[+] 等待5秒 cookie 失效")
            await context.close()
            await browser.close()
            return False
        # 2024.06.17 抖音创作者中心改版
        if await page.get_by_text('手机号登录').count() or await page.get_by_text('扫码登录').count():
            print("[+] 等待5秒 cookie 失效")
            return False
        else:
            print("[+] cookie 有效")
            return True


async def douyin_setup(account_file, handle=False):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            # Todo alert message
            return False
        douyin_logger.info('[+] cookie文件不存在或已失效，即将自动打开浏览器，请扫码登录，登陆后会自动生成cookie文件')
        await douyin_cookie_gen(account_file)
    return True


async def douyin_cookie_gen(account_file):
    async with async_playwright() as playwright:
        options = {
            'headless': False,
            # 'args': ['--start-maximized']  # 启动时最大化窗口
        }
        # Make sure to run headed.
        browser = await playwright.chromium.launch(**options)
        # Setup context however you like.
        context = await browser.new_context(
            permissions=[],  # 禁用所有权限请求
            geolocation=None,  # 禁用地理位置
            locale='zh-CN',  # 设置语言为中文
            timezone_id='Asia/Shanghai'  # 设置时区
        )
        context = await set_init_script(context)
        # Pause the page, and start recording manually.
        page = await context.new_page()
        await page.goto("https://creator.douyin.com/")
        await page.pause()
        # 点击调试器的继续，保存cookie
        await save_context_storage_state(context, account_file)


class DouYinVideo(object):
    DRAFT_CONTROL_PATTERN = re.compile(r"^(?:暂存离开|保存草稿)$")
    DRAFT_DIRECT_CONFIRMATIONS = (
        "草稿已保存",
        "已保存到草稿",
        "已保存至草稿",
        "已保存为草稿",
        "保存草稿成功",
    )
    DRAFT_MANAGE_EVIDENCE = (
        "未发布",
        "未发布草稿",
        "继续编辑",
        "编辑草稿",
    )
    DRAFT_RESTORE_EVIDENCE = (
        "还有上次未发布的视频",
        "存在上次未发布的视频",
        "上次未发布的视频",
    )
    DRAFT_RESTORE_PATTERN = re.compile(
        r"^(?:你)?(?:还有|存在)上次未发布的视频(?:[，,]?\s*是否继续编辑)?[？?]?$"
    )
    DRAFT_RESTORE_FRAGMENT_PATTERN = re.compile(r"上次未发布的视频")
    DRAFT_CONTROL_WAIT_ATTEMPTS = 15
    DRAFT_VERIFY_ATTEMPTS = 30
    TOPIC_CANDIDATE_WAIT_ATTEMPTS = 12
    UPLOAD_WAIT_ATTEMPTS = 180
    COVER_VERIFY_ATTEMPTS = 60
    PUBLISH_RESULT_WAIT_ATTEMPTS = 300
    PUBLISH_SECURITY_VERIFICATION_TEXTS = (
        "接收短信验证码",
        "获取验证码",
        "使用原设备扫码",
    )

    def __init__(self, title, file_path, tags, publish_date: datetime, account_file, category=None, thumbnail_path=None, thumbnail_paths=None, dry_run=False, dry_run_hold_browser=True, save_draft_only=False, description=None):
        self.title = title  # 视频标题
        self.file_path = file_path
        all_tags = normalize_publish_tags(tags, max_count=1000)
        if save_draft_only and len(all_tags) > 5:
            raise ValueError(f"抖音草稿最多允许 5 个话题，当前为 {len(all_tags)} 个")
        self.tags = normalize_publish_tags(tags)
        self.publish_date = publish_date
        self.account_file = account_file
        self.category = category  # 新增category参数
        self.date_format = '%Y年%m月%d日 %H:%M'
        self.local_executable_path = LOCAL_CHROME_PATH
        self.thumbnail_path = thumbnail_path
        self.thumbnail_paths = dict(thumbnail_paths or {})
        if thumbnail_path and "3:4" not in self.thumbnail_paths:
            self.thumbnail_paths["3:4"] = thumbnail_path
        self.dry_run = dry_run
        self.dry_run_hold_browser = dry_run_hold_browser
        self.save_draft_only = bool(save_draft_only)
        self.description = description
        self.form_verification = None
        self.cover_verification = None
        self.draft_save_result = None
        self.location_payload = None
        self.location_verification = ""
        self.commerce_payload = None
        self.commerce_verification = {}
        self.commerce_discovery_payload = None
        self.commerce_candidates = []
        # 抖音带货视频不设置独立封面，避免封面裁剪弹层遮挡后续的地点、
        # 带货和定时控件。普通抖音发布仍沿用原有封面流程。
        self.skip_thumbnail = False
        self.music_payload = None
        self.music_verification = {}
        self.schedule_verification = ""
        self.publish_result = None
        # 仅由抖音带货临时会话注入；普通发布保持没有客户端进度回调的旧行为。
        self.progress_callback = None
        if self.save_draft_only and self.dry_run:
            raise ValueError("保存草稿模式与 dry_run 预发布检查不能同时开启")

    def _report_commerce_progress(
        self,
        phase: str,
        label: str,
        state: str = "running",
    ) -> None:
        """把非敏感阶段投递给带货桌面端，不影响普通发布或上传本身。"""

        callback = getattr(self, "progress_callback", None)
        if not callable(callback):
            return
        try:
            callback({"phase": phase, "label": label, "state": state})
        except Exception:
            douyin_logger.debug("抖音带货客户端进度回调失败")

    async def set_schedule_time_douyin(self, page, publish_date):
        target_time = publish_date.strftime("%Y-%m-%d %H:%M")
        douyin_logger.info(f"正在设置抖音定时发布时间：{target_time}")

        schedule_radio = await self._schedule_radio_douyin(page)
        for _ in range(3):
            checked = (await schedule_radio.get_attribute("data-checked") or "").lower()
            if checked == "true":
                break
            await schedule_radio.click(timeout=5000)
            await page.wait_for_timeout(1000)
        checked = (await schedule_radio.get_attribute("data-checked") or "").lower()
        if checked != "true":
            raise RuntimeError("抖音定时发布开关点击后未保持选中")

        date_input = await self._schedule_time_input_douyin(page)
        await date_input.click(force=True, timeout=5000)
        await date_input.fill(target_time, timeout=5000)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(800)

        try:
            await date_input.evaluate(
                """
                node => {
                  node.dispatchEvent(new Event('input', { bubbles: true }));
                  node.dispatchEvent(new Event('change', { bubbles: true }));
                  node.blur();
                }
                """
            )
        except Exception:
            await page.keyboard.press("Escape")
        await page.wait_for_timeout(800)

        actual_time = await self._schedule_time_input_value(date_input)

        if self._schedule_time_matches(actual_time, target_time):
            self.schedule_verification = actual_time or target_time
            douyin_logger.success(f"抖音定时发布时间已确认：{actual_time or target_time}")
            return self.schedule_verification

        raise RuntimeError(f"抖音定时发布时间写入失败，目标={target_time}，实际={actual_time}")

    async def clear_schedule_time_douyin(self, page) -> str:
        """切回实际可见的“立即发布”，并回读确认没有遗留定时。"""

        immediate_radio = await self._publish_mode_radio_douyin(page, "立即发布")
        for _ in range(3):
            checked = (await immediate_radio.get_attribute("data-checked") or "").lower()
            if checked == "true":
                break
            await immediate_radio.click(timeout=5000)
            await page.wait_for_timeout(800)
        checked = (await immediate_radio.get_attribute("data-checked") or "").lower()
        if checked != "true":
            raise RuntimeError("抖音立即发布开关点击后未保持选中")

        schedule_radio = await self._schedule_radio_douyin(page)
        schedule_checked = (await schedule_radio.get_attribute("data-checked") or "").lower()
        if schedule_checked == "true":
            raise RuntimeError("抖音切回立即发布后定时发布仍处于选中状态")

        actual_time = await self.read_schedule_time_douyin(page)
        if actual_time:
            raise RuntimeError(f"抖音切回立即发布后仍回读到定时：{actual_time}")
        self.schedule_verification = ""
        douyin_logger.success("抖音发布方式已切回立即发布")
        return ""

    async def read_schedule_time_douyin(self, page) -> str:
        """只读回当前编辑页的定时状态，不点击、不修改平台字段。"""

        schedule_radio = await self._schedule_radio_douyin(page)
        checked = (await schedule_radio.get_attribute("data-checked") or "").lower()
        if checked != "true":
            return ""
        date_input = await self._schedule_time_input_douyin(page)
        actual_time = await self._schedule_time_input_value(date_input)
        if not actual_time:
            raise RuntimeError("抖音定时发布已开启但未能回读日期和时间")
        return actual_time

    @staticmethod
    async def _schedule_time_input_value(date_input) -> str:
        try:
            return (await date_input.input_value(timeout=3000)).strip()
        except Exception:
            return ""

    async def _schedule_radio_douyin(self, page):
        """定位新版抖音定时发布的唯一可见标签。"""

        return await self._publish_mode_radio_douyin(page, "定时发布")

    async def _publish_mode_radio_douyin(self, page, expected_text: str):
        """定位发布设置中唯一可见的“立即发布”或“定时发布”选项。"""

        mode_radio = None
        labels = page.locator("label")
        for index in range(await labels.count()):
            label = labels.nth(index)
            try:
                if not await label.is_visible():
                    continue
                if " ".join((await label.inner_text()).split()) == expected_text:
                    if mode_radio is not None:
                        raise RuntimeError(
                            f"抖音发布方式“{expected_text}”出现多个可点击节点，已安全停止"
                        )
                    mode_radio = label
            except RuntimeError:
                raise
            except Exception:
                continue
        if mode_radio is None:
            raise RuntimeError(f"抖音未找到唯一可点击的发布方式“{expected_text}”")
        return mode_radio

    async def _schedule_time_input_douyin(self, page):
        """定位开启定时后唯一可用的日期时间输入框。"""

        date_input = None
        date_inputs = page.locator('.semi-input[placeholder="日期和时间"]')
        for index in range(await date_inputs.count()):
            candidate = date_inputs.nth(index)
            try:
                if not await candidate.is_visible() or not await candidate.is_enabled():
                    continue
                if date_input is not None:
                    raise RuntimeError("抖音定时日期输入框出现多个可用节点，已安全停止")
                date_input = candidate
            except RuntimeError:
                raise
            except Exception:
                continue
        if date_input is None:
            raise RuntimeError("抖音开启定时发布后未找到可用日期输入框")
        return date_input

    @staticmethod
    def _schedule_time_matches(actual_time: str, target_time: str) -> bool:
        normalized_actual = "".join(str(actual_time or "").split())
        normalized_target = "".join(str(target_time or "").split())
        return bool(normalized_actual) and (
            normalized_actual == normalized_target
            or normalized_actual.startswith(normalized_target)
            or normalized_target in normalized_actual
        )

    async def handle_upload_error(self, page):
        douyin_logger.info('视频出错了，重新上传中')
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def _visible_title_inputs(self, page):
        """只定位作品标题，排除定时发布等其他文本输入框。"""
        selectors = (
            "input[placeholder*='作品标题']:visible",
            "input[placeholder*='填写标题']:visible",
            "input[placeholder*='标题']:visible",
        )
        for selector in selectors:
            candidates = await self._visible_enabled_items(page.locator(selector))
            if candidates:
                return candidates
        return []

    async def clear_platform_title(self, page):
        title_inputs = await self._visible_title_inputs(page)
        if len(title_inputs) != 1:
            raise RuntimeError(
                f"抖音独立标题输入框数量异常：{len(title_inputs)}，已停止填写"
            )
        title = (self.title or "").strip()
        await title_inputs[0].fill(title)
        actual_title = (await title_inputs[0].input_value()).strip()
        if actual_title != title:
            raise RuntimeError(f"抖音标题写入后不一致：期望={title}，实际={actual_title}")
        douyin_logger.info("抖音独立标题已填写并回读确认" if title else "抖音独立标题已留空并回读确认")

    async def fill_description_and_topics(self, page):
        douyin_logger.info("正在填充抖音作品描述...")
        editor = page.locator(".zone-container").first
        await editor.wait_for(state="visible", timeout=15000)
        body = self.description if self.description is not None else self.title
        body = str(body or "")
        if not body.strip():
            raise RuntimeError("抖音作品详情为空，已停止填写")
        if "#" in body:
            raise RuntimeError("抖音作品详情包含手写 # 文本，已停止填写")

        await self._fill_editor_body(page, editor, body)
        expected_topics = [str(tag).strip().lstrip("#") for tag in self.tags if str(tag).strip().lstrip("#")]
        if self.save_draft_only and not 1 <= len(expected_topics) <= 5:
            raise RuntimeError(f"抖音草稿必须包含 1-5 个平台话题，当前为 {len(expected_topics)} 个")

        confirmed_topics, skipped_topics = await self._apply_platform_topics(
            page,
            editor,
            body,
            expected_topics,
        )
        if skipped_topics:
            if not confirmed_topics:
                raise RuntimeError("抖音没有返回任何可用的话题候选，已停止填写")
            self.tags = confirmed_topics
            douyin_logger.warning(
                f"抖音未返回精确候选，已跳过单个话题：{skipped_topics}；"
                f"保留话题：{confirmed_topics}"
            )

        self.form_verification = await self.verify_prepublish_form(page, require_covers=False)
        douyin_logger.info(f"抖音作品详情已写入，并确认{len(confirmed_topics)}个候选话题组件")

    async def sync_uploaded_editor_content(
        self,
        page: Page,
        *,
        title: str,
        description: str,
        tags: list[str],
    ) -> dict[str, object]:
        """在现有抖音编辑页同步文字字段并完成页面回读。

        此入口只处理标题、文案与平台话题组件；不触碰视频上传、封面、音乐、
        定位、声明、定时、草稿、预览或发表。
        """

        self.title = str(title or "").strip()
        self.description = str(description or "").strip()
        self.tags = [
            str(tag).strip().lstrip("#")
            for tag in tags
            if str(tag).strip().lstrip("#")
        ]
        await self.clear_platform_title(page)
        await self.fill_description_and_topics(page)
        form = dict(self.form_verification or {})
        return {
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "form": form,
        }

    async def _apply_platform_topics(self, page, editor, body, expected_topics):
        confirmed_topics = []
        skipped_topics = []
        for tag_name in expected_topics:
            try:
                await self._add_platform_topic(page, editor, tag_name)
                confirmed_topics.append(tag_name)
            except TopicCandidateUnavailable:
                skipped_topics.append(tag_name)
                await self._fill_editor_body(page, editor, body)
                for confirmed_tag in confirmed_topics:
                    await self._add_platform_topic(page, editor, confirmed_tag)
        return confirmed_topics, skipped_topics

    async def _fill_editor_body(self, page, editor, body):
        """清空后写入作品文案，并确认编辑器没有保留旧内容。

        Windows 端的抖音富文本编辑器偶尔会吞掉 ``fill(\"\")`` 的清空事件，
        导致新输入的文案追加在旧内容之后。这里通过键盘全选删除和两次页面回读
        确认写入结果；有限重试后仍不一致才停止流程，避免保存错误草稿。
        """

        expected_body = self._normalize_body_text(body)
        lines = str(body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for attempt in range(1, 4):
            await editor.fill("")
            await editor.click(force=True)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(250)

            actual_after_clear = self._normalize_body_text(
                await self._read_raw_editor_text(editor)
            )
            if actual_after_clear:
                if attempt < 3:
                    douyin_logger.warning(
                        f"抖音详情编辑器未清空，第{attempt}次重试：{actual_after_clear!r}"
                    )
                    continue
                raise RuntimeError(
                    "抖音详情编辑器未能清空旧文案，已停止同步以避免内容重复"
                )

            for index, line in enumerate(lines):
                if line:
                    await page.keyboard.insert_text(line)
                if index < len(lines) - 1:
                    await page.keyboard.press("Enter")
            await page.wait_for_timeout(350)

            actual_after_write = self._normalize_body_text(
                await self._read_raw_editor_text(editor)
            )
            if actual_after_write == expected_body:
                return
            if attempt < 3:
                douyin_logger.warning(
                    "抖音详情编辑器写入回读不一致，"
                    f"第{attempt}次重试：期望={expected_body!r}，实际={actual_after_write!r}"
                )
                continue
            raise RuntimeError(
                "抖音详情写入后回读不一致，已停止同步以避免内容重复；"
                f"期望={expected_body!r}，实际={actual_after_write!r}"
            )

    @staticmethod
    def _normalize_topic_mention(value):
        return "".join(str(value or "").replace("\u200b", "").split()).lstrip("#")

    async def _read_topic_mentions(self, editor):
        values = await editor.locator('[data-mention="#"]').all_text_contents()
        return [self._normalize_topic_mention(value) for value in values if self._normalize_topic_mention(value)]

    async def _read_raw_editor_text(self, editor):
        span_values = await editor.locator('span[data-string="true"]').all_text_contents()
        if any("\u200b" in value for value in span_values):
            return "".join(value.replace("\u200b", "\n") for value in span_values)
        return await editor.evaluate(
            """
            node => {
              const mentions = [...node.querySelectorAll('[data-mention="#"]')];
              const previous = mentions.map(item => item.style.display);
              try {
                mentions.forEach(item => { item.style.display = 'none'; });
                return node.innerText;
              } finally {
                mentions.forEach((item, index) => { item.style.display = previous[index]; });
              }
            }
            """
        )

    async def _find_unique_topic_candidate(self, page, tag_name):
        for attempt in range(self.TOPIC_CANDIDATE_WAIT_ATTEMPTS):
            candidates = page.locator('span[class*="tag-hash-view-name"]')
            exact_matches = []
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                try:
                    if await candidate.is_visible() and (await candidate.inner_text()).strip() == tag_name:
                        exact_matches.append(candidate)
                except Exception:
                    continue
            if len(exact_matches) == 1:
                return exact_matches[0]
            if len(exact_matches) > 1:
                raise RuntimeError(f"抖音话题“{tag_name}”出现多个可见的精确候选，已停止选择")
            if attempt < self.TOPIC_CANDIDATE_WAIT_ATTEMPTS - 1:
                await page.wait_for_timeout(500)
        raise TopicCandidateUnavailable(f"抖音未返回话题“{tag_name}”的精确平台候选")

    async def _add_platform_topic(self, page, editor, tag_name):
        add_controls = await self._visible_enabled_items(page.get_by_text("#添加话题", exact=True))
        if len(add_controls) != 1:
            raise RuntimeError(
                f"抖音“#添加话题”入口数量异常：{len(add_controls)}，已停止选择“{tag_name}”"
            )
        before_mentions = await self._read_topic_mentions(editor)
        await add_controls[0].click(timeout=5000)
        await editor.press_sequentially(tag_name, delay=50)
        candidate = await self._find_unique_topic_candidate(page, tag_name)
        await candidate.click(timeout=5000)

        for attempt in range(10):
            mentions = await self._read_topic_mentions(editor)
            if len(mentions) == len(before_mentions) + 1 and mentions[-1] == tag_name:
                return
            if attempt < 9:
                await page.wait_for_timeout(300)
        raise RuntimeError(f"抖音话题“{tag_name}”点击候选后未形成平台话题组件")

    @staticmethod
    def _normalize_body_text(value):
        text = str(value or "").replace("\u200b", "").replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.replace("\u00a0", " ").rstrip() for line in text.split("\n")]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    async def verify_prepublish_form(self, page, require_covers=True):
        editor = page.locator(".zone-container").first
        await editor.wait_for(state="visible", timeout=15000)
        expected_body = self.description if self.description is not None else self.title
        raw_text = await self._read_raw_editor_text(editor)
        if "#" in raw_text:
            raise RuntimeError("抖音详情中仍存在手写 # 文本，不能保存草稿")
        expected_normalized = self._normalize_body_text(expected_body)
        actual_normalized = self._normalize_body_text(raw_text)
        if expected_normalized != actual_normalized:
            raise RuntimeError(
                "抖音详情回读与发布包不一致，不能保存草稿；"
                f"期望={expected_normalized!r}，实际={actual_normalized!r}"
            )

        expected_topics = [str(tag).strip().lstrip("#") for tag in self.tags if str(tag).strip().lstrip("#")]
        mentions = await self._read_topic_mentions(editor)
        if mentions != expected_topics:
            raise RuntimeError(
                f"抖音平台话题组件不一致：期望={expected_topics}，实际={mentions}"
            )

        expected_cover_ratios = [ratio for ratio in ("4:3", "3:4") if self.thumbnail_paths.get(ratio)]
        if self.save_draft_only and require_covers and expected_cover_ratios:
            actual_cover_ratios = (self.cover_verification or {}).get("ratios") or []
            if actual_cover_ratios != expected_cover_ratios:
                raise RuntimeError(
                    f"抖音双封面回读不完整：期望={expected_cover_ratios}，实际={actual_cover_ratios}"
                )
            await self.verify_cover_persistence(page, expected_cover_ratios)

        title_confirmed = None
        if self.description is not None:
            title_inputs = await self._visible_title_inputs(page)
            if len(title_inputs) != 1:
                raise RuntimeError(f"抖音标题输入框数量异常：{len(title_inputs)}，不能保存草稿")
            actual_title = (await title_inputs[0].input_value()).strip()
            expected_title = (self.title or "").strip()
            if actual_title != expected_title:
                raise RuntimeError(
                    f"抖音标题回读不一致：期望={expected_title}，实际={actual_title}"
                )
            title_confirmed = True

        return {
            "title_confirmed": title_confirmed,
            "detail_confirmed": True,
            "topics_confirmed": mentions,
            "topic_entry_method": "platform_candidate_selection",
            "covers_confirmed": list(expected_cover_ratios) if require_covers else [],
        }

    async def wait_publish_button_ready(self, page):
        self._assert_formal_publish_allowed()
        publish_button = page.get_by_role('button', name="发布", exact=True).last
        await publish_button.wait_for(state="visible", timeout=60000)

        for attempt in range(60):
            try:
                button_class = await publish_button.get_attribute("class") or ""
                disabled_attr = await publish_button.get_attribute("disabled")
                aria_disabled = await publish_button.get_attribute("aria-disabled")
                is_enabled = await publish_button.is_enabled(timeout=1000)
                if (
                    is_enabled
                    and disabled_attr is None
                    and aria_disabled != "true"
                    and "disabled" not in button_class.lower()
                ):
                    await publish_button.scroll_into_view_if_needed(timeout=3000)
                    return publish_button
            except Exception as e:
                douyin_logger.warning(f"抖音发布按钮状态检测失败，第{attempt + 1}次重试: {e}")
            await page.wait_for_timeout(1000)

        raise RuntimeError("抖音发布按钮长时间不可用，已停止点击，避免重复触发页面闪动")

    def _assert_formal_publish_allowed(self):
        if self.save_draft_only:
            raise RuntimeError("保存草稿模式已在代码层禁止定位或点击正式发布按钮")

    async def detect_publish_verification(self, page: Page):
        """识别当前页面唯一、可受控的发布验证挑战。

        此方法只保留挑战类型及二维码内存字节；不读取或返回页面文本、选择器、
        二维码地址等平台诊断数据。任何控件不唯一均不猜测目标。
        """

        current_url = str(page.url or "")
        if "/creator-micro/content/manage" in current_url:
            return None

        markers = await self._visible_exact_text(
            page,
            self.PUBLISH_SECURITY_VERIFICATION_TEXTS,
        )
        if not markers:
            return None

        inputs = await self._visible_enabled_items(page.get_by_role("textbox"))
        buttons = await self._visible_enabled_items(page.get_by_role("button"))
        if inputs:
            if len(inputs) != 1:
                raise RuntimeError("抖音验证输入框无法唯一确认，发布已安全停止")
            if len(buttons) != 1:
                raise RuntimeError("抖音验证确认按钮无法唯一确认，发布已安全停止")
            from app_core.douyin_verification import VerificationChallenge

            return VerificationChallenge(kind="sms", message="请在一键发客户端输入短信验证码")

        images = await self._visible_enabled_items(page.get_by_role("img"))
        if len(images) != 1:
            raise RuntimeError("抖音验证二维码无法唯一确认，发布已安全停止")
        try:
            qr_image = await images[0].screenshot()
            from app_core.douyin_verification import (
                VerificationChallenge,
                _validate_qr_image,
            )

            return VerificationChallenge(
                kind="qr",
                message="请在一键发客户端扫码完成验证",
                qr_image=_validate_qr_image(qr_image),
            )
        except Exception as exc:
            raise RuntimeError("抖音验证二维码无法在内存中解析，发布已安全停止") from exc

    async def apply_sms_verification_code(self, page: Page, challenge, code: str) -> None:
        """在原 Playwright 会话填入已由原生客户端提交的短信验证码。"""

        if getattr(challenge, "kind", "") != "sms":
            raise RuntimeError("当前抖音验证不是短信验证码，发布已安全停止")
        inputs = await self._visible_enabled_items(page.get_by_role("textbox"))
        buttons = await self._visible_enabled_items(page.get_by_role("button"))
        if len(inputs) != 1:
            raise RuntimeError("抖音验证输入框无法唯一确认，发布已安全停止")
        if len(buttons) != 1:
            raise RuntimeError("抖音验证确认按钮无法唯一确认，发布已安全停止")

        await inputs[0].fill(str(code))
        if await inputs[0].input_value() != str(code):
            raise RuntimeError("抖音验证码填写后未能回读，发布已安全停止")
        await buttons[0].click(timeout=10_000)
        for _ in range(20):
            if await self.detect_publish_verification(page) is None:
                return
            await page.wait_for_timeout(250)
        raise RuntimeError("抖音验证码确认后仍停留在验证页，发布已安全停止")

    async def _wait_formal_publish_result(self, page: Page, on_verification=None):
        security_verification_seen = False
        for attempt in range(self.PUBLISH_RESULT_WAIT_ATTEMPTS):
            current_url = str(page.url or "")
            if "/creator-micro/content/manage" in current_url:
                return {
                    "status": "published",
                    "verified_by": "content_manage_navigation",
                    "url": current_url,
                }

            challenge = await self.detect_publish_verification(page)
            if challenge is not None:
                security_verification_seen = True
                if not callable(on_verification):
                    raise RuntimeError("抖音要求二次安全验证，但当前会话没有可用验证协调器，发布已安全停止")
                result = on_verification(challenge)
                if hasattr(result, "__await__"):
                    await result
                continue

            if attempt < self.PUBLISH_RESULT_WAIT_ATTEMPTS - 1:
                await page.wait_for_timeout(1000)

        if security_verification_seen:
            raise RuntimeError(
                "抖音二次安全验证后 5 分钟内未进入作品管理页，本次未确认发布成功"
            )
        raise RuntimeError(
            "抖音点击发布后 5 分钟内未进入作品管理页，本次未确认发布成功"
        )

    @staticmethod
    async def _visible_enabled_items(locator):
        items = []
        for index in range(await locator.count()):
            item = locator.nth(index)
            try:
                if await item.is_visible() and await item.is_enabled():
                    items.append(item)
            except Exception:
                continue
        return items

    @staticmethod
    async def _visible_exact_text(page, values):
        visible = set()
        for value in values:
            locator = page.get_by_text(value, exact=True)
            for index in range(await locator.count()):
                try:
                    if await locator.nth(index).is_visible():
                        visible.add(value)
                        break
                except Exception:
                    continue
        return visible

    async def _visible_restore_evidence(self, page):
        """保守识别未发布视频提示；兼容嵌套节点、空白和零宽字符。"""
        locator = page.get_by_text(self.DRAFT_RESTORE_FRAGMENT_PATTERN)
        visible = set()
        for index in range(await locator.count()):
            item = locator.nth(index)
            try:
                if await item.is_visible():
                    raw = await item.inner_text(timeout=2000)
                    value = "".join(
                        str(raw or "")
                        .replace("\u200b", "")
                        .replace("\u00a0", " ")
                        .split()
                    )
                    if "上次未发布的视频" in value:
                        visible.add(value)
            except Exception:
                continue
        return visible

    async def _find_unique_continue_editing_control(self, page, required=True):
        role_controls = await self._visible_enabled_items(
            page.get_by_role("button", name="继续编辑", exact=True)
        )
        if len(role_controls) == 1:
            return role_controls[0]
        if len(role_controls) > 1:
            raise RuntimeError(f"草稿恢复页“继续编辑”按钮数量异常：{len(role_controls)}")

        text_controls = await self._visible_enabled_items(page.get_by_text("继续编辑", exact=True))
        if len(text_controls) == 1:
            return text_controls[0]
        if len(text_controls) > 1:
            raise RuntimeError(f"草稿恢复页“继续编辑”入口数量异常：{len(text_controls)}")
        if required:
            raise RuntimeError("草稿恢复页未找到唯一的“继续编辑”入口")
        return None

    async def _wait_restore_evidence(self, page, attempts=30):
        for attempt in range(attempts):
            evidence = await self._visible_restore_evidence(page)
            if evidence:
                return evidence
            if attempt < attempts - 1:
                await page.wait_for_timeout(1000)
        return set()

    async def _find_unique_draft_control(self, page):
        for attempt in range(self.DRAFT_CONTROL_WAIT_ATTEMPTS):
            locator = page.get_by_role("button", name=self.DRAFT_CONTROL_PATTERN)
            controls = await self._visible_enabled_items(locator)
            if len(controls) == 1:
                return controls[0]
            if len(controls) > 1:
                raise RuntimeError(
                    f"页面同时出现 {len(controls)} 个可操作的草稿控件，无法唯一判定，已停止点击"
                )
            if attempt < self.DRAFT_CONTROL_WAIT_ATTEMPTS - 1:
                await page.wait_for_timeout(1000)
        raise RuntimeError("未找到唯一可识别的“暂存离开/保存草稿”按钮，已停止点击")

    async def _assert_no_existing_draft(self, page, attempts=30):
        for attempt in range(attempts):
            evidence = await self._visible_restore_evidence(page)
            continue_control = await self._find_unique_continue_editing_control(page, required=False)
            if evidence or continue_control is not None:
                screenshot_path = await self._capture_draft_screenshot(page, "existing_draft_blocked")
                screenshot_note = f"，截图：{screenshot_path}" if screenshot_path else ""
                raise RuntimeError(
                    "检测到账号已有未发布草稿。为避免覆盖旧草稿，桥接器已停止；"
                    f"平台证据={sorted(evidence) or ['继续编辑']}{screenshot_note}"
                )
            if attempt < attempts - 1:
                await page.wait_for_timeout(500)

    async def _is_draft_title_visible(self, page):
        title = (self.title or "").strip()
        if not title:
            return False
        locator = page.get_by_text(title, exact=True)
        for index in range(await locator.count()):
            try:
                if await locator.nth(index).is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _verify_draft_saved(
        self,
        page,
        editor_url,
        baseline_confirmations,
        baseline_restore_evidence,
        form_evidence,
    ):
        for attempt in range(self.DRAFT_VERIFY_ATTEMPTS):
            confirmations = await self._visible_exact_text(page, self.DRAFT_DIRECT_CONFIRMATIONS)
            new_confirmations = confirmations - baseline_confirmations
            if new_confirmations:
                return {
                    "status": "draft_saved",
                    "verified_by": "platform_confirmation",
                    "evidence": sorted(new_confirmations),
                    "url": str(page.url),
                }

            restore_evidence = await self._visible_restore_evidence(page)
            current_url = str(page.url)
            left_editor = current_url != editor_url and "/content/post/video" not in current_url

            new_restore_evidence = restore_evidence - baseline_restore_evidence
            if left_editor and new_restore_evidence:
                raise RuntimeError(
                    "抖音仅返回“上次未发布的视频”提示。该提示属于当前自动化"
                    "浏览器的本地未发布缓存，不是平台后台草稿箱中的服务器草稿；"
                    "为避免假成功，本次不得记录为平台草稿已保存"
                )

            if attempt < self.DRAFT_VERIFY_ATTEMPTS - 1:
                await page.wait_for_timeout(1000)

        raise RuntimeError(
            "点击草稿控件后未发现新的保存成功回执，"
            "也未能通过“上次未发布的视频”入口回开并复核本次草稿"
        )

    async def _reopen_and_verify_draft(self, page, expected_form):
        control = await self._find_unique_continue_editing_control(page)
        await control.click(timeout=5000)
        editor = page.locator(".zone-container").first
        await editor.wait_for(state="visible", timeout=20000)
        restored_form = await self.verify_prepublish_form(page)
        comparable_keys = (
            "title_confirmed",
            "detail_confirmed",
            "topics_confirmed",
            "topic_entry_method",
            "covers_confirmed",
        )
        expected = {key: expected_form.get(key) for key in comparable_keys}
        actual = {key: restored_form.get(key) for key in comparable_keys}
        if actual != expected:
            raise RuntimeError(f"回开草稿后的表单证据不一致：期望={expected}，实际={actual}")
        return restored_form

    async def verify_existing_draft(self, page):
        """只读回开并核验已有草稿；不上传、不保存，也不触达正式发布控件。"""
        if not self.save_draft_only:
            raise RuntimeError("只有保存草稿模式才允许核验已有草稿")
        restore_evidence = await self._wait_restore_evidence(page)
        if not restore_evidence:
            raise RuntimeError("未发现平台的“上次未发布的视频”提示，无法核验已有草稿")

        control = await self._find_unique_continue_editing_control(page)
        await control.click(timeout=5000)
        editor = page.locator(".zone-container").first
        await editor.wait_for(state="visible", timeout=20000)

        form = await self.verify_prepublish_form(page, require_covers=False)
        ratios = [ratio for ratio in ("4:3", "3:4") if self.thumbnail_paths.get(ratio)]
        preview_present = {}
        preview_keys = {}
        for ratio in ratios:
            signature = await self._cover_card_signature(page, ratio)
            has_preview = bool(signature.get("images") or signature.get("backgrounds"))
            preview_present[ratio] = has_preview
            preview_keys[ratio] = self._cover_signature_key(signature)
            if not has_preview:
                raise RuntimeError(f"回开草稿后未发现抖音{ratio}封面预览")
        if len(set(preview_keys.values())) != len(preview_keys):
            raise RuntimeError("回开草稿后的横竖封面预览相同，无法证明双封面均已恢复")

        cover_markers = await self._wait_cover_success_markers(page)
        if not cover_markers:
            raise RuntimeError("回开草稿后未发现封面检测通过结果")
        form["covers_confirmed"] = ratios
        screenshot_path = await self._capture_draft_screenshot(page, "existing_draft_verified")
        if not screenshot_path or not os.path.isfile(screenshot_path) or os.path.getsize(screenshot_path) <= 0:
            raise RuntimeError("已有草稿核验截图未能落盘，不能记录为验收成功")
        return {
            "status": "local_unpublished_cache_verified",
            "verified_by": "existing_local_unpublished_cache_reopened_and_verified",
            "evidence": sorted(restore_evidence),
            "url": str(page.url),
            "form": form,
            "covers": {
                "ratios": ratios,
                "preview_present": preview_present,
                "platform_status": sorted(cover_markers),
            },
            "verification_screenshot": screenshot_path,
        }

    async def _capture_draft_screenshot(self, page, label):
        os.makedirs(DOUYIN_SCREENSHOT_DIR, exist_ok=True)
        screenshot_path = os.path.join(
            DOUYIN_SCREENSHOT_DIR,
            f"douyin_draft_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png",
        )
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            return screenshot_path
        except Exception as error:
            douyin_logger.warning(f"保存草稿截图未能写入（{label}）: {error}")
            return None

    async def _capture_draft_failure(self, page):
        return await self._capture_draft_screenshot(page, "unverified")

    async def save_draft_safely(self, page):
        """只执行可验证的草稿保存；任何歧义或缺少平台证据都会失败。"""
        if not self.save_draft_only:
            raise RuntimeError("只有显式传入 save_draft_only=True 才允许执行草稿保存")
        editor_url = str(page.url)
        form_evidence = await self.verify_prepublish_form(page)
        baseline_confirmations = await self._visible_exact_text(page, self.DRAFT_DIRECT_CONFIRMATIONS)
        baseline_restore_evidence = await self._visible_restore_evidence(page)
        pre_save_screenshot = await self._capture_draft_screenshot(page, "before_save")
        try:
            control = await self._find_unique_draft_control(page)
            try:
                control_name = (await control.inner_text(timeout=2000)).strip()
            except Exception:
                control_name = "暂存离开/保存草稿"
            await control.click(timeout=10000)
            result = await self._verify_draft_saved(
                page,
                editor_url,
                baseline_confirmations,
                baseline_restore_evidence,
                form_evidence,
            )
            result["control"] = control_name
            result["form"] = form_evidence
            result["pre_save_screenshot"] = pre_save_screenshot
            result["post_save_screenshot"] = await self._capture_draft_screenshot(page, "after_save")
            self.draft_save_result = result
            douyin_logger.success(f"抖音草稿已通过平台证据确认：{result['evidence']}")
            return result
        except Exception as error:
            screenshot_path = await self._capture_draft_failure(page)
            screenshot_note = f"，已保留截图：{screenshot_path}" if screenshot_path else ""
            raise RuntimeError(
                f"抖音草稿未验证成功，不得记录为已保存；原因：{error}{screenshot_note}"
            ) from error

    async def _save_draft_and_close(self, page, context, browser, managed_browser):
        try:
            return await self.save_draft_safely(page)
        finally:
            try:
                await save_context_storage_state(
                    context,
                    self.account_file,
                    include_indexed_db=True,
                )
                douyin_logger.success("抖音草稿会话状态已更新（含 IndexedDB）")
            except Exception as error:
                douyin_logger.warning(f"抖音草稿会话状态保存失败：{error}")
            if managed_browser:
                await context.close()
                await browser.close()

    async def prepare_uploaded_video_editor(
        self,
        page: Page,
        *,
        reveal_editor: bool = True,
    ) -> None:
        """进入抖音视频编辑页、上传视频并回读标题/文案。

        这段是“上传完成”与后续平台字段的明确边界。普通发布仍由
        :meth:`upload` 在同一次调用中继续处理封面、音乐、地点等字段；抖音
        带货分步向导则在同一受控会话中先调用本方法，再等待用户从客户端选择
        收藏音乐和地点。无论哪种路径，这里都不会保存草稿或点击发布。

        ``reveal_editor=False`` 仅用于带货向导的后台上传阶段：浏览器窗口保持
        隐藏，直到遇到必须人工处理的登录/验证才由上层明确前置。
        """
        try:
            if reveal_editor:
                await goto_and_reveal(
                    page,
                    "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
                    timeout=30000,
                )
            else:
                await page.goto(
                    "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            douyin_logger.info("[+] 成功进入version_2发布页面!")
        except Exception as e:
            douyin_logger.error(f"  [-] 超时未进入视频发布页面，cookies过期或者其他原因，重新尝试...{e}")
            if self.save_draft_only or not reveal_editor:
                mode = "带货后台上传" if not reveal_editor else "保存草稿模式"
                raise RuntimeError(f"{mode}未能确认进入抖音发布页，已停止后续操作") from e

        if self.save_draft_only:
            await page.wait_for_timeout(1200)
            await self._assert_no_existing_draft(page)

        self._report_commerce_progress("uploading_video", "正在上传视频")
        await page.locator("div[class^='upload-card'] input[type=file]").set_input_files(self.file_path)  #上传视频
        douyin_logger.info(f'[+]正在上传-------{self.title}.mp4')
        await asyncio.sleep(1)

        self._report_commerce_progress("reading_content", "正在回读标题与文案")
        await self.clear_platform_title(page)
        await self.fill_description_and_topics(page)

        upload_completed = False
        for upload_attempt in range(self.UPLOAD_WAIT_ATTEMPTS):
            # 判断重新上传按钮是否存在，如果不存在，代表视频正在上传，则等待
            try:
                #  新版：定位重新上传
                number = await page.locator('[class^="long-card"] div:has-text("重新上传")').count()
                if number > 0:
                    douyin_logger.success("  [-]视频上传完毕")
                    upload_completed = True
                    break
                else:
                    if upload_attempt == 0:
                        self._report_commerce_progress("waiting_platform", "正在等待平台处理")
                    douyin_logger.info("  [-] 正在上传视频中...")
                    await asyncio.sleep(2)

                    if await page.locator('div.progress-div > div:has-text("上传失败")').count():
                        douyin_logger.error("  [-] 发现上传出错了... 准备重试")
                        await self.handle_upload_error(page)
            except Exception as error:
                douyin_logger.info(f"  [-] 正在上传视频中（第{upload_attempt + 1}次检查）: {error}")
                await asyncio.sleep(2)

        if not upload_completed:
            screenshot_path = await self._capture_draft_screenshot(page, "upload_timeout")
            screenshot_note = f"，截图：{screenshot_path}" if screenshot_path else ""
            raise RuntimeError(
                f"抖音视频上传在 {self.UPLOAD_WAIT_ATTEMPTS * 2} 秒内未完成，已停止后续操作{screenshot_note}"
            )

        # 抖音带货视频由平台使用视频首帧/平台默认展示，不进入“设置封面”
        # 裁剪弹层；普通发布保持原有封面逻辑不变。
        if getattr(self, "skip_thumbnail", False):
            douyin_logger.info("抖音带货已跳过封面设置，等待用户选择收藏音乐")
        else:
            await self.set_thumbnail(page, self.thumbnail_path)
        await asyncio.sleep(1)

    async def upload(self, playwright: Playwright) -> None:
        page = getattr(self, "external_page", None)
        context = getattr(self, "external_context", None)
        browser = getattr(self, "external_browser", None)
        managed_browser = page is None
        if managed_browser:
            # 使用 Chromium 浏览器启动一个浏览器实例
            browser = await launch_publish_browser(playwright, executable_path=self.local_executable_path)
            # 创建一个浏览器上下文，使用指定的 cookie 文件
            context = await new_publish_context(
                browser,
                storage_state=f"{self.account_file}",
            )
            context = await set_init_script(context)

            # 创建一个新的页面
            page = await context.new_page()
            self._managed_context = context
            self._managed_browser = browser

        await self.prepare_uploaded_video_editor(page)
        await self.set_favorite_music(page)
        await self.set_collection(page)
        await self.set_structured_location(page)
        await self.set_commerce_store(page)
        if getattr(self, "content_declaration", ""):
            await self.set_content_declaration(page)
        else:
            await self.set_ai_generated_declaration(page)
        if hasattr(self, "sync_to_toutiao"):
            await self.set_toutiao_sync(page)

        if self.publish_date != 0 and not self.save_draft_only:
            await self.set_schedule_time_douyin(page, self.publish_date)

        if self.save_draft_only:
            return await self._save_draft_and_close(page, context, browser, managed_browser)

        if self.dry_run:
            if managed_browser:
                await keep_browser_open_for_dry_run(
                    page,
                    context,
                    browser,
                    account_file=self.account_file,
                    logger=douyin_logger,
                    platform_name="抖音",
                    block_until_close=self.dry_run_hold_browser,
                )
            return

        publish_button = await self.wait_publish_button_ready(page)
        douyin_logger.info("抖音发布按钮已可用，准备点击发布")

        async def close_unexpected_publish_popup(popup):
            try:
                await popup.wait_for_load_state("commit", timeout=1500)
            except Exception:
                pass
            try:
                douyin_logger.warning(f"抖音发布瞬间检测到异常新标签页，已关闭: {popup.url}")
                await popup.close()
            except Exception:
                pass

        def on_new_page(popup):
            asyncio.create_task(close_unexpected_publish_popup(popup))

        context.on("page", on_new_page)
        try:
            self._assert_formal_publish_allowed()
            await publish_button.click(timeout=10000)
            self.publish_result = await self._wait_formal_publish_result(page)
            douyin_logger.success("  [-]视频发布成功")
        except Exception as e:
            screenshot_path = os.path.join(DOUYIN_SCREENSHOT_DIR, f"douyin_publish_timeout_{int(asyncio.get_event_loop().time()*1000)}.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            if isinstance(e, RuntimeError) and str(e).startswith("抖音"):
                raise RuntimeError(f"{e}，已保留截图：{screenshot_path}") from e
            raise RuntimeError(
                f"抖音点击发布后未确认跳转，已保留截图：{screenshot_path}"
            ) from e
        finally:
            try:
                context.remove_listener("page", on_new_page)
            except Exception:
                pass

        await save_context_storage_state(context, self.account_file)  # 保存cookie
        douyin_logger.success('  [-]cookie更新完毕！')
        # 关闭浏览器上下文和浏览器实例
        if managed_browser:
            await context.close()
            await browser.close()
        return self.publish_result

    async def _click_visible_exact_text(self, page: Page, text: str):
        items = await self._visible_enabled_items(page.get_by_text(text, exact=True))
        if not items:
            raise RuntimeError(f"抖音未找到可点击控件：{text}")
        target = items[-1]
        await target.scroll_into_view_if_needed(timeout=3000)
        await target.click(force=True, timeout=5000)
        return target

    async def set_collection(self, page: Page):
        collection_name = str(getattr(self, "collection_name", "") or "").strip()
        if not collection_name:
            douyin_logger.info("抖音未指定合集，本次不设置合集")
            return

        try:
            await self._click_visible_exact_text(page, "请选择合集")
        except RuntimeError:
            if not self.save_draft_only:
                raise
            detail = f"未找到合集入口，目标合集为“{collection_name}”"
            douyin_logger.warning(f"抖音{detail}")
            record_draft_field_warning("抖音", "合集", detail)
            return
        await page.wait_for_timeout(500)
        options = await self._visible_enabled_items(
            page.get_by_text(collection_name, exact=True)
        )
        if not options:
            if not self.save_draft_only:
                raise RuntimeError(f"抖音未找到合集“{collection_name}”")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            detail = f"当前账号没有可选合集“{collection_name}”"
            douyin_logger.warning(f"抖音{detail}")
            record_draft_field_warning("抖音", "合集", detail)
            return
        await options[-1].click(force=True, timeout=5000)
        await page.wait_for_timeout(500)

        selected = await self._visible_exact_text(page, {collection_name})
        if collection_name not in selected:
            if not self.save_draft_only:
                raise RuntimeError(f"抖音合集选择后未能回读“{collection_name}”")
            detail = f"选择“{collection_name}”后平台未能回读确认"
            douyin_logger.warning(f"抖音{detail}")
            record_draft_field_warning("抖音", "合集", detail)
            return
        douyin_logger.info(f"抖音合集已选择并回读确认：{collection_name}")

    async def set_structured_location(self, page: Page) -> str:
        """使用一键发已选官方 POI 填写新版“发布定位”并做控件回读。

        恢复上传器此前只支持裸关键词，且其旧选择器无法适配新版嵌套
        ``semi-select``。这里复用一键发预检已验证的唯一控件识别与 POI
        匹配规则；没有选择地点时明确跳过，绝不猜选相似地点。
        """

        payload = getattr(self, "location_payload", None)
        if not isinstance(payload, dict):
            self.location_verification = ""
            return ""

        # 抖音上传完成后偶尔会自行展示“横封面展示”提示。带货流程不设置
        # 独立封面，这个提示若遮挡定位控件，只允许关闭提示再重试一次，绝不
        # 点击“立即设置”或进入封面编辑器。
        overlay_state = "absent"
        if getattr(self, "skip_thumbnail", False):
            overlay_state = await self._dismiss_commerce_cover_promotion(page)
        if overlay_state == "blocked":
            raise RuntimeError(
                "抖音封面展示提示遮挡发布定位，未进入封面设置且无法安全关闭"
            )
        try:
            from app_core.oneclick_preflight import _douyin_set_location

            selected = await _douyin_set_location(page, payload)
        except Exception as exc:
            if (
                getattr(self, "skip_thumbnail", False)
                and self._is_commerce_cover_promotion_interception(exc)
            ):
                retry_state = await self._dismiss_commerce_cover_promotion(page)
                if retry_state != "blocked":
                    try:
                        selected = await _douyin_set_location(page, payload)
                    except Exception as retry_exc:
                        exc = retry_exc
                    else:
                        self.location_verification = str(selected or "")
                        if self.location_verification:
                            douyin_logger.info(
                                "抖音发布定位已选择并回读："
                                f"{self.location_verification}（已跳过封面提示）"
                            )
                        return self.location_verification
            detail = " ".join(str(exc).splitlines()[0:1]).strip()
            if "intercepts pointer events" in str(exc):
                detail = "页面仍有未关闭的抖音浮层遮挡定位控件"
            raise RuntimeError(f"抖音发布定位未能唯一确认：{detail}") from exc
        self.location_verification = str(selected or "")
        if self.location_verification:
            douyin_logger.info(
                f"抖音发布定位已选择并回读：{self.location_verification}"
            )
        return self.location_verification

    @staticmethod
    def _is_commerce_cover_promotion_interception(error: object) -> bool:
        """识别抖音“横封面展示”提示的确定性遮挡，不把普通控件错误混入。"""

        text = " ".join(str(error or "").casefold().split())
        return (
            "coverimgcontainer" in text
            or "你的作品可能会在精选频道" in text
            or ("立即设置" in text and "封面" in text and "intercepts pointer events" in text)
        )

    async def _dismiss_commerce_cover_promotion(self, page: Page) -> str:
        """只关闭已确认的封面展示提示，返回 absent/dismissed/blocked。

        页面仍保留平台原生的“设置封面”区域，但带货工作流绝不打开封面编辑
        器。若平台提示没有可验证的关闭方式，本方法返回 ``blocked``，由上层
        安全停止，而不是猜测点击任何封面操作。
        """

        async def visible_prompt_count() -> int:
            result = await page.evaluate(
                """() => {
                    const normalize = value => String(value || '')
                        .replace(/[\\u200b\\u00a0]/g, ' ')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const visible = node => {
                        if (!(node instanceof HTMLElement)) return false;
                        const rect = node.getBoundingClientRect();
                        const style = getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                            && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    // ``dy-creator-content-portal`` 本身可能是零尺寸的挂载根，
                    // 真正可见的是它内部的 modal-wrap。此前按 portal 根节点是否
                    // 可见过滤，会把已在前台遮挡的横封面提示误判为不存在。
                    const portals = Array.from(document.querySelectorAll(
                        '[class*="dy-creator-content-portal"]'
                    ));
                    return portals.filter(portal => {
                        const hasVisibleDescendant = [portal, ...portal.querySelectorAll('*')]
                            .some(visible);
                        if (!hasVisibleDescendant) return false;
                        const text = normalize(portal.innerText || portal.textContent);
                        return text.includes('你的作品可能会在精选频道')
                            || (text.includes('立即设置') && text.includes('横封面'));
                    }).length;
                }"""
            )
            return int(result or 0)

        count = await visible_prompt_count()
        if count == 0:
            return "absent"
        if count != 1:
            return "blocked"
        try:
            # Escape 是平台弹层的关闭手势，不会打开“立即设置”、上传封面或
            # 修改视频字段。只有已确认的提示可见时才发送。
            await page.keyboard.press("Escape")
        except Exception:
            return "blocked"
        for _ in range(10):
            await page.wait_for_timeout(150)
            if await visible_prompt_count() == 0:
                douyin_logger.info("抖音带货已关闭遮挡定位的封面展示提示，未设置封面")
                return "dismissed"
        return "blocked"

    async def _content_declaration_dialog_state(self, page: Page) -> dict:
        """定位当前真实可见的“作品内容声明”弹层。

        抖音编辑器会同时保留主页面、门户根节点和其他浮层。不能以全页的文本
        定位“无需添加自主声明”，否则文本节点可能位于被遮挡层或隐藏模板中。
        这里以标题和至少三项平台原生声明选项共同识别最内层弹层，并临时标记
        唯一根节点，后续点击和确认都严格限制在该弹层内。
        """

        result = await page.evaluate(
            """() => {
                const marker = 'data-oneclick-douyin-declaration-dialog';
                document.querySelectorAll(`[${marker}]`).forEach(node => node.removeAttribute(marker));
                const normalize = value => String(value || '')
                    .replace(/[\\u200b\\u00a0]/g, ' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const visible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const title = '对作品内容添加声明';
                const choices = [
                    '内容由AI生成',
                    '内容为个人观点或见解',
                    '内容为转载信息',
                    '内容含营销推广信息',
                    '虚构演绎，仅供娱乐',
                    '无需添加自主声明',
                ];
                const roots = Array.from(document.querySelectorAll(
                    '[role="dialog"], [role="none"][class*="modal"], [class*="modal-wrap"], [class*="Modal"]'
                )).filter(visible).filter(node => {
                    const text = normalize(node.innerText || node.textContent);
                    if (!text.includes(title)) return false;
                    const choiceCount = choices.filter(choice => text.includes(choice)).length;
                    return choiceCount >= 3;
                });
                // 同一个弹层的 portal 外壳与内部 modal-wrap 都可能满足条件；只
                // 保留最内层候选，避免将其他提示一起包进可点击范围。
                const innerRoots = roots.filter(root => !roots.some(
                    other => other !== root && root.contains(other)
                ));
                if (innerRoots.length === 1) {
                    innerRoots[0].setAttribute(marker, 'active');
                }
                return {
                    count: innerRoots.length,
                    texts: innerRoots.map(node => normalize(node.innerText || node.textContent).slice(0, 160)),
                };
            }"""
        )
        if not isinstance(result, dict):
            return {"count": 0, "texts": []}
        try:
            count = int(result.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        texts = result.get("texts")
        return {
            "count": count,
            "texts": [str(item) for item in texts] if isinstance(texts, list) else [],
        }

    async def _mark_content_declaration_select_opener(self, page: Page, labels) -> dict:
        """标记新版编辑页中唯一可见的“自主声明”下拉控件。

        抖音新版主页面会把“自主声明”和当前选项拆成多个 DOM 文本节点。
        因此 ``get_by_text(..., exact=True)`` 在第二次切换时可能找不到完整
        的“自主声明 + 当前选项”，尽管视觉上入口已经存在。这里只在原有的
        精确文本入口全部落空后，收敛到页面实际使用的 ``semi-select`` 控件
        根节点；仍要求唯一、可见且普通点击，不会使用 force-click 或猜测
        其他页面元素。
        """

        result = await page.evaluate(
            """labels => {
                const marker = 'data-oneclick-douyin-declaration-opener';
                document.querySelectorAll(`[${marker}]`).forEach(node => node.removeAttribute(marker));
                const normalize = value => String(value || '')
                    .replace(/[\\u200b\\u00a0]/g, ' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const visible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const selectedOptions = labels
                    .filter(label => label.startsWith('自主声明 '))
                    .map(label => label.slice('自主声明 '.length));
                const initialLabel = '请选择自主声明';
                const roots = new Set();
                for (const node of document.querySelectorAll('div.semi-select, [class*="semi-select"]')) {
                    const root = node.closest('div.semi-select') || node;
                    if (visible(root)) roots.add(root);
                }
                const candidates = [...roots].filter(root => {
                    const text = normalize(root.innerText || root.textContent);
                    const attributes = normalize([
                        root.getAttribute('aria-label'),
                        root.getAttribute('title'),
                        root.getAttribute('data-testid'),
                    ].filter(Boolean).join(' '));
                    const value = `${text} ${attributes}`;
                    if (value.includes(initialLabel)) return true;
                    return selectedOptions.some(option => value.includes(option));
                });
                // 2026 年新版编辑器并不总是使用 semi-select：部分账号页面把
                // “自主声明”标题、当前值和可点击容器拆成并列节点。此时不能以
                // 全页的当前值反查，因为移动端预览也会出现同一声明。改为从
                // “自主声明”的直接文本节点向上限定一个局部范围，再在该范围内
                // 找唯一的当前选项并向上收敛到最小可点击容器。
                if (candidates.length === 0) {
                    const directText = node => normalize(Array.from(node.childNodes || [])
                        .filter(child => child.nodeType === Node.TEXT_NODE)
                        .map(child => child.textContent || '')
                        .join(' '));
                    const allVisible = Array.from(document.querySelectorAll('body *'))
                        .filter(visible);
                    const declarationLabels = allVisible.filter(node => {
                        const text = normalize(node.innerText || node.textContent);
                        return directText(node) === '自主声明' || text === '自主声明';
                    });
                    const nearestInteractive = (node, boundary) => {
                        let current = node;
                        while (current instanceof HTMLElement) {
                            const role = current.getAttribute('role') || '';
                            const tabIndex = current.getAttribute('tabindex');
                            const className = String(current.className || '');
                            const style = getComputedStyle(current);
                            const interactive = current.matches(
                                'button, input, select, [role="button"], [role="combobox"]'
                            ) || role === 'listbox' || /select/i.test(className)
                                || style.cursor === 'pointer'
                                || (tabIndex !== null && Number(tabIndex) >= 0);
                            if (interactive && visible(current)) return current;
                            if (current === boundary) break;
                            current = current.parentElement;
                        }
                        return node;
                    };
                    for (const labelNode of declarationLabels) {
                        let scope = labelNode.parentElement;
                        for (let depth = 0; scope && depth < 6; depth += 1) {
                            const matchingValues = allVisible.filter(node => {
                                if (!scope.contains(node)) return false;
                                const text = normalize(node.innerText || node.textContent);
                                return selectedOptions.includes(text);
                            });
                            // 同一段文本可能存在 span 与其父节点两层；保留最内层
                            // 文本节点，避免把外层卡片和移动端预览一并纳入候选。
                            const leafValues = matchingValues.filter(node => !matchingValues.some(
                                other => other !== node && node.contains(other)
                            ));
                            if (leafValues.length === 1) {
                                const target = nearestInteractive(leafValues[0], scope);
                                if (visible(target)) candidates.push(target);
                                break;
                            }
                            scope = scope.parentElement;
                        }
                    }
                }
                const uniqueCandidates = [...new Set(candidates)];
                if (uniqueCandidates.length === 1) {
                    uniqueCandidates[0].setAttribute(marker, 'active');
                }
                return {
                    count: uniqueCandidates.length,
                    texts: uniqueCandidates.map(node => normalize(node.innerText || node.textContent).slice(0, 160)),
                };
            }""",
            list(labels),
        )
        if not isinstance(result, dict):
            return {"count": 0, "texts": []}
        try:
            count = int(result.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        texts = result.get("texts")
        return {
            "count": count,
            "texts": [str(item) for item in texts] if isinstance(texts, list) else [],
        }

    async def _open_content_declaration_dialog(self, page: Page):
        """返回唯一可见的声明弹层；缺失时只打开声明入口一次。"""

        state = await self._content_declaration_dialog_state(page)
        if state["count"] == 0:
            # 声明入口不能沿用通用的 force-click：若遇到未知合规/风控浮层，
            # 强制穿透会破坏“未知提示一律停住”的边界。这里仅允许一个真实可见
            # 且可用的入口，并执行普通点击。
            #
            # 首次未选择时，抖音显示“请选择自主声明”；选择过任一项后，入口会
            # 原地变为“自主声明 + 当前选项”。此前只识别首次文案，导致第二次
            # 切换时错误报出 0 个入口。这里仅接受这两类平台固定、完整的入口
            # 文案，既支持切换，也不会把移动端预览中的“作者声明”误作入口。
            from app_core.douyin_commerce_service import CONTENT_DECLARATION_OPTIONS

            opener_labels = (
                "请选择自主声明",
                *tuple(
                    f"自主声明 {option}" for option in CONTENT_DECLARATION_OPTIONS
                ),
            )
            openers = []
            for label in opener_labels:
                candidates = await self._visible_enabled_items(
                    page.get_by_text(label, exact=True)
                )
                if len(candidates) > 1:
                    raise RuntimeError(
                        f"抖音作品内容声明入口“{label}”不是唯一可点击节点（实际 {len(candidates)} 个）"
                    )
                openers.extend(candidates)
            if not openers:
                # 新版页面将“自主声明”和当前值拆分为相邻节点时，精确文本
                # 定位会得到 0 个。此处只允许唯一的可见 semi-select 控件作为
                # 受控后备入口；多个或零个均继续安全停止并带回最小诊断信息。
                fallback = await self._mark_content_declaration_select_opener(
                    page, opener_labels
                )
                if fallback["count"] != 1:
                    detail = "；".join(fallback.get("texts") or [])[:240]
                    suffix = f"：{detail}" if detail else ""
                    raise RuntimeError(
                        "抖音作品内容声明入口不是唯一可点击节点"
                        f"（实际 {fallback['count']} 个）{suffix}"
                    )
                marked_openers = await self._visible_enabled_items(
                    page.locator('[data-oneclick-douyin-declaration-opener="active"]')
                )
                if len(marked_openers) != 1:
                    raise RuntimeError(
                        "抖音作品内容声明入口标记后不是唯一可点击节点"
                        f"（实际 {len(marked_openers)} 个）"
                    )
                openers = marked_openers
            if len(openers) != 1:
                raise RuntimeError(
                    f"抖音作品内容声明入口不是唯一可点击节点（实际 {len(openers)} 个）"
                )
            opener = openers[0]
            await opener.scroll_into_view_if_needed(timeout=3_000)
            await opener.click(timeout=5_000)
            for _ in range(10):
                await page.wait_for_timeout(150)
                state = await self._content_declaration_dialog_state(page)
                if state["count"]:
                    break
        if state["count"] != 1:
            detail = "；".join(state.get("texts") or [])[:240]
            suffix = f"：{detail}" if detail else ""
            raise RuntimeError(
                f"抖音作品内容声明弹层无法唯一识别（实际 {state['count']} 个）{suffix}"
            )
        dialog = page.locator('[data-oneclick-douyin-declaration-dialog="active"]')
        if await dialog.count() != 1:
            raise RuntimeError("抖音作品内容声明弹层标记后不唯一，已安全停止")
        try:
            if not await dialog.first.is_visible():
                raise RuntimeError("抖音作品内容声明弹层标记后不可见，已安全停止")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("抖音作品内容声明弹层无法确认可见状态，已安全停止") from exc
        return dialog.first

    async def set_favorite_music(self, page: Page) -> dict:
        """为抖音带货视频选择收藏列表第一首音乐并做页面回读。

        ``music_payload`` 未设置时，此步骤对历史通用发布完全无影响。带货
        流程只允许 ``favorite-first``，收藏为空、控件不唯一或选择没有回读
        时立即失败，不会继续带货绑定或最终提交。
        """

        payload = getattr(self, "music_payload", None)
        if not isinstance(payload, dict):
            self.music_verification = {}
            return {}
        douyin_logger.info("抖音带货开始选择收藏列表第一首音乐")
        try:
            from app_core.douyin_music_service import (
                DouyinMusicError,
                select_first_favorite_music,
                validate_favorite_music_mode,
            )

            validate_favorite_music_mode(payload.get("mode"))
            selected = await select_first_favorite_music(page)
        except DouyinMusicError as exc:
            raise RuntimeError(f"抖音收藏音乐未能唯一选择并回读：{exc}") from exc
        except Exception as exc:
            detail = " ".join(str(exc).splitlines()[:1]).strip() or exc.__class__.__name__
            douyin_logger.error(f"抖音收藏音乐步骤异常：{detail}")
            raise RuntimeError(
                f"抖音收藏音乐步骤异常，已安全停止：{detail[:220]}"
            ) from exc
        self.music_verification = dict(selected or {})
        if not self.music_verification:
            raise RuntimeError("抖音收藏音乐未返回页面回读，已安全停止")
        douyin_logger.info(
            "抖音已选择并回读收藏音乐："
            f"{self.music_verification.get('title') or ''}"
        )
        return dict(self.music_verification)

    async def set_commerce_store(self, page: Page) -> dict:
        """读取候选或绑定唯一可回读的本地团购门店。"""

        discovery_payload = getattr(self, "commerce_discovery_payload", None)
        if isinstance(discovery_payload, dict):
            try:
                from app_core.douyin_commerce_service import (
                    read_commerce_store_candidates,
                )

                candidates = await read_commerce_store_candidates(
                    page,
                    discovery_payload.get("locationPoi") or {},
                )
            except Exception as exc:
                raise RuntimeError(f"抖音带货门店候选未能唯一读取：{exc}") from exc
            self.commerce_candidates = [dict(item) for item in candidates]
            if not self.commerce_candidates:
                raise RuntimeError("抖音未返回可唯一回读的团购门店候选")
            douyin_logger.info(
                f"抖音带货已读取 {len(self.commerce_candidates)} 个可绑定门店候选"
            )
            # 候选读取阶段不选择门店、不保存草稿、不点击发表。
            self.commerce_verification = {}
            return {}

        payload = getattr(self, "commerce_payload", None)
        if not isinstance(payload, dict):
            self.commerce_verification = {}
            return {}
        try:
            from app_core.douyin_commerce_service import apply_commerce_store_to_page

            selected = await apply_commerce_store_to_page(
                page,
                payload.get("commerceStore") or {},
                payload.get("locationPoi") or {},
            )
        except Exception as exc:
            raise RuntimeError(f"抖音带货门店未能唯一绑定并回读：{exc}") from exc
        self.commerce_verification = dict(selected or {})
        if not self.commerce_verification:
            raise RuntimeError("抖音带货门店未返回可用回读，已安全停止")
        douyin_logger.info(
            "抖音带货门店已绑定并回读："
            f"{self.commerce_verification.get('name') or ''}"
        )
        return dict(self.commerce_verification)

    async def set_content_declaration(self, page: Page, declaration: str | None = None) -> str:
        """选择用户明确指定的一项抖音“自主声明”，并从页面回读。

        不根据素材、标题或 AI 标记推断声明；调用方必须传入平台当前可见的精确
        选项。控件、选项、确认按钮或最终回读任一不唯一时立即停止。
        """

        from app_core.douyin_commerce_service import normalize_content_declaration

        option_text = normalize_content_declaration(
            declaration if declaration is not None else getattr(self, "content_declaration", "")
        )
        # 带货流程不设置独立封面，但抖音上传后可能延迟弹出“横封面展示”
        # 提示。它不是声明弹窗的一部分，若仍停在前台会遮住声明选项。只在
        # 文案精确命中已知提示时按 Escape 关闭；绝不点击“立即设置”，也不对
        # 任何未知弹窗执行关闭或强制点击。
        if getattr(self, "skip_thumbnail", False):
            overlay_state = await self._dismiss_commerce_cover_promotion(page)
            if overlay_state == "blocked":
                raise RuntimeError(
                    "抖音横封面展示提示遮挡作品内容声明，未进入封面设置且无法安全关闭"
                )
        dialog = await self._open_content_declaration_dialog(page)

        async def unique_option(current_dialog):
            # 实机 DOM/无障碍树已确认：声明文字只是单选行内部的展示节点，真正
            # 可交互的是带名称的 ``radio``。点击文字节点会被同层的说明浮层或
            # 行容器拦截，即使声明弹层本身已经定位正确。因此必须以平台原生
            # 单选控件为目标，不能再以 get_by_text() 代替。
            options = await self._visible_enabled_items(
                current_dialog.get_by_role("radio", name=option_text, exact=True)
            )
            if len(options) != 1:
                raise RuntimeError(
                    f"抖音作品内容声明弹层中“{option_text}”不是唯一可点击的单选控件，已安全停止"
                )
            return options[0]

        option = await unique_option(dialog)
        try:
            await option.click(timeout=5_000)
        except Exception as exc:
            # 先由页面真实可见状态确认是否存在“横封面展示”提示，不依赖
            # Playwright 错误文本是否被客户端截断。只有这个已知提示可按 Escape
            # 关闭后重试；未知浮层、验证码或风控提示一律不穿透。
            overlay_state = "absent"
            if getattr(self, "skip_thumbnail", False):
                overlay_state = await self._dismiss_commerce_cover_promotion(page)
            if overlay_state == "blocked":
                raise RuntimeError(
                    "抖音横封面展示提示遮挡作品内容声明，未进入封面设置且无法安全关闭"
                ) from exc
            if overlay_state != "dismissed":
                # 页面没有精确命中可安全关闭的横封面提示时，保留原始错误，避免
                # 将任意未知弹层误判为封面提示后继续操作。
                raise
            # Escape 由抖音最上层弹层接收：有的版本会保留声明弹层，有的版本会
            # 一并收起。统一重新定位，必要时仅重新打开“请选择自主声明”。
            dialog = await self._open_content_declaration_dialog(page)
            option = await unique_option(dialog)
            try:
                await option.click(timeout=5_000)
            except Exception as retry_exc:
                raise RuntimeError(
                    "关闭横封面展示提示后，抖音作品内容声明仍无法安全选择"
                ) from retry_exc
        await page.wait_for_timeout(300)
        confirm_buttons = []
        for button_name in ("确定", "确认", "完成", "保存"):
            confirm_buttons = await self._visible_enabled_items(
                dialog.get_by_role("button", name=button_name, exact=True)
            )
            if len(confirm_buttons) == 1:
                break
            if len(confirm_buttons) > 1:
                raise RuntimeError("抖音自主声明确认按钮不唯一，已安全停止")
        if len(confirm_buttons) != 1:
            raise RuntimeError("抖音自主声明未找到唯一可用的确认按钮")
        await confirm_buttons[0].click(timeout=5_000)
        dialog_closed = False
        for _ in range(10):
            await page.wait_for_timeout(150)
            state = await self._content_declaration_dialog_state(page)
            if state["count"] == 0:
                dialog_closed = True
                break
        if not dialog_closed:
            raise RuntimeError("抖音自主声明确认后弹层未关闭，无法确认页面回读")
        selected = await self._visible_exact_text(page, {option_text})
        if option_text not in selected:
            raise RuntimeError(f"抖音自主声明选择后未能回读：{option_text}")
        self.content_declaration_verification = option_text
        douyin_logger.info(f"抖音自主声明已选择并回读确认：{option_text}")
        return option_text

    async def set_ai_generated_declaration(self, page: Page):
        """兼容旧发布路径的 AI 声明入口，实际委托给受控的自主声明选择。"""

        if not getattr(self, "ai_generated", False):
            return ""
        if not getattr(self, "content_declaration", ""):
            self.content_declaration = "内容由AI生成"
        return await self.set_content_declaration(page)

    async def _radio_checked_for_text(self, page: Page, text: str):
        return await page.evaluate(
            """text => {
                const normalize = value => String(value || '')
                    .replace(/[\\u200b\\u00a0]/g, ' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const visible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const label = Array.from(document.querySelectorAll('body *'))
                    .filter(node => visible(node)
                        && normalize(node.innerText || node.textContent) === text)
                    .sort((left, right) => left.childElementCount - right.childElementCount)[0];
                if (!label) return null;
                for (let node = label; node && node !== document.body; node = node.parentElement) {
                    const input = node.matches('input') ? node : node.querySelector('input');
                    if (input && ['radio', 'checkbox'].includes(input.type)) {
                        return Boolean(input.checked);
                    }
                    const ariaChecked = node.getAttribute('aria-checked');
                    if (ariaChecked === 'true') return true;
                    if (ariaChecked === 'false') return false;
                    const className = String(node.className || '').toLowerCase();
                    if (/radio[^ ]*checked|checked[^ ]*radio|selected|active/.test(className)) {
                        return true;
                    }
                    if (node.parentElement && node.parentElement.children.length > 8) break;
                }
                return null;
            }""",
            text,
        )

    async def _set_toutiao_switch(self, page: Page, enabled: bool):
        state = await self._toutiao_switch_state(page)
        if not state.get("found"):
            raise RuntimeError("抖音未找到今日头条同步开关")
        if state.get("checked") is None:
            raise RuntimeError(f"抖音无法读取今日头条同步开关状态：{state}")
        if bool(state.get("checked")) != enabled:
            switches = page.locator('[data-codex-toutiao-switch="true"]')
            if await switches.count() != 1:
                raise RuntimeError("抖音今日头条同步开关定位结果不唯一")
            await switches.first.click(force=True, timeout=5000)

        current = state
        for attempt in range(10):
            current = await self._toutiao_switch_state(page)
            if current.get("found") and current.get("checked") is enabled:
                return current
            if attempt < 9:
                await page.wait_for_timeout(300)
        raise RuntimeError(
            "抖音今日头条同步开关点击后未生效："
            f"期望={'开启' if enabled else '关闭'}，实际={current}"
        )

    async def _toutiao_switch_state(self, page: Page):
        return await page.evaluate(
            """() => {
                const normalize = value => String(value || '')
                    .replace(/[\\u200b\\u00a0]/g, ' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const visible = node => {
                    if (!(node instanceof HTMLElement)) return false;
                    const rect = node.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const textNode = Array.from(document.querySelectorAll('body *'))
                    .filter(node => {
                        if (!visible(node)) return false;
                        const text = normalize(node.innerText || node.textContent);
                        return text === '今日头条'
                            || (text.includes('今日头条') && text.length <= 24);
                    })
                    .sort((left, right) => left.childElementCount - right.childElementCount)[0];
                if (!textNode) return { found: false };
                document.querySelectorAll('[data-codex-toutiao-switch]')
                    .forEach(node => node.removeAttribute('data-codex-toutiao-switch'));
                let switchNode = null;
                for (let node = textNode.parentElement; node && node !== document.body; node = node.parentElement) {
                    const candidates = Array.from(node.querySelectorAll(
                        '[role="switch"], .semi-switch, input.semi-switch-native-control'
                    )).filter(visible);
                    if (!candidates.length) continue;
                    const labelRect = textNode.getBoundingClientRect();
                    candidates.sort((left, right) => {
                        const leftRect = left.getBoundingClientRect();
                        const rightRect = right.getBoundingClientRect();
                        const leftDistance = Math.abs(
                            (leftRect.top + leftRect.height / 2)
                            - (labelRect.top + labelRect.height / 2)
                        );
                        const rightDistance = Math.abs(
                            (rightRect.top + rightRect.height / 2)
                            - (labelRect.top + labelRect.height / 2)
                        );
                        return leftDistance - rightDistance;
                    });
                    switchNode = candidates[0];
                    break;
                }
                if (!switchNode) return { found: false };
                const input = switchNode.matches('input')
                    ? switchNode
                    : switchNode.querySelector('input[type="checkbox"], input.semi-switch-native-control');
                const ariaChecked = switchNode.getAttribute('aria-checked');
                const className = String(switchNode.className || '').toLowerCase();
                let checked = null;
                if (input) checked = Boolean(input.checked);
                else if (ariaChecked === 'true' || ariaChecked === 'false') {
                    checked = ariaChecked === 'true';
                } else if (/switch[^ ]*checked|checked[^ ]*switch/.test(className)) {
                    checked = true;
                } else if (/switch/.test(className)) {
                    checked = false;
                }
                const clickable = switchNode.matches('input')
                    ? switchNode.parentElement || switchNode
                    : switchNode;
                clickable.setAttribute('data-codex-toutiao-switch', 'true');
                const rect = clickable.getBoundingClientRect();
                return {
                    found: true,
                    checked,
                    tag: clickable.tagName,
                    className: String(clickable.className || ''),
                    x: rect.left + rect.width / 2,
                    y: rect.top + rect.height / 2,
                };
            }"""
        )

    async def set_toutiao_sync(self, page: Page):
        enabled = bool(getattr(self, "sync_to_toutiao", False))
        choice_candidates = (
            ("同时发布到", "同时发布到今日头条", "同步发布到今日头条")
            if enabled
            else ("不同步发布", "不同时发布", "不同步到今日头条")
        )
        choice = None
        for candidate in choice_candidates:
            items = await self._visible_enabled_items(
                page.get_by_text(candidate, exact=True)
            )
            if not items:
                continue
            target = items[-1]
            await target.scroll_into_view_if_needed(timeout=3000)
            await target.click(force=True, timeout=5000)
            choice = candidate
            break
        if choice is not None:
            await page.wait_for_timeout(500)
            checked = await self._radio_checked_for_text(page, choice)
            if checked is not True:
                raise RuntimeError(f"抖音“{choice}”选择后未生效")
            if enabled:
                state = await self._set_toutiao_switch(page, True)
                douyin_logger.info(f"抖音今日头条开关回读：{state}")
        else:
            # 新版发布页已把“同时/不同步发布”单选项合并为今日头条开关。
            # 以开关的真实回读状态为准，避免继续依赖易变的展示文案。
            state = await self._toutiao_switch_state(page)
            if not state.get("found") or state.get("checked") is None:
                if not enabled:
                    try:
                        body_text = await page.locator("body").inner_text(
                            timeout=3000
                        )
                    except Exception:
                        body_text = ""
                    if "今日头条" not in body_text:
                        douyin_logger.info(
                            "抖音当前页面未提供今日头条同步入口，"
                            "按不可同步状态完成关闭校验"
                        )
                        return
                raise RuntimeError(
                    "抖音未找到可回读的今日头条同步控件："
                    + " / ".join(choice_candidates)
                )
            state = await self._set_toutiao_switch(page, enabled)
            douyin_logger.info(f"抖音新版今日头条开关回读：{state}")
        douyin_logger.info(
            f"抖音今日头条同步已设置并回读：{'开启' if enabled else '关闭'}"
        )

    async def set_thumbnail(self, page: Page, thumbnail_path: str):
        targets = []
        if self.thumbnail_paths.get("4:3"):
            targets.append(("4:3", self.thumbnail_paths["4:3"]))
        if self.thumbnail_paths.get("3:4"):
            targets.append(("3:4", self.thumbnail_paths["3:4"]))
        if not targets and thumbnail_path:
            targets.append(("3:4", thumbnail_path))

        if not targets:
            douyin_logger.info("没上传封面，就不要封面了，让系统自动生成吧...")
            await asyncio.sleep(1)
            return

        before_signatures = {
            ratio: await self._cover_card_signature(page, ratio)
            for ratio, _ in targets
        }
        first_ratio, first_path = targets[0]
        await self.open_cover_editor(page, first_ratio)
        switched_by_action = False
        for index, (ratio, cover_path) in enumerate(targets):
            if index > 0 and not switched_by_action:
                await self.switch_cover_ratio(page, ratio)
            switched_by_action = False
            await self.wait_cover_editor_ready(page, ratio)
            await self.upload_cover_image(page, cover_path, ratio)
            if ratio == "4:3" and index < len(targets) - 1:
                await self.click_cover_action(page, "设置竖封面")
                switched_by_action = True
            else:
                await self.click_cover_action(page, "完成")
        await asyncio.sleep(1)
        await self.verify_cover_upload(
            page,
            [ratio for ratio, _ in targets],
            before_signatures=before_signatures,
        )

    async def _get_cover_card(self, page: Page, ratio: str):
        expected_labels = {
            "4:3": "横封面4:3",
            "3:4": "竖封面3:4",
        }
        label = expected_labels[ratio]
        candidates = page.locator(f'div[class*="coverControl-"]:has-text("{label}")')
        visible = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            try:
                if await candidate.is_visible():
                    visible.append(candidate)
            except Exception:
                continue
        if len(visible) != 1:
            raise RuntimeError(f"抖音{ratio}封面卡片数量异常：{len(visible)}")
        return visible[0]

    async def _cover_card_signature(self, page: Page, ratio: str):
        card = await self._get_cover_card(page, ratio)
        preview_marker = f"codex-cover-preview-{ratio.replace(':', '-')}"
        signature = await card.evaluate(
            """
            (node, args) => {
              const { ratio, marker } = args;
              const normalize = value => String(value || '')
                .replace(/[\\u200b\\u00a0]/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
              const elements = [node, ...node.querySelectorAll('*')];
              const images = elements
                .filter(item => item.tagName === 'IMG')
                .map(item => item.currentSrc || item.getAttribute('src') || '')
                .filter(Boolean);
              const backgrounds = elements
                .map(item => item.style?.backgroundImage || getComputedStyle(item).backgroundImage || '')
                .filter(value => value && value !== 'none');
              const canvases = elements
                .filter(item => item.tagName === 'CANVAS')
                .map(item => {
                  try {
                    return item.toDataURL('image/png');
                  } catch (_) {
                    return `${item.width}x${item.height}`;
                  }
                })
                .filter(Boolean);
              document.querySelectorAll('[data-codex-cover-preview]')
                .forEach(item => item.removeAttribute('data-codex-cover-preview'));
              const expectedLabel = ratio === '4:3' ? '横封面4:3' : '竖封面3:4';
              const label = elements
                .filter(item => normalize(item.innerText || item.textContent) === expectedLabel)
                .sort((left, right) => left.childElementCount - right.childElementCount)[0];
              const labelRect = label?.getBoundingClientRect();
              const mediaCandidates = elements
                .map(item => {
                  const rect = item.getBoundingClientRect?.();
                  if (!rect || rect.width <= 20 || rect.height <= 20) return null;
                  const style = getComputedStyle(item);
                  if (style.display === 'none' || style.visibility === 'hidden') return null;
                  const source = item.tagName === 'IMG'
                    ? item.currentSrc || item.getAttribute('src') || ''
                    : '';
                  const background = item.style?.backgroundImage || style.backgroundImage || '';
                  const isCanvas = item.tagName === 'CANVAS';
                  if (!source && (!background || background === 'none') && !isCanvas) return null;
                  const centerX = rect.left + rect.width / 2;
                  const centerY = rect.top + rect.height / 2;
                  const labelCenterX = labelRect
                    ? labelRect.left + labelRect.width / 2
                    : centerX;
                  const labelCenterY = labelRect
                    ? labelRect.top + labelRect.height / 2
                    : centerY;
                  const belowLabelPenalty = labelRect && rect.top >= labelRect.top ? 10000 : 0;
                  const score = belowLabelPenalty
                    + Math.abs(centerX - labelCenterX)
                    + Math.abs(centerY - labelCenterY) * 1.5;
                  return {
                    item,
                    source,
                    background,
                    isCanvas,
                    score,
                    area: rect.width * rect.height,
                  };
                })
                .filter(Boolean)
                .sort((left, right) => left.score - right.score || right.area - left.area);
              const primary = mediaCandidates[0] || null;
              if (primary) {
                primary.item.setAttribute('data-codex-cover-preview', marker);
              }
              return {
                images,
                backgrounds,
                canvases,
                html: node.innerHTML,
                primary_assets: primary
                  ? [primary.source, primary.background].filter(value => value && value !== 'none')
                  : [],
                primary_is_canvas: Boolean(primary?.isCanvas),
              };
            }
            """,
            {"ratio": ratio, "marker": preview_marker},
        )
        try:
            screenshot = await card.screenshot(type="png")
            signature["visual_hash"] = hashlib.sha256(screenshot).hexdigest()
            signature["visual_png"] = screenshot
            image = Image.open(BytesIO(screenshot)).convert("L").resize(
                (16, 16),
                Image.Resampling.LANCZOS,
            )
            pixels = list(image.getdata())
            average = sum(pixels) / len(pixels)
            bits = "".join("1" if value >= average else "0" for value in pixels)
            signature["visual_phash"] = f"{int(bits, 2):064x}"
        except Exception:
            signature["visual_hash"] = ""
            signature["visual_png"] = b""
            signature["visual_phash"] = ""
        preview = page.locator(
            f'[data-codex-cover-preview="{preview_marker}"]'
        )
        try:
            if await preview.count() == 1:
                preview_screenshot = await preview.first.screenshot(type="png")
                signature["preview_visual_hash"] = hashlib.sha256(
                    preview_screenshot
                ).hexdigest()
                signature["preview_visual_png"] = preview_screenshot
                preview_image = Image.open(BytesIO(preview_screenshot)).convert("L").resize(
                    (16, 16),
                    Image.Resampling.LANCZOS,
                )
                preview_pixels = list(preview_image.getdata())
                preview_average = sum(preview_pixels) / len(preview_pixels)
                preview_bits = "".join(
                    "1" if value >= preview_average else "0"
                    for value in preview_pixels
                )
                signature["preview_visual_phash"] = (
                    f"{int(preview_bits, 2):064x}"
                )
        except Exception:
            signature["preview_visual_hash"] = ""
            signature["preview_visual_png"] = b""
            signature["preview_visual_phash"] = ""
        return signature

    @staticmethod
    def _cover_signature_key(signature):
        primary_assets = list((signature or {}).get("primary_assets") or [])
        preview_visual_hash = str(
            (signature or {}).get("preview_visual_hash") or ""
        )
        if primary_assets or preview_visual_hash:
            return repr((primary_assets, preview_visual_hash))
        images = list((signature or {}).get("images") or [])
        backgrounds = list((signature or {}).get("backgrounds") or [])
        canvases = list((signature or {}).get("canvases") or [])
        visual_hash = str((signature or {}).get("visual_hash") or "")
        if images or backgrounds or canvases or visual_hash:
            return repr((images, backgrounds, canvases, visual_hash))
        return str((signature or {}).get("html") or "")

    @staticmethod
    def _stable_cover_asset(value: str) -> str:
        text = str(value or "").strip().strip("\"'")
        if not text:
            return ""
        if text.startswith("data:"):
            return f"data:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
        if text.startswith("blob:"):
            return ""
        parts = urlsplit(text)
        if parts.scheme in {"http", "https"} or parts.netloc:
            return parts.path
        if parts.scheme:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        return text.split("?", 1)[0].split("#", 1)[0]

    @classmethod
    def _stable_cover_assets(cls, signature) -> tuple[str, ...]:
        primary_values = list((signature or {}).get("primary_assets") or [])
        primary_normalized = [
            cls._stable_cover_asset(value)
            for value in primary_values
        ]
        primary_normalized = [
            value for value in primary_normalized if value
        ]
        if primary_normalized:
            return tuple(primary_normalized)

        values = list((signature or {}).get("images") or [])
        for background in (signature or {}).get("backgrounds") or []:
            matches = re.findall(r"url\((['\"]?)(.*?)\1\)", str(background))
            values.extend(match[1] for match in matches)
        normalized = [
            cls._stable_cover_asset(value)
            for value in values
        ]
        return tuple(value for value in normalized if value)

    @staticmethod
    def _perceptual_hash_distance(left: str, right: str) -> int | None:
        if not left or not right or len(left) != len(right):
            return None
        try:
            return (int(left, 16) ^ int(right, 16)).bit_count()
        except ValueError:
            return None

    @classmethod
    def _cover_signatures_equivalent(cls, stored, current) -> bool:
        stored_assets = cls._stable_cover_assets(stored)
        current_assets = cls._stable_cover_assets(current)
        if stored_assets and stored_assets == current_assets:
            return True

        preview_distance = cls._perceptual_hash_distance(
            str((stored or {}).get("preview_visual_phash") or ""),
            str((current or {}).get("preview_visual_phash") or ""),
        )
        if preview_distance is not None and preview_distance <= 32:
            return True

        stored_preview = str((stored or {}).get("preview_visual_hash") or "")
        current_preview = str((current or {}).get("preview_visual_hash") or "")
        if stored_preview and stored_preview == current_preview:
            return True

        stored_canvases = tuple((stored or {}).get("canvases") or [])
        current_canvases = tuple((current or {}).get("canvases") or [])
        if stored_canvases and stored_canvases == current_canvases:
            return True

        distance = cls._perceptual_hash_distance(
            str((stored or {}).get("visual_phash") or ""),
            str((current or {}).get("visual_phash") or ""),
        )
        if distance is not None and distance <= 32:
            return True

        stored_visual = str((stored or {}).get("visual_hash") or "")
        current_visual = str((current or {}).get("visual_hash") or "")
        return bool(stored_visual and stored_visual == current_visual)

    async def _wait_cover_success_markers(self, page):
        attempts = min(self.COVER_VERIFY_ATTEMPTS, 8)
        for attempt in range(attempts):
            success_markers = await self._visible_exact_text(
                page,
                ("封面效果检测通过", "封面检测通过"),
            )
            if success_markers:
                return success_markers
            if attempt < attempts - 1:
                await page.wait_for_timeout(1000)
        return set()

    async def verify_cover_upload(self, page: Page, ratios, before_signatures):
        after_signatures = {}
        changed = {}
        for ratio in ratios:
            before = before_signatures.get(ratio) or {}
            before_key = self._cover_signature_key(before)
            after = {}
            for attempt in range(12):
                after = await self._cover_card_signature(page, ratio)
                if self._cover_signature_key(after) != before_key:
                    break
                if attempt < 11:
                    await page.wait_for_timeout(1000)
            after_signatures[ratio] = after
            changed[ratio] = self._cover_signature_key(after) != before_key
            if not changed[ratio]:
                evidence_paths = []
                os.makedirs(DOUYIN_SCREENSHOT_DIR, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                for label, signature in (("before", before), ("after", after)):
                    image = signature.get("visual_png")
                    if not image:
                        continue
                    path = os.path.join(
                        DOUYIN_SCREENSHOT_DIR,
                        f"douyin_cover_{ratio.replace(':', 'x')}_{label}_{timestamp}.png",
                    )
                    with open(path, "wb") as handle:
                        handle.write(image)
                    evidence_paths.append(path)
                evidence_text = f"，证据：{evidence_paths}" if evidence_paths else ""
                raise RuntimeError(
                    f"抖音{ratio}封面预览上传后等待12秒仍没有变化，已停止保存草稿{evidence_text}"
                )

        success_markers = await self._wait_cover_success_markers(page)
        if not success_markers:
            preview_keys = {
                ratio: self._cover_signature_key(after_signatures[ratio])
                for ratio in ratios
            }
            previews_are_distinct = len(set(preview_keys.values())) == len(preview_keys)
            if not all(changed.values()) or not previews_are_distinct:
                raise RuntimeError(
                    "抖音封面检测未返回通过结果，且横竖封面预览证据不完整，已停止保存草稿"
                )
            success_markers = {"横竖封面预览已更新"}
            douyin_logger.info("抖音新版页面未显示旧版封面检测文案，已使用横竖预览变化完成回读")
        self.cover_verification = {
            "ratios": list(ratios),
            "platform_status": sorted(success_markers),
            "preview_changed": changed,
            "after_signatures": {
                ratio: {
                    key: value
                    for key, value in signature.items()
                    if key not in {"visual_png", "preview_visual_png"}
                }
                for ratio, signature in after_signatures.items()
            },
        }
        douyin_logger.info(f"抖音封面回读通过：{ratios}")

    async def verify_cover_persistence(self, page: Page, ratios):
        verification = self.cover_verification or {}
        stored = verification.get("after_signatures") or {}
        for ratio in ratios:
            if not verification.get("preview_changed", {}).get(ratio):
                raise RuntimeError(f"抖音{ratio}封面缺少预览变化证据")
            current = await self._cover_card_signature(page, ratio)
            if not self._cover_signatures_equivalent(
                stored.get(ratio),
                current,
            ):
                raise RuntimeError(f"抖音{ratio}封面在保存前回读发生变化")

    async def open_cover_editor(self, page: Page, ratio: str):
        label = "横封面4:3" if ratio == "4:3" else "竖封面3:4"
        card = await self._get_cover_card(page, ratio)
        await card.wait_for(state='visible', timeout=15000)
        await card.click(force=True, timeout=5000)
        douyin_logger.info(f"点击成功->{label}选择封面")
        await page.wait_for_timeout(2000)

    async def wait_cover_editor_ready(self, page: Page, ratio: str):
        await self._get_unique_cover_control(page, "上传封面")
        douyin_logger.info(f"抖音{ratio}封面编辑器已就绪")
        await page.wait_for_timeout(800)

    async def switch_cover_ratio(self, page: Page, ratio: str):
        action_text = "设置横封面" if ratio == "4:3" else "设置竖封面"
        control = await self._get_unique_cover_control(page, action_text)
        await control.click(force=True, timeout=5000)
        await page.wait_for_timeout(1000)

    async def upload_cover_image(self, page: Page, cover_path: str, ratio: str):
        before_error_count = await page.locator('text=不支持的图片格式').count()
        try:
            upload_button = await self._get_unique_cover_control(page, "上传封面")
            async with page.expect_file_chooser(timeout=8000) as fc_info:
                await upload_button.click(force=True, timeout=5000)
            file_chooser = await fc_info.value
            await file_chooser.set_files(cover_path)
        except Exception as e:
            douyin_logger.warning(f"点击正式上传封面入口失败，尝试隐藏文件入口: {e}")
            inputs = page.locator('input[type="file"][accept*="image"]')
            if await inputs.count() != 1:
                raise RuntimeError(f"封面隐藏文件入口数量异常：{await inputs.count()}") from e
            await inputs.first.wait_for(state='attached', timeout=15000)
            await inputs.first.set_input_files(cover_path)
        douyin_logger.info(f"上传抖音{ratio}封面成功")
        await page.wait_for_timeout(2500)
        after_error_count = await page.locator('text=不支持的图片格式').count()
        if after_error_count > before_error_count:
            raise RuntimeError(f"抖音{ratio}封面上传失败：平台提示不支持的图片格式")

    async def _get_unique_cover_control(self, page: Page, action_text: str):
        """优先使用按钮语义，避免按钮及其子节点产生同名文本重复匹配。"""
        role_controls = await self._visible_enabled_items(
            page.get_by_role("button", name=action_text, exact=True)
        )
        if len(role_controls) == 1:
            return role_controls[0]
        if len(role_controls) > 1:
            raise RuntimeError(f"抖音封面按钮“{action_text}”数量异常：{len(role_controls)}")

        text_controls = await self._visible_enabled_items(
            page.get_by_text(action_text, exact=True)
        )
        if len(text_controls) != 1:
            raise RuntimeError(f"抖音封面入口“{action_text}”数量异常：{len(text_controls)}")
        return text_controls[0]

    async def click_cover_action(self, page: Page, action_text: str):
        control = await self._get_unique_cover_control(page, action_text)
        await control.click(force=True, timeout=5000)
        douyin_logger.info(f"点击抖音封面按钮成功：{action_text}")
        await page.wait_for_timeout(2500 if action_text != "完成" else 1500)

    async def set_location(self, page: Page, location: str = "杭州市"):
        # todo supoort location later
        # await page.get_by_text('添加标签').locator("..").locator("..").locator("xpath=following-sibling::div").locator(
        #     "div.semi-select-single").nth(0).click()
        await page.locator('div.semi-select span:has-text("输入地理位置")').click()
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(2000)
        await page.keyboard.type(location)
        await page.wait_for_selector('div[role="listbox"] [role="option"]', timeout=5000)
        await page.locator('div[role="listbox"] [role="option"]').first.click()

    async def main(self):
        async with async_playwright() as playwright:
            try:
                return await self.upload(playwright)
            finally:
                context = getattr(self, "_managed_context", None)
                browser = getattr(self, "_managed_browser", None)
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass
