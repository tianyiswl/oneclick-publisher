"""一键发账号授权入口。

国内平台使用一键发已验收的授权执行器；海外平台直接复用已恢复的
蚁小二浏览器登录流程，但会话和账号仍保存在一键发的用户数据目录。
"""

from __future__ import annotations

import asyncio
import queue
import threading

from . import account_service

from .oneclick_authorization import start_authorization


class RecoveredOverseasLoginSession:
    """把恢复的海外登录协程适配为当前登录弹窗的会话接口。"""

    def __init__(
        self,
        platform_type: int,
        profile_name: str,
        *,
        update_mode: bool = False,
        record_id: int | None = None,
    ) -> None:
        self.platform_type = account_service.login_platform_type(platform_type)
        self.profile_name = str(profile_name or "").strip()
        self.update_mode = bool(update_mode)
        self.record_id = record_id
        self.queue: queue.Queue[str] = queue.Queue()
        self._cancel_requested = threading.Event()

    def start(self) -> None:
        threading.Thread(
            target=self._thread_main,
            daemon=True,
            name=f"oneclick-overseas-login-{self.platform_type}",
        ).start()

    def save(self) -> None:
        # 恢复流程只在登录和发布入口校验通过后自动保存，
        # 不允许手动按钮跳过安全检查。
        self.queue.put("海外平台会在登录与发布入口校验通过后自动保存。")

    def cancel(self) -> None:
        self._cancel_requested.set()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self.queue.put(f"ERROR:{type(exc).__name__}: {exc}")

    async def _run(self) -> None:
        from myUtils.login import meta_cookie_gen, tiktok_cookie_gen, youtube_cookie_gen

        callback = {
            6: tiktok_cookie_gen,
            7: youtube_cookie_gen,
            8: meta_cookie_gen,
        }.get(self.platform_type)
        if callback is None:
            raise ValueError("当前海外平台登录执行器尚未恢复")
        await callback(
            self.profile_name,
            self.queue,
            self.update_mode,
            self.record_id,
            self._cancel_requested,
            False,
        )


def start_login(
    platform_type: int,
    profile_name: str,
    *,
    update_mode: bool = False,
    record_id: int | None = None,
    background_mode: bool = False,
):
    """用户显式点击后打开官方授权页；不支持隐藏登录，避免误导性后台操作。"""

    del background_mode
    login_type = account_service.login_platform_type(platform_type)
    if login_type in account_service.OVERSEAS_PLATFORM_TYPES:
        session = RecoveredOverseasLoginSession(
            login_type,
            profile_name,
            update_mode=update_mode,
            record_id=record_id,
        )
        session.start()
        return session
    return start_authorization(
        platform_type,
        profile_name,
        update_mode=update_mode,
        record_id=record_id,
    )
