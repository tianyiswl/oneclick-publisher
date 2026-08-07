# -*- coding: utf-8 -*-
"""抖音账号昵称来源的离线回归测试。"""

import asyncio
import unittest

from app_core.account_service import _detect_display_name
from app_core.oneclick_authorization import identity_response_display_name


class _UnexpectedPageAccess:
    """抖音昵称识别不应读取页面任意节点。"""

    def locator(self, *_args, **_kwargs):
        raise AssertionError("抖音昵称不得从页面选择器读取")

    async def evaluate(self, *_args, **_kwargs):
        raise AssertionError("抖音昵称不得从页面文字猜测")


class DouyinAccountIdentityTests(unittest.TestCase):
    def test_douyin_display_name_only_uses_official_identity_response(self) -> None:
        body = {"data": {"user": {"nickname": "正确的抖音昵称"}}}
        self.assertEqual(identity_response_display_name(body), "正确的抖音昵称")

    def test_douyin_page_text_never_overrides_identity_response(self) -> None:
        value = asyncio.run(_detect_display_name(_UnexpectedPageAccess(), 3))
        self.assertIsNone(value)


if __name__ == "__main__":
    unittest.main()
