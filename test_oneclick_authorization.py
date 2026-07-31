# -*- coding: utf-8 -*-
"""一键发授权执行器的离线测试：不启动浏览器，不访问平台。"""

import unittest

from app_core.oneclick_authorization import (
    authorization_browser_launch_options,
    authorization_plan,
    identity_response_display_name,
    login_response_confirms,
    saved_identity_matches,
    session_check_browser_launch_options,
)


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

    def test_non_migrated_platform_is_rejected_without_browser_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "尚未迁入"):
            authorization_plan(6, "海外主体")

    def test_login_response_rules_require_platform_identity_evidence(self) -> None:
        self.assertTrue(login_response_confirms(3, "https://creator.douyin.com/media/user/info", {"user": {"id": "x"}}))
        self.assertTrue(login_response_confirms(1, "https://creator.xiaohongshu.com/galaxy/creator/home/personal_info", {"data": {"id": "x"}}))
        self.assertTrue(login_response_confirms(10, "https://mp.weixin.qq.com/cgi-bin/bizlogin?action=login", {"base_resp": {"ret": 0}}))
        self.assertFalse(login_response_confirms(10, "https://mp.weixin.qq.com/cgi-bin/bizlogin?action=login", {"base_resp": {"ret": -1}}))
        self.assertFalse(login_response_confirms(2, "https://channels.weixin.qq.com/other", {"finderUser": {}}))

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


if __name__ == "__main__":
    unittest.main()
