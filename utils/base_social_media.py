import asyncio
import os
import sys
from pathlib import Path
from typing import List, Optional

from conf import BASE_DIR, DEBUG_DRY_RUN_HOLD_SECONDS, RESOURCE_DIR, USE_SYSTEM_BROWSER
from utils.publish_observer import get_publish_context, publish_event

SOCIAL_MEDIA_DOUYIN = "douyin"
SOCIAL_MEDIA_TENCENT = "tencent"
SOCIAL_MEDIA_TIKTOK = "tiktok"
SOCIAL_MEDIA_BILIBILI = "bilibili"
SOCIAL_MEDIA_KUAISHOU = "kuaishou"


def get_supported_social_media() -> List[str]:
    return [
        SOCIAL_MEDIA_DOUYIN,
        SOCIAL_MEDIA_TENCENT,
        SOCIAL_MEDIA_TIKTOK,
        SOCIAL_MEDIA_BILIBILI,
        SOCIAL_MEDIA_KUAISHOU,
    ]


def get_cli_action() -> List[str]:
    return ["upload", "login", "watch"]


async def set_init_script(context):
    stealth_js_path = Path(RESOURCE_DIR / "utils/stealth.min.js")
    if not stealth_js_path.is_file():
        # 一键发源码开发版会把 RESOURCE_DIR 指向自身 UI 工程；恢复的发布
        # 能力仍从本模块目录加载。开发态优先复用能力源码随附的脚本，
        # 打包态继续沿用应用 Resources，不复制或读取任何账号数据。
        stealth_js_path = Path(__file__).resolve().parent / "stealth.min.js"
    if not stealth_js_path.is_file():
        raise FileNotFoundError("发布浏览器初始化脚本不存在")
    await context.add_init_script(path=stealth_js_path)
    return context


async def save_context_storage_state(context, account_file, include_indexed_db: bool = False):
    """保存浏览器登录态；视频号这类平台需要尽量把 IndexedDB 一起保存。"""
    path = Path(account_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if include_indexed_db:
        try:
            await context.storage_state(path=str(path), indexed_db=True)
            harden_sensitive_file_permissions(path)
            return
        except Exception as e:
            # 旧版 Playwright 不支持 indexed_db，或平台 IndexedDB 快照失败时，退回普通 Cookie/localStorage 保存。
            print(f"[storage] IndexedDB 登录态保存失败，已降级普通保存：{e}")
    await context.storage_state(path=str(path))
    harden_sensitive_file_permissions(path)


def harden_sensitive_file_permissions(path: Path | str) -> None:
    """把登录态等敏感文件限制为仅当前用户可读写。"""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"敏感状态文件未生成：{target}")
    if os.name == "posix":
        target.chmod(0o600)


PUBLISH_WINDOW_WIDTH = 1600
PUBLISH_WINDOW_HEIGHT = 1000
PUBLISH_DEVICE_SCALE_FACTOR = 1
PUBLISH_BASELINE_CHROMIUM_VERSION = "148.0.7778.96"
PUBLISH_BASELINE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"HeadlessChrome/{PUBLISH_BASELINE_CHROMIUM_VERSION} Safari/537.36"
)
PUBLISH_WINDOW_X = 48
PUBLISH_WINDOW_Y = 36
PUBLISH_WINDOW_MARGIN = 24
HIDDEN_WINDOW_X = -32000
HIDDEN_WINDOW_Y = -32000
_REVEALED_WINDOW_IDS = set()


def is_publish_background_mode() -> bool:
    """Return whether the current publish task must run without a browser window."""
    return bool(get_publish_context().get("background_mode"))


