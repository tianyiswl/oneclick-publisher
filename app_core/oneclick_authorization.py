"""一键发自身的平台授权执行器。

本模块只在用户点击一键发的“开始登录”后启动官方平台页面。浏览器资料和
storage state 均保存在一键发运行目录；不读取、迁移或调用任何其它客户端。
它不会上传素材、填写发布页或点击平台发布按钮。
"""

from __future__ import annotations

import asyncio
import queue
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import account_service
from .paths import COOKIE_DIR, USER_DATA_DIR, ensure_runtime_dirs


@dataclass(frozen=True)
class AuthorizationPlan:
    platform_type: int
    platform_name: str
    login_url: str
    profile_directory: Path


_DOMESTIC_LOGIN_URLS = {
    1: "https://creator.xiaohongshu.com/",
    2: "https://channels.weixin.qq.com/platform/",
    3: "https://creator.douyin.com/",
    4: "https://cp.kuaishou.com/",
    5: "https://member.bilibili.com/platform/home",
    10: "https://mp.weixin.qq.com/",
}

_OVERSEAS_LOGIN_URLS = {
    6: "https://www.tiktok.com/tiktokstudio/upload?lang=en",
    7: "https://studio.youtube.com/",
    8: "https://business.facebook.com/latest/composer/",
    9: "https://business.facebook.com/latest/composer/",
}

# 规则来自恢复包 local-rpa 的授权回执监听。这里只保留“已登录”判断，
# 不保存、显示或上传接口返回的账号资料。
_LOGIN_RESPONSE_PATHS = {
    1: "/galaxy/creator/home/personal_info",
    2: "mmfinderassistant-bin/auth/auth_data",
    3: "media/user/info",
    4: "creator/pc/home/userInfo",
    5: "x/passport-login/web/cookie/info",
    10: "/cgi-bin/bizlogin?action=login",
}


def authorization_browser_launch_options() -> dict[str, bool]:
    """绑定/重新登录必须让用户看到官方页面。"""

    return {"headless": False}


def session_check_browser_launch_options() -> dict[str, bool]:
    """登录态检测默认在后台静默运行。"""

    return {"headless": True}


def saved_identity_matches(account: dict, detected_name: object) -> bool:
    """已登录身份必须与本地账号一致，避免误用其他账号会话。"""

    detected = " ".join(str(detected_name or "").split())
    if not detected:
        return False
    expected = " ".join(
        str(account.get("userName") or account.get("profileName") or "").split()
    )
    placeholders = {
        "",
        "未命名账号",
        "B站账号",
        "哔哩哔哩账号",
    }
    return expected in placeholders or detected == expected


def wechat_home_session_confirms(
    account: dict,
    current_url: object,
    *,
    home_visible: bool,
    login_visible: bool,
    detected_name: object,
) -> bool:
    """用公众号后台首页与账号身份共同确认已登录会话。

    新版公众号后台在复用有效 storage_state 时不一定重新请求旧版
    ``bizlogin?action=login`` 接口，因此接口监听只能作为第一证据。首页
    兜底必须同时满足官方域名、后台首页、可见首页导航、无可见登录控件，
    并且页面账号名与本地账号记录一致；不能只凭 URL 判定登录成功。
    """

    parsed = urlsplit(str(current_url or ""))
    if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com":
        return False
    if parsed.path.rstrip("/") != "/cgi-bin/home":
        return False
    return bool(
        home_visible
        and not login_visible
        and saved_identity_matches(account, detected_name)
    )


def _safe_fragment(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "-", str(value or "").strip())
    return cleaned.strip("-_")[:48] or "default"


def authorization_plan(platform_type: int, profile_name: str) -> AuthorizationPlan:
    """返回一键发将要打开的官方授权页，不产生浏览器或网络访问。"""

    platform_type = int(platform_type)
    login_url = (
        _DOMESTIC_LOGIN_URLS.get(platform_type)
        or _OVERSEAS_LOGIN_URLS.get(platform_type)
    )
    if not login_url:
        platform = account_service.PLATFORMS.get(platform_type, "该平台")
        raise ValueError(f"{platform}的一键发授权执行器尚未迁入。")
    platform = account_service.PLATFORMS[platform_type]
    folder = USER_DATA_DIR / "oneclick-browser-profiles" / (
        f"{platform_type}-{_safe_fragment(profile_name)}"
    )
    return AuthorizationPlan(platform_type, platform, login_url, folder)


def login_response_confirms(platform_type: int, response_url: str, body: object) -> bool:
    """根据官方创作后台的身份回执确认登录；纯函数，便于离线测试。"""

    fragment = _LOGIN_RESPONSE_PATHS.get(int(platform_type))
    if not fragment or fragment not in str(response_url or ""):
        return False
    data = body if isinstance(body, dict) else {}
    if platform_type == 1:
        return bool(data.get("red_num") or data.get("user") or data.get("data"))
    if platform_type == 2:
        return bool(data.get("finderUser") or data.get("data", {}).get("finderUser"))
    if platform_type == 3:
        return bool(data.get("user") or data.get("data", {}).get("user"))
    if platform_type == 4:
        return bool(data.get("coreUserInfo") or data.get("data", {}).get("coreUserInfo"))
    if platform_type == 5:
        return True
    if platform_type == 10:
        result = data.get("base_resp") or data.get("baseResp") or data.get("data", {}).get("base_resp")
        return isinstance(result, dict) and result.get("ret") == 0
    return False


