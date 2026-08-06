# -*- coding: utf-8 -*-
from datetime import datetime

from playwright.async_api import Error as PlaywrightError, Playwright, TimeoutError as PlaywrightTimeoutError, async_playwright
import os
import asyncio
import re

from conf import LOCAL_CHROME_PATH
from utils.base_social_media import (
    set_init_script,
    launch_chromium_with_codecs,
    launch_publish_browser,
    new_publish_context,
    goto_and_reveal,
    keep_browser_open_for_dry_run,
    save_context_storage_state,
)
from utils.files_times import get_absolute_path
from utils.log import TENCENT_SCREENSHOT_DIR, tencent_logger
from utils.platform_draft import (
    find_unique_draft_control,
    page_body_text,
    record_draft_field_warning,
    wait_for_draft_success,
)
from utils.publish_limits import normalize_publish_tags


TENCENT_SHORT_TITLE_MAX_LENGTH = 10


def format_str_for_short_title(origin_title: str) -> str:
    # 定义允许的特殊字符
    allowed_special_chars = "《》“”:+?%°"

    # 移除不允许的特殊字符
    filtered_chars = [char if char.isalnum() or char in allowed_special_chars else ' ' if char == ',' else '' for
                      char in origin_title]
    formatted_string = ''.join(filtered_chars)

    # 调整字符串长度
    if len(formatted_string) > TENCENT_SHORT_TITLE_MAX_LENGTH:
        # 截断字符串
        formatted_string = formatted_string[:TENCENT_SHORT_TITLE_MAX_LENGTH]

    return formatted_string


async def cookie_auth(account_file):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://channels.weixin.qq.com/platform/post/create")
        try:
            await page.wait_for_selector('div.title-name:has-text("微信小店")', timeout=5000)  # 等待5秒
            tencent_logger.error("[+] 等待5秒 cookie 失效")
            return False
        except:
            tencent_logger.success("[+] cookie 有效")
            return True


async def get_tencent_cookie(account_file):
    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(playwright, headless=False, executable_path=None)
        # Setup context however you like.
        context = await browser.new_context(
            permissions=[],  # 禁用所有权限请求
            geolocation=None,  # 禁用地理位置
            locale='zh-CN',  # 设置语言为中文
            timezone_id='Asia/Shanghai'  # 设置时区
        )  # Pass any options
        # Pause the page, and start recording manually.
        context = await set_init_script(context)
        page = await context.new_page()
        await page.goto("https://channels.weixin.qq.com")
        await page.pause()
        # 点击调试器的继续，保存cookie
        await save_context_storage_state(context, account_file, include_indexed_db=True)


async def weixin_setup(account_file, handle=False):
    account_file = get_absolute_path(account_file, "tencent_uploader")
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            # Todo alert message
            return False
        tencent_logger.info('[+] cookie文件不存在或已失效，即将自动打开浏览器，请扫码登录，登陆后会自动生成cookie文件')
        await get_tencent_cookie(account_file)
    return True


