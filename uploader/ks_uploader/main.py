# -*- coding: utf-8 -*-
from datetime import datetime

from playwright.async_api import Page, Playwright, async_playwright
import os
import asyncio
import re

from conf import LOCAL_CHROME_PATH
from utils.base_social_media import (
    set_init_script,
    launch_publish_browser,
    new_publish_context,
    reveal_page_window,
    keep_browser_open_for_dry_run,
    save_context_storage_state,
)
from utils.ai_disclosure import (
    NATIVE_AI_DISCLOSURE_LABELS,
    click_visible_text_option,
    open_control_until_option_visible,
)
from utils.files_times import get_absolute_path
from utils.log import kuaishou_logger, KUAISHOU_SCREENSHOT_DIR
from utils.platform_draft import record_draft_field_warning
from utils.publish_limits import KUAISHOU_TAG_COUNT, normalize_publish_tags


async def cookie_auth(account_file):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://cp.kuaishou.com/article/publish/video")
        try:
            await page.wait_for_selector("div.names div.container div.name:text('机构服务')", timeout=5000)  # 等待5秒

            kuaishou_logger.info("[+] 等待5秒 cookie 失效")
            return False
        except:
            kuaishou_logger.success("[+] cookie 有效")
            return True


async def ks_setup(account_file, handle=False):
    account_file = get_absolute_path(account_file, "ks_uploader")
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            return False
        kuaishou_logger.info('[+] cookie文件不存在或已失效，即将自动打开浏览器，请扫码登录，登陆后会自动生成cookie文件')
        await get_ks_cookie(account_file)
    return True


async def get_ks_cookie(account_file):
    async with async_playwright() as playwright:
        options = {
            'args': [
                '--lang en-GB'
            ],
            'headless': False,  # Set headless option here
        }
        # Make sure to run headed.
        browser = await playwright.chromium.launch(**options)
        # Setup context however you like.
        context = await browser.new_context(
            permissions=[],  # 禁用所有权限请求
            geolocation=None,  # 禁用地理位置
            locale='zh-CN',  # 设置语言为中文
            timezone_id='Asia/Shanghai'  # 设置时区
        )  # Pass any options
        context = await set_init_script(context)
        # Pause the page, and start recording manually.
        page = await context.new_page()
        await page.goto("https://cp.kuaishou.com")
        await page.pause()
        # 点击调试器的继续，保存cookie
        await save_context_storage_state(context, account_file)