def identity_response_display_name(body: object) -> str | None:
    """从平台身份回执中提取可显示昵称；只保存昵称，不保存回执原文。"""

    wanted_keys = {"nickname", "nick_name", "user_name", "username", "account_name", "name"}
    blocked = {"首页", "发布", "登录", "活动管理", "账号管理", "创作中心", "平台"}

    def walk(value: object) -> str | None:
        if isinstance(value, dict):
            for key, candidate in value.items():
                if str(key).lower() in wanted_keys and isinstance(candidate, str):
                    text = " ".join(candidate.split())
                    if 2 <= len(text) <= 40 and not any(word in text for word in blocked):
                        return text
            for candidate in value.values():
                found = walk(candidate)
                if found:
                    return found
        elif isinstance(value, list):
            for candidate in value:
                found = walk(candidate)
                if found:
                    return found
        return None

    return walk(body)


@dataclass
class AuthorizationSession:
    """供 Qt 登录弹窗轮询的本地授权会话。"""

    platform_type: int
    profile_name: str
    update_mode: bool = False
    record_id: int | None = None
    queue: queue.Queue[str] = field(default_factory=queue.Queue)
    _save_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    _cancel_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    _login_detected: bool = field(default=False, init=False, repr=False)
    _detected_display_name: str | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        threading.Thread(target=self._thread_main, daemon=True, name="oneclick-authorization").start()

    def save(self) -> None:
        """由用户明确确认已完成官方页面登录后调用。"""

        self._save_requested.set()

    def cancel(self) -> None:
        self._cancel_requested.set()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # 保留在 UI 的安全提示中，不泄露会话数据。
            self.queue.put(f"ERROR:{type(exc).__name__}: {exc}")

    async def _run(self) -> None:
        plan = authorization_plan(self.platform_type, self.profile_name)
        ensure_runtime_dirs()
        plan.profile_directory.mkdir(parents=True, exist_ok=True)
        self.queue.put(f"准备在一键发独立浏览器会话中打开{plan.platform_name}官方登录页。")

        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        context = None
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(plan.profile_directory),
                **authorization_browser_launch_options(),
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("response", self._response_observer)

            # 部分平台会把扫码确认放在新标签页或弹层中完成。为该页面补上
            # 同一套回执监听，避免只监听首个页面而遗漏已登录信号。
            def observe_new_page(new_page) -> None:
                new_page.on("response", self._response_observer)

            context.on("page", observe_new_page)
            await page.goto(plan.login_url, wait_until="domcontentloaded", timeout=45_000)
            self.queue.put("BROWSER_OPENED")
            self.queue.put("请在官方页面完成登录；一键发检测到身份回执后会自动保存账号。")

            while not self._cancel_requested.is_set() and not self._save_requested.is_set():
                # B站扫码完成后不一定再次请求 cookie/info 接口，导致单靠网络
                # 回执无法收尾。仅对已知的创作中心身份区做第二道确认：必须
                # 读到有效账号昵称，绝不以“页面已打开”作为已登录判断。
                await self._detect_logged_in_page(page)
                await asyncio.sleep(0.25)

            if self._cancel_requested.is_set():
                self.queue.put("CANCELLED")
                return

            state_file = COOKIE_DIR / (
                f"oneclick_{self.platform_type}_{uuid.uuid4().hex}.json"
            )
            await context.storage_state(path=str(state_file))
            account_id = account_service.save_oneclick_authorized_account(
                self.platform_type,
                self.profile_name,
                state_file.name,
                record_id=self.record_id if self.update_mode else None,
                display_name=self._detected_display_name,
            )
            # 登录页仍保留着平台已经渲染好的身份区，直接回填头像和昵称。
            # 回填不属于登录成功条件，因此页面结构变动时也不能阻断账号保存。
            try:
                avatar_name, page_display_name = await account_service.capture_account_identity_from_page(
                    account_id, page, self.platform_type
                )
                if avatar_name:
                    self.queue.put("ACCOUNT_AVATAR_SAVED")
                if page_display_name:
                    self._detected_display_name = page_display_name
            except Exception:
                self.queue.put("ACCOUNT_AVATAR_PENDING")
            self.queue.put(f"ACCOUNT_SAVED:{account_id}")
        finally:
            if context:
                await context.close()
            await playwright.stop()

    def _response_observer(self, response) -> None:
        """Playwright 回调运行在授权线程的事件循环中。"""

        if self._login_detected:
            return
        asyncio.create_task(self._inspect_login_response(response))

    async def _inspect_login_response(self, response) -> None:
        if self._login_detected:
            return
        try:
            response_url = str(response.url)
            fragment = _LOGIN_RESPONSE_PATHS.get(self.platform_type, "")
            if not fragment or fragment not in response_url:
                return
            body = await response.json()
        except Exception:
            return
        if not login_response_confirms(self.platform_type, response_url, body):
            return
        self._detected_display_name = identity_response_display_name(body)
        self._login_detected = True
        self.queue.put("LOGIN_DETECTED")
        # 等待平台写入所有 Cookie/storage，再自动保存一键发本地会话。
        await asyncio.sleep(1.5)
        self._save_requested.set()

    async def _detect_logged_in_page(self, page) -> None:
        """用创作中心的可见身份区补充登录判定。

        网络身份回执仍然是首选。此处仅用于 B站已确认的页面行为：登录成功后
        创作中心头部会出现账号昵称；登录前不会出现可用昵称。这样既能自动保存，
        又不会将登录页、二维码页或普通导航文字误存为账号。
        """

        if self._login_detected or int(self.platform_type) != 5:
            return
        try:
            display_name = await account_service._detect_display_name(page, self.platform_type)
        except Exception:
            return
        if not display_name:
            return
        self._detected_display_name = display_name
        self._login_detected = True
        self.queue.put("LOGIN_DETECTED")
        # 与网络回执路径一致：留出 Cookie / localStorage 最后落盘的时间。
        await asyncio.sleep(1.5)
        self._save_requested.set()


