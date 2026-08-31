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
from urllib.parse import urljoin, urlsplit

from . import account_service
from .paths import COOKIE_DIR, USER_DATA_DIR, ensure_runtime_dirs
from .overseas_meta_errors import FacebookPagePublishError
from .overseas_meta_page_identity import (
    FacebookPageIdentity,
    resolve_facebook_page_selection,
)


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
    4: "https://cp.kuaishou.com/profile",
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

    return bool(
        wechat_authorization_page_confirms(
            current_url,
            home_visible=home_visible,
            login_visible=login_visible,
            detected_name=detected_name,
        )
        and saved_identity_matches(account, detected_name)
    )


def wechat_authorization_page_confirms(
    current_url: object,
    *,
    home_visible: bool,
    login_visible: bool,
    detected_name: object,
) -> bool:
    """确认公众号授权页已经从登录态进入可用后台首页。

    首次绑定还没有本地账号名可供比对，因此必须组合官方域名、后台首页、
    可见首页导航、登录控件消失和有效公众号名称五项证据。任何单项都不能
    触发自动保存，避免把二维码页或跳转中间页误判为登录成功。
    """

    parsed = urlsplit(str(current_url or ""))
    if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com":
        return False
    if parsed.path.rstrip("/") != "/cgi-bin/home":
        return False
    display_name = " ".join(str(detected_name or "").split())
    return bool(
        home_visible
        and not login_visible
        and account_service._is_display_name(display_name)
    )


def kuaishou_passport_page_confirms(current_url: object) -> bool:
    """确认快手门户已经跳到官方账号登录页。"""

    parsed = urlsplit(str(current_url or ""))
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "passport.kuaishou.com"
        and parsed.path.rstrip("/") == "/pc/account/login"
    )


