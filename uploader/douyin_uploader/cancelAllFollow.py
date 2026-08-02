# -*- coding: utf-8 -*-
'''单独的运行文件，用于取消 Web 端的抖音关注。'''
import asyncio
import os
import sys
import random
from pathlib import Path

from playwright.async_api import async_playwright

# Ensure project root is importable
ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from conf import LOCAL_CHROME_PATH  # noqa: E402
from utils.base_social_media import set_init_script  # noqa: E402


DOUYIN_FOLLOWING_URL = "https://creator.douyin.com/creator-micro/data/following/following"
MAX_UNFOLLOW_TIMES = 100


def resolve_cookie_file() -> Path | None:
    """从命令行参数或环境变量读取登录态文件，避免在发布包里留下测试账号路径。"""
    raw_path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DOUYIN_COOKIE_FILE", "")
    if not raw_path:
        print("[-] 请通过命令行参数或 DOUYIN_COOKIE_FILE 环境变量指定自己的 Cookie 文件。")
        return None

    cookie_path = Path(raw_path)
    if not cookie_path.is_absolute():
        cookie_path = ROOT_DIR / "cookiesFile" / raw_path

    cookie_path = cookie_path.resolve()
    cookies_dir = (ROOT_DIR / "cookiesFile").resolve()
    try:
        cookie_path.relative_to(cookies_dir)
    except ValueError:
        print("[-] 为避免误操作，只允许使用 cookiesFile 目录内的 Cookie 文件。")
        return None

    return cookie_path


async def click_unfollow_once(page) -> bool:
    """
    点击第一个可见的“取消关注”，如出现确认弹窗则尝试确认。
    返回 True 表示本轮点击成功，否则返回 False。
    """
    # 优先找链接按钮，找不到时再使用文本兜底。
    unfollow = page.locator("a:has-text('取消关注')").first
    if not await unfollow.count():
        unfollow = page.get_by_text("取消关注").first
        if not await unfollow.count():
            return False

    try:
        await unfollow.scroll_into_view_if_needed()
        await unfollow.click()
    except Exception:
        return False

    # 处理可能出现的确认弹窗。
    try:
        # Common confirm actions: 确定 / 确认 / 取消关注
        for selector in [
            "button:has-text('确认')",
            "button:has-text('取消关注')",
            "[role='button']:has-text('确认')",
        ]:
            if await page.locator(selector).first.count():
                await page.locator(selector).first.click()
                break
    except Exception:
        pass

    # 给页面一点时间更新关注状态。
    await page.wait_for_timeout(random.randint(1000, 2000))
    return True


async def main():
    if os.getenv("CONFIRM_CANCEL_ALL_FOLLOW") != "1":
        print("[-] 安全保护：该脚本会批量取消关注。确认需要执行时，请设置 CONFIRM_CANCEL_ALL_FOLLOW=1。")
        return

    cookie_file = resolve_cookie_file()
    if cookie_file is None:
        return

    if not os.path.exists(cookie_file):
        print(f"[-] Cookie file not found: {cookie_file}")
        return

    async with async_playwright() as p:
        launch_kwargs = {"headless": False}
        if LOCAL_CHROME_PATH:
            launch_kwargs["executable_path"] = LOCAL_CHROME_PATH
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(storage_state=str(cookie_file))
        context = await set_init_script(context)
        page = await context.new_page()

        success = 0
        for i in range(MAX_UNFOLLOW_TIMES):
            print(f"[+] Round {i + 1}/{MAX_UNFOLLOW_TIMES}: opening following page ...")
            await page.goto(DOUYIN_FOLLOWING_URL, wait_until="domcontentloaded")

            # Wait for any unfollow entry to appear
            try:
                await page.wait_for_selector("text=取消关注", timeout=8000)
            except Exception:
                print("[-] No '取消关注' found on the page. Stopping.")
                break

            if await click_unfollow_once(page):
                success += 1
                print(f"  [-] Unfollowed successfully. Total: {success}")
            else:
                print("  [-] Could not click '取消关注'. Stopping.")
                break

            # Refresh for the next iteration
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(0.5, 2))

        # 保存登录态更新。
        await context.storage_state(path=str(cookie_file))
        print(f"[+] Done. Unfollow attempts: {success}/{MAX_UNFOLLOW_TIMES}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