class TencentVideo(object):
    def __init__(self, title, file_path, tags, publish_date: datetime, account_file, category=None, thumbnail_path=None, thumbnail_paths=None, dry_run=False, dry_run_hold_browser=True, save_draft_only=False, description=None):
        self.title = title  # 视频标题
        self.file_path = file_path
        self.tags = normalize_publish_tags(tags)
        self.publish_date = publish_date
        self.account_file = account_file
        self.category = category
        self.thumbnail_path = thumbnail_path
        self.thumbnail_paths = dict(thumbnail_paths or {})
        if thumbnail_path and "4:3" not in self.thumbnail_paths:
            self.thumbnail_paths["4:3"] = thumbnail_path
        self.dry_run = dry_run
        self.dry_run_hold_browser = dry_run_hold_browser
        self.save_draft_only = bool(save_draft_only)
        self.description = description
        self.local_executable_path = LOCAL_CHROME_PATH
        if self.save_draft_only and self.dry_run:
            raise ValueError("视频号保存草稿模式与预发布检查不能同时开启")

    async def set_schedule_time_tencent(self, page, publish_date):
        label_element = page.locator("label").filter(has_text="定时").nth(1)
        await label_element.click()
        await page.wait_for_timeout(500)

        date_input = page.locator('input[placeholder="请选择发表时间"]').first
        await date_input.click()

        str_month = str(publish_date.month) if publish_date.month > 9 else "0" + str(publish_date.month)
        current_month = str_month + "月"
        # 获取当前的月份
        page_month = await page.inner_text('span.weui-desktop-picker__panel__label:has-text("月")')

        # 检查当前月份是否与目标月份相同
        if page_month != current_month:
            await page.click('button.weui-desktop-btn__icon__right')

        # 获取页面元素
        elements = await page.query_selector_all('table.weui-desktop-picker__table a')

        # 遍历元素并点击匹配的元素
        for element in elements:
            if 'weui-desktop-picker__disabled' in await element.evaluate('el => el.className'):
                continue
            text = await element.inner_text()
            if text.strip() == str(publish_date.day):
                await element.click()
                break

        # 输入完整时间，不能只写小时，否则随机分钟会被平台重置到整点。
        time_input = page.locator('input[placeholder="请选择时间"]').first
        await time_input.click()
        await page.keyboard.press("Control+KeyA")
        await time_input.fill(publish_date.strftime("%H:%M"))
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(500)

        # 选择标题栏（令定时时间生效）
        await page.locator("div.input-editor").click()
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

        actual_date = (await date_input.input_value()).strip()
        actual_time = (await time_input.input_value()).strip()
        expected_date = publish_date.strftime("%Y-%m-%d")
        expected_time = publish_date.strftime("%H:%M")
        normalized_date = re.sub(r"\D", "", actual_date)
        if publish_date.strftime("%Y%m%d") not in normalized_date:
            raise RuntimeError(
                f"视频号定时日期回读失败，目标={expected_date}，实际={actual_date}"
            )
        if actual_time != expected_time:
            raise RuntimeError(
                f"视频号定时时间回读失败，目标={expected_time}，实际={actual_time}"
            )
        tencent_logger.success(
            f"视频号定时发布时间已设置并回读：{expected_date} {expected_time}"
        )

    async def handle_upload_error(self, page):
        tencent_logger.info("视频出错了，重新上传中")
        await page.locator('div.media-status-content div.tag-inner:has-text("删除")').click()
        await page.get_by_role('button', name="删除", exact=True).click()
        file_input = page.locator('input[type="file"]')
        await file_input.set_input_files(self.file_path)

    async def ensure_video_upload_input(self, page):
        selector = 'input[type="file"][accept*="video"], input[type="file"]'
        file_input = page.locator(selector).first
        try:
            await file_input.wait_for(state="attached", timeout=5000)
            return file_input
        except PlaywrightTimeoutError:
            pass

        if await self.video_publish_form_is_loaded(page):
            await file_input.wait_for(state="attached", timeout=30000)
            tencent_logger.info("视频号发布表单已加载，上传控件已就绪")
            return file_input

        # 视频号在无窗口模式下直接访问 /post/create 会先回到工作台，
        # 必须从工作台的“发表视频”入口进入，之后才会挂载上传控件。
        entry = page.get_by_role("button", name="发表视频", exact=True).last
        try:
            await entry.click(timeout=10000)
        except Exception:
            entry = page.get_by_text("发表视频", exact=True).last
            await entry.click(timeout=10000)

        try:
            await page.wait_for_url("**/platform/post/create**", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        file_input = page.locator(selector).first
        await file_input.wait_for(state="attached", timeout=30000)
        tencent_logger.info("视频号已从工作台进入发表视频页面")
        return file_input

    async def video_publish_form_is_loaded(self, page):
        current_url = str(getattr(page, "url", "") or "")
        if "/platform/post/create" not in current_url:
            return False
        body_text = await page_body_text(page)
        form_markers = (
            "视频描述",
            "短标题",
            "添加到合集",
            "上传时长",
        )
        return sum(marker in body_text for marker in form_markers) >= 2

    async def upload(self, playwright: Playwright) -> None:
        page = getattr(self, "external_page", None)
        context = getattr(self, "external_context", None)
        browser = getattr(self, "external_browser", None)
        managed_browser = page is None
        if managed_browser:
            # 使用系统浏览器优先，避免 H.264 错误
            browser = await launch_publish_browser(playwright, executable_path=self.local_executable_path)
        try:
            if managed_browser:
                # 创建一个浏览器上下文，使用指定的 cookie 文件
                context = await new_publish_context(
                    browser,
                    storage_state=f"{self.account_file}",
                )
                context = await set_init_script(context)

                # 创建一个新的页面
                page = await context.new_page()
            # 访问指定的 URL
            await goto_and_reveal(page, "https://channels.weixin.qq.com/platform/post/create")
            tencent_logger.info(f'[+]正在上传-------{self.title}.mp4')
            file_input = await self.ensure_video_upload_input(page)
            await file_input.set_input_files(self.file_path)
            tencent_logger.info("视频号视频上传中...")

            # 填充标题和话题
            await self.add_title_tags(page)
            # 添加短标题
            await self.add_short_title(page)

            await self.set_thumbnail_if_needed(page)

            # 添加商品
            # await self.add_product(page)
            # 合集功能
            await self.add_collection(page)
            # 原创选择
            if getattr(self, "original_declaration", False):
                await self.add_original(page)
            await self.set_ai_generated_declaration(page)
            if self.publish_date != 0 and not self.save_draft_only:
                await self.set_schedule_time_tencent(page, self.publish_date)
            # 检测上传状态和表单就绪状态
            await self.detect_upload_status(page)

            if self.save_draft_only:
                result = await self.save_draft_safely(page)
                await save_context_storage_state(
                    context,
                    self.account_file,
                    include_indexed_db=True,
                )
                tencent_logger.success(
                    f"视频号草稿已通过平台回执确认：{result['evidence']}"
                )
                return

            if self.dry_run:
                if managed_browser:
                    await keep_browser_open_for_dry_run(
                        page,
                        context,
                        browser,
                        account_file=self.account_file,
                        logger=tencent_logger,
                        platform_name="视频号",
                        block_until_close=self.dry_run_hold_browser,
                        include_indexed_db=True,
                    )
                return

            await self.click_publish(page)

            await save_context_storage_state(
                context,
                self.account_file,
                include_indexed_db=True,
            )
            tencent_logger.success('  [-]视频号登录态已刷新！')
            await asyncio.sleep(2)  # 这里延迟是为了方便眼睛直观的观看
        finally:
            # 关闭浏览器上下文和浏览器实例
            try:
                if managed_browser and 'context' in locals():
                    try:
                        await context.close()
                    except Exception:
                        pass
            finally:
                try:
                    if managed_browser:
                        await browser.close()
                except Exception:
                    pass

    async def save_draft_safely(self, page):
        if not self.save_draft_only:
            raise RuntimeError("只有显式开启视频号保存草稿模式才允许执行")
        control, control_name = await find_unique_draft_control(
            page,
            ("保存草稿", "存草稿", "暂存"),
        )
        if control is None:
            raise RuntimeError("视频号当前页面未提供可验证的保存草稿入口")

        success_texts = (
            "已保存",
            "草稿已保存",
            "已保存到草稿",
            "已保存至草稿",
            "已保存为草稿",
            "保存草稿成功",
            "保存成功",
        )
        baseline_text = await page_body_text(page)
        baseline_visible_markers = set(
            await self.visible_draft_success_markers(page, success_texts)
        )
        try:
            control_enabled = await control.is_enabled(timeout=1000)
        except Exception:
            control_enabled = True
        if control_enabled is False:
            if "定时" in baseline_text and (
                "无法保存草稿" in baseline_text or "取消定时" in baseline_text
            ):
                raise RuntimeError(
                    "视频号设置定时发布后无法保存草稿，请取消定时发布后重试"
                )
            raise RuntimeError("视频号保存草稿按钮当前不可用，请检查页面必填项")

        baseline_url = page.url
        await control.scroll_into_view_if_needed(timeout=5000)
        await control.click(timeout=10000)

        for _ in range(40):
            visible_markers = await self.visible_draft_success_markers(
                page,
                success_texts,
            )
            new_visible_markers = [
                marker
                for marker in visible_markers
                if marker not in baseline_visible_markers
            ]
            if new_visible_markers:
                return {
                    "status": "draft_saved",
                    "control": control_name,
                    "evidence": new_visible_markers,
                }

            evidence = await wait_for_draft_success(
                page,
                success_texts=success_texts,
                baseline_text=baseline_text,
                attempts=1,
                interval_seconds=0.25,
            )
            if evidence:
                return {
                    "status": "draft_saved",
                    "control": control_name,
                    "evidence": evidence,
                }

            current_url = page.url
            if current_url != baseline_url and "/post/create" not in current_url:
                return {
                    "status": "draft_saved",
                    "control": control_name,
                    "evidence": [f"已离开编辑页：{current_url}"],
                }

            current_text = await page_body_text(page)
            title_marker = self.title[:10].strip()
            if (
                title_marker
                and title_marker in current_text
                and "草稿" in current_text
                and "保存草稿" not in current_text
            ):
                return {
                    "status": "draft_saved",
                    "control": control_name,
                    "evidence": ["草稿列表已回读当前标题"],
                }

        if await self.verify_saved_draft_from_draft_box(page):
            return {
                "status": "draft_saved",
                "control": control_name,
                "evidence": ["视频号草稿箱已回读当前标题"],
            }

        screenshot_path = os.path.join(
            TENCENT_SCREENSHOT_DIR,
            "tencent_draft_unverified_"
            f"{datetime.now():%Y%m%d_%H%M%S_%f}.png",
        )
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            evidence_note = f"，截图：{screenshot_path}"
        except Exception:
            evidence_note = ""
        raise RuntimeError(
            f"视频号点击“{control_name}”后未返回草稿保存成功回执，"
            f"页面也未离开编辑页{evidence_note}"
        )

    def text_roots(self, page):
        try:
            frames = list(page.frames)
        except Exception:
            frames = []
        roots = [*frames, page]
        unique_roots = []
        seen_ids = set()
        for root in roots:
            root_id = id(root)
            if root_id in seen_ids:
                continue
            seen_ids.add(root_id)
            unique_roots.append(root)
        return unique_roots

    async def visible_draft_success_markers(self, page, markers):
        """读取主页面和内嵌发布页中的可见草稿保存提示。"""
        visible_markers = []
        for marker in markers:
            found = False
            for root in self.text_roots(page):
                try:
                    locator = root.get_by_text(marker, exact=True)
                    for index in range(await locator.count()):
                        if await locator.nth(index).is_visible(timeout=150):
                            visible_markers.append(marker)
                            found = True
                            break
                except Exception:
                    continue
                if found:
                    break
        return visible_markers

    async def all_page_body_text(self, page):
        body_texts = []
        for root in self.text_roots(page):
            try:
                body_text = await root.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
            except Exception:
                body_text = ""
            if body_text:
                body_texts.append(body_text)
        return "\n".join(body_texts)

    def draft_title_markers(self):
        markers = [
            str(self.title or "").strip(),
            format_str_for_short_title(str(self.title or "")).strip(),
            str(self.title or "")[:10].strip(),
        ]
        return [marker for marker in dict.fromkeys(markers) if len(marker) >= 4]

    async def verify_saved_draft_from_draft_box(self, page):
        """用同一登录会话只读回查草稿箱，补足视频号缺失的保存回执。"""
        verify_page = None
        try:
            verify_page = await page.context.new_page()
            await verify_page.goto(
                "https://channels.weixin.qq.com/platform/post/list",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await verify_page.wait_for_timeout(1000)

            draft_entry_clicked = False
            for root in self.text_roots(verify_page):
                draft_entries = root.get_by_text("草稿箱", exact=True)
                for index in range(await draft_entries.count()):
                    entry = draft_entries.nth(index)
                    try:
                        if not await entry.is_visible(timeout=500):
                            continue
                        await entry.click(timeout=5000)
                        draft_entry_clicked = True
                        break
                    except Exception:
                        continue
                if draft_entry_clicked:
                    break
            if not draft_entry_clicked:
                tencent_logger.warning("视频号草稿回查未找到草稿箱入口")
                return False

            markers = self.draft_title_markers()
            for _ in range(16):
                body_text = await self.all_page_body_text(verify_page)
                if any(marker in body_text for marker in markers):
                    tencent_logger.success("视频号草稿箱已回读当前标题")
                    return True
                await verify_page.wait_for_timeout(500)
            tencent_logger.warning("视频号草稿箱未回读到当前标题")
            return False
        except Exception as error:
            tencent_logger.warning(f"视频号草稿箱回查失败：{error}")
            return False
        finally:
            if verify_page is not None:
                try:
                    await verify_page.close()
                except Exception:
                    pass

    async def set_thumbnail_if_needed(self, page):
        cover_targets = [
            (ratio, self.thumbnail_paths.get(ratio))
            for ratio in ("3:4", "4:3")
            if self.thumbnail_paths.get(ratio)
        ]
        if not cover_targets:
            tencent_logger.info("未设置视频号封面，跳过封面编辑")
            return

        if await self.uses_combined_cover_card(page):
            combined_path = (
                self.thumbnail_paths.get("3:4")
                or self.thumbnail_paths.get("4:3")
            )
            cover_targets = [("3:4", combined_path)]
            tencent_logger.info(
                "视频号当前页面使用“个人主页和分享卡片(3:4)”合并封面，"
                "本次仅设置平台实际支持的 3:4 封面"
            )

        tencent_logger.info("等待封面生成完成...")
        try:
            await page.wait_for_selector('.gen-text:has-text("生成中")', state='hidden', timeout=30000)
            tencent_logger.info("封面生成完成")
        except PlaywrightTimeoutError:
            tencent_logger.info("未检测到生成中文本，继续等待")

        await self.wait_for_cover_options_ready(page, [ratio for ratio, _ in cover_targets])

        for ratio, thumbnail_path in cover_targets:
            await self.set_one_thumbnail(page, ratio, thumbnail_path)

    async def uses_combined_cover_card(self, page):
        body_text = (await self.all_page_body_text(page)).replace(" ", "")
        return any(
            marker in body_text
            for marker in (
                "个人主页和分享卡片(3:4)",
                "个人主页和分享卡片（3:4）",
            )
        )

    async def wait_for_cover_options_ready(self, page, ratios, timeout=12000):
        started_at = asyncio.get_running_loop().time()
        pending = set(ratios)
        while pending and asyncio.get_running_loop().time() - started_at < timeout / 1000:
            for ratio in list(pending):
                if await self.find_cover_edit_button(page, ratio):
                    pending.remove(ratio)
            if pending:
                await asyncio.sleep(0.2)
        if pending:
            tencent_logger.warning(f"视频号封面入口等待超时，继续尝试处理：{sorted(pending)}")

    async def set_one_thumbnail(self, page, ratio, thumbnail_path):
        started_at = asyncio.get_running_loop().time()
        entry_state = None
        last_error = None
        for attempt in range(1, 6):
            edit_button = await self.find_cover_edit_button(page, ratio)
            if not edit_button:
                last_error = f"未检测到视频号{ratio}封面编辑入口"
                await asyncio.sleep(0.4)
                continue

            try:
                await self.click_cover_edit_button(edit_button, ratio)
            except Exception as e:
                last_error = str(e)
                await asyncio.sleep(0.4)
                continue

            entry_state = await self.wait_for_cover_entry_state(page, ratio, timeout=2500)
            if entry_state == "direct_edit":
                if await self.click_cover_direct_edit_if_needed(page):
                    entry_state = await self.wait_for_cover_entry_state(page, ratio, timeout=5000)

            if not entry_state:
                try:
                    await edit_button.click(force=True, timeout=5000)
                    tencent_logger.info(f"视频号{ratio}DOM事件无响应，已补充可信点击")
                    entry_state = await self.wait_for_cover_entry_state(page, ratio, timeout=4000)
                except Exception as e:
                    last_error = str(e)

            if entry_state == "dialog":
                break
            tencent_logger.warning(f"视频号{ratio}第 {attempt} 次点击编辑后未出现裁剪弹窗，准备重试")
            await asyncio.sleep(0.5)

        if entry_state != "dialog" and not await self.wait_for_cover_dialog(page, ratio, timeout=3000):
            current_title = await self.get_visible_cover_dialog_title(page)
            evidence_note = await self.capture_cover_failure(page, ratio)
            raise RuntimeError(
                f"点击视频号{ratio}封面编辑后打开的弹窗不匹配："
                f"{current_title or last_error or '未检测到封面弹窗'}{evidence_note}"
            )

        thumbnail_input = await self.find_cover_upload_input(page, ratio)
        if not thumbnail_input:
            raise RuntimeError(f"未检测到视频号{ratio}封面上传入口")
        await thumbnail_input.set_input_files(thumbnail_path)
        if not await self.click_cover_confirm(page, ratio):
            raise RuntimeError(f"视频号{ratio}封面上传后无法确认")
        elapsed = asyncio.get_running_loop().time() - started_at
        tencent_logger.info(f"已设置视频号{ratio}封面，耗时 {elapsed:.1f} 秒")
        await page.wait_for_timeout(500)

    async def click_cover_edit_button(self, edit_button, ratio):
        try:
            await edit_button.wait_for(state='visible', timeout=5000)
            await edit_button.evaluate(
                """element => {
                    element.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
                    element.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                    element.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                    element.click();
                }"""
            )
            tencent_logger.info(f"DOM事件点击{ratio}编辑封面弹窗成功")
        except Exception as e:
            tencent_logger.warning(f"[tencent]DOM事件点击{ratio}编辑封面失败: {e}, 尝试强制点击")
            try:
                await edit_button.click(force=True, timeout=5000)
                tencent_logger.info(f"强制点击{ratio}编辑封面弹窗成功")
            except Exception as e2:
                raise RuntimeError(f"点击视频号{ratio}封面编辑失败：{e2}") from e2

    def get_cover_roots(self, page):
        content_frames = [
            frame for frame in page.frames
            if "channels.weixin.qq.com/micro/content/post/create" in frame.url
        ]
        other_frames = [frame for frame in page.frames if frame not in content_frames]
        return [*content_frames, *other_frames, page]

    def get_cover_meta(self, ratio):
        if ratio == "3:4":
            return {
                "wrap_class": "vertical-cover-wrap",
                "card_title": "个人主页卡片",
                "dialog_title": "编辑个人主页卡片",
            }
        if ratio == "4:3":
            return {
                "wrap_class": "horizon-cover-wrap",
                "card_title": "分享卡片",
                "dialog_title": "编辑分享卡片",
            }
        return {
            "wrap_class": "",
            "card_title": "",
            "dialog_title": "",
        }

    def get_cover_dialog_selectors(self, dialog_title):
        return [
            f'.weui-desktop-dialog:has-text("{dialog_title}")',
            f'.finder-common-dialog:has-text("{dialog_title}")',
            f'.edit-cover-dialog:has-text("{dialog_title}")',
        ]

    def get_generic_cover_dialog_selectors(self):
        return [
            ".weui-desktop-dialog",
            ".finder-common-dialog",
            ".edit-cover-dialog",
            '[role="dialog"]',
        ]

    async def find_visible_cover_dialog(self, page, dialog_title=None):
        strict_titles = (
            [dialog_title]
            if dialog_title
            else ["编辑个人主页卡片", "编辑分享卡片"]
        )
        for root in self.get_cover_roots(page):
            for title in strict_titles:
                for selector in self.get_cover_dialog_selectors(title):
                    dialogs = root.locator(selector)
                    try:
                        for index in range(await dialogs.count() - 1, -1, -1):
                            dialog = dialogs.nth(index)
                            if await dialog.is_visible():
                                return dialog
                    except PlaywrightError:
                        continue

            # 视频号会灰度更新封面弹窗标题。标题变化时，仅接受带图片上传控件，
            # 或同时包含“封面”和编辑动作的可见弹窗，避免误认其他提示框。
            for selector in self.get_generic_cover_dialog_selectors():
                dialogs = root.locator(selector)
                try:
                    for index in range(await dialogs.count() - 1, -1, -1):
                        dialog = dialogs.nth(index)
                        if not await dialog.is_visible():
                            continue
                        image_inputs = dialog.locator(
                            'input[type="file"][accept*="image"]'
                        )
                        if await image_inputs.count():
                            return dialog
                        text = " ".join(
                            (await dialog.inner_text(timeout=1000)).split()
                        )
                        if "封面" in text and any(
                            marker in text
                            for marker in ("编辑", "裁剪", "上传", "更换", "选择图片")
                        ):
                            return dialog
                except PlaywrightError:
                    continue
        return None

    async def wait_for_cover_dialog(self, page, ratio, timeout=8000):
        dialog_title = self.get_cover_meta(ratio)["dialog_title"]
        if not dialog_title:
            return False
        started_at = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - started_at < timeout / 1000:
            if await self.is_cover_dialog_visible(page, dialog_title):
                return True
            await asyncio.sleep(0.15)
        return False

    async def wait_for_cover_entry_state(self, page, ratio, timeout=2500):
        dialog_title = self.get_cover_meta(ratio)["dialog_title"]
        started_at = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - started_at < timeout / 1000:
            if await self.is_cover_dialog_visible(page, dialog_title):
                return "dialog"
            if await self.is_cover_direct_edit_visible(page):
                return "direct_edit"
            await asyncio.sleep(0.15)
        return None

    async def is_cover_dialog_visible(self, page, dialog_title=None):
        return await self.find_visible_cover_dialog(page, dialog_title) is not None

    async def wait_for_cover_dialog_closed(self, page, dialog_title, timeout=12000):
        started_at = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - started_at < timeout / 1000:
            if not await self.is_cover_dialog_visible(page, dialog_title):
                return True
            await asyncio.sleep(0.2)
        return False

    async def get_visible_cover_dialog_title(self, page):
        dialog = await self.find_visible_cover_dialog(page)
        if dialog is None:
            return None
        try:
            text = " ".join((await dialog.inner_text(timeout=1000)).split())
            return text[:120] or "已检测到无标题封面弹窗"
        except PlaywrightError:
            return "已检测到封面弹窗"

    async def find_cover_upload_input(self, page, ratio):
        dialog_title = self.get_cover_meta(ratio)["dialog_title"]
        dialog = await self.find_visible_cover_dialog(page, dialog_title)
        if dialog is not None:
            try:
                file_input = dialog.locator(
                    'input[type="file"][accept*="image"]'
                ).last
                if await file_input.count():
                    return file_input
            except PlaywrightError:
                pass
        for root in self.get_cover_roots(page):
            file_input = root.locator('input[type="file"][accept*="image"]').last
            try:
                if await file_input.count():
                    return file_input
            except PlaywrightError:
                continue
        return None

    async def click_cover_direct_edit_if_needed(self, page):
        for root in self.get_cover_roots(page):
            try:
                direct_edit = root.get_by_text("直接编辑", exact=True).last
                if not await direct_edit.count() or not await direct_edit.is_visible():
                    continue
                await direct_edit.click(force=True, timeout=5000)
                tencent_logger.info("检测到视频号封面素材浮层，已点击直接编辑")
                return True
            except PlaywrightError:
                continue
        return False

    async def is_cover_direct_edit_visible(self, page):
        for root in self.get_cover_roots(page):
            try:
                direct_edit = root.get_by_text("直接编辑", exact=True).last
                if await direct_edit.count() and await direct_edit.is_visible():
                    return True
            except PlaywrightError:
                continue
        return False

    async def click_cover_confirm(self, page, ratio):
        dialog_title = self.get_cover_meta(ratio)["dialog_title"]
        selectors = [
            'button.weui-desktop-btn_primary:has-text("确认")',
            'button.weui-desktop-btn_primary:has-text("确定")',
            'button:has-text("确认")',
            'button:has-text("确定")',
            '.weui-desktop-btn:has-text("确认")',
            '.weui-desktop-btn:has-text("确定")',
        ]
        for attempt in range(1, 4):
            dialog = await self.find_visible_cover_dialog(page, dialog_title)
            if dialog is None:
                return False
            for selector in selectors:
                locator = dialog.locator(selector).last
                try:
                    if await locator.count():
                        await locator.click(force=True, timeout=5000)
                        if await self.wait_for_cover_dialog_closed(page, dialog_title):
                            return True
                        tencent_logger.warning(f"视频号{ratio}封面第 {attempt} 次确认后弹窗仍未关闭，准备重试")
                except PlaywrightError:
                    continue
        return False

    async def find_cover_edit_button(self, page, ratio):
        meta = self.get_cover_meta(ratio)
        if not meta["wrap_class"]:
            return None
        selectors = [
            f'div.{meta["wrap_class"]}:has-text("{meta["card_title"]}"):has-text("{ratio}") .edit-btn',
            f'div.{meta["wrap_class"]}:has-text("{ratio}") .edit-btn',
            f'div.{meta["wrap_class"]}:has-text("{meta["card_title"]}") .edit-btn',
        ]
        for root in self.get_cover_roots(page):
            for selector in selectors:
                locator = root.locator(selector).first
                try:
                    if await locator.count():
                        return locator
                except PlaywrightError:
                    continue

            ratio_label = root.get_by_text(ratio, exact=True).first
            try:
                if await ratio_label.count():
                    locator = ratio_label.locator(
                        "xpath=ancestor::*[contains(@class, 'img-popover-wrap')][1]//*[contains(@class, 'edit-btn')]"
                    ).first
                    if await locator.count():
                        return locator
            except PlaywrightError:
                continue

            semantic_selector = f'[data-codex-cover-edit="{ratio}"]'
            try:
                marked = await root.evaluate(
                    """ratio => {
                        const normalize = value => String(value || '')
                            .replace(/\\s+/g, '')
                            .trim();
                        const visible = node => {
                            if (!(node instanceof HTMLElement)) return false;
                            const rect = node.getBoundingClientRect();
                            const style = getComputedStyle(node);
                            return rect.width > 0 && rect.height > 0
                                && style.display !== 'none'
                                && style.visibility !== 'hidden';
                        };
                        document.querySelectorAll('[data-codex-cover-edit]')
                            .forEach(node => node.removeAttribute('data-codex-cover-edit'));
                        const labels = ratio === '4:3'
                            ? ['4:3', '分享卡片', '横版封面', '横向封面']
                            : ['3:4', '个人主页卡片', '竖版封面', '竖向封面'];
                        const textNodes = Array.from(document.querySelectorAll('body *'))
                            .filter(node => {
                                if (!visible(node)) return false;
                                const text = normalize(node.innerText || node.textContent);
                                return labels.some(label => text === normalize(label));
                            })
                            .sort((left, right) =>
                                left.getBoundingClientRect().width
                                - right.getBoundingClientRect().width
                            );
                        for (const label of textNodes) {
                            let row = label;
                            for (let depth = 0; row && depth < 7; depth += 1, row = row.parentElement) {
                                const candidates = Array.from(row.querySelectorAll(
                                    '.edit-btn, [class*="cover-edit"], [class*="edit-cover"], '
                                    + 'button, [role="button"]'
                                )).filter(node => {
                                    if (!visible(node)) return false;
                                    const text = normalize(node.innerText || node.textContent);
                                    const className = String(node.className || '').toLowerCase();
                                    return text === '编辑'
                                        || text.includes('编辑封面')
                                        || className.includes('edit-btn')
                                        || className.includes('cover-edit')
                                        || className.includes('edit-cover');
                                });
                                if (candidates.length) {
                                    candidates[0].setAttribute('data-codex-cover-edit', ratio);
                                    return true;
                                }
                                if (
                                    row !== label
                                    && visible(row)
                                    && /(?:img-popover-wrap|cover-wrap|cover-card)/i.test(String(row.className || ''))
                                ) {
                                    row.setAttribute('data-codex-cover-edit', ratio);
                                    return true;
                                }
                            }
                        }
                        return false;
                    }""",
                    ratio,
                )
                if marked:
                    locator = root.locator(semantic_selector).last
                    if await locator.count() and await locator.is_visible():
                        return locator
            except (PlaywrightError, AttributeError, TypeError):
                continue
        return None

    async def capture_cover_failure(self, page, ratio):
        """保存封面入口失败现场，便于平台页面再次变化时定位。"""

        os.makedirs(TENCENT_SCREENSHOT_DIR, exist_ok=True)
        screenshot_path = os.path.join(
            TENCENT_SCREENSHOT_DIR,
            f"tencent_cover_{ratio.replace(':', 'x')}_unverified_"
            f"{datetime.now():%Y%m%d_%H%M%S_%f}.png",
        )
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            return f"，截图：{screenshot_path}"
        except Exception:
            return ""

    async def add_short_title(self, page):
        candidates = [
            page.locator('input[placeholder*="填写短标题"]').first,
            page.locator('input[placeholder*="概括视频主要内容"]').first,
            page.get_by_text("短标题", exact=True)
            .locator("xpath=following-sibling::*")
            .locator("input")
            .first,
        ]
        short_title_element = None
        for candidate in candidates:
            try:
                if await candidate.count() and await candidate.is_visible(timeout=500):
                    short_title_element = candidate
                    break
            except Exception:
                continue
        if short_title_element is None:
            raise RuntimeError("视频号未找到短标题输入框")

        short_title = str(getattr(self, "short_title", "") or "").strip()
        if not short_title:
            short_title = format_str_for_short_title(self.title)
        if len(short_title) > TENCENT_SHORT_TITLE_MAX_LENGTH:
            raise RuntimeError(
                f"视频号短标题最多 {TENCENT_SHORT_TITLE_MAX_LENGTH} 字，"
                f"当前为 {len(short_title)} 字：{short_title}"
            )

        await short_title_element.fill(short_title)
        await page.wait_for_timeout(300)
        actual_title = (await short_title_element.input_value()).strip()
        if actual_title != short_title:
            raise RuntimeError(
                f"视频号短标题回读失败，目标={short_title}，实际={actual_title}"
            )
        tencent_logger.success(f"视频号短标题已写入并回读：{actual_title}")

    async def click_publish(self, page):
        for attempt in range(1, 7):
            if page.is_closed():
                raise RuntimeError("视频号发布页已关闭，无法继续点击发表")
            try:
                publish_buttion = page.locator('div.form-btns button:has-text("发表")')
                if not await publish_buttion.count():
                    publish_buttion = page.get_by_role("button", name="发表", exact=True)
                if not await publish_buttion.count():
                    raise RuntimeError("未找到视频号发表按钮")

                await publish_buttion.first.wait_for(state="visible", timeout=10000)
                button_class = await publish_buttion.first.get_attribute("class") or ""
                if "disabled" in button_class or "weui-desktop-btn_disabled" in button_class:
                    raise RuntimeError("视频号发表按钮仍不可用，请检查视频是否上传完成或必填项是否填写")

                await publish_buttion.first.click()
                await page.wait_for_url("https://channels.weixin.qq.com/platform/post/list", timeout=8000)
                tencent_logger.success("  [-]视频发布成功")
                return
            except PlaywrightTimeoutError as e:
                current_url = "" if page.is_closed() else page.url
                if "https://channels.weixin.qq.com/platform/post/list" in current_url:
                    tencent_logger.success("  [-]视频发布成功")
                    return
                tencent_logger.warning(f"  [-] 视频号第 {attempt} 次点击发表后未跳转: {e}")
                await asyncio.sleep(1)
            except Exception as e:
                current_url = "" if page.is_closed() else page.url
                if "https://channels.weixin.qq.com/platform/post/list" in current_url:
                    tencent_logger.success("  [-]视频发布成功")
                    return
                if page.is_closed() or "Target page" in str(e):
                    raise RuntimeError(f"视频号发布页已关闭：{e}") from e
                tencent_logger.warning(f"  [-] 视频号第 {attempt} 次发表失败: {e}")
                await asyncio.sleep(1)
        raise RuntimeError("视频号发布未完成：多次点击发表后仍未进入作品列表")

    async def detect_upload_status(self, page):
        max_wait_seconds = 20 * 60
        started_at = asyncio.get_running_loop().time()
        while True:
            if page.is_closed():
                raise RuntimeError("视频号发布页已关闭，无法确认上传状态")
            if asyncio.get_running_loop().time() - started_at > max_wait_seconds:
                raise RuntimeError("视频号上传等待超时")
            # 匹配删除按钮，代表视频上传完毕，如果不存在，代表视频正在上传，则等待
            try:
                # 匹配删除按钮，代表视频上传完毕
                publish_button_class = await page.get_by_role("button", name="发表").get_attribute('class') or ""
                if "weui-desktop-btn_disabled" not in publish_button_class and "disabled" not in publish_button_class:
                    tencent_logger.info("  [-]视频上传完毕")
                    break
                else:
                    tencent_logger.info("  [-] 正在上传视频中...")
                    await asyncio.sleep(2)
                    # 出错了视频出错
                    if await page.locator('div.status-msg.error').count() and await page.locator(
                            'div.media-status-content div.tag-inner:has-text("删除")').count():
                        tencent_logger.error("  [-] 发现上传出错了...准备重试")
                        await self.handle_upload_error(page)
            except PlaywrightError as e:
                if page.is_closed() or "Target page" in str(e):
                    raise RuntimeError(f"视频号发布页已关闭：{e}") from e
                tencent_logger.info("  [-] 正在上传视频中...")
                await asyncio.sleep(2)
            except Exception as e:
                tencent_logger.warning(f"  [-] 检查视频号上传状态失败，继续等待: {e}")
                await asyncio.sleep(2)

    async def add_title_tags(self, page):
        description = self.build_description_text()
        editor = await self.find_description_editor(page)
        await self.fill_editor(page, editor, description)
        actual_text = await self.get_editor_text(editor)
        expected_body = str(self.description if self.description is not None else self.title or "").strip()
        if expected_body and expected_body not in actual_text:
            raise RuntimeError("视频号视频描述填写失败：文案未写入描述框")
        missing_tags = [
            tag for tag in self.normalized_tags()
            if f"#{tag}" not in actual_text and tag not in actual_text
        ]
        if missing_tags:
            raise RuntimeError(f"视频号视频描述填写失败：话题未写入 {missing_tags}")
        tencent_logger.info(f"成功添加hashtag: {len(self.tags)}")

    def normalized_tags(self):
        normalized = []
        for tag in self.tags or []:
            tag = str(tag).strip().lstrip("#")
            if tag:
                normalized.append(tag)
        return normalized

    def build_description_text(self):
        title = str(self.description if self.description is not None else self.title or "").strip()
        tags_text = " ".join(f"#{tag}" for tag in self.normalized_tags())
        return " ".join(part for part in (title, tags_text) if part).strip()

    async def find_description_editor(self, page):
        candidates = [
            page.get_by_text("视频描述", exact=True)
            .locator("xpath=following-sibling::div")
            .locator('[contenteditable="true"], textarea, input, div.input-editor')
            .first,
            page.locator('div.input-editor[placeholder*="添加描述"]').first,
            page.locator('[placeholder*="添加描述"]').first,
            page.locator('[contenteditable="true"][data-placeholder*="添加描述"]').first,
            page.locator('div.input-editor[contenteditable="true"]').first,
            page.locator('div.input-editor').first,
            page.locator('[contenteditable="true"]').first,
        ]
        for locator in candidates:
            try:
                if await locator.count():
                    await locator.wait_for(state="visible", timeout=5000)
                    return locator
            except PlaywrightError:
                continue
        raise RuntimeError("未找到视频号视频描述输入框")

    async def fill_editor(self, page, locator, text):
        await locator.scroll_into_view_if_needed()
        await locator.click(force=True, timeout=10000)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(text, delay=12)
        await page.wait_for_timeout(500)

    async def get_editor_text(self, locator):
        return await locator.evaluate(
            """(element) => {
                const target = element.matches('textarea,input,[contenteditable="true"]')
                    ? element
                    : element.querySelector('textarea,input,[contenteditable="true"]') || element;
                return target.value || target.innerText || target.textContent || '';
            }"""
        )

    async def add_collection(self, page):
        collection_name = str(getattr(self, "collection_name", "") or "").strip()
        if not collection_name:
            return
        try:
            await self._add_collection_strict(page, collection_name)
        except RuntimeError as error:
            if not self.save_draft_only:
                raise
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            detail = str(error).removeprefix("视频号")
            tencent_logger.warning(f"视频号合集未完成：{detail}")
            record_draft_field_warning("视频号", "合集", detail)

    async def _add_collection_strict(self, page, collection_name: str):

        control = await self.find_visible_form_control(page, "添加到合集")
        if control is None:
            raise RuntimeError(f"视频号未找到合集入口，无法选择“{collection_name}”")

        selected_value = await self.read_first_control_value(control)
        if collection_name not in selected_value:
            added_from_recommendation = False
            for root in self.text_roots(page):
                recommendation_names = root.get_by_text(
                    collection_name,
                    exact=True,
                )
                for index in range(
                    await recommendation_names.count() - 1,
                    -1,
                    -1,
                ):
                    recommendation_name = recommendation_names.nth(index)
                    try:
                        if not await recommendation_name.is_visible(timeout=300):
                            continue
                        recommendation_row = recommendation_name.locator(
                            'xpath=ancestor::*[contains(normalize-space(.), "推荐合集") '
                            'and .//*[normalize-space(.)="添加"]][1]'
                        )
                        if not await recommendation_row.count():
                            continue
                        add_buttons = recommendation_row.get_by_text(
                            "添加",
                            exact=True,
                        )
                        for button_index in range(
                            await add_buttons.count() - 1,
                            -1,
                            -1,
                        ):
                            add_button = add_buttons.nth(button_index)
                            if not await add_button.is_visible(timeout=300):
                                continue
                            await add_button.click(timeout=5000)
                            added_from_recommendation = True
                            break
                        if added_from_recommendation:
                            break
                    except Exception:
                        continue
                if added_from_recommendation:
                    break

            if not added_from_recommendation:
                selected = False
                visible_options = []
                option_selector = (
                    ".option-list-wrap > div:visible, "
                    ".option-list-wrap .option-item, "
                    '[role="listbox"] [role="option"], '
                    "li.weui-desktop-dropdown__list-ele, "
                    ".weui-desktop-dropdown__list-ele, "
                    "[class*='dropdown'] [class*='option']"
                )
                # 视频号的合集列表由 iframe 内异步接口加载。五平台同时预打开时，
                # 首次点击后经常要等待数秒，不能在固定 500ms 后就判定为空。
                for attempt in range(1, 5):
                    opened = False
                    for root in self.text_roots(page):
                        for placeholder_text in ("选择合集", "添加到合集"):
                            placeholder = root.get_by_text(
                                placeholder_text,
                                exact=True,
                            ).last
                            try:
                                if not await placeholder.count() or not await placeholder.is_visible(timeout=300):
                                    continue
                                await placeholder.click(timeout=5000)
                                opened = True
                                break
                            except Exception:
                                continue
                        if opened:
                            break
                    if not opened:
                        await control.click(timeout=5000)

                    for _ in range(8):
                        await page.wait_for_timeout(500)
                        for root in self.text_roots(page):
                            all_options = root.locator(option_selector)
                            for index in range(await all_options.count()):
                                option = all_options.nth(index)
                                try:
                                    if not await option.is_visible(timeout=300):
                                        continue
                                    option_text = " ".join(
                                        (await option.inner_text()).split()
                                    )
                                    if option_text:
                                        visible_options.append(option_text)
                                    if collection_name not in option_text:
                                        continue
                                    await option.click(timeout=5000)
                                    selected = True
                                    break
                                except Exception:
                                    continue
                            if selected:
                                break
                        if selected:
                            break
                    if selected:
                        break
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    tencent_logger.warning(
                        f"视频号合集列表第 {attempt} 次未加载，正在重试"
                    )
                if not selected:
                    raise RuntimeError(
                        f"视频号未找到可见合集“{collection_name}”，"
                        f"当前可见选项={list(dict.fromkeys(visible_options))}"
                    )

            await page.wait_for_timeout(700)
            selected_value = await self.read_first_control_value(control)

        if collection_name not in selected_value:
            raise RuntimeError(
                f"视频号合集选择后回读失败，目标={collection_name}，"
                f"实际={selected_value or '空'}"
            )
        tencent_logger.success(f"视频号合集已选择并回读：{selected_value}")

    async def find_visible_form_control(self, page, label_text):
        labels = page.get_by_text(label_text, exact=True)
        for label_index in range(await labels.count() - 1, -1, -1):
            label = labels.nth(label_index)
            try:
                if not await label.is_visible(timeout=300):
                    continue
                siblings = label.locator("xpath=following-sibling::*")
                for sibling_index in range(await siblings.count()):
                    sibling = siblings.nth(sibling_index)
                    if await sibling.is_visible(timeout=300):
                        return sibling
            except Exception:
                continue
        return None

    async def read_first_control_value(self, control):
        return str(await control.evaluate(
            """
            node => {
              const input = node.querySelector('input');
              if (input && String(input.value || '').trim()) {
                return String(input.value).trim();
              }
              const lines = String(node.innerText || node.textContent || '')
                .split(/\\n+/)
                .map(text => text.trim())
                .filter(Boolean);
              return lines[0] || '';
            }
            """
        )).strip()

    async def add_original(self, page):
        original_checkbox = await self.find_checkbox_near_text(
            page,
            ("声明原创", "视频为原创"),
            visible_only=True,
        )
        if original_checkbox is None:
            raise RuntimeError("视频号未找到声明原创勾选框")

        if not await original_checkbox.is_checked():
            await original_checkbox.check(force=True, timeout=5000)
            await page.wait_for_timeout(500)

        agreement_checkbox = await self.find_checkbox_near_text(
            page,
            ("我已阅读并同意",),
            visible_only=True,
        )
        if agreement_checkbox is not None:
            if not await agreement_checkbox.is_checked():
                await agreement_checkbox.check(force=True, timeout=5000)
            declare_buttons = page.get_by_role(
                "button",
                name="声明原创",
                exact=True,
            )
            clicked = False
            for index in range(await declare_buttons.count() - 1, -1, -1):
                button = declare_buttons.nth(index)
                try:
                    if not await button.is_visible(timeout=300):
                        continue
                    await button.click(timeout=5000)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                raise RuntimeError("视频号原创声明弹窗未找到确认按钮")

        await page.wait_for_timeout(700)
        if not await original_checkbox.is_checked():
            raise RuntimeError("视频号原创声明提交后未保持开启")
        tencent_logger.success("视频号原创声明已开启并回读")

    async def find_checkbox_near_text(self, page, texts, visible_only=False):
        for text in texts:
            labels = page.get_by_text(re.compile(re.escape(text)), exact=False)
            for index in range(await labels.count() - 1, -1, -1):
                label = labels.nth(index)
                try:
                    if visible_only and not await label.is_visible(timeout=300):
                        continue
                    current = label
                    for _ in range(6):
                        checkboxes = current.locator('input[type="checkbox"]')
                        for checkbox_index in range(await checkboxes.count()):
                            return checkboxes.nth(checkbox_index)
                        current = current.locator("xpath=..")
                except Exception:
                    continue
        return None

    async def set_ai_generated_declaration(self, page):
        if not getattr(self, "ai_generated", False):
            return

        control = await self.find_video_label_control(page)
        if control is None:
            raise RuntimeError("视频号未找到视频标注入口，无法声明 AI 生成内容")

        selected_value = await self.read_first_control_value(control)
        if not re.search(r"(AI|人工智能).*(生成|合成)|(生成|合成).*(AI|人工智能)", selected_value, re.I):
            await control.click(timeout=5000)
            await page.wait_for_timeout(500)
            option_selectors = (
                ".option-list-wrap .option-item, "
                '[role="listbox"] [role="option"], '
                "li.weui-desktop-dropdown__list-ele, "
                ".weui-desktop-dropdown__list-ele"
            )
            options = page.locator(option_selectors)
            text_options = page.get_by_text(re.compile(r"AI|人工智能", re.I))
            candidates = [
                options.nth(index)
                for index in range(await options.count())
            ] + [
                text_options.nth(index)
                for index in range(await text_options.count())
            ]
            selected = False
            selected_option_text = ""
            visible_options = []
            for option in candidates:
                try:
                    if not await option.is_visible(timeout=300):
                        continue
                    option_text = (await option.inner_text()).strip()
                    if option_text:
                        visible_options.append(option_text)
                    if re.search(
                        r"(AI|人工智能).*(生成|合成)|(生成|合成).*(AI|人工智能)",
                        option_text,
                        re.I,
                    ):
                        await option.click(force=True, timeout=5000)
                        selected = True
                        selected_option_text = option_text
                        break
                except Exception:
                    continue
            if not selected:
                raise RuntimeError(
                    "视频号视频标注中未找到 AI 生成选项，"
                    f"当前可见选项={visible_options}"
                )
            await page.wait_for_timeout(700)
            control = await self.find_video_label_control(page)
            if control is not None:
                selected_value = await self.read_first_control_value(control)
            else:
                selected_value = await self.read_selected_video_label_text(
                    page,
                    selected_option_text,
                )

        if not re.search(r"(AI|人工智能).*(生成|合成)|(生成|合成).*(AI|人工智能)", selected_value, re.I):
            raise RuntimeError(
                f"视频号 AI 视频标注回读失败，实际={selected_value or '空'}"
            )
        tencent_logger.success(f"视频号 AI 视频标注已选择并回读：{selected_value}")

    async def read_selected_video_label_text(self, page, expected_text):
        if not expected_text:
            return ""
        candidates = page.get_by_text(expected_text, exact=True)
        for index in range(await candidates.count() - 1, -1, -1):
            candidate = candidates.nth(index)
            try:
                if not await candidate.is_visible(timeout=300):
                    continue
                inside_visible_dropdown = await candidate.evaluate(
                    """
                    node => {
                      const dropdown = node.closest(
                        '.option-list-wrap, [role="listbox"], '
                        + '.weui-desktop-dropdown__list'
                      );
                      if (!dropdown) return false;
                      const style = window.getComputedStyle(dropdown);
                      const rect = dropdown.getBoundingClientRect();
                      return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0;
                    }
                    """
                )
                if not inside_visible_dropdown:
                    return (await candidate.inner_text()).strip()
            except Exception:
                continue
        return ""

    async def find_video_label_control(self, page):
        control = await self.find_visible_form_control(page, "视频标注")
        if control is not None:
            return control

        placeholders = [
            page.locator('[placeholder*="选择视频标注"]'),
            page.get_by_text("选择视频标注", exact=True),
        ]
        for candidates in placeholders:
            for index in range(await candidates.count() - 1, -1, -1):
                candidate = candidates.nth(index)
                try:
                    if not await candidate.is_visible(timeout=300):
                        continue
                    if await candidate.evaluate(
                        "node => node.matches('input, textarea, [role=\"combobox\"], [role=\"button\"]')"
                    ):
                        return candidate
                    parent = candidate.locator("xpath=..")
                    if await parent.is_visible(timeout=300):
                        return parent
                    return candidate
                except Exception:
                    continue
        return None

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