def _runtime_base_dirs() -> list[Path]:
    """返回源码态及 macOS 应用包内可能的只读资源根目录。"""

    candidates: list[Path] = []
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        candidates.append(Path(frozen_base))
        # PyInstaller 的 macOS BUNDLE 会把数据文件放在 Contents/Resources，
        # 而 _MEIPASS 在部分版本中指向 Contents/Frameworks。
        candidates.append(Path(sys.executable).resolve().parent.parent / "Resources")
    candidates.append(Path(RESOURCE_DIR))

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _configure_bundled_playwright_browsers(announce: bool = True) -> Path | None:
    candidates = []
    for base_dir in _runtime_base_dirs():
        candidates.extend(
            (
                base_dir / "runtime" / "playwright-browsers",
                base_dir / "third_party" / "playwright" / "ms-playwright",
            )
        )
    for candidate in candidates:
        if candidate.exists():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
            if announce:
                print(f"[launch] Using bundled Playwright browsers at {candidate}")
            return candidate
    return None


# Playwright 在驱动进程启动时读取浏览器目录，必须在 async_playwright() 启动前配置。
_configure_bundled_playwright_browsers(announce=False)


async def launch_chromium_with_codecs(
    playwright,
    headless: bool = False,
    executable_path: Optional[str] = None,
    hide_until_ready: bool = False,
    force_bundled: bool = False,
):
    """
    Prefer launching with system Chrome/Edge for proprietary codecs (H.264) and stable media playback.
    Falls back to bundled Chromium if neither is available.
    """
    launch_args = [
        "--window-size=1600,1000",
        "--autoplay-policy=no-user-gesture-required",
        "--enable-gpu",
        "--ignore-gpu-blocklist",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--mute-audio",
        "--lang=zh-CN",
    ]
    _configure_bundled_playwright_browsers()
    if not headless:
        if hide_until_ready:
            # 先请求最小化，再用页面创建后的 CDP 最小化回执兜底。macOS 对离屏
            # 坐标没有强制保证，二者配合才能避免上传阶段抢占用户当前窗口。
            launch_args.append("--start-minimized")
            launch_args.append(f"--window-position={HIDDEN_WINDOW_X},{HIDDEN_WINDOW_Y}")
        else:
            launch_args.append(f"--window-position={PUBLISH_WINDOW_X},{PUBLISH_WINDOW_Y}")

    if force_bundled:
        print("[launch] Using fixed bundled Chromium baseline.")
        return await playwright.chromium.launch(headless=headless, args=launch_args)

    # If user prefers bundled/runtime-only mode, skip system channels and try third_party or bundled Chromium
    if not USE_SYSTEM_BROWSER:
        # Probe portable Chrome/Edge under third_party (with proprietary codecs)
        portable_candidates = [
            Path(RESOURCE_DIR / "third_party" / "chrome-win" / "chrome.exe"),
            Path(RESOURCE_DIR / "third_party" / "chrome" / "chrome.exe"),
            Path(RESOURCE_DIR / "third_party" / "edge" / "msedge.exe"),
            Path(RESOURCE_DIR / "third_party" / "msedge" / "msedge.exe"),
        ]
        for candidate in portable_candidates:
            try:
                if candidate.exists():
                    print(f"[launch] Using portable browser: {candidate}")
                    browser = await playwright.chromium.launch(
                        headless=headless,
                        executable_path=str(candidate),
                        args=launch_args,
                    )
                    try:
                        # Probe by opening and closing a dummy browser context
                        context = await browser.new_context()
                        await context.close()
                        print(f"[launch] Portable browser ready: {candidate}")
                    except Exception as e:
                        print(f"[launch] Portable browser probe failed: {e}")
                    return browser
            except Exception as e:
                print(f"[launch] portable exec failed {candidate}: {e}")

        print("[launch] Forcing bundled Chromium (no system browser). H.264 may be unavailable.")
        return await playwright.chromium.launch(headless=headless, args=launch_args)

    # 1) Explicit executable path wins
    try:
        if executable_path and executable_path.strip():
            return await playwright.chromium.launch(
                headless=headless,
                executable_path=executable_path,
                args=launch_args,
            )
    except Exception as e:
        print(f"[launch] executable_path failed: {e}")

    # 2) Try installed Chrome
    try:
        return await playwright.chromium.launch(
            headless=headless,
            channel="chrome",
            args=launch_args,
        )
    except Exception as e:
        print(f"[launch] channel=chrome failed: {e}")

    # 3) Try installed Edge (also supports proprietary codecs on Windows)
    try:
        return await playwright.chromium.launch(
            headless=headless,
            channel="msedge",
            args=launch_args,
        )
    except Exception as e:
        print(f"[launch] channel=msedge failed: {e}")

    # 4) Fallback to bundled Chromium (may lack H.264)
    print("[launch] Falling back to bundled Chromium; H.264 may be unavailable.")
    return await playwright.chromium.launch(headless=headless, args=launch_args)


