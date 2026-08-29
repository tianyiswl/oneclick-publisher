"""一键发账号授权入口。

国内平台使用一键发已验收的授权执行器；Meta 复用已恢复的浏览器登录流程，
TikTok 使用系统浏览器专用临时资料，YouTube 使用 Google 官方桌面 OAuth。
"""

from __future__ import annotations

import asyncio
import queue
import threading
from dataclasses import dataclass

import conf

from . import account_service
from .oneclick_authorization import start_authorization
from .overseas_meta_page_identity import (
    FacebookPageIdentity,
    facebook_page_v1_enabled,
    resolve_facebook_page_selection,
)


@dataclass(frozen=True, slots=True)
class FacebookPageSelectionRequest:
    pages: tuple[FacebookPageIdentity, ...]


class RecoveredOverseasLoginSession:
    """把恢复的海外登录协程适配为当前登录弹窗的会话接口。"""

    def __init__(
        self,
        platform_type: int,
        profile_name: str,
        *,
        update_mode: bool = False,
        record_id: int | None = None,
        existing_account: dict | None = None,
    ) -> None:
        self.platform_type = account_service.login_platform_type(platform_type)
        self.profile_name = str(profile_name or "").strip()
        self.update_mode = bool(update_mode)
        self.record_id = record_id
        self.existing_account = dict(existing_account) if existing_account else None
        self.queue: queue.Queue[object] = queue.Queue()
        self._cancel_requested = threading.Event()
        self._selection_requests: queue.Queue[FacebookPageSelectionRequest] = queue.Queue()
        self._selection_responses: queue.Queue[str] = queue.Queue()
        self._selection_lock = threading.Lock()
        self._active_selection_request: FacebookPageSelectionRequest | None = None
        self._complete = threading.Event()
        self._failure: BaseException | None = None
        self.manual_save_supported = self.platform_type != 9

    def start(self) -> None:
        if self.platform_type == 9 and not facebook_page_v1_enabled():
            raise RuntimeError("Facebook Page V1 默认关闭，当前不能启动登录。")
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

    async def next_selection_request(self) -> FacebookPageSelectionRequest:
        return await asyncio.to_thread(self._selection_requests.get)

    def select_facebook_page(self, page_id: str) -> None:
        with self._selection_lock:
            request = self._active_selection_request
        if request is None:
            resolve_facebook_page_selection((), page_id)
        selected = resolve_facebook_page_selection(request.pages, page_id)
        self._selection_responses.put(selected.page_id)

    async def _request_facebook_page_selection(
        self,
        pages: tuple[FacebookPageIdentity, ...],
    ) -> FacebookPageIdentity:
        request = FacebookPageSelectionRequest(tuple(pages))
        with self._selection_lock:
            self._active_selection_request = request
        self._selection_requests.put(request)
        self.queue.put(request)
        try:
            while not self._cancel_requested.is_set():
                try:
                    selected_page_id = self._selection_responses.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.05)
                    continue
                return resolve_facebook_page_selection(
                    request.pages,
                    selected_page_id,
                )
            raise asyncio.CancelledError
        finally:
            with self._selection_lock:
                if self._active_selection_request is request:
                    self._active_selection_request = None

    async def wait_until_complete(self) -> None:
        await asyncio.to_thread(self._complete.wait)
        if self._failure is not None:
            raise self._failure

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            self.queue.put("CANCELLED")
        except Exception as exc:
            self._failure = exc
            self.queue.put(f"ERROR:{type(exc).__name__}: {exc}")
        finally:
            self._complete.set()

    async def _run(self) -> None:
        from myUtils.login import (
            facebook_page_cookie_gen,
            meta_cookie_gen,
            youtube_cookie_gen,
        )

        callback = {
            7: youtube_cookie_gen,
            8: meta_cookie_gen,
            9: facebook_page_cookie_gen,
        }.get(self.platform_type)
        if callback is None:
            raise ValueError("当前海外平台登录执行器尚未恢复")
        args = (
            self.profile_name,
            self.queue,
            self.update_mode,
            self.record_id,
            self._cancel_requested,
            False,
        )
        if self.platform_type == 9:
            await callback(
                *args,
                selection_callback=self._request_facebook_page_selection,
                expected_account=self.existing_account,
            )
        else:
            await callback(*args)


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
    requested_type = int(platform_type)
    if requested_type == 9 and not facebook_page_v1_enabled():
        raise RuntimeError("Facebook Page V1 默认关闭，当前不能启动登录。")
    login_type = account_service.login_platform_type(platform_type)
    if login_type == 6:
        from .overseas_tiktok_system_login import TikTokSystemBrowserLoginSession

        existing_account = (
            account_service.get_managed_account(int(record_id))
            if update_mode and record_id is not None
            else None
        )
        session = TikTokSystemBrowserLoginSession(
            profile_name=profile_name,
            update_mode=update_mode,
            record_id=record_id,
            existing_account=existing_account,
        )
        session.start()
        return session
    if login_type == 7:
        from .overseas_youtube_login import YouTubeOAuthLoginSession

        existing_account = (
            account_service.get_managed_account(int(record_id))
            if update_mode and record_id is not None
            else None
        )
        session = YouTubeOAuthLoginSession(
            client_id=conf.YOUTUBE_OAUTH_CLIENT_ID,
            profile_name=profile_name,
            update_mode=update_mode,
            record_id=record_id,
            existing_account=existing_account,
            account_saver=account_service.save_youtube_oauth_account,
        )
        session.start()
        return session
    if login_type in account_service.OVERSEAS_PLATFORM_TYPES:
        existing_account = (
            account_service.get_managed_account(int(record_id))
            if update_mode and record_id is not None
            else None
        )
        session = RecoveredOverseasLoginSession(
            login_type,
            profile_name,
            update_mode=update_mode,
            record_id=record_id,
            existing_account=existing_account,
        )
        session.start()
        return session
    return start_authorization(
        platform_type,
        profile_name,
        update_mode=update_mode,
        record_id=record_id,
    )
