"""一键发内的已登录账号后台窗口。

只使用一键发保存的本地会话打开对应平台官网；不读取蚁小二或发射台的
账号目录，也不会上传素材、创建草稿或点击发布。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from .oneclick_authorization import authorization_plan
from .paths import COOKIE_DIR, ensure_runtime_dirs


_session_lock = threading.Lock()
_backend_threads: dict[int, threading.Thread] = {}


def _account_key(account: dict) -> int:
    account_id = int(account.get("id") or 0)
    if account_id <= 0:
        raise ValueError("账号记录无效，无法打开平台后台")
    return account_id


async def _open_backend(account: dict) -> None:
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
        await page.bring_to_front()
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


def _thread_target(account: dict, account_id: int) -> None:
    try:
        asyncio.run(_open_backend(account))
    finally:
        with _session_lock:
            _backend_threads.pop(account_id, None)


def open_account_backend(account: dict) -> bool:
    """打开该账号的官方后台；若窗口已存在则复用该会话。"""

    ensure_runtime_dirs()
    account_id = _account_key(account)
    # 在启动线程前完成本地参数校验，让界面能立即给出可理解的错误。
    authorization_plan(int(account.get("type") or 0), str(account.get("profileName") or ""))
    state_file = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not state_file.is_file():
        raise RuntimeError("一键发本地登录会话不存在，请重新登录")
    with _session_lock:
        current = _backend_threads.get(account_id)
        if current and current.is_alive():
            return True
        worker = threading.Thread(
            target=_thread_target,
            args=(dict(account), account_id),
            daemon=True,
            name=f"oneclick-account-backend-{account_id}",
        )
        _backend_threads[account_id] = worker
        worker.start()
    return False


def close_all_backend_sessions(*, wait: bool = False) -> None:
    """主窗口退出时不阻塞；Playwright 子进程会随解释器结束关闭。"""

    if not wait:
        return
    with _session_lock:
        workers = list(_backend_threads.values())
    for worker in workers:
        worker.join(timeout=0.2)
