import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
import time

from playwright.async_api import Error as PlaywrightError, Playwright, TimeoutError as PlaywrightTimeoutError

from conf import LOCAL_CHROME_PATH
from utils.base_social_media import (
    set_init_script,
    launch_publish_browser,
    new_publish_context,
    goto_and_reveal,
    prevent_new_tabs,
    keep_browser_open_for_dry_run,
    save_context_storage_state,
)
from utils.ai_disclosure import NATIVE_AI_DISCLOSURE_LABELS
from utils.log import bilibili_logger
from utils.platform_draft import (
    find_unique_draft_control,
    page_body_text,
    record_draft_field_warning,
)
from utils.publish_limits import normalize_publish_tags


OPEN_DEBUG_BROWSERS: list = []

BILIBILI_SELF_AUTH_TEXT = "内容为自制：未经作者允许，禁止转载"
BILIBILI_DRAFT_PARTITION_WARNING = (
    "B站平台草稿不会保存新版分区值；重新打开草稿时页面可能回显“影视”。"
    "正式发布流程会在投稿前重新选择并校验目标分区。"
)
BILIBILI_HUMAN_TYPE_IDS = {
    "影视": 1001,
    "娱乐": 1002,
    "音乐": 1003,
    "舞蹈": 1004,
    "动画": 1005,
    "绘画": 1006,
    "鬼畜": 1007,
    "游戏": 1008,
    "资讯": 1009,
    "知识": 1010,
    "人工智能": 1011,
    "科技数码": 1012,
    "汽车": 1013,
    "时尚美妆": 1014,
    "家装房产": 1015,
    "户外潮流": 1016,
    "健身": 1017,
    "体育运动": 1018,
    "手工": 1019,
    "美食": 1020,
    "小剧场": 1021,
    "旅游出行": 1022,
    "三农": 1023,
    "动物": 1024,
    "亲子": 1025,
    "健康": 1026,
    "情感": 1027,
    "vlog": 1029,
    "生活兴趣": 1030,
    "生活经验": 1031,
}
BILIBILI_PARTITION_ALIASES = {
    "科技": "科技数码",
    "生活": "生活经验",
    "运动": "体育运动",
    "时尚": "时尚美妆",
    "动物圈": "动物",
    "VLOG": "vlog",
}
BILIBILI_TAG_MAX_LENGTH = 20


def normalize_bilibili_tags(tags: list[str]) -> list[str]:
    """按 B站 20 字符限制整理标签，不对超限语义做静默截断。"""

    normalized = []
    for raw_tag in normalize_publish_tags(tags):
        tag = str(raw_tag).strip().lstrip("#").strip()
        if len(tag) <= BILIBILI_TAG_MAX_LENGTH:
            normalized.append(tag)
            continue

        # 英文短语去除空白后仍保持原有字符顺序与语义，可安全适配为话题标签。
        compact = "".join(tag.split())
        if compact and len(compact) <= BILIBILI_TAG_MAX_LENGTH:
            bilibili_logger.info(
                f"[bilibili] 标签超过 {BILIBILI_TAG_MAX_LENGTH} 字符，"
                f"已移除空白适配平台限制: {tag} -> {compact}"
            )
            normalized.append(compact)
            continue

        raise ValueError(
            f"B站标签超过 {BILIBILI_TAG_MAX_LENGTH} 字符且无法安全压缩：{tag}"
        )
    return normalized