class KSVideo(object):
    PUBLISH_URL = "https://cp.kuaishou.com/article/publish/video"

    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime,
        account_file,
        thumbnail_path=None,
        thumbnail_paths=None,
        dry_run=False,
        dry_run_hold_browser=True,
        save_draft_only=False,
        description=None,
    ):
        self.title = title  # 视频标题
        self.file_path = file_path
        all_tags = normalize_publish_tags(tags, max_count=1000)
        if len(all_tags) > KUAISHOU_TAG_COUNT:
            raise ValueError(
                f"快手最多允许 {KUAISHOU_TAG_COUNT} 个话题，当前为 {len(all_tags)} 个"
            )
        self.tags = all_tags
        self.publish_date = publish_date
        self.account_file = account_file
        self.date_format = '%Y-%m-%d %H:%M'
        self.local_executable_path = LOCAL_CHROME_PATH
        self.thumbnail_path = thumbnail_path
        self.thumbnail_paths = {
            str(ratio): path
            for ratio, path in (thumbnail_paths or {}).items()
            if ratio in {"3:4", "4:3"} and path
        }
        self.dry_run = dry_run
        self.dry_run_hold_browser = dry_run_hold_browser
        self.save_draft_only = bool(save_draft_only)
        self.description = description
        if self.save_draft_only and self.dry_run:
            raise ValueError("快手保存草稿模式与预发布检查不能同时开启")

    async def handle_upload_error(self, page):
        kuaishou_logger.error("视频出错了，重新上传中")
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def wait_publish_button_ready(self, page: Page):
        await self.dismiss_creator_guide(page)
        publish_button = page.get_by_text("发布", exact=True).last
        await publish_button.wait_for(state="visible", timeout=60000)

        for _ in range(60):
            await self.dismiss_creator_guide(page)
            try:
                button_class = await publish_button.get_attribute("class") or ""
                disabled_attr = await publish_button.get_attribute("disabled")
                aria_disabled = await publish_button.get_attribute("aria-disabled")
                if (
                    await publish_button.is_enabled(timeout=800)
                    and disabled_attr is None
                    and aria_disabled != "true"
                    and "disabled" not in button_class.lower()
                ):
                    await publish_button.scroll_into_view_if_needed(timeout=3000)
                    return publish_button
            except Exception:
                pass
            await page.wait_for_timeout(1000)

        raise RuntimeError("快手发布按钮长时间不可用")

    async def upload(self, playwright: Playwright) -> None:
        page = getattr(self, "external_page", None)
        context = getattr(self, "external_context", None)
        browser = getattr(self, "external_browser", None)
        managed_browser = page is None
        if managed_browser:
            # 使用 Chromium 浏览器启动一个浏览器实例
            browser = await launch_publish_browser(playwright, executable_path=self.local_executable_path)
            context = await new_publish_context(
                browser,
                storage_state=f"{self.account_file}",
            )
            context = await set_init_script(context)
            # 创建一个新的页面
            page = await context.new_page()
        await self.prepare_publish_page(page)
        await reveal_page_window(page)
        await self.dismiss_creator_guide(page)
        kuaishou_logger.info('正在上传-------{}.mp4'.format(self.title))
        await self.upload_video_file(page)

        await page.locator("#work-description-edit").wait_for(state="visible", timeout=60000)
        await self.dismiss_creator_guide(page)

        # 等待按钮可交互
        new_feature_button = page.locator('button[type="button"] span:text("我知道了")')
        if await new_feature_button.count() > 0:
            await new_feature_button.click()

        await self.dismiss_creator_guide(page)
        await self.fill_description_and_topics(page)
        await self.wait_video_upload_done(page)

        if self.thumbnail_path or self.thumbnail_paths:
            await self.dismiss_creator_guide(page)
            await self.set_thumbnail_from_cover_dialog(page)

        await self.set_collection(page)
        await self.set_ai_generated_declaration(page)

        # 定时任务
        if self.publish_date != 0 and not self.save_draft_only:
            await self.dismiss_creator_guide(page)
            await self.set_schedule_time(page, self.publish_date)

        if self.save_draft_only:
            result = await self.save_draft_safely(page)
            await save_context_storage_state(context, self.account_file)
            kuaishou_logger.success(
                f"快手草稿已通过恢复入口确认：{result['evidence']}"
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
                    logger=kuaishou_logger,
                    platform_name="快手",
                    block_until_close=self.dry_run_hold_browser,
                )
            return

        publish_button = await self.wait_publish_button_ready(page)
        await publish_button.click(timeout=10000)

        confirm_button = page.get_by_text("确认发布").last
        try:
            if await confirm_button.count() > 0 and await confirm_button.is_visible(timeout=5000):
                await confirm_button.click(timeout=10000)
        except Exception:
            pass

        try:
            await page.wait_for_url(
                "https://cp.kuaishou.com/article/manage/video?status=2&from=publish",
                timeout=90000,
            )
            kuaishou_logger.success("视频发布成功")
        except Exception as e:
            screenshot_path = os.path.join(KUAISHOU_SCREENSHOT_DIR, f"ks_publish_timeout_{int(asyncio.get_event_loop().time()*1000)}.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            raise RuntimeError(f"快手点击发布后未确认成功，已保留截图：{screenshot_path}") from e

        await save_context_storage_state(context, self.account_file)  # 保存cookie
        kuaishou_logger.info('cookie更新完毕！')
        # 关闭浏览器上下文和浏览器实例
        if managed_browser:
            await context.close()
            await browser.close()

    async def prepare_publish_page(self, page: Page):
        current_url = str(getattr(page, "url", "") or "")
        if current_url.startswith(self.PUBLISH_URL):
            try:
                await page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=90000,
                )
            except Exception:
                pass
            kuaishou_logger.info("快手已复用预打开的发布页面")
        else:
            await page.goto(
                self.PUBLISH_URL,
                wait_until="domcontentloaded",
                timeout=90000,
            )
            kuaishou_logger.info("快手发布页已打开")
        await page.wait_for_timeout(800)

    async def set_collection(self, page: Page):
        collection_name = str(getattr(self, "collection_name", "") or "").strip()
        if not collection_name:
            kuaishou_logger.info("快手未指定合集，本次不设置合集")
            return

        opened = False
        for trigger_text in (
            "选择要加入到的合集",
            "请选择合集",
            "选择合集",
            "添加到合集",
        ):
            trigger = page.get_by_text(trigger_text, exact=True)
            for index in range(await trigger.count() - 1, -1, -1):
                item = trigger.nth(index)
                try:
                    if await item.is_visible(timeout=500):
                        await item.click(force=True, timeout=3000)
                        opened = True
                        break
                except Exception:
                    continue
            if opened:
                break
        if not opened:
            if not self.save_draft_only:
                raise RuntimeError(f"快手未找到合集入口，无法选择“{collection_name}”")
            detail = f"未找到合集入口，目标合集为“{collection_name}”"
            kuaishou_logger.warning(f"快手{detail}")
            record_draft_field_warning("快手", "合集", detail)
            return

        await page.wait_for_timeout(600)
        options = page.get_by_text(collection_name, exact=True)
        selected = False
        for index in range(await options.count() - 1, -1, -1):
            item = options.nth(index)
            try:
                if await item.is_visible(timeout=500):
                    await item.click(force=True, timeout=3000)
                    selected = True
                    break
            except Exception:
                continue
        if not selected:
            if not self.save_draft_only:
                raise RuntimeError(f"快手未找到合集“{collection_name}”")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            detail = f"当前账号没有可选合集“{collection_name}”"
            kuaishou_logger.warning(f"快手{detail}")
            record_draft_field_warning("快手", "合集", detail)
            return

        await page.wait_for_timeout(500)
        body_text = await page.locator("body").inner_text(timeout=3000)
        if collection_name not in body_text:
            if not self.save_draft_only:
                raise RuntimeError(f"快手合集选择后未能回读“{collection_name}”")
            detail = f"选择“{collection_name}”后平台未能回读确认"
            kuaishou_logger.warning(f"快手{detail}")
            record_draft_field_warning("快手", "合集", detail)
            return
        kuaishou_logger.success(f"快手合集已选择并回读：{collection_name}")

    async def set_ai_generated_declaration(self, page: Page):
        if not getattr(self, "ai_generated", False):
            return

        target_text = NATIVE_AI_DISCLOSURE_LABELS[4]
        body_text = await page.locator("body").inner_text(timeout=3000)
        if f"作者声明： {target_text}" in body_text or f"作者声明：{target_text}" in body_text:
            kuaishou_logger.info(f"快手 AI 内容声明已是目标值：{target_text}")
            return

        opened = await open_control_until_option_visible(
            page,
            "为作品添加补充说明",
            target_text,
        )
        if not opened:
            raise RuntimeError("快手未找到作者声明下拉框，不能确认 AI 生成内容声明")

        selected = await click_visible_text_option(page, target_text)
        if not selected:
            raise RuntimeError(f"快手作者声明中未找到“{target_text}”")

        await page.wait_for_timeout(500)
        body_text = await page.locator("body").inner_text(timeout=3000)
        if target_text not in body_text:
            raise RuntimeError(f"快手 AI 内容声明点击后未能回读“{target_text}”")
        kuaishou_logger.success(f"快手 AI 内容声明已选择并回读：{target_text}")

    async def upload_video_file(self, page):
        await self.dismiss_previous_draft_prompt(page)
        await self.dismiss_creator_guide(page)

        file_inputs = [
            'input[type="file"][accept*="video"]',
            'input[type="file"]',
            '[class^="upload-btn-input"]',
        ]
        upload_button_factories = [
            lambda: page.locator("button[class^='_upload-btn']").first,
            lambda: page.get_by_role("button", name="上传视频").first,
            lambda: page.get_by_text("上传视频", exact=True).first,
            lambda: page.locator('button:has-text("上传")').first,
        ]

        started_at = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - started_at < 60:
            await self.dismiss_previous_draft_prompt(page)
            await self.dismiss_creator_guide(page)

            for selector in file_inputs:
                locator = page.locator(selector).first
                try:
                    if await locator.count():
                        await locator.set_input_files(self.file_path)
                        kuaishou_logger.info(f"已通过文件输入框上传视频: {selector}")
                        return
                except Exception as e:
                    kuaishou_logger.warning(f"快手文件输入框上传失败 {selector}: {e}")

            for factory in upload_button_factories:
                upload_button = factory()
                try:
                    if await upload_button.count():
                        await upload_button.wait_for(state='visible', timeout=3000)
                        async with page.expect_file_chooser(timeout=5000) as fc_info:
                            await upload_button.click(force=True, timeout=5000)
                        file_chooser = await fc_info.value
                        await file_chooser.set_files(self.file_path)
                        kuaishou_logger.info("已通过上传按钮选择视频")
                        return
                except Exception as e:
                    kuaishou_logger.warning(f"快手上传按钮尝试失败: {e}")
            await asyncio.sleep(1)
        raise RuntimeError("未找到快手视频上传入口")

    async def dismiss_creator_guide(self, page: Page):
        """Dismiss Kuaishou multi-step creator guidance overlays that block inputs."""
        progress_pattern = re.compile(r"\b[1-9]\s*/\s*[1-9]\b")
        for _ in range(8):
            try:
                body_text = await page.locator("body").inner_text(timeout=1200)
            except Exception:
                return

            next_count = await self.visible_text_count(page, "下一步")
            guide_active = bool(progress_pattern.search(body_text)) and (
                next_count > 0
                or "便捷填写作品关键信息" in body_text
                or "作品信息" in body_text
            )
            if not guide_active:
                await self.click_first_visible_text(page, ["我知道了", "知道了"])
                await self.remove_creator_guide_overlays(page)
                return

            kuaishou_logger.info("检测到快手发布页新手引导，正在自动处理")
            if await self.click_first_visible_text(page, ["跳过", "我知道了", "知道了", "完成", "关闭"]):
                await page.wait_for_timeout(500)
                continue
            if await self.click_first_visible_close_button(page):
                await page.wait_for_timeout(500)
                continue
            if await self.click_first_visible_text(page, ["下一步"]):
                await page.wait_for_timeout(700)
                continue
            await self.remove_creator_guide_overlays(page)
            return

        await self.remove_creator_guide_overlays(page)

    async def remove_creator_guide_overlays(self, page: Page):
        try:
            removed = await page.evaluate(
                r"""
                () => {
                  const textNeedles = ['作品信息', '便捷填写作品关键信息', '下一步', '1/4', '2/4', '3/4', '4/4'];
                  const includesNeedle = text => textNeedles.some(needle => String(text || '').includes(needle));
                  const removableSelectors = [
                    '.ant-tour',
                    '.ant-tour-mask',
                    '.ant-tour-placeholder',
                    '.ant-tour-target-placeholder',
                    '.ant-popover',
                    '.driver-overlay',
                    '.driver-popover',
                    '.joyride-overlay',
                    '.joyride-tooltip',
                    '[class*="tour"]',
                    '[class*="guide"]'
                  ];
                  let removed = 0;

                  const removeNode = node => {
                    if (node && node.parentElement && node !== document.body && node !== document.documentElement) {
                      node.remove();
                      removed += 1;
                    }
                  };

                  for (const selector of removableSelectors) {
                    for (const node of Array.from(document.querySelectorAll(selector))) {
                      const text = node.innerText || node.textContent || '';
                      const cls = String(node.className || '').toLowerCase();
                      const isMask = /mask|overlay|placeholder/.test(cls);
                      if (isMask || includesNeedle(text) || /tour|driver|joyride|guide/.test(cls)) {
                        removeNode(node);
                      }
                    }
                  }

                  for (const node of Array.from(document.querySelectorAll('body *'))) {
                    if (!(node instanceof HTMLElement) || !includesNeedle(node.innerText || node.textContent)) continue;
                    const shell = node.closest('.ant-tour, .ant-popover, [class*="tour"], [class*="guide"], [class*="driver"], [class*="joyride"]');
                    if (shell) {
                      removeNode(shell);
                    }
                  }

                  document.body.style.pointerEvents = 'auto';
                  document.documentElement.style.pointerEvents = 'auto';
                  return removed;
                }
                """
            )
            if removed:
                kuaishou_logger.info(f"已直接清理快手引导遮罩节点：{removed} 个")
                await page.wait_for_timeout(300)
        except Exception as e:
            kuaishou_logger.debug(f"快手引导遮罩直接清理跳过：{e}")

    async def click_first_visible_text(self, page: Page, texts):
        for text in texts:
            locator = page.get_by_text(text, exact=True)
            count = await locator.count()
            for index in range(count - 1, -1, -1):
                item = locator.nth(index)
                try:
                    if await item.is_visible(timeout=500):
                        await item.click(force=True, timeout=2000)
                        kuaishou_logger.info(f"已点击快手引导按钮：{text}")
                        return True
                except Exception:
                    continue
        return False

    async def click_first_visible_close_button(self, page: Page):
        selectors = [
            ".ant-tour-close",
            ".ant-popover-close",
            ".ant-modal-close",
            "[aria-label='Close']",
            "[aria-label='close']",
            "[class*='close']",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count - 1, -1, -1):
                item = locator.nth(index)
                try:
                    if await item.is_visible(timeout=500):
                        await item.click(force=True, timeout=2000)
                        kuaishou_logger.info(f"已点击快手引导关闭按钮：{selector}")
                        return True
                except Exception:
                    continue
        return False

    async def dismiss_previous_draft_prompt(self, page: Page):
        try:
            body_text = await page.locator("body").inner_text(timeout=2000)
        except Exception:
            return
        if "还有上次未发布的视频" not in body_text:
            return

        if self.save_draft_only:
            raise RuntimeError(
                "快手账号已有未发布草稿。为避免覆盖旧草稿，已停止本次保存"
            )

        kuaishou_logger.info("检测到快手未发布草稿提示，放弃旧草稿后重新上传")
        abandon_button = page.get_by_text("放弃", exact=True).last
        try:
            await abandon_button.click(force=True, timeout=5000)
            await page.wait_for_timeout(1000)
        except Exception as e:
            kuaishou_logger.warning(f"快手旧草稿提示关闭失败，将继续尝试上传：{e}")

    async def save_draft_safely(self, page: Page):
        if not self.save_draft_only:
            raise RuntimeError("只有显式开启快手保存草稿模式才允许执行")
        raise RuntimeError(
            "快手发布页仅提供当前自动化浏览器的“未发布视频”本地缓存，"
            "没有可验证的后台草稿箱回执；为避免假成功，本次不得记录为"
            "平台草稿已保存"
        )

    async def fill_description_and_topics(self, page: Page):
        kuaishou_logger.info("正在填充快手作品描述和话题...")
        editor = page.locator("#work-description-edit").first
        await editor.wait_for(state="visible", timeout=30000)
        editor = await self.write_description_with_retry(page, editor)

        async def get_current_editor():
            current_editor = page.locator("#work-description-edit").first
            await current_editor.wait_for(state="visible", timeout=5000)
            return current_editor

        async def get_tag_nodes():
            current_editor = await get_current_editor()
            return await current_editor.locator(".at-tag-item").evaluate_all(
                r"""els => els.map(el => ({
                    text: (el.innerText || el.textContent || "").replace(/\s+/g, ""),
                    name: el.getAttribute("data-tag-name") || ""
                }))"""
            )

        async def has_tag_node(tag_name: str) -> bool:
            tag_nodes = await get_tag_nodes()
            return any(
                (item.get("name") or item.get("text", "").lstrip("#")) == tag_name
                for item in tag_nodes
            )

        for index, tag in enumerate(self.tags, start=1):
            clean_tag = str(tag).lstrip("#").strip()
            if not clean_tag:
                continue
            kuaishou_logger.info(f"正在添加快手第{index}个话题：#{clean_tag}")
            accepted = False
            for attempt in range(1, 4):
                await self.dismiss_creator_guide(page)
                editor = await get_current_editor()
                try:
                    await editor.click(force=True, timeout=3000)
                    await page.keyboard.press("End")
                    await page.keyboard.press("Space")
                    await page.wait_for_timeout(200)
                    await page.keyboard.press("Shift+Digit3")
                    await page.wait_for_timeout(800)
                    await page.keyboard.type(clean_tag, delay=110)
                    await page.wait_for_timeout(1000)
                    await page.keyboard.press("Space")
                    await page.wait_for_timeout(900)
                except Exception as e:
                    kuaishou_logger.warning(
                        f"快手话题 #{clean_tag} 第{attempt}次输入时编辑框被替换，准备重新定位：{e}"
                    )
                    await page.wait_for_timeout(500)
                    continue
                if await has_tag_node(clean_tag):
                    accepted = True
                    break
                kuaishou_logger.warning(f"快手话题 #{clean_tag} 第{attempt}次未生成节点，准备重试")
                await page.keyboard.press("Control+Z")
                await page.wait_for_timeout(500)
            if not accepted:
                tag_nodes = await get_tag_nodes()
                editor = await get_current_editor()
                text = (await editor.inner_text(timeout=3000)).replace("\xa0", " ")
                raise RuntimeError(f"快手话题节点写入校验失败，缺失：{clean_tag}，当前内容：{text}，节点={tag_nodes}")

        tag_nodes = await get_tag_nodes()
        tag_names = {item.get("name") or item.get("text", "").lstrip("#") for item in tag_nodes}
        missing_tags = [
            tag
            for tag in self.tags
            if str(tag).lstrip("#").strip() not in tag_names
        ]
        if missing_tags:
            editor = await get_current_editor()
            text = (await editor.inner_text(timeout=3000)).replace("\xa0", " ")
            raise RuntimeError(f"快手话题节点写入校验失败，缺失：{missing_tags}，当前内容：{text}，节点={tag_nodes}")
        kuaishou_logger.success("快手作品描述和话题已写入")

    async def write_description_with_retry(self, page: Page, editor):
        expected_title = (self.description if self.description is not None else self.title or "").strip()
        for attempt in range(1, 4):
            await self.dismiss_creator_guide(page)
            editor = page.locator("#work-description-edit").first
            try:
                await editor.wait_for(state="visible", timeout=5000)
                await editor.scroll_into_view_if_needed(timeout=3000)
                await editor.click(force=True, timeout=5000)
                await page.keyboard.press("Control+KeyA")
                await page.keyboard.press("Delete")
                if expected_title:
                    await page.keyboard.insert_text(expected_title)
                await page.wait_for_timeout(400)
            except Exception as e:
                kuaishou_logger.warning(
                    f"快手作品描述第{attempt}次输入时编辑框被替换，准备重新定位：{e}"
                )
                await page.wait_for_timeout(500)
                continue

            text = (await editor.inner_text(timeout=3000)).replace("\xa0", " ").strip()
            if not expected_title or expected_title in text:
                return editor

            kuaishou_logger.warning(
                f"快手作品描述第{attempt}次写入后回读为空或不一致，准备清理引导后重试：{text}"
            )
            await self.remove_creator_guide_overlays(page)
            await page.wait_for_timeout(500)

        editor = page.locator("#work-description-edit").first
        await editor.wait_for(state="visible", timeout=5000)
        text = (await editor.inner_text(timeout=3000)).replace("\xa0", " ").strip()
        raise RuntimeError(f"快手作品描述写入失败，当前内容：{text}")

    async def visible_text_count(self, page: Page, text: str) -> int:
        locator = page.locator(f"text={text}")
        try:
            return await locator.evaluate_all(
                """els => els.filter(el => {
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== "none"
                        && style.visibility !== "hidden"
                        && rect.width > 0
                        && rect.height > 0;
                }).length"""
            )
        except Exception:
            return 0

    async def wait_video_upload_done(self, page: Page):
        started_at = asyncio.get_running_loop().time()
        last_log_second = -1
        while asyncio.get_running_loop().time() - started_at < 180:
            visible_uploading = await self.visible_text_count(page, "上传中")
            body_text = ""
            try:
                body_text = await page.locator("body").inner_text(timeout=2000)
            except Exception:
                pass

            if visible_uploading == 0 and "上传中" not in body_text and "重新上传" in body_text:
                kuaishou_logger.success("快手视频上传完毕")
                return

            elapsed = int(asyncio.get_running_loop().time() - started_at)
            if elapsed // 10 != last_log_second // 10:
                kuaishou_logger.info("快手视频仍在上传或处理封面帧...")
                last_log_second = elapsed
            await page.wait_for_timeout(1000)

        raise RuntimeError("快手视频上传等待超时，无法继续设置封面")

    def choose_thumbnail_for_cover_editor(self):
        if self.thumbnail_paths.get("3:4"):
            return "3:4", self.thumbnail_paths["3:4"]
        if self.thumbnail_paths.get("4:3"):
            return "4:3", self.thumbnail_paths["4:3"]
        return None, self.thumbnail_path

    async def get_cover_modal(self, page: Page):
        modal = page.locator(".ant-modal-content").filter(has_text="封面截取").filter(has_text="上传封面").last
        await modal.wait_for(state="visible", timeout=10000)
        return modal

    async def select_cover_ratio(self, page: Page, modal, ratio: str | None):
        if ratio not in {"3:4", "4:3"}:
            return

        kuaishou_logger.info(f"快手封面裁剪比例选择：{ratio}")
        ratio_item = modal.locator('div[class*="ratio-item"]').filter(has_text=ratio).first
        await ratio_item.wait_for(state="visible", timeout=5000)
        await ratio_item.click(force=True, timeout=5000)
        await page.wait_for_timeout(800)

    async def get_cover_modal_signature(self, modal):
        return await modal.evaluate(
            """
            node => {
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
              const files = elements
                .filter(item => item.tagName === 'INPUT' && item.type === 'file')
                .flatMap(item => Array.from(item.files || []).map(file => `${file.name}:${file.size}`));
              return {images, backgrounds, canvases, files};
            }
            """
        )

    @staticmethod
    def cover_modal_signature_key(signature):
        signature = signature or {}
        return repr(
            (
                list(signature.get("images") or []),
                list(signature.get("backgrounds") or []),
                list(signature.get("canvases") or []),
                list(signature.get("files") or []),
            )
        )

    async def wait_cover_image_ready(self, page: Page, modal, previous_signature):
        for _ in range(50):
            try:
                modal_text = await modal.inner_text(timeout=2000)
            except Exception:
                modal_text = ""
            try:
                current_signature = await self.get_cover_modal_signature(modal)
            except Exception:
                current_signature = {}
            signature_changed = self.cover_modal_signature_key(
                current_signature
            ) != self.cover_modal_signature_key(previous_signature)
            file_received = bool((current_signature or {}).get("files"))
            confirm_button = modal.locator("button").filter(has_text="确认").last
            try:
                confirm_ready = bool(
                    await confirm_button.count()
                    and await confirm_button.is_visible(timeout=500)
                    and not await confirm_button.evaluate(
                        """el => {
                            const button = el.closest('button') || el;
                            return Boolean(
                                button.disabled ||
                                button.getAttribute('aria-disabled') === 'true' ||
                                String(button.className || '').includes('disabled')
                            );
                        }"""
                    )
                )
            except Exception:
                confirm_ready = False
            is_processing = any(text in modal_text for text in ("上传中", "加载中", "处理中"))
            ready_marker = any(text in modal_text for text in ("清空上传", "重新上传", "更换图片"))
            has_upload_evidence = signature_changed or file_received or ready_marker
            if not is_processing and has_upload_evidence and confirm_ready:
                kuaishou_logger.info("快手封面上传状态和确认按钮已回读通过")
                return
            await page.wait_for_timeout(500)
        raise RuntimeError("快手封面图片上传后未进入可确认状态")

    async def wait_cover_confirm_ready(self, page: Page, confirm_button):
        for _ in range(60):
            try:
                visible = await confirm_button.is_visible(timeout=500)
                disabled = await confirm_button.evaluate(
                    """el => {
                        const button = el.closest('button') || el;
                        return Boolean(
                            button.disabled ||
                            button.getAttribute('aria-disabled') === 'true' ||
                            button.className.includes('disabled')
                        );
                    }"""
                )
                if visible and not disabled:
                    return
            except Exception:
                pass
            await page.wait_for_timeout(500)
        raise RuntimeError("快手封面确认按钮长时间不可用")

    async def wait_cover_modal_closed(self, page: Page, modal):
        for _ in range(90):
            try:
                if not await modal.is_visible(timeout=500):
                    await page.wait_for_timeout(1200)
                    return
            except Exception:
                await page.wait_for_timeout(1200)
                return
            await page.wait_for_timeout(500)
        raise RuntimeError("快手封面确认后弹窗未关闭，封面可能仍在上传或裁剪处理中")

    async def click_upload_cover_tab(self, page: Page, modal):
        upload_tab = modal.locator('[role="tab"]').filter(has_text="上传封面").first
        if not await upload_tab.count():
            upload_tab = modal.get_by_text("上传封面", exact=True).last
        await upload_tab.click(force=True, timeout=5000)
        await page.wait_for_timeout(1000)

    async def set_thumbnail_from_cover_dialog(self, page: Page):
        target_ratio, thumbnail_path = self.choose_thumbnail_for_cover_editor()
        if not thumbnail_path or not os.path.exists(thumbnail_path):
            kuaishou_logger.warning(f"快手封面文件不存在，已跳过：{thumbnail_path}")
            return

        kuaishou_logger.info("正在设置快手封面...")
        opener = page.locator('div[class*="cover-full-editor"]').first
        await opener.wait_for(state="visible", timeout=30000)
        await opener.scroll_into_view_if_needed()
        await opener.click(force=True, timeout=5000)

        modal = await self.get_cover_modal(page)
        await self.click_upload_cover_tab(page, modal)
        await self.select_cover_ratio(page, modal, target_ratio)
        started_at = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - started_at < 20:
            modal_text = await modal.inner_text(timeout=2000)
            if "加载中" not in modal_text:
                break
            await page.wait_for_timeout(500)

        previous_signature = await self.get_cover_modal_signature(modal)
        image_input = modal.locator('input[type="file"][accept*="image"]').last
        if not await image_input.count():
            image_input = page.locator('input[type="file"][accept*="image"]').last
        await image_input.wait_for(state="attached", timeout=10000)
        await image_input.set_input_files(thumbnail_path)
        kuaishou_logger.info(f"快手封面已按 {target_ratio or '当前'} 比例上传：{thumbnail_path}")

        await self.wait_cover_image_ready(page, modal, previous_signature)
        await page.wait_for_timeout(3000)

        confirm_button = modal.locator("button").filter(has_text="确认").last
        if not await confirm_button.count():
            confirm_button = modal.get_by_text("确认", exact=True).last
        for attempt in range(1, 4):
            await self.wait_cover_confirm_ready(page, confirm_button)
            await confirm_button.click(force=True, timeout=5000)

            for _ in range(36):
                body_text = await page.locator("body").inner_text(timeout=2000)
                try:
                    modal_visible = await modal.is_visible(timeout=500)
                except Exception:
                    modal_visible = False
                if "请上传图片" in body_text:
                    raise RuntimeError("快手封面上传失败：平台提示请上传图片")
                if not modal_visible:
                    await self.wait_cover_modal_closed(page, modal)
                    kuaishou_logger.success("快手封面弹窗已关闭，封面应用完成")
                    return
                await page.wait_for_timeout(500)

            kuaishou_logger.warning(f"快手封面确认后弹窗未关闭，重试确认 {attempt}/3")

        raise RuntimeError("快手封面确认后未检测到应用成功")

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def set_schedule_time(self, page, publish_date):
        target = publish_date.strftime("%Y-%m-%d %H:%M:%S")
        kuaishou_logger.info(f"正在设置快手定时发布时间：{target}")
        schedule_label = page.get_by_text(
            "定时发布",
            exact=True,
        ).last.locator("xpath=ancestor::label[1]")
        await schedule_label.wait_for(state="visible", timeout=10000)
        await schedule_label.click(force=True, timeout=5000)
        await page.wait_for_timeout(500)

        schedule_radio = schedule_label.locator('input[type="radio"]')
        if not await schedule_radio.is_checked(timeout=3000):
            raise RuntimeError("快手定时发布选项点击后未保持选中")

        date_input = page.locator(
            'div.ant-picker-input input[placeholder="选择日期时间"]'
        ).last
        await date_input.wait_for(state="visible", timeout=10000)
        await self._apply_schedule_picker_selection(
            page,
            date_input,
            publish_date,
        )

        actual = (await date_input.input_value(timeout=3000)).strip()
        if actual != target:
            raise RuntimeError(
                f"快手定时发布时间写入失败，目标={target}，实际={actual}"
            )

        selected_date, selected_time = await self._read_schedule_picker_selection(
            page,
            date_input,
        )
        expected_date = publish_date.strftime("%Y-%m-%d")
        expected_time = publish_date.strftime("%H:%M:%S")
        if selected_date != expected_date or selected_time != expected_time:
            raise RuntimeError(
                "快手定时面板回读失败，"
                f"目标={expected_date} {expected_time}，"
                f"实际={selected_date or '未选择'} {selected_time or '未选择'}"
            )
        if not await schedule_radio.is_checked(timeout=3000):
            raise RuntimeError("快手定时发布时间设置后，定时发布选项未保持选中")
        kuaishou_logger.success(f"快手定时发布时间已确认：{target}")

    async def _apply_schedule_picker_selection(
        self,
        page: Page,
        date_input,
        publish_date: datetime,
    ):
        await date_input.click(force=True, timeout=5000)
        panel = page.locator(".ant-picker-panel-container:visible").last
        await panel.wait_for(state="visible", timeout=10000)

        target_date = publish_date.strftime("%Y-%m-%d")
        target_month = (publish_date.year, publish_date.month)
        for _ in range(24):
            date_cell = panel.locator(
                f'.ant-picker-cell[title="{target_date}"]'
                ":not(.ant-picker-cell-disabled)"
            ).last
            if await date_cell.count():
                await date_cell.click(force=True, timeout=5000)
                break

            year_text = await panel.locator(
                ".ant-picker-year-btn"
            ).last.inner_text(timeout=3000)
            month_text = await panel.locator(
                ".ant-picker-month-btn"
            ).last.inner_text(timeout=3000)
            current_month = (
                int(re.sub(r"\D", "", year_text)),
                int(re.sub(r"\D", "", month_text)),
            )
            selector = (
                ".ant-picker-header-next-btn"
                if current_month < target_month
                else ".ant-picker-header-prev-btn"
            )
            await panel.locator(selector).last.click(
                force=True,
                timeout=5000,
            )
            await page.wait_for_timeout(150)
        else:
            raise RuntimeError(f"快手定时面板中未找到可选日期：{target_date}")

        columns = panel.locator(".ant-picker-time-panel-column")
        if await columns.count() != 3:
            raise RuntimeError("快手定时面板的时分秒选择列数量异常")
        for index, value in enumerate(
            (
                publish_date.strftime("%H"),
                publish_date.strftime("%M"),
                publish_date.strftime("%S"),
            )
        ):
            option = columns.nth(index).locator(
                ".ant-picker-time-panel-cell"
                ":not(.ant-picker-time-panel-cell-disabled) "
                ".ant-picker-time-panel-cell-inner"
            ).get_by_text(value, exact=True).last
            if not await option.count():
                raise RuntimeError(
                    f"快手定时面板中无法选择时间值：{value}"
                )
            await option.scroll_into_view_if_needed(timeout=5000)
            await option.click(force=True, timeout=5000)

        confirm_button = panel.get_by_role(
            "button",
            name="确定",
            exact=True,
        )
        await confirm_button.click(timeout=5000)
        await panel.wait_for(state="hidden", timeout=10000)
        await page.wait_for_timeout(500)

    async def _read_schedule_picker_selection(self, page: Page, date_input):
        await date_input.click(force=True, timeout=5000)
        panel = page.locator(".ant-picker-panel-container:visible").last
        await panel.wait_for(state="visible", timeout=10000)

        selected_date = ""
        selected_cell = panel.locator(".ant-picker-cell-selected").last
        if await selected_cell.count():
            selected_date = str(
                await selected_cell.get_attribute("title") or ""
            ).strip()
        selected_parts = await panel.locator(
            ".ant-picker-time-panel-cell-selected "
            ".ant-picker-time-panel-cell-inner"
        ).all_inner_texts()
        selected_time = (
            ":".join(part.strip().zfill(2) for part in selected_parts)
            if len(selected_parts) == 3
            else ""
        )
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        return selected_date, selected_time
