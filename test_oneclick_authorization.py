# -*- coding: utf-8 -*-
"""一键发授权执行器的离线测试：不启动浏览器，不访问平台。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from app_core.oneclick_authorization import (
    AuthorizationPlan,
    AuthorizationSession,
    authorization_browser_launch_options,
    authorization_plan,
    identity_response_display_name,
    login_response_confirms,
    saved_identity_matches,
    session_check_browser_launch_options,
    wechat_authorization_page_confirms,
    wechat_home_session_confirms,
)


class _VisibleNode:
    def __init__(self, visible: bool) -> None:
        self.visible = visible

    async def is_visible(self, timeout: int = 0) -> bool:
        del timeout
        return self.visible


class _NodeList:
    def __init__(self, values: list[bool]) -> None:
        self.values = values

    async def count(self) -> int:
        return len(self.values)

    def nth(self, index: int) -> _VisibleNode:
        return _VisibleNode(self.values[index])


class _WechatHomePage:
    url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=redacted"

    def locator(self, selector: str) -> _NodeList:
        if "login__type__container__scan" in selector:
            return _NodeList([])
        return _NodeList([True])


class _KuaishouLoginLink:
    def __init__(self, page) -> None:
        self.page = page
        self.clicked = False

    async def is_visible(self, timeout: int = 0) -> bool:
        del timeout
        return True

    async def inner_text(self) -> str:
        return "立即登录"

    async def get_attribute(self, name: str) -> str | None:
        return "/rest/infra/logout" if name == "href" else None

    async def click(self, timeout: int = 0) -> None:
        del timeout
        self.clicked = True
        self.page.url = "https://passport.kuaishou.com/pc/account/login/?sid=test"


class _KuaishouLoginNodes:
    def __init__(self, link: _KuaishouLoginLink) -> None:
        self.link = link

    async def count(self) -> int:
        return 1

    def nth(self, index: int) -> _KuaishouLoginLink:
        if index != 0:
            raise IndexError(index)
        return self.link


class _KuaishouAuthorizationPage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.login_link = _KuaishouLoginLink(self)
        self.goto_urls: list[str] = []

    def on(self, *_args) -> None:
        return None

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_urls.append(url)
        self.url = "https://cp.kuaishou.com/profile"

    def locator(self, selector: str) -> _KuaishouLoginNodes:
        self.last_selector = selector
        return _KuaishouLoginNodes(self.login_link)

    async def wait_for_url(self, _pattern, timeout: int = 0) -> None:
        del timeout
        return None

    def is_closed(self) -> bool:
        return False


class _KuaishouAuthorizationContext:
    def __init__(self, page: _KuaishouAuthorizationPage) -> None:
        self.pages = [page]
        self.closed = False

    def on(self, *_args) -> None:
        return None

    async def new_page(self):
        return self.pages[0]

    async def close(self) -> None:
        self.closed = True


class _KuaishouAuthorizationChromium:
    def __init__(self, context: _KuaishouAuthorizationContext) -> None:
        self.context = context

    async def launch_persistent_context(self, **_kwargs):
        return self.context


class _KuaishouAuthorizationPlaywright:
    def __init__(self, context: _KuaishouAuthorizationContext) -> None:
        self.chromium = _KuaishouAuthorizationChromium(context)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _KuaishouAuthorizationManager:
    def __init__(self, playwright: _KuaishouAuthorizationPlaywright) -> None:
        self.playwright = playwright

    async def start(self):
        return self.playwright


class OneClickAuthorizationTests(unittest.TestCase):
    def test_binding_is_visible_but_session_check_is_headless(self) -> None:
        self.assertEqual(
            authorization_browser_launch_options(),
            {"headless": False},
        )
        self.assertEqual(
            session_check_browser_launch_options(),
            {"headless": True},
        )

    def test_bilibili_saved_identity_requires_expected_account(self) -> None:
        self.assertTrue(
            saved_identity_matches({"userName": "墨白手记"}, "墨白手记")
        )
        self.assertFalse(
            saved_identity_matches({"userName": "墨白手记"}, "其他账号")
        )
        self.assertTrue(
            saved_identity_matches({"userName": "B站账号"}, "真实昵称")
        )

    def test_six_domestic_platforms_use_official_urls_and_local_profiles(self) -> None:
        expected_hosts = {
            1: "creator.xiaohongshu.com",
            2: "channels.weixin.qq.com",
            3: "creator.douyin.com",
            4: "cp.kuaishou.com",
            5: "member.bilibili.com",
            10: "mp.weixin.qq.com",
        }
        for platform_type, host in expected_hosts.items():
            plan = authorization_plan(platform_type, "一键发 测试主体")
            self.assertIn(host, plan.login_url)
            self.assertIn("oneclick-browser-profiles", str(plan.profile_directory))

    def test_overseas_platforms_use_recovered_official_backends(self) -> None:
        expected_hosts = {
            6: "tiktok.com",
            7: "studio.youtube.com",
            8: "business.facebook.com",
            9: "business.facebook.com",
        }
        for platform_type, host in expected_hosts.items():
            self.assertIn(
                host,
                authorization_plan(platform_type, "海外主体").login_url,
            )

    def test_unknown_platform_is_rejected_without_browser_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "尚未迁入"):
            authorization_plan(99, "未知主体")

    def test_login_response_rules_require_platform_identity_evidence(self) -> None:
        self.assertTrue(login_response_confirms(3, "https://creator.douyin.com/media/user/info", {"user": {"id": "x"}}))
        self.assertTrue(login_response_confirms(1, "https://creator.xiaohongshu.com/galaxy/creator/home/personal_info", {"data": {"id": "x"}}))
        self.assertTrue(login_response_confirms(10, "https://mp.weixin.qq.com/cgi-bin/bizlogin?action=login", {"base_resp": {"ret": 0}}))
        self.assertFalse(login_response_confirms(10, "https://mp.weixin.qq.com/cgi-bin/bizlogin?action=login", {"base_resp": {"ret": -1}}))
        self.assertFalse(login_response_confirms(2, "https://channels.weixin.qq.com/other", {"finderUser": {}}))

    def test_wechat_home_fallback_requires_matching_identity_and_visible_home(self) -> None:
        account = {"userName": "硅基进化", "profileName": "硅基进化"}
        self.assertTrue(
            wechat_home_session_confirms(
                account,
                "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=redacted",
                home_visible=True,
                login_visible=False,
                detected_name="硅基进化",
            )
        )
        for kwargs in (
            {"detected_name": "其他公众号"},
            {"home_visible": False},
            {"login_visible": True},
        ):
            evidence = {
                "home_visible": True,
                "login_visible": False,
                "detected_name": "硅基进化",
            }
            evidence.update(kwargs)
            self.assertFalse(
                wechat_home_session_confirms(
                    account,
                    "https://mp.weixin.qq.com/cgi-bin/home?t=home/index",
                    **evidence,
                )
            )
        self.assertFalse(
            wechat_home_session_confirms(
                account,
                "https://example.com/cgi-bin/home",
                home_visible=True,
                login_visible=False,
                detected_name="硅基进化",
            )
        )

    def test_wechat_first_authorization_requires_complete_home_evidence(self) -> None:
        url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=redacted"
        self.assertTrue(
            wechat_authorization_page_confirms(
                url,
                home_visible=True,
                login_visible=False,
                detected_name="硅基进化",
            )
        )
        for evidence in (
            {"home_visible": False},
            {"login_visible": True},
            {"detected_name": ""},
            {"detected_name": "公众号后台"},
        ):
            values = {
                "home_visible": True,
                "login_visible": False,
                "detected_name": "硅基进化",
            }
            values.update(evidence)
            self.assertFalse(
                wechat_authorization_page_confirms(url, **values)
            )
        self.assertFalse(
            wechat_authorization_page_confirms(
                "https://example.com/cgi-bin/home",
                home_visible=True,
                login_visible=False,
                detected_name="硅基进化",
            )
        )

    def test_bilibili_cookie_response_confirms(self) -> None:
        self.assertTrue(
            login_response_confirms(
                5,
                "https://passport.bilibili.com/x/passport-login/web/cookie/info",
                {"code": 0},
            )
        )

    def test_video_channel_identity_response_extracts_display_name(self) -> None:
        body = {"data": {"finderUser": {"nickName": "视频号测试账号"}}}
        self.assertEqual(identity_response_display_name(body), "视频号测试账号")


class OneClickWechatAuthorizationAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_wechat_home_detection_requests_automatic_save(self) -> None:
        session = AuthorizationSession(10, "测试主体")

        with patch(
            "app_core.oneclick_authorization.account_service._detect_display_name",
            new=AsyncMock(return_value="硅基进化"),
        ), patch(
            "app_core.oneclick_authorization.asyncio.sleep",
            new=AsyncMock(),
        ):
            await session._detect_logged_in_page(_WechatHomePage())

        self.assertTrue(session._login_detected)
        self.assertTrue(session._save_requested.is_set())
        self.assertEqual(session._detected_display_name, "硅基进化")
        self.assertEqual(session.queue.get_nowait(), "LOGIN_DETECTED")


class OneClickKuaishouAuthorizationAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_portal_login_automatically_enters_official_passport_page(self) -> None:
        """快手授权不能停在仍需人工二次点击的创作者门户页。"""

        page = _KuaishouAuthorizationPage()
        context = _KuaishouAuthorizationContext(page)
        playwright = _KuaishouAuthorizationPlaywright(context)
        session = AuthorizationSession(4, "快手测试主体")
        # 只验证打开页面的第一阶段；进入登录页后立刻结束离线会话。
        session._cancel_requested.set()
        with TemporaryDirectory() as temporary, patch(
            "app_core.oneclick_authorization.authorization_plan",
            return_value=AuthorizationPlan(
                4,
                "快手",
                "https://cp.kuaishou.com/profile",
                Path(temporary),
            ),
        ), patch(
            "playwright.async_api.async_playwright",
            return_value=_KuaishouAuthorizationManager(playwright),
        ):
            await session._run()

        self.assertEqual(page.goto_urls, ["https://cp.kuaishou.com/profile"])
        self.assertTrue(page.login_link.clicked)
        self.assertEqual(
            page.url,
            "https://passport.kuaishou.com/pc/account/login/?sid=test",
        )
        self.assertTrue(context.closed)
        self.assertTrue(playwright.stopped)


if __name__ == "__main__":
    unittest.main()
