"""一键发内的已登录账号后台窗口。

只使用一键发保存的本地会话打开对应平台官网；不读取蚁小二或发射台的
账号目录，也不会上传素材、创建草稿或点击发布。
"""

from __future__ import annotations

import asyncio
import queue
import threading
import webbrowser
from pathlib import Path

from . import account_service
from .oneclick_authorization import authorization_plan
from .overseas_meta_errors import FacebookPagePublishError
from .overseas_meta_page_identity import activate_saved_facebook_page
from .overseas_youtube_profile import youtube_studio_url
from .paths import COOKIE_DIR, ensure_runtime_dirs


_session_lock = threading.Lock()
_backend_threads: dict[int, threading.Thread] = {}

def _account_key(account: dict) -> int:
    account_id = int(account.get("id") or 0)
    if account_id <= 0:
        raise ValueError("账号记录无效，无法打开平台后台")
    return account_id


async def _open_backend(
    account: dict,
    startup_results: queue.Queue[object] | None = None,
) -> None:
    """保持可见官方后台直至用户自行关闭窗口。"""

    plan = authorization_plan(
        int(account.get("type") or 0),
        str(account.get("profileName") or ""),
    )
    state_file = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not state_file.is_file():
        raise RuntimeError("一键发本地登录会话不存在，请重新登录")

    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = None
    context = None
    try:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=str(state_file))
        page = await context.new_page()
        await page.goto(plan.login_url, wait_until="domcontentloaded", timeout=45_000)
        if int(account.get("type") or 0) == 9:
            expected_page_id = account_service.validate_saved_facebook_page_account(
                account
            )
            await activate_saved_facebook_page(page, expected_page_id)
        await page.bring_to_front()
        if startup_results is not None:
            startup_results.put(True)
        # 不在这里做登录检测或任何发布操作；用户可像普通浏览器一样查看后台。
        # Playwright 等待事件默认 30 秒超时；账号后台是交给用户手动操作的
        # 长驻窗口，必须一直保持到用户主动关闭页面。
        await page.wait_for_event("close", timeout=0)
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        await playwright.stop()


def _thread_target(
    account: dict,
    account_id: int,
    startup_results: queue.Queue[object] | None = None,
) -> None:
    try:
        asyncio.run(_open_backend(account, startup_results))
    except Exception as exc:
        if startup_results is None:
            raise
        try:
            if (
                int(account.get("type") or 0) == 9
                and isinstance(exc, FacebookPagePublishError)
            ):
                account_service.update_status(account_id, 0)
        except Exception:
            # Even if local health persistence fails, the caller must receive
            # the original Page failure and must not report a successful open.
            pass
        finally:
            startup_results.put(exc)
    finally:
        with _session_lock:
            _backend_threads.pop(account_id, None)


def open_account_backend(account: dict) -> bool:
    """打开该账号的官方后台；若窗口已存在则复用该会话。"""

    ensure_runtime_dirs()
    account_id = _account_key(account)
    if str(account.get("authMode") or "browser") == "youtube_oauth":
        if int(account.get("type") or 0) != 7:
            raise RuntimeError("YouTube OAuth 账号记录无效")
        if not webbrowser.open(youtube_studio_url(account)):
            raise RuntimeError("系统浏览器未能打开 YouTube Studio")
        return False
    if int(account.get("type") or 0) == 9:
        account_service.validate_saved_facebook_page_account(account)
    # 在启动线程前完成本地参数校验，让界面能立即给出可理解的错误。
    authorization_plan(int(account.get("type") or 0), str(account.get("profileName") or ""))
    state_file = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not state_file.is_file():
        raise RuntimeError("一键发本地登录会话不存在，请重新登录")
    with _session_lock:
        current = _backend_threads.get(account_id)
        if current and current.is_alive():
            return True
        startup_results = (
            queue.Queue()
            if int(account.get("type") or 0) == 9
            else None
        )
        worker = threading.Thread(
            target=_thread_target,
            args=(dict(account), account_id, startup_results),
            daemon=True,
            name=f"oneclick-account-backend-{account_id}",
        )
        _backend_threads[account_id] = worker
        worker.start()
    if startup_results is not None:
        startup_result = startup_results.get()
        if isinstance(startup_result, Exception):
            raise startup_result
    return False


def close_all_backend_sessions(*, wait: bool = False) -> None:
    """主窗口退出时不阻塞；Playwright 子进程会随解释器结束关闭。"""

    if not wait:
        return
    with _session_lock:
        workers = list(_backend_threads.values())
    for worker in workers:
        worker.join(timeout=0.2)