async def _enter_kuaishou_login_page(page, platform_type: int) -> bool:
    """从快手创作者门户进入官方登录页，不写死易变化的回跳参数。

    快手当前的未登录门户会渲染一个“立即登录”链接。它的官方入口负责
    生成 ``sid`` 和 ``callback``，因此这里只点击门户自己的链接，并校验
    最终域名和路径；已有登录态或门户不再显示入口时不做任何操作。
    """

    if int(platform_type) != 4:
        return False
    if kuaishou_passport_page_confirms(getattr(page, "url", "")):
        return False

    current = urlsplit(str(getattr(page, "url", "") or ""))
    if current.scheme != "https" or current.hostname != "cp.kuaishou.com":
        raise RuntimeError("快手授权入口不是官方创作者中心，已停止自动跳转。")

    login_link = None
    for _attempt in range(12):
        candidates = page.locator('a[href*="/rest/infra/logout"]')
        visible_matches = []
        for index in range(min(await candidates.count(), 8)):
            candidate = candidates.nth(index)
            try:
                if not await candidate.is_visible(timeout=250):
                    continue
                text = "".join((await candidate.inner_text()).split())
            except Exception:
                continue
            if text in {"登录", "立即登录"}:
                visible_matches.append(candidate)
        if len(visible_matches) > 1:
            raise RuntimeError("快手门户出现多个可见登录入口，已停止自动点击。")
        if visible_matches:
            login_link = visible_matches[0]
            break
        # 已登录会话可能在等待期间由门户自行跳转，不能再点击旧入口。
        current = urlsplit(str(getattr(page, "url", "") or ""))
        if current.hostname != "cp.kuaishou.com":
            return False
        await asyncio.sleep(0.25)

    if login_link is None:
        return False

    href = await login_link.get_attribute("href")
    target = urlsplit(urljoin(str(page.url), str(href or "")))
    if not (
        target.scheme == "https"
        and target.hostname == "cp.kuaishou.com"
        and target.path.rstrip("/") == "/rest/infra/logout"
    ):
        raise RuntimeError("快手门户登录入口不是预期的官方链接，已停止自动点击。")

    await login_link.click(timeout=5_000)
    await page.wait_for_url(
        re.compile(r"^https://passport\.kuaishou\.com/pc/account/login(?:/|\?|$)"),
        timeout=15_000,
    )
    if not kuaishou_passport_page_confirms(page.url):
        raise RuntimeError("快手门户没有进入官方账号登录页，已停止授权。")
    return True


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
            if await _enter_kuaishou_login_page(page, self.platform_type):
                self.queue.put("已自动进入快手官方登录页。")
            self.queue.put("BROWSER_OPENED")
            self.queue.put("请在官方页面完成登录；一键发检测到身份回执后会自动保存账号。")

            while not self._cancel_requested.is_set() and not self._save_requested.is_set():
                # B站扫码完成后不一定再次请求 cookie/info 接口，导致单靠网络
                # 回执无法收尾。仅对已知的创作中心身份区做第二道确认：必须
                # 读到有效账号昵称，绝不以“页面已打开”作为已登录判断。
                # 扫码确认可能在原标签页跳转，也可能打开同一持久化会话中的
                # 新标签页。逐页检查严格身份证据，避免只盯首个登录页而漏掉
                # 已经进入后台首页的公众号会话。
                for candidate_page in reversed(context.pages):
                    if candidate_page.is_closed():
                        continue
                    await self._detect_logged_in_page(candidate_page)
                    if self._login_detected:
                        page = candidate_page
                        break
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

        platform_type = int(self.platform_type)
        if self._login_detected or platform_type not in {5, 10}:
            return
        if platform_type == 10:
            try:
                display_name = await account_service._detect_display_name(
                    page,
                    platform_type,
                )
                home_markers = page.locator(
                    'a[href*="/cgi-bin/home"], '
                    '.weui-desktop-menu__link:has-text("首页"), '
                    '.weui-desktop-menu__name:has-text("首页")'
                )
                login_controls = page.locator(
                    '.login__type__container__scan, .login_qrcode, .qrcode, '
                    'button:has-text("登录"), a:has-text("登录")'
                )
                home_visible = False
                for index in range(min(await home_markers.count(), 8)):
                    if await home_markers.nth(index).is_visible(timeout=250):
                        home_visible = True
                        break
                login_visible = False
                for index in range(min(await login_controls.count(), 8)):
                    if await login_controls.nth(index).is_visible(timeout=200):
                        login_visible = True
                        break
            except Exception:
                return
            if not wechat_authorization_page_confirms(
                page.url,
                home_visible=home_visible,
                login_visible=login_visible,
                detected_name=display_name,
            ):
                return
            self._detected_display_name = display_name
            self._login_detected = True
            self.queue.put("LOGIN_DETECTED")
            # 首页已经稳定渲染，但仍为 Cookie / localStorage 留出最终落盘时间。
            await asyncio.sleep(1.5)
            self._save_requested.set()
            return
        try:
            display_name = await account_service._detect_display_name(page, platform_type)
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


async def _verify_saved_session_async(account: dict) -> bool | FacebookPageIdentity:
    """在后台访问官方页面复核已保存会话。"""

    platform_type = int(account.get("type") or 0)
    plan = authorization_plan(platform_type, str(account.get("profileName") or ""))
    expected_facebook_page_id = None
    if platform_type == 9:
        expected_facebook_page_id = (
            account_service.validate_saved_facebook_page_account(account)
        )
    state_file = COOKIE_DIR / Path(str(account.get("filePath") or "")).name
    if not state_file.is_file():
        if platform_type == 9:
            raise FacebookPagePublishError(
                "facebook_page_identity_mismatch",
                "Facebook Page 本地登录会话不存在，请重新绑定。",
            )
        return False

    # 恢复的蚁小二海外平台代码已包含各自的官方后台判定。
    # 直接复用这些只读检测，不用国内平台的通用页面文字猜测。
    if platform_type in account_service.OVERSEAS_PLATFORM_TYPES:
        from myUtils.auth import check_cookie

        if platform_type == 9:
            expected_page_id = expected_facebook_page_id
            identity = await check_cookie(
                9,
                state_file.name,
                preview=False,
                account_reference=expected_page_id,
            )
            if not isinstance(identity, FacebookPageIdentity):
                raise FacebookPagePublishError(
                    "facebook_page_identity_mismatch",
                    "保存的 Facebook Page 无法精确回读，已停止操作。",
                )
            return resolve_facebook_page_selection(
                (identity,),
                expected_page_id,
            )
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


def verify_saved_session(account: dict) -> bool | FacebookPageIdentity:
    """同步封装；正常检测不显示浏览器。"""

    return asyncio.run(_verify_saved_session_async(dict(account)))