def start_authorization(
    platform_type: int,
    profile_name: str,
    *,
    update_mode: bool = False,
    record_id: int | None = None,
) -> AuthorizationSession:
    """创建并启动一键发本地授权会话。仅由用户在 UI 中显式触发。"""

    session = AuthorizationSession(
        platform_type=int(platform_type),
        profile_name=str(profile_name).strip(),
        update_mode=bool(update_mode),
        record_id=record_id,
    )
    session.start()
    return session


async def _verify_saved_session_async(account: dict) -> bool:
    """在后台访问官方页面复核已保存会话。"""

    platform_type = int(account.get("type") or 0)
    plan = authorization_plan(platform_type, str(account.get("profileName") or ""))
    state_file = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not state_file.is_file():
        return False

    # 恢复的蚁小二海外平台代码已包含各自的官方后台判定。
    # 直接复用这些只读检测，不用国内平台的通用页面文字猜测。
    if platform_type in account_service.OVERSEAS_PLATFORM_TYPES:
        from myUtils.auth import check_cookie

        return bool(await check_cookie(platform_type, state_file.name, preview=False))

    from playwright.async_api import async_playwright

    confirmed = asyncio.Event()
    playwright = await async_playwright().start()
    browser = None
    context = None
    try:
        # 这里只读取已保存 storage state 并监听官方身份回执，
        # 不填写、不保存新会话，因此默认静默运行。失效时由 UI
        # 单独打开可见官方页面，避免每次检测都弹窗。
        browser = await playwright.chromium.launch(
            **session_check_browser_launch_options()
        )
        context = await browser.new_context(storage_state=str(state_file))
        page = await context.new_page()

        def observe(response) -> None:
            async def inspect() -> None:
                try:
                    fragment = _LOGIN_RESPONSE_PATHS.get(platform_type, "")
                    if not fragment or fragment not in str(response.url):
                        return
                    body = await response.json()
                    if login_response_confirms(platform_type, str(response.url), body):
                        confirmed.set()
                except Exception:
                    return

            asyncio.create_task(inspect())

        page.on("response", observe)
        await page.goto(plan.login_url, wait_until="domcontentloaded", timeout=45_000)
        # 大多数平台通过身份接口回执确认。B站创作中心
        # 首页在已登录时不一定重新请求 cookie/info，但官方 nav
        # 身份接口可读到当前昵称。两种证据任一成立即结束，
        # 不用页面上含糊的“登录”文字作为判定。
        for _attempt in range(24):
            if confirmed.is_set():
                return True
            if platform_type == 5:
                detected_name = await account_service._detect_display_name(
                    page,
                    platform_type,
                )
                if saved_identity_matches(account, detected_name):
                    return True
            if platform_type == 10:
                detected_name = await account_service._detect_display_name(
                    page,
                    platform_type,
                )
                home_link = page.locator(
                    'a[href*="/cgi-bin/home"], a[href*="cgi-bin/home"]'
                ).first
                login_controls = page.locator(
                    '.login__type__container__scan, .login_qrcode, .qrcode, '
                    'button:has-text("登录"), a:has-text("登录")'
                )
                try:
                    home_visible = bool(
                        await home_link.count()
                        and await home_link.is_visible(timeout=300)
                    )
                except Exception:
                    home_visible = False
                login_visible = False
                try:
                    for index in range(min(await login_controls.count(), 8)):
                        if await login_controls.nth(index).is_visible(timeout=200):
                            login_visible = True
                            break
                except Exception:
                    login_visible = True
                if wechat_home_session_confirms(
                    account,
                    page.url,
                    home_visible=home_visible,
                    login_visible=login_visible,
                    detected_name=detected_name,
                ):
                    return True
            await asyncio.sleep(0.5)
        return False
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        await playwright.stop()


def verify_saved_session(account: dict) -> bool:
    """同步封装；正常检测不显示浏览器。"""

    return bool(asyncio.run(_verify_saved_session_async(dict(account))))