async def launch_publish_browser(playwright, executable_path: Optional[str] = None):
    """Launch publish browser; background tasks use true headless mode."""
    background_mode = is_publish_background_mode()
    browser = await launch_chromium_with_codecs(
        playwright,
        headless=background_mode,
        executable_path=executable_path,
        hide_until_ready=not background_mode,
        force_bundled=True,
    )
    browser_version = getattr(browser, "version", "")
    if browser_version and browser_version != PUBLISH_BASELINE_CHROMIUM_VERSION:
        await browser.close()
        raise RuntimeError(
            "发布浏览器 Chromium 版本不一致："
            f"期望 {PUBLISH_BASELINE_CHROMIUM_VERSION}，实际 {browser_version}。"
            "请重新安装项目锁定的 Playwright Chromium。"
        )
    return browser


async def new_publish_context(browser, storage_state: Optional[str] = None, **kwargs):
    # 五个平台以已验收通过的 Mac 后台环境为统一基准，避免页面因操作系统
    # 窗口装饰或显示缩放不同而切换布局、改变控件位置。
    context_options = {
        "viewport": {
            "width": PUBLISH_WINDOW_WIDTH,
            "height": PUBLISH_WINDOW_HEIGHT,
        },
        "screen": {
            "width": PUBLISH_WINDOW_WIDTH,
            "height": PUBLISH_WINDOW_HEIGHT,
        },
        "device_scale_factor": PUBLISH_DEVICE_SCALE_FACTOR,
        "user_agent": PUBLISH_BASELINE_USER_AGENT,
        "permissions": [],
        "geolocation": None,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
    }
    if storage_state:
        context_options["storage_state"] = str(storage_state) if isinstance(storage_state, (str, Path)) else storage_state
    context_options.update(kwargs)
    return await browser.new_context(**context_options)


async def reveal_page_window(page):
    """Move the off-screen browser window into view once, then only switch tabs."""
    if is_publish_background_mode():
        return False
    try:
        session = await page.context.new_cdp_session(page)
        window_info = await session.send("Browser.getWindowForTarget")
        window_id = window_info["windowId"]
        if window_id in _REVEALED_WINDOW_IDS:
            await page.bring_to_front()
            return

        await session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {
                    "windowState": "normal",
                    "left": PUBLISH_WINDOW_X,
                    "top": PUBLISH_WINDOW_Y,
                    "width": PUBLISH_WINDOW_WIDTH,
                    "height": PUBLISH_WINDOW_HEIGHT,
                },
            },
        )
        _REVEALED_WINDOW_IDS.add(window_id)
        await page.bring_to_front()
        return True
    except Exception as e:
        print(f"[launch] reveal via cdp failed: {e}")

    try:
        await page.evaluate(
            """([margin]) => {
                const left = Math.max(0, window.screen.availLeft || 0) + margin;
                const top = Math.max(0, window.screen.availTop || 0) + margin;
                const width = Math.max(1024, window.screen.availWidth - margin * 2);
                const height = Math.max(720, window.screen.availHeight - margin * 2);
                window.moveTo(left, top);
                window.resizeTo(width, height);
            }""",
            [PUBLISH_WINDOW_MARGIN],
        )
    except Exception as e:
        print(f"[launch] reveal via window api failed: {e}")
    return False


