import asyncio
import configparser
import os

from playwright.async_api import async_playwright
from xhs import XhsClient

from conf import BASE_DIR
from utils.base_social_media import launch_chromium_with_codecs, save_context_storage_state, set_init_script
from utils.log import (
    tencent_logger,
    kuaishou_logger,
    douyin_logger,
    meta_logger,
    tiktok_logger,
    youtube_logger,
    xiaohongshu_logger,
)
from pathlib import Path
from uploader.xhs_uploader.main import sign_local

async def cookie_auth_douyin(account_file, preview: bool = False):
    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(playwright, headless=not preview, executable_path=None)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        try:
            # 访问指定的 URL
            await page.goto("https://creator.douyin.com/creator-micro/content/upload")
            try:
                await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=5000)
            except:
                douyin_logger.warning("[douyin] cookie 失效")
                if preview:
                    await page.wait_for_timeout(1500)
                return False
            # 2024.06.17 抖音创作者中心改版
            if await page.get_by_text('手机号登录').count() or await page.get_by_text('扫码登录').count():
                douyin_logger.warning("[douyin] cookie 失效")
                if preview:
                    await page.wait_for_timeout(1500)
                return False
            else:
                douyin_logger.success("[douyin] cookie 有效")
                if preview:
                    await page.wait_for_timeout(1500)
                return True
        finally:
            await context.close()
            await browser.close()

def _tencent_auth_page_is_login(
    url: str | None,
    text: str,
    has_wechat_login_frame: bool,
) -> bool:
    value = (url or "").lower()
    normalized_text = text.lower()
    return (
        "/login" in value
        or "oauth-callback" in value
        or has_wechat_login_frame
        or any(
            marker in normalized_text
            for marker in (
                "扫码登录",
                "请使用微信扫码",
                "微信扫一扫",
                "登录后可使用",
                "微信快捷登录",
                "使用其他头像、昵称或账号",
            )
        )
    )