class BilibiliVideo:
    def __init__(
        self,
        title: str,
        file_path: str,
        tags: list[str],
        publish_date: datetime | int,
        account_file: Path,
        thumbnail_path: str | None = None,
        thumbnail_paths: dict[str, str] | None = None,
        desc: str | None = None,
        bili_type: str | None = None,
        partition: str | None = None,
        dry_run: bool = False,
        dry_run_hold_browser: bool = True,
        save_draft_only: bool = False,
    ) -> None:
        self.title = title
        self.file_path = file_path
        self.tags = normalize_bilibili_tags(tags)
        self.publish_date = publish_date
        self.account_file = account_file
        self.thumbnail_path = thumbnail_path
        self.thumbnail_paths = {
            str(ratio): str(path)
            for ratio, path in (thumbnail_paths or {}).items()
            if path
        }
        self.local_executable_path = LOCAL_CHROME_PATH
        self.desc = desc or ""
        self.bili_type = (bili_type or "自制").strip()
        requested_partition = (partition or "").strip()
        self.partition = BILIBILI_PARTITION_ALIASES.get(
            requested_partition,
            requested_partition,
        )
        self.dry_run = dry_run
        self.dry_run_hold_browser = dry_run_hold_browser
        self.save_draft_only = bool(save_draft_only)
        if self.save_draft_only and self.dry_run:
            raise ValueError("B站保存草稿模式与预发布检查不能同时开启")
        if (
            self.save_draft_only
            and self.publish_date not in (None, 0)
        ):
            raise ValueError(
                "B站平台草稿不会保留定时发布时间；"
                "请取消定时发布，或改用预发布检查/正式发布"
            )

    async def _fill_title(self, page) -> None:
        started_at = time.perf_counter()
        # 基于实际B站页面的选择器（探测结果：placeholder="请输入稿件标题"）
        candidates = [
            'input[placeholder="请输入稿件标题"]',  # 精确匹配
            'input[placeholder*="标题"]',  # 回退
        ]
        for selector in candidates:
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    try:
                        await locator.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    await locator.fill(self.title[:80])
                    actual_title = (await locator.input_value()).strip()
                    if actual_title != self.title[:80]:
                        raise RuntimeError(
                            f"B站标题回读失败，目标={self.title[:80]}，实际={actual_title}"
                        )
                    bilibili_logger.info(f"[bilibili] 标题已填写并回读，耗时 {time.perf_counter() - started_at:.1f} 秒: {actual_title}")
                    return
                bilibili_logger.warning(f"[bilibili] 未能找到标题输入框: {selector}")
            except Exception:
                continue
        raise RuntimeError("B站标题输入框未找到")

    async def _fill_tags(self, page) -> None:
        if not self.tags:
            return
        started_at = time.perf_counter()
        locator = await self._find_tag_input(page)
        if locator is None:
            raise RuntimeError("B站标签输入框未找到")

        try:
            await locator.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await locator.click(force=True, timeout=3000)
            await locator.fill("")
            await self._clear_existing_tags(page, locator)
            bilibili_logger.info("[bilibili] 已清空标签输入框和页面默认标签")
        except Exception as error:
            bilibili_logger.warning(f"[bilibili] 清空标签失败: {error}")

        for tag in self.tags:
            tag = str(tag).strip().lstrip("#")
            if not tag:
                continue
            await locator.fill(tag)
            await locator.press("Enter")
            await page.wait_for_timeout(500)
            bilibili_logger.info(f"[bilibili] 标签已填写: {tag}")

        async def read_missing_tags():
            try:
                selected_region_text = await page.evaluate(
                    """
                    (input) => {
                      const root = input.closest('.form-item, [class*="form-item"]')
                        || input.parentElement;
                      if (!root) return '';
                      const selected = root.querySelector(
                        '.tag-pre-wrp, .input-container, '
                        + '[class*="selected-tag"], [class*="tag-list"]'
                      ) || root;
                      return selected.innerText || selected.textContent || '';
                    }
                    """,
                    await locator.element_handle(),
                )
            except Exception:
                selected_region_text = ""
            normalized_region_text = "".join(selected_region_text.split())

            missing = []
            for tag_value in self.tags:
                tag_text = str(tag_value).strip().lstrip("#")
                if "".join(tag_text.split()) in normalized_region_text:
                    continue
                chips = page.get_by_text(tag_text, exact=True)
                visible_chip = False
                for chip_index in range(await chips.count()):
                    try:
                        if await chips.nth(chip_index).is_visible(timeout=300):
                            visible_chip = True
                            break
                    except Exception:
                        continue
                if not visible_chip:
                    missing.append(tag_text)
            return missing

        # B站标签在上传资源繁忙时会延迟生成标签节点；等待页面真正提交，
        # 仍未提交时再补一次 Enter，避免把瞬时状态误报为失败。
        missing_tags = await read_missing_tags()
        for _ in range(10):
            if not missing_tags:
                break
            await page.wait_for_timeout(500)
            missing_tags = await read_missing_tags()
        if missing_tags:
            for tag_text in missing_tags:
                await locator.fill(tag_text)
                await locator.press("Enter")
                await page.wait_for_timeout(700)
            for _ in range(6):
                missing_tags = await read_missing_tags()
                if not missing_tags:
                    break
                await page.wait_for_timeout(500)
        if missing_tags:
            if self.save_draft_only:
                detail = f"平台未接受标签 {missing_tags}"
                bilibili_logger.warning(f"[bilibili] B站标签未完整写入：{detail}")
                record_draft_field_warning("B站", "标签", detail)
                return
            raise RuntimeError(f"B站标签回读失败，缺少={missing_tags}")
        bilibili_logger.info(
            f"[bilibili] 标签填写完成，耗时 {time.perf_counter() - started_at:.1f} 秒"
        )

    async def _find_tag_input(self, page):
        candidates = (
            'input[placeholder*="按回车键Enter创建标签"]',
            'input[placeholder*="Enter创建标签"]',
            'input[placeholder*="创建标签"]',
            'input[placeholder*="输入标签"]',
            'input[placeholder*="标签"]',
            'textarea[placeholder*="标签"]',
            '.tag-input-wrp input',
            '.tag-input input',
            '.tag-pre-wrp input',
            '[contenteditable="true"][data-placeholder*="标签"]',
            '[contenteditable="true"][aria-label*="标签"]',
        )
        for selector in candidates:
            items = page.locator(selector)
            for index in range(await items.count()):
                item = items.nth(index)
                try:
                    if await item.is_visible(timeout=300):
                        bilibili_logger.info(
                            f"[bilibili] 标签输入框已匹配: {selector}"
                        )
                        return item
                except Exception:
                    continue

        marker = "data-sau-bilibili-tag-input"
        detected = await page.evaluate(
            """
            (marker) => {
              const visible = node => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 20 && rect.height > 12
                  && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const text = value => String(value || '').replace(/\\s+/g, '');
              const nodes = Array.from(document.querySelectorAll(
                'input, textarea, [contenteditable="true"]'
              )).filter(visible);
              let best = null;
              let bestScore = 0;
              for (const node of nodes) {
                const placeholder = text(
                  node.getAttribute('placeholder')
                  || node.getAttribute('data-placeholder')
                  || node.getAttribute('aria-label')
                );
                const className = text(node.className).toLowerCase();
                const parentText = text(
                  node.closest('.form-item, [class*="form-item"], [class*="tag"]')
                    ?.innerText
                  || node.parentElement?.innerText
                );
                let score = 0;
                if (placeholder.includes('标签')) score += 100;
                if (/enter|回车/i.test(placeholder)) score += 50;
                if (className.includes('tag')) score += 40;
                if (parentText.includes('标签')) score += 30;
                if (placeholder.includes('标题')) score -= 100;
                if (score > bestScore) {
                  best = node;
                  bestScore = score;
                }
              }
              if (!best || bestScore < 30) return false;
              document.querySelectorAll(`[${marker}]`).forEach(
                node => node.removeAttribute(marker)
              );
              best.setAttribute(marker, 'true');
              return true;
            }
            """,
            marker,
        )
        if detected:
            return page.locator(f'[{marker}="true"]').first
        return None

    async def _clear_existing_tags(self, page, input_locator) -> None:
        """B站会自动带出推荐标签；发布时只保留用户传入的预设标签。"""
        try:
            before_text = await page.evaluate(
                """(input) => {
                    const root = input.closest('.form-item') || input.parentElement;
                    return root ? (root.innerText || '') : '';
                }""",
                await input_locator.element_handle(),
            )
        except Exception:
            before_text = ""

        # 优先点已选标签 chip 自带的关闭按钮；范围限定在顶部已选标签容器，避免误触推荐标签/话题。
        try:
            clicked = await page.evaluate(
                """(input) => {
                    const root = input.closest('.form-item') || input.parentElement;
                    if (!root) return 0;
                    const selectedWrap = root.querySelector('.tag-pre-wrp') || root.querySelector('.input-container');
                    if (!selectedWrap) return 0;
                    let count = 0;
                    const candidates = Array.from(selectedWrap.querySelectorAll(
                        '.label-item-v2-container svg, .label-item-v2-container [class*="close"], .label-item-v2-container [class*="delete"], .label-item-v2-container [class*="remove"]'
                    ));
                    for (const el of candidates) {
                        const rect = el.getBoundingClientRect();
                        const text = (el.innerText || el.textContent || '').trim();
                        if (rect.width > 0 && rect.width <= 28 && rect.height > 0 && rect.height <= 28 && !text) {
                            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                            count += 1;
                        }
                    }
                    return count;
                }""",
                await input_locator.element_handle(),
            )
            if clicked:
                bilibili_logger.info(f"[bilibili] 已点击默认标签关闭按钮: {clicked} 个")
                await page.wait_for_timeout(300)
        except Exception as e:
            bilibili_logger.debug(f"[bilibili] 标签关闭按钮清理跳过: {e}")

        # 再用空输入框 Backspace 兜底删除 chip。
        try:
            await input_locator.click(force=True, timeout=2000)
            await input_locator.fill("")
            for _ in range(12):
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(60)
        except Exception as e:
            bilibili_logger.debug(f"[bilibili] 标签 Backspace 清理跳过: {e}")

        try:
            after_text = await page.evaluate(
                """(input) => {
                    const root = input.closest('.form-item') || input.parentElement;
                    return root ? (root.innerText || '') : '';
                }""",
                await input_locator.element_handle(),
            )
            if before_text != after_text:
                bilibili_logger.info("[bilibili] 页面默认标签已尝试清理")
        except Exception:
            pass

    async def _fill_desc(self, page) -> None:
        if not self.desc:
            return

        bilibili_logger.info(f"[bilibili] 正在填写简介: {self.desc[:50]}...")

        # 基于实际B站页面的Quill编辑器结构
        desc_selectors = [
            '.ql-editor[contenteditable="true"]',  # 精确匹配Quill编辑器
            '.ql-editor',  # Quill富文本编辑器
            '[contenteditable="true"][data-placeholder*="简介"]',  # 有简介placeholder的可编辑元素
            '[contenteditable="true"]',  # 通用可编辑div
        ]

        for selector in desc_selectors:
            try:
                if await page.locator(selector).first.count():
                    bilibili_logger.info(f"[bilibili] 找到简介输入框: {selector}")

                    # 滚动到元素可见位置
                    try:
                        await page.locator(selector).first.scroll_into_view_if_needed()
                        bilibili_logger.info(f"[bilibili] 简介输入框已滚动到可见位置: {selector}")
                    except Exception:
                        pass

                    # 输入内容
                    await page.locator(selector).first.fill(self.desc[:2000])
                    actual_desc = (
                        await page.locator(selector).first.inner_text()
                    ).strip()
                    if self.desc[:2000] not in actual_desc:
                        raise RuntimeError("B站简介填写后未能回读目标内容")
                    bilibili_logger.info(f"[bilibili] 简介已填写并回读: {self.desc[:2000]}")
                    return

            except Exception as e:
                bilibili_logger.warning(f"[bilibili] 简介选择器 {selector} 失败: {e}")
                continue

        bilibili_logger.warning("[bilibili] 未能找到简介输入框")

    async def _set_type(self, page) -> None:
        if not self.bili_type:
            return

        if self.bili_type not in {"自制", "转载"}:
            raise RuntimeError(
                f"B站投稿类型不正确：{self.bili_type}，仅支持自制或转载"
            )

        bilibili_logger.info(f"[bilibili] 正在选择投稿类型: {self.bili_type}")
        statement = page.locator(
            '#video-up-app .creation-statement-container'
        ).first
        if not await statement.count():
            raise RuntimeError("B站未找到创作声明组件，无法设置投稿类型")

        if self.bili_type == "转载":
            if getattr(self, "ai_generated", False):
                raise RuntimeError(
                    "B站创作声明不能同时选择“含AI生成内容”和“内容为转载”"
                )
            current = (
                await statement.locator(
                    'input.bcc-select-input-inner'
                ).first.input_value()
            ).strip()
            if current != "内容为转载":
                raise RuntimeError(
                    f"B站转载投稿类型回读失败，当前创作声明为：{current or '未选择'}"
                )
            bilibili_logger.info("[bilibili] 投稿类型已回读确认: 转载")
            return

        auth_content = statement.locator('.auth-content').first
        auth_text = auth_content.locator('.option-text').first
        opener = statement.locator('input.bcc-select-input-inner').first
        if not await auth_content.count() or not await auth_text.count():
            raise RuntimeError(
                "B站创作声明底部未找到“内容为自制”授权选项"
            )

        auth_class = await auth_text.get_attribute("class") or ""
        if "option-text-selected" not in auth_class:
            if not await opener.count():
                raise RuntimeError("B站未找到创作声明下拉框")
            await opener.scroll_into_view_if_needed()
            await opener.click(force=True, timeout=3000)
            await auth_content.wait_for(state="visible", timeout=3000)
            await auth_content.click(force=True, timeout=3000)
            await page.wait_for_timeout(250)

        auth_class = await auth_text.get_attribute("class") or ""
        has_selected_icon = bool(
            await auth_content.locator('.option-icon').count()
        )
        if (
            "option-text-selected" not in auth_class
            or not has_selected_icon
        ):
            raise RuntimeError("B站投稿类型“自制”选择后未保持开启")
        bilibili_logger.info(
            f"[bilibili] 投稿类型已选择并回读确认: {BILIBILI_SELF_AUTH_TEXT}"
        )

        list_wrap = statement.locator('.bcc-select-list-wrap').first
        if await list_wrap.count() and await list_wrap.is_visible():
            await opener.click(force=True, timeout=3000)
            try:
                await list_wrap.wait_for(state="hidden", timeout=1500)
            except PlaywrightTimeoutError:
                await page.keyboard.press("Escape")
                await list_wrap.wait_for(state="hidden", timeout=1500)

    async def _set_creation_statement(self, page) -> None:
        """选择当前 B 站新版必填的“创作声明”。"""
        try:
            container = page.locator('#video-up-app').first
            statement = container.locator('.creation-statement-container').first
            if not await statement.count():
                bilibili_logger.info("[bilibili] 未检测到新版创作声明组件，跳过")
                return

            target_texts = [NATIVE_AI_DISCLOSURE_LABELS[5]] if getattr(self, "ai_generated", False) else ["内容无需标注"]
            if self.bili_type == "转载" and not getattr(self, "ai_generated", False):
                target_texts = ["内容为转载"]

            before_text = ""
            try:
                before_text = (await statement.locator('input.bcc-select-input-inner').first.input_value()).strip()
            except Exception:
                pass
            if any(text in before_text for text in target_texts):
                bilibili_logger.info(f"[bilibili] 创作声明已是目标值: {before_text}")
                return

            opener = statement.locator('.bcc-select').first
            if not await opener.count():
                opener = statement.locator('input.bcc-select-input-inner').first
            if not await opener.count():
                message = "[bilibili] 未找到创作声明下拉框"
                if getattr(self, "ai_generated", False):
                    raise RuntimeError(message)
                bilibili_logger.warning(message)
                return

            await opener.scroll_into_view_if_needed()
            await opener.click(force=True, timeout=3000)
            await page.wait_for_timeout(250)

            for text in target_texts:
                try:
                    option = statement.locator(f'li.bcc-option:has-text("{text}")').first
                    if not await option.count():
                        continue
                    await option.scroll_into_view_if_needed(timeout=3000)
                    await option.click(timeout=5000)
                    await page.wait_for_timeout(300)
                    after_text = (await statement.locator('input.bcc-select-input-inner').first.input_value()).strip()
                    if text in after_text:
                        bilibili_logger.info(f"[bilibili] 创作声明已选择并回读确认: {after_text}")
                        return
                    bilibili_logger.warning(f"[bilibili] 创作声明点击后未回读到目标值: target={text}, after={after_text}")
                except PlaywrightError:
                    continue

            message = f"[bilibili] 未能选择创作声明选项：{target_texts}"
            if getattr(self, "ai_generated", False):
                raise RuntimeError(message)
            bilibili_logger.warning(message)
        except Exception as e:
            if getattr(self, "ai_generated", False):
                raise RuntimeError(f"B站 AI 创作声明选择失败: {e}") from e
            bilibili_logger.warning(f"[bilibili] 创作声明选择失败: {e}")

    async def _set_partition(self, page) -> None:
        if not self.partition:
            return

        expected_type_id = BILIBILI_HUMAN_TYPE_IDS.get(self.partition)
        if expected_type_id is None:
            raise RuntimeError(f"B站分区映射中不存在“{self.partition}”")

        bilibili_logger.info(f"[bilibili] 正在选择分区: {self.partition}")
        partition_item = page.locator(
            '#video-up-app .form-item:has('
            '.section-title-content-main:has-text("分区"))'
        ).first
        if not await partition_item.count():
            raise RuntimeError("B站未找到分区设置区域")

        control = partition_item.locator('.select-controller').first
        selected_text = control.locator(
            '.select-item-cont-inserted, .select-item-cont'
        ).first
        if not await control.count() or not await selected_text.count():
            raise RuntimeError("B站未找到分区下拉控件")

        current = (await selected_text.inner_text()).strip()
        current_type_id = await self._read_human_type_id(partition_item)
        if current == self.partition and current_type_id == expected_type_id:
            bilibili_logger.info(
                f"[bilibili] 分区已是目标值: {self.partition} "
                f"(human_type2={current_type_id})"
            )
            return

        await control.scroll_into_view_if_needed()
        await control.click(timeout=3000)
        await page.wait_for_timeout(200)

        target_option = None
        options = page.locator('.drop-list-v2-item-cont')
        for index in range(await options.count()):
            option = options.nth(index)
            try:
                if (
                    await option.is_visible(timeout=300)
                    and (await option.inner_text()).strip() == self.partition
                ):
                    target_option = option
                    break
            except Exception:
                continue
        if target_option is None:
            raise RuntimeError(
                f"B站分区列表中不存在“{self.partition}”"
            )

        await target_option.scroll_into_view_if_needed(timeout=3000)
        await target_option.click(timeout=3000)
        for _ in range(10):
            current = (await selected_text.inner_text()).strip()
            current_type_id = await self._read_human_type_id(partition_item)
            if (
                current == self.partition
                and current_type_id == expected_type_id
            ):
                bilibili_logger.info(
                    f"[bilibili] 分区已选择并回读确认: {current} "
                    f"(human_type2={current_type_id})"
                )
                return
            await page.wait_for_timeout(150)
        raise RuntimeError(
            "B站分区选择后回读不一致，"
            f"目标={self.partition}/{expected_type_id}，"
            f"当前={current or '未选择'}/{current_type_id or '未知'}"
        )

    async def _read_human_type_id(self, partition_item) -> int | None:
        human_type = partition_item.locator(".video-human-type").first
        if not await human_type.count():
            return None
        value = await human_type.evaluate(
            """element => {
                const component = element.__vue__;
                const raw = component?.value ?? component?.$props?.value;
                const parsed = Number(raw);
                return Number.isFinite(parsed) ? parsed : null;
            }"""
        )
        return int(value) if value is not None else None

    async def _set_schedule(self, page) -> None:
        # 仅当提供了具体发布时间才尝试
        if self.save_draft_only or not self.publish_date or self.publish_date == 0:
            return

        try:
            bilibili_logger.info("[bilibili] 开始设置定时发布")

            ts = self._parse_publish_datetime()
            if ts is None:
                return
            ts = self._round_bilibili_time(ts)
            date_str = ts.strftime('%Y-%m-%d')
            time_str = ts.strftime('%H:%M')

            bilibili_logger.info(f"[bilibili] 设置发布时间: {date_str} {time_str}")

            await self._enable_bilibili_schedule_switch(page)
            await self._set_bilibili_schedule_date(page, ts)
            await self._set_bilibili_schedule_time(page, ts)
            await self._assert_bilibili_schedule(page, date_str, time_str)

            bilibili_logger.info("[bilibili] 定时发布设置完成")

        except Exception as e:
            raise RuntimeError(f"B站定时发布设置失败: {e}") from e

    def _parse_publish_datetime(self) -> datetime | None:
        if isinstance(self.publish_date, str):
            try:
                return datetime.strptime(self.publish_date, '%Y-%m-%d %H:%M')
            except Exception:
                bilibili_logger.warning(f"[bilibili] 时间格式解析失败: {self.publish_date}")
                return None
        if isinstance(self.publish_date, datetime):
            return self.publish_date
        if isinstance(self.publish_date, (int, float)):
            return datetime.fromtimestamp(self.publish_date)
        bilibili_logger.warning(f"[bilibili] 不支持的时间格式: {type(self.publish_date)}")
        return None

    async def set_collection(self, page) -> None:
        collection_name = str(getattr(self, "collection_name", "") or "").strip()
        if not collection_name:
            bilibili_logger.info("[bilibili] 未指定合集，本次不设置合集")
            return

        trigger = page.locator(".video-season .season-enter:visible").first
        if not await trigger.count() or not await trigger.is_visible():
            raise RuntimeError(f"B站未找到合集入口，无法选择“{collection_name}”")
        await trigger.scroll_into_view_if_needed()
        trigger_box = await trigger.bounding_box()
        if not trigger_box:
            raise RuntimeError("B站合集入口没有可点击区域")
        await page.mouse.click(
            trigger_box["x"] + trigger_box["width"] / 2,
            trigger_box["y"] + trigger_box["height"] / 2,
        )

        options = page.get_by_text(collection_name, exact=True)
        option = None
        for _ in range(20):
            for index in range(await options.count()):
                candidate = options.nth(index)
                if await candidate.is_visible():
                    option = candidate
                    break
            if option is not None:
                break
            await page.wait_for_timeout(250)
        if option is None:
            raise RuntimeError(f"B站未找到可见合集“{collection_name}”")

        await option.scroll_into_view_if_needed()
        option_box = await option.bounding_box()
        if not option_box:
            raise RuntimeError(f"B站合集“{collection_name}”没有可点击区域")
        await page.mouse.click(
            option_box["x"] + option_box["width"] / 2,
            option_box["y"] + option_box["height"] / 2,
        )
        await page.wait_for_timeout(500)

        selected = page.locator(
            ".video-season .season-enter-text:visible"
        ).first
        selected_title = (await selected.get_attribute("title") or "").strip()
        if selected_title != collection_name:
            selected_text = (await selected.inner_text()).strip()
            if selected_text != collection_name:
                raise RuntimeError(
                    "B站合集选择后回读失败："
                    f"目标={collection_name}，实际={selected_title or selected_text}"
                )
        bilibili_logger.info(f"[bilibili] 合集已选择并回读：{collection_name}")

    def _round_bilibili_time(self, ts: datetime) -> datetime:
        # B站时间选择器分钟粒度为 5 分钟，随机分钟需要就近落到可选项。
        remainder = ts.minute % 5
        if remainder == 0:
            return ts.replace(second=0, microsecond=0)
        delta = 5 - remainder if remainder >= 3 else -remainder
        rounded = ts + timedelta(minutes=delta)
        rounded = rounded.replace(second=0, microsecond=0)
        bilibili_logger.info(
            f"[bilibili] 目标分钟 {ts.strftime('%H:%M')} 已按平台粒度就近调整为 {rounded.strftime('%H:%M')}"
        )
        return rounded

    async def _enable_bilibili_schedule_switch(self, page) -> None:
        switch = page.locator('.time-switch-wrp .switch-container').first
        await switch.wait_for(state='visible', timeout=10000)
        cls = await switch.get_attribute('class') or ''
        if 'active' not in cls:
            await switch.click(force=True, timeout=5000)
            await page.wait_for_timeout(500)
        cls = await switch.get_attribute('class') or ''
        if 'active' not in cls:
            raise RuntimeError("定时发布开关未成功打开")

    async def _set_bilibili_schedule_date(self, page, ts: datetime) -> None:
        await page.locator('.date-picker-date .date-show').first.click(force=True, timeout=5000)
        await page.wait_for_timeout(300)
        picker = page.locator('.date-picker-container').first
        await picker.wait_for(state='visible', timeout=5000)

        target_month = f"{ts.year}年{ts.month}月"
        title = picker.locator('.date-picker-nav-title').first
        for _ in range(3):
            current_month = (await title.inner_text()).strip()
            if current_month == target_month:
                break
            next_button = picker.locator('[class*="right"], [class*="next"], .bcc-icon-ic_next').first
            if not await next_button.count():
                raise RuntimeError(f"日期选择器当前为 {current_month}，未找到切换到 {target_month} 的按钮")
            await next_button.click(force=True, timeout=3000)
            await page.wait_for_timeout(300)

        day_items = picker.locator('.date-wrp .date-picker-body-item')
        for index in range(await day_items.count()):
            item = day_items.nth(index)
            text = (await item.inner_text()).strip()
            cls = await item.get_attribute('class') or ''
            if text == str(ts.day) and 'disabled' not in cls:
                await item.click(force=True, timeout=3000)
                await page.wait_for_timeout(300)
                return
        raise RuntimeError(f"未能在 B站日期选择器中选择 {ts.strftime('%Y-%m-%d')}")

    async def _set_bilibili_schedule_time(self, page, ts: datetime) -> None:
        await page.locator('.date-picker-timer .date-show').first.click(force=True, timeout=5000)
        await page.wait_for_timeout(300)
        await self._select_bilibili_time_value(page, 0, f"{ts.hour:02d}")
        await self._select_bilibili_time_value(page, 1, f"{ts.minute:02d}")
        await page.locator('#video-up-app').first.click(position={"x": 20, "y": 20}, force=True)
        await page.wait_for_timeout(300)

    async def _select_bilibili_time_value(self, page, column_index: int, value: str) -> None:
        panel = page.locator('.time-picker-container .time-picker-panel-select-wrp').nth(column_index)
        await panel.wait_for(state='visible', timeout=5000)
        items = panel.locator('.time-picker-panel-select-item')
        for index in range(await items.count()):
            item = items.nth(index)
            text = (await item.inner_text()).strip()
            cls = await item.get_attribute('class') or ''
            if text == value and 'disabled' not in cls:
                await item.scroll_into_view_if_needed(timeout=3000)
                await item.click(force=True, timeout=3000)
                await page.wait_for_timeout(200)
                return
        raise RuntimeError(f"B站时间选择器未找到可用选项 {value}")

    async def _assert_bilibili_schedule(self, page, date_str: str, time_str: str) -> None:
        container = page.locator('#video-up-app').first
        date_text = (await container.locator('.date-picker-date .date-show').first.inner_text()).strip()
        time_text = (await container.locator('.date-picker-timer .date-show').first.inner_text()).strip()
        bilibili_logger.info(f"[bilibili] 回读定时：date={date_text or '-'} time={time_text or '-'}  (目标: {date_str} {time_str})")
        if date_text != date_str or time_text != time_str:
            raise RuntimeError(f"定时回读不一致，目标 {date_str} {time_str}，当前 {date_text} {time_text}")

    async def _dismiss_unsubmitted_prompt(self, page) -> None:
        """轮询几次，如果存在“未提交的视频”提示则点击“不用了”。避免瞬时出现被错过。"""
        try:
            # 最长轮询 ~3 秒（10 次，每次 300ms）
            for _ in range(10):
                try:
                    tip = page.locator('.upload-wrp .entrance-tip').first
                    if (
                        await tip.count()
                        and await tip.is_visible(timeout=300)
                    ):
                        if self.save_draft_only:
                            raise RuntimeError(
                                "B站账号已有未提交视频。为避免覆盖旧草稿，"
                                "已停止本次保存"
                            )
                        bilibili_logger.info('[bilibili] 检测到未提交视频提示，尝试关闭')
                        candidates = [
                            '.upload-wrp .entrance-tip .entrance-tip-btn[data-reporter-id="32"]',
                            '.upload-wrp .entrance-tip .entrance-tip-btn:has-text("不用了")',
                            'text=不用了',
                        ]
                        clicked = False
                        for sel in candidates:
                            try:
                                btn = page.locator(sel).first
                                if await btn.count():
                                    await btn.scroll_into_view_if_needed()
                                    await btn.click()
                                    bilibili_logger.info(f"[bilibili] 已点击‘不用了’: {sel}")
                                    clicked = True
                                    break
                            except Exception:
                                continue
                        if clicked:
                            # 给页面一点时间收起提示
                            await asyncio.sleep(0.3)
                            return
                except RuntimeError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(0.3)
        except RuntimeError:
            raise
        except Exception as e:
            bilibili_logger.warning(f"[bilibili] 关闭未提交提示失败: {e}")

    async def _wait_upload_complete(self, page) -> None:
        bilibili_logger.info("[bilibili] 等待视频上传完成...")

        # 轮询检测上传完成状态（要求完成状态稳定出现多次，避免误判）
        success_stable_ticks = 0
        container = page.locator('#video-up-app').first

        for i in range(180):  # 最多轮询 ~3 分钟
            try:
                # 读取状态容器文本（若有）
                status_text = None
                try:
                    if await container.locator('.file-item-content-status-text').first.count():
                        status_text = (await container.locator('.file-item-content-status-text').first.inner_text() or '').strip()
                except Exception:
                    status_text = None

                # 1) 判断是否仍在上传中
                in_progress = False
                # 文案信号
                if status_text and any(key in status_text for key in ("上传中", "当前速度", "剩余时间")):
                    in_progress = True

                # 2) 判断是否上传完成（结合容器文本/图标/百分比）
                success_found = False
                # 图标/文本标识
                if await container.locator('.file-item-content-status-text .success:has-text("上传完成")').count():
                    success_found = True
                elif status_text and ("上传完成" in status_text):
                    success_found = True

                # 稳定判定：需非上传中且连续3次检测到完成
                bilibili_logger.info(f"[bilibili] success_found: {success_found}, in_progress: {in_progress}")
                if success_found and not in_progress:
                    success_stable_ticks += 1
                    if success_stable_ticks >= 3:
                        bilibili_logger.info("[bilibili] 上传完成状态稳定，继续后续流程")
                        return
                else:
                    success_stable_ticks = 0

                # 每3秒打印一次当前状态
                if i % 3 == 0:
                    if status_text:
                        bilibili_logger.info(f"[bilibili] 上传状态: {status_text}")
                    else:
                        # 回退打印部分结构存在性
                        exists = await container.locator('.upload-audit-progress').count()
                        bilibili_logger.info(f"[bilibili] 仍在等待上传完成... ({i}秒) progress_container={exists>0}")

            except Exception as e:
                bilibili_logger.warning(f"[bilibili] 检测上传状态时出错: {e}")

            await asyncio.sleep(1)

        bilibili_logger.warning("[bilibili] 上传完成检测超时（3分钟）")

    async def _click_publish(self, page) -> None:
        # 基于实际B站页面的发布按钮选择器
        bilibili_logger.info("[bilibili] 正在寻找发布按钮...")
        await prevent_new_tabs(page, logger=bilibili_logger, label="B站投稿")

        # 基于实际HTML结构，发布按钮是span元素
        publish_selectors = [
            'span.submit-add:has-text("立即投稿")',  # 精确匹配实际结构
        ]

        for selector in publish_selectors:
            try:
                if await page.locator(selector).count():
                    bilibili_logger.info(f"[bilibili] 找到发布按钮: {selector}")
                    await page.locator(selector).scroll_into_view_if_needed()
                    await page.locator(selector).click()
                    bilibili_logger.info(f"[bilibili] 成功点击发布按钮: {selector}")
                    return
            except Exception as e:
                bilibili_logger.warning(f"[bilibili] 发布按钮选择器失败 {selector}: {e}")
                continue

        raise RuntimeError("B站未找到立即投稿按钮")

    async def _wait_publish_result(self, page) -> bool:
        """等待发布结果，返回是否发布成功"""
        bilibili_logger.info("[bilibili] 等待发布结果...")

        # 轮询检测发布结果。发布按钮消失即可视为已提交，避免卡住后续平台。
        for i in range(20):
            try:
                # 1. 检测是否出现成功提示 - 基于实际HTML结构
                success_indicators = [
                    '.step-des:has-text("稿件投递成功")',  # 精确匹配实际成功页面
                    '.video-complete .step-des',  # 成功页面的容器
                    'text=稿件投递成功',  # 文本匹配
                ]

                for indicator in success_indicators:
                    if await page.locator(indicator).count():
                        bilibili_logger.info(f"[bilibili] 检测到发布成功标识: {indicator}")
                        return True

                # 2. 检测是否出现错误提示
                error_indicators = [
                    '.error-tip',
                    '.error-message',
                    '.upload-error',
                    'text=发布失败',
                    'text=投稿失败',
                    'text=上传失败',
                ]

                for indicator in error_indicators:
                    if await page.locator(indicator).count():
                        error_text = ""
                        try:
                            error_text = await page.locator(indicator).first.inner_text()
                        except Exception:
                            pass
                        bilibili_logger.error(f"[bilibili] 检测到发布失败标识: {indicator}, 错误信息: {error_text}")
                        return False

                # 3. 检测是否页面跳转（发布成功后通常会跳转）
                current_url = page.url
                if "upload" not in current_url and "member.bilibili.com" in current_url:
                    bilibili_logger.info(f"[bilibili] 页面已跳转，可能发布成功: {current_url}")
                    return True

                # 4. 检测发布按钮是否消失（表示已经提交到平台处理）
                publish_button_exists = False
                for selector in ['span.submit-add:has-text("立即投稿")', '.submit-add:has-text("立即投稿")']:
                    if await page.locator(selector).count():
                        publish_button_exists = True
                        break

                if not publish_button_exists:
                    bilibili_logger.info("[bilibili] 发布按钮已消失，视为投稿已提交")
                    return True

                if i % 5 == 0:
                    bilibili_logger.info(f"[bilibili] 仍在等待发布结果... ({i}秒)")

            except Exception as e:
                bilibili_logger.warning(f"[bilibili] 检测发布结果时出错: {e}")

            await asyncio.sleep(1)

        bilibili_logger.warning("[bilibili] 发布结果检测超时（20秒）")
        return False

    async def upload(self, playwright: Playwright) -> None:
        page = getattr(self, "external_page", None)
        context = getattr(self, "external_context", None)
        browser = getattr(self, "external_browser", None)
        managed_browser = page is None
        if managed_browser:
            browser = await launch_publish_browser(
                playwright,
                executable_path=self.local_executable_path,
            )
            context = await new_publish_context(
                browser,
                storage_state=str(self.account_file),
            )
            context = await set_init_script(context)
            page = await context.new_page()

        async def _handle_unexpected_filechooser(file_chooser) -> None:
            try:
                bilibili_logger.warning("[bilibili] 检测到意外文件选择器触发，已拦截并置空。请检查是否仍有可见上传按钮被误点。")
                await file_chooser.set_files([])
            except Exception as e:
                bilibili_logger.warning(f"[bilibili] 拦截意外文件选择器失败: {e}")

        page.on("filechooser", lambda file_chooser: asyncio.create_task(_handle_unexpected_filechooser(file_chooser)))

        bilibili_logger.info("[bilibili] goto upload page")
        # 打开B站创作中心上传页（优先带 page_from 参数）
        try:
            await goto_and_reveal(
                page,
                "https://member.bilibili.com/platform/upload/video/frame?page_from=creative_home_top_upload",
                timeout=15000,
            )
        except Exception:
            try:
                await goto_and_reveal(
                    page,
                    "https://member.bilibili.com/platform/upload/video/frame",
                    timeout=15000,
                )
            except Exception:
                # 回退到旧地址
                await goto_and_reveal(
                    page,
                    "https://member.bilibili.com/platform/upload/video",
                    timeout=20000,
                )

        bilibili_logger.info("[bilibili] wait page ready")

        # 若存在“未提交的视频”提示，优先关闭
        await self._dismiss_unsubmitted_prompt(page)

        # 选择视频上传输入框（第一个，接受视频格式的）
        video_input_selector = 'input[type="file"][accept*=".mp4"]'
        await page.locator(video_input_selector).first.set_input_files(self.file_path)
        bilibili_logger.info(f"[bilibili] 视频文件已设置: {self.file_path}")

        bilibili_logger.info("[bilibili] wait upload complete")
        # 等待上传完成或稳定
        await self._wait_upload_complete(page)
        # 设置封面（可选）
        await self._set_cover(page)
        await self._fill_title(page)
        await self._set_creation_statement(page)
        await self._set_type(page)
        await self._set_partition(page)
        await self._fill_tags(page)
        await self._fill_desc(page)
        await self.set_collection(page)
        await self._set_schedule(page)

        if self.save_draft_only:
            result = await self.save_draft_safely(page)
            await save_context_storage_state(context, self.account_file)
            bilibili_logger.info(
                f"[bilibili] 平台草稿保存成功: {result['evidence']}"
            )
            if managed_browser:
                await context.close()
                await browser.close()
            return

        if self.dry_run:
            if managed_browser:
                await keep_browser_open_for_dry_run(
                    page,
                    context,
                    browser,
                    account_file=self.account_file,
                    logger=bilibili_logger,
                    platform_name="B站",
                    block_until_close=self.dry_run_hold_browser,
                )
            return

        # 发布
        bilibili_logger.info("[bilibili] click publish")
        await self._click_publish(page)

        # 等待发布结果
        publish_success = await self._wait_publish_result(page)

        if publish_success:
            bilibili_logger.info("[bilibili] 视频发布成功！")
        else:
            raise RuntimeError("B站点击投稿后未确认提交成功")

        # 保存cookie
        await save_context_storage_state(context, self.account_file)
        # 关闭浏览器上下文和浏览器实例
        if managed_browser:
            await context.close()
            await browser.close()

    async def save_draft_safely(self, page) -> dict:
        if not self.save_draft_only:
            raise RuntimeError("只有显式开启B站保存草稿模式才允许执行")

        controls = []
        draft_controls = page.locator("span.submit-draft")
        for index in range(await draft_controls.count()):
            item = draft_controls.nth(index)
            try:
                text = (await item.inner_text()).strip()
                if (
                    text in ("存草稿", "保存草稿")
                    and await item.is_visible(timeout=500)
                ):
                    controls.append((item, text))
            except Exception:
                continue

        if len(controls) > 1:
            raise RuntimeError(
                f"B站页面同时出现 {len(controls)} 个可见草稿控件，"
                "无法唯一判定，已停止点击"
            )
        if controls:
            control, control_name = controls[0]
        else:
            control, control_name = await find_unique_draft_control(
                page,
                ("存草稿", "保存草稿"),
            )
        if control is None:
            raise RuntimeError("B站当前页面未提供可验证的“存草稿”入口")

        control_class = await control.get_attribute("class") or ""
        if "btn-disabled" in control_class:
            raise RuntimeError("B站“存草稿”当前不可用，请检查视频上传和必填项")

        baseline_text = await page_body_text(page)
        await control.scroll_into_view_if_needed(timeout=5000)
        await control.click(timeout=10000)

        success_texts = (
            "已存入草稿箱",
            "草稿已保存",
            "已保存到草稿",
            "保存草稿成功",
        )
        error_texts = (
            "保存失败，请重试",
            "多p稿件不支持保存草稿",
            "该视频还未上传完成，请上传完成再保存",
            "草稿箱数量已达上限",
        )
        evidence = []
        for _ in range(40):
            body_text = await page_body_text(page)
            evidence = [
                marker
                for marker in success_texts
                if body_text.count(marker) > baseline_text.count(marker)
            ]
            if evidence:
                break

            current_url = str(getattr(page, "url", "") or "")
            if (
                "/upload-manager/article" in current_url
                and "group=draft" in current_url
            ):
                evidence = ["已进入B站草稿箱"]
                break

            errors = [
                marker
                for marker in error_texts
                if body_text.count(marker) > baseline_text.count(marker)
            ]
            if errors:
                raise RuntimeError(f"B站保存草稿失败：{'；'.join(errors)}")
            await page.wait_for_timeout(500)

        if not evidence:
            raise RuntimeError(
                f"B站点击“{control_name}”后未返回草稿保存成功回执"
            )
        warnings = []
        if self.partition:
            warnings.append(BILIBILI_DRAFT_PARTITION_WARNING)
            bilibili_logger.warning(
                f"[bilibili] {BILIBILI_DRAFT_PARTITION_WARNING}"
            )
        return {
            "status": "draft_saved",
            "control": control_name,
            "evidence": evidence,
            "warnings": warnings,
        }

    async def main(self) -> None:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def _set_cover(self, page) -> None:
        cover_by_ratio = {
            ratio: path
            for ratio, path in {
                "4:3": self.thumbnail_paths.get("4:3"),
                "16:9": self.thumbnail_paths.get("16:9"),
            }.items()
            if path
        }
        fallback_path = self.thumbnail_path or cover_by_ratio.get("4:3") or cover_by_ratio.get("16:9")
        if not fallback_path:
            return
        started_at = time.perf_counter()
        try:
            if cover_by_ratio:
                bilibili_logger.info(f"[bilibili] 开始设置 B站双规格封面: {cover_by_ratio}")
            else:
                bilibili_logger.info(f"[bilibili] 开始设置封面: {fallback_path}")
            await self._open_cover_dialog(page)
            await self._switch_to_cover_upload_tab(page)

            if cover_by_ratio:
                if set(cover_by_ratio) == {"4:3"}:
                    await self._select_cover_ratio(page, "4:3")
                    before_signature = await self._get_cover_upload_area_signature(page)
                    await self._upload_cover_file(page, cover_by_ratio["4:3"])
                    await self._wait_cover_ratio_uploaded(
                        page,
                        "4:3",
                        before_signature=before_signature,
                    )
                else:
                    await self._ensure_cover_sync_disabled(page)
                    for ratio in ("4:3", "16:9"):
                        path = cover_by_ratio.get(ratio)
                        if not path:
                            continue
                        await self._select_cover_ratio(page, ratio)
                        before_signature = await self._get_cover_upload_area_signature(page)
                        await self._upload_cover_file(page, path)
                        await self._wait_cover_ratio_uploaded(page, ratio, before_signature=before_signature)
            else:
                await self._upload_cover_file(page, fallback_path)

            await self._confirm_cover_dialog(page)
            bilibili_logger.info(f"[bilibili] 封面设置完成，耗时 {time.perf_counter() - started_at:.1f} 秒")
        except Exception as e:
            bilibili_logger.error("[bilibili] 设置封面失败")
            bilibili_logger.error(e)
            raise RuntimeError(f"B站封面设置失败：{e}") from e

    async def _open_cover_dialog(self, page) -> None:
        selectors = [
            '#video-up-app :text-is("添加封面")',
            'text=封面设置',
            '.cover:has-text("封面设置")',
            'span:has-text("更换封面")',
            'button:has-text("更换封面")',
        ]
        for selector in selectors:
            locator = page.locator(selector).last
            try:
                if await locator.count() and await locator.is_visible():
                    await locator.scroll_into_view_if_needed()
                    await locator.click(force=True, timeout=5000)
                    await page.locator('.cover-editor.bcc-dialog__wrap-mask, .cover-editor').last.wait_for(
                        state="visible",
                        timeout=5000,
                    )
                    bilibili_logger.info(f"[bilibili] 已打开封面弹窗: {selector}")
                    return
            except PlaywrightError:
                continue
        raise RuntimeError("未找到 B站封面编辑入口")

    async def _switch_to_cover_upload_tab(self, page) -> None:
        editor = page.locator('.cover-editor.bcc-dialog__wrap-mask, .cover-editor').last
        await editor.wait_for(state="visible", timeout=5000)
        image_input = editor.locator('input[type="file"][accept*="image"]').last
        try:
            await image_input.wait_for(state="attached", timeout=5000)
            if await image_input.count():
                bilibili_logger.info("[bilibili] 已检测到封面图片 input，跳过上传封面 tab 切换")
                return
        except PlaywrightError:
            pass

        bilibili_logger.info("[bilibili] 未检测到封面图片 input，不点击上传封面按钮，避免弹出系统资源管理器")

    async def _ensure_cover_sync_disabled(self, page) -> None:
        try:
            editor = page.locator('.cover-editor.bcc-dialog__wrap-mask, .cover-editor').last
            checkbox = editor.locator('.cover-editor-panel-canvas input[type="checkbox"]').first
            if await checkbox.count() and await checkbox.is_checked():
                await checkbox.click(force=True, timeout=2000)
                bilibili_logger.info("[bilibili] 已关闭双比例同步改动，准备分别上传 4:3 和 16:9")
                await page.wait_for_timeout(150)
        except Exception as e:
            bilibili_logger.debug(f"[bilibili] 检查双比例同步状态失败，继续分别上传: {e}")

    async def _select_cover_ratio(self, page, ratio: str) -> None:
        editor = page.locator('.cover-editor.bcc-dialog__wrap-mask, .cover-editor').last
        await editor.wait_for(state="visible", timeout=5000)
        ratio_text = "首页推荐封面（4:3）" if ratio == "4:3" else "个人空间封面（16:9）"
        selectors = [
            f'.cover-editor-panel-canvas > div:has-text("{ratio_text}")',
            f'.cover-editor-panel-canvas-title:has-text("{ratio_text}")',
            f'.cover-editor-panel-canvas span.text:has-text("{ratio_text}")',
            f'text={ratio_text}',
        ]
        for selector in selectors:
            locator = editor.locator(selector).first
            try:
                if await locator.count():
                    await locator.scroll_into_view_if_needed()
                    await locator.click(force=True, timeout=3000)
                    bilibili_logger.info(f"[bilibili] 已切换封面比例: {ratio}")
                    await page.wait_for_timeout(200)
                    return
            except PlaywrightError:
                continue
        raise RuntimeError(f"未找到 B站 {ratio} 封面编辑区域")

    async def _upload_cover_file(self, page, cover_path: str) -> None:
        editor = page.locator('.cover-editor.bcc-dialog__wrap-mask, .cover-editor').last
        await editor.wait_for(state="visible", timeout=5000)
        selectors = [
            'input[type="file"][accept*="image"]',
            'input[type="file"][accept*=".png"]',
        ]
        for selector in selectors:
            locator = editor.locator(selector).last
            try:
                if await locator.count():
                    await locator.set_input_files(cover_path)
                    bilibili_logger.info(f"[bilibili] 封面文件已设置: {selector} -> {cover_path}")
                    return
            except PlaywrightError:
                continue
        raise RuntimeError("未找到 B站封面图片上传 input")

    async def _get_cover_upload_area_signature(self, page) -> str:
        try:
            return await page.evaluate(
                """() => {
                    const editor = document.querySelector('.cover-editor');
                    const area = editor?.querySelector('.cover-editor-panel-select .upload-area.has-image')
                        || editor?.querySelector('.cover-editor-panel-select .upload-area');
                    if (!area) return '';
                    const style = getComputedStyle(area);
                    return style.backgroundImage || area.getAttribute('style') || area.className || '';
                }""",
            ) or ""
        except Exception:
            return ""

    async def _wait_cover_ratio_uploaded(self, page, ratio: str, before_signature: str = "", timeout=8000) -> None:
        started_at = time.perf_counter()
        while time.perf_counter() - started_at < timeout / 1000:
            try:
                signature = await self._get_cover_upload_area_signature(page)
                if signature and (not before_signature or signature != before_signature):
                    await page.wait_for_timeout(1400)
                    bilibili_logger.info(f"[bilibili] {ratio} 封面上传素材已更新并等待应用")
                    return
            except PlaywrightError:
                pass
            await page.wait_for_timeout(200)
        await page.wait_for_timeout(1400)
        bilibili_logger.warning(f"[bilibili] 未检测到 {ratio} 上传素材变化，已额外等待后继续")

    async def _confirm_cover_dialog(self, page) -> None:
        editor = page.locator('.cover-editor.bcc-dialog__wrap-mask, .cover-editor').last
        await editor.wait_for(state="visible", timeout=5000)
        selectors = [
            '.cover-editor-button .button.submit:has-text("完成")',
            '.cover-editor-content-right-bottom .button.submit:has-text("完成")',
            '.cover-editor .button.submit:has-text("完成")',
        ]
        for selector in selectors:
            locator = editor.locator(selector).last
            try:
                await locator.wait_for(state="visible", timeout=5000)
                await locator.click(force=True, timeout=5000)
                if await self._wait_cover_dialog_closed(page):
                    bilibili_logger.info(f"[bilibili] 封面弹窗已确认: {selector}")
                    return
                bilibili_logger.warning(f"[bilibili] 点击封面完成后弹窗仍未关闭: {selector}")
            except PlaywrightError:
                continue
        raise RuntimeError("未找到 B站封面完成/确定按钮")

    async def _wait_cover_dialog_closed(self, page, timeout=8000) -> bool:
        started_at = time.perf_counter()
        while time.perf_counter() - started_at < timeout / 1000:
            dialog = page.locator('.cover-editor.bcc-dialog__wrap-mask, .bcc-dialog:has-text("封面制作")').first
            try:
                if not await dialog.count() or not await dialog.is_visible():
                    return True
            except PlaywrightError:
                return True
            await page.wait_for_timeout(200)
        return False