async def hide_page_window(page):
    """最小化受控浏览器窗口，并以 CDP 回执作为后台运行的最低保证。

    ``--window-position`` 在 macOS 上只是启动建议，窗口管理器可能仍把有窗口
    Chromium 放到前台。带货上传需要保留同一有窗口会话，供后续二维码或最终
    确认时恢复，因此不能改成不可恢复的 headless；这里在创建页面后立即最小化。
    如果 CDP 无法确认最小化，调用方必须停止，而不能继续在可见窗口中上传。
    """

    try:
        session = await page.context.new_cdp_session(page)
        window_info = await session.send("Browser.getWindowForTarget")
        window_id = window_info["windowId"]
        await session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {"windowState": "minimized"},
            },
        )
        _REVEALED_WINDOW_IDS.discard(window_id)
        return True
    except Exception as e:
        print(f"[launch] hide via cdp failed: {e}")
        return False


async def goto_and_reveal(page, url: str, wait_until: str = "domcontentloaded", timeout: Optional[int] = None):
    goto_options = {"wait_until": wait_until}
    if timeout is not None:
        goto_options["timeout"] = timeout
    publish_event("open_publish_page", f"正在打开发布页面：{url}", url=url)
    try:
        response = await page.goto(url, **goto_options)
        publish_event("open_publish_page_success", "发布页面已打开", url=url)
        return response
    except Exception as exc:
        publish_event("open_publish_page_failed", f"发布页面打开失败：{exc}", level="error", url=url)
        raise
    finally:
        await reveal_page_window(page)


async def prevent_new_tabs(page, logger=None, label: str = "发布"):
    """Prevent platform submit clicks from opening transient blank/helper tabs."""
    try:
        await page.evaluate(
            """(label) => {
                if (window.__sauPreventNewTabsInstalled) return;
                window.__sauPreventNewTabsInstalled = true;
                window.__sauOriginalWindowOpen = window.open;
                window.open = (...args) => {
                    console.debug(`[SAU] blocked window.open during ${label}`, args);
                    return null;
                };
                const normalizeTargets = () => {
                    document.querySelectorAll('a[target="_blank"], form[target="_blank"]').forEach(node => {
                        node.setAttribute('target', '_self');
                    });
                };
                normalizeTargets();
                const observer = new MutationObserver(normalizeTargets);
                observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['target'] });
            }""",
            label,
        )
    except Exception as e:
        if logger:
            logger.warning(f"{label} 新标签拦截脚本注入失败: {e}")
        else:
            print(f"{label} new-tab blocker inject failed: {e}")


async def keep_browser_open_for_dry_run(
    page,
    context,
    browser,
    account_file=None,
    logger=None,
    platform_name: str = "平台",
    block_until_close: bool = True,
    auto_close_seconds: Optional[int] = None,
    include_indexed_db: bool = False,
):
    """Keep the headed publish window open after dry-run so the operator can inspect the filled form."""
    if account_file:
        await save_context_storage_state(context, account_file, include_indexed_db=include_indexed_db)

    background_mode = is_publish_background_mode()
    if background_mode:
        block_until_close = False
        auto_close_seconds = 0
    elif auto_close_seconds is None:
        auto_close_seconds = DEBUG_DRY_RUN_HOLD_SECONDS

    if background_mode:
        message = f"{platform_name} 后台预发布检查完成：已跳过最终发布，浏览器会话将自动关闭。"
    elif block_until_close:
        message = f"{platform_name} dry_run 已完成：已跳过最终发布，浏览器窗口会保持打开；检查完成后请手动关闭窗口。"
    else:
        message = f"{platform_name} dry_run 已完成：已跳过最终发布，窗口保留 {auto_close_seconds} 秒后自动关闭以继续批量预发布。"
    if logger:
        logger.info(message)
    else:
        print(message)
    publish_event("dry_run_ready", message, platformName=platform_name)

    try:
        if block_until_close:
            while browser.is_connected():
                pages = []
                try:
                    for item in browser.contexts:
                        pages.extend(item.pages)
                except Exception:
                    pages = [page]
                if pages and all(p.is_closed() for p in pages):
                    break
                await asyncio.sleep(1)
        elif auto_close_seconds and auto_close_seconds > 0:
            await asyncio.sleep(auto_close_seconds)
    finally:
        try:
            await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