async def cookie_auth_tencent(account_file, preview: bool = False):
    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(playwright, headless=not preview, executable_path=None)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            await page.goto(
                "https://channels.weixin.qq.com/platform/post/create",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(1000)

            text = ""
            try:
                text = await page.locator("body").inner_text(timeout=3000)
            except Exception:
                pass
            has_wechat_login_frame = bool(
                await page.locator(
                    'iframe[src*="open.weixin.qq.com"], '
                    'iframe[src*="qrconnect"], '
                    'iframe[src*="login"]'
                ).count()
            )
            if _tencent_auth_page_is_login(
                page.url,
                text,
                has_wechat_login_frame,
            ):
                tencent_logger.error("[tencent] cookie 失效，仍停留在微信登录页")
                if preview:
                    await page.wait_for_timeout(1500)
                return False
            if "/platform" not in (page.url or "").lower():
                tencent_logger.error(f"[tencent] cookie 失效，未进入创作者后台: {page.url}")
                if preview:
                    await page.wait_for_timeout(1500)
                return False

            await save_context_storage_state(context, account_file, include_indexed_db=True)
            tencent_logger.success("[tencent] cookie 有效，登录态已刷新")
            if preview:
                await page.wait_for_timeout(1500)
            return True
        except Exception as e:
            tencent_logger.error(f"[tencent] cookie 校验失败: {e}")
            return False
        finally:
            await context.close()
            await browser.close()

async def cookie_auth_ks(account_file, preview: bool = False):
    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(playwright, headless=not preview, executable_path=None)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            await page.goto(
                "https://cp.kuaishou.com/article/publish/video",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(1000)

            login_indicators = [
                'a:has-text("立即登录")',
                'button:has-text("登录")',
                'text=扫码登录',
                'text=快手扫码登录',
                'text=请扫码登录',
                'img[alt="qrcode"]',
            ]
            for indicator in login_indicators:
                if await page.locator(indicator).count():
                    kuaishou_logger.info(f"[kuaishou] cookie 失效，命中登录提示: {indicator}")
                    if preview:
                        await page.wait_for_timeout(1500)
                    return False

            kuaishou_logger.success("[kuaishou] cookie 有效")
            if preview:
                await page.wait_for_timeout(1500)
            return True
        except Exception as e:
            kuaishou_logger.error(f"[kuaishou] cookie 校验失败: {e}")
            return False
        finally:
            await context.close()
            await browser.close()


async def cookie_auth_xhs(account_file, preview: bool = False):
    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(playwright, headless=not preview, executable_path=None)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        try:
            # 访问指定的 URL
            await page.goto("https://creator.xiaohongshu.com/creator-micro/content/upload")
            try:
                await page.wait_for_url("https://creator.xiaohongshu.com/creator-micro/content/upload", timeout=5000)
            except:
                xiaohongshu_logger.warning("[xhs] cookie 失效")
                if preview:
                    await page.wait_for_timeout(1500)
                return False
            # 2024.06.17 抖音创作者中心改版
            if await page.get_by_text('手机号登录').count() or await page.get_by_text('扫码登录').count():
                xiaohongshu_logger.warning("[xhs] cookie 失效")
                if preview:
                    await page.wait_for_timeout(1500)
                return False
            else:
                xiaohongshu_logger.success("[xhs] cookie 有效")
                if preview:
                    await page.wait_for_timeout(1500)
                return True
        finally:
            await context.close()
            await browser.close()


async def _page_body_text(page) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return ""


async def cookie_auth_tiktok(account_file, preview: bool = False):
    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(playwright, headless=not preview, executable_path=None)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            await page.goto(
                "https://www.tiktok.com/tiktokstudio/upload?lang=en",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_timeout(2500)
            url = page.url.lower()
            text = await _page_body_text(page)
            if "/login" in url or "log in to tiktok" in text or "sign up for tiktok" in text:
                tiktok_logger.warning("[tiktok] 登录状态已失效")
                return False
            ready_selectors = (
                'input[type="file"]',
                'button:has-text("Select video")',
                'button:has-text("Select file")',
            )
            ready = False
            for selector in ready_selectors:
                if await page.locator(selector).count():
                    ready = True
                    break
            if ready:
                tiktok_logger.success("[tiktok] 登录状态有效")
            return bool(ready)
        except Exception as exc:
            tiktok_logger.error(f"[tiktok] 登录状态检查失败: {exc}")
            return False
        finally:
            await context.close()
            await browser.close()


async def cookie_auth_youtube(account_file, preview: bool = False):
    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(playwright, headless=not preview, executable_path=None)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            await page.goto("https://studio.youtube.com/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            url = page.url.lower()
            ready = "accounts.google.com" not in url and "/signin" not in url and (
                "/channel/" in url or await page.locator("ytcp-app").count() > 0
            )
            if ready:
                youtube_logger.success("[youtube] 登录状态有效")
            else:
                youtube_logger.warning("[youtube] 登录状态已失效或未进入频道后台")
            return bool(ready)
        except Exception as exc:
            youtube_logger.error(f"[youtube] 登录状态检查失败: {exc}")
            return False
        finally:
            await context.close()
            await browser.close()


async def cookie_auth_meta(account_file, platform_type: int, preview: bool = False):
    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(playwright, headless=not preview, executable_path=None)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            await page.goto(
                "https://business.facebook.com/latest/composer/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_timeout(3000)
            url = page.url.lower()
            text = await _page_body_text(page)
            denied = any(
                marker in text
                for marker in (
                    "无法用这个账户访问 meta business suite",
                    "can't access meta business suite",
                    "cannot access meta business suite",
                    "you don't have access to meta business suite",
                )
            )
            if denied or "facebook.com/login" in url or "checkpoint" in url:
                meta_logger.warning("[meta] 登录状态或 Business Suite 权限不可用")
                return False
            composer_ready = any(
                marker in text
                for marker in ("create post", "create reel", "add video", "创建帖子", "创建 reel", "添加视频")
            ) or await page.locator('[contenteditable="true"][role="textbox"]').count() > 0
            destination = "instagram" if int(platform_type) == 8 else "facebook"
            ready = composer_ready and destination in text
            if ready:
                meta_logger.success(f"[meta] {destination} 发布入口可用")
            else:
                meta_logger.warning(f"[meta] 未检测到 {destination} 发布入口")
            return bool(ready)
        except Exception as exc:
            meta_logger.error(f"[meta] 登录状态检查失败: {exc}")
            return False
        finally:
            await context.close()
            await browser.close()


async def check_cookie(type, file_path, preview: bool = False):
    match type:
        # 小红书
        case 1:
            return await cookie_auth_xhs(Path(BASE_DIR / "cookiesFile" / file_path), preview)
        # 视频号
        case 2:
            return await cookie_auth_tencent(Path(BASE_DIR / "cookiesFile" / file_path), preview)
        # 抖音
        case 3:
            return await cookie_auth_douyin(Path(BASE_DIR / "cookiesFile" / file_path), preview)
        # 快手
        case 4:
            return await cookie_auth_ks(Path(BASE_DIR / "cookiesFile" / file_path), preview)
        # B站
        case 5:
            return await cookie_auth_bilibili(Path(BASE_DIR / "cookiesFile" / file_path), preview)
        # TikTok
        case 6:
            return await cookie_auth_tiktok(Path(BASE_DIR / "cookiesFile" / file_path), preview)
        # YouTube
        case 7:
            return await cookie_auth_youtube(Path(BASE_DIR / "cookiesFile" / file_path), preview)
        # Instagram Reels / Facebook Reels 共用 Meta 登录态
        case 8 | 9:
            return await cookie_auth_meta(Path(BASE_DIR / "cookiesFile" / file_path), type, preview)
        case _:
            return False

async def cookie_auth_bilibili(account_file, preview: bool = False):
    """B站cookie验证函数"""
    from utils.log import bilibili_logger

    bilibili_logger.info(f"[bilibili_auth] 开始验证B站cookie: {account_file}")

    async with async_playwright() as playwright:
        browser = await launch_chromium_with_codecs(playwright, headless=not preview, executable_path=None)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        page = await context.new_page()

        try:
            # 访问B站创作中心
            await page.goto("https://member.bilibili.com/platform/upload/video/frame")
            bilibili_logger.info(f"[bilibili_auth] 页面已加载: {page.url}")

            # 等待页面加载完成
            await page.wait_for_timeout(3000)

            # 更精确的登录状态检查
            # 首先检查是否被重定向到登录页面
            if "passport.bilibili.com" in page.url:
                bilibili_logger.warning("[bilibili_auth] 页面重定向到登录页，cookie失效")
                if preview:
                    await page.wait_for_timeout(1500)
                await context.close()
                await browser.close()
                return False

            # 检查页面是否有明确的登录表单或按钮（更精确的选择器）
            login_form_indicators = [
                'button:has-text("立即登录")',  # 登录按钮
                'button:has-text("扫码登录")',  # 扫码登录按钮
                'form[action*="login"]',  # 登录表单
                '.login-form',  # 登录表单类
                'input[type="password"]',  # 密码输入框
                '.qr-login',  # 二维码登录区域
            ]

            found_login_form = False
            for indicator in login_form_indicators:
                if await page.locator(indicator).count():
                    bilibili_logger.warning(f"[bilibili_auth] 发现登录表单元素: {indicator}")
                    found_login_form = True
                    break

            if found_login_form:
                bilibili_logger.warning("[bilibili_auth] 检测到登录表单，cookie失效")
                if preview:
                    await page.wait_for_timeout(1500)
                await context.close()
                await browser.close()
                return False

            # 额外调试：记录页面上的所有"登录"文字
            login_texts = await page.locator('text=登录').all()
            if login_texts:
                bilibili_logger.info(f"[bilibili_auth] 页面上发现{len(login_texts)}个'登录'文字，但都不是登录表单")
                for i, text_elem in enumerate(login_texts[:3]):  # 只记录前3个
                    try:
                        content = await text_elem.text_content()
                        bilibili_logger.info(f"[bilibili_auth] 登录文字{i+1}: {content}")
                    except:
                        pass

            # 如果没有登录提示，认为验证成功
            bilibili_logger.info("[bilibili_auth] cookie验证成功")
            if preview:
                await page.wait_for_timeout(1500)
            await context.close()
            await browser.close()
            return True

        except Exception as e:
            bilibili_logger.error(f"[bilibili_auth] cookie验证失败: {e}")
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
            return False

# a = asyncio.run(check_cookie(1,"3a6cfdc0-3d51-11f0-8507-44e51723d63c.json"))
# print(a)
