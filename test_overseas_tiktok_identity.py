# -*- coding: utf-8 -*-
"""TikTok 稳定账号身份合同的离线测试，不访问真实平台。"""

from __future__ import annotations

import asyncio
import unittest

from app_core.overseas_tiktok_identity import (
    TikTokIdentity,
    TikTokIdentityError,
    normalize_tiktok_handle,
    read_tiktok_identity,
    validate_identity_binding,
)


class _FakeProfileLink:
    def __init__(self, href: str, text: str = "", *, visible: bool = True) -> None:
        self.href = href
        self.text = text
        self.visible = visible

    async def is_visible(self, **_kwargs) -> bool:
        return self.visible

    async def get_attribute(self, name: str) -> str | None:
        return self.href if name == "href" else None

    async def inner_text(self, **_kwargs) -> str:
        return self.text


class _FakeProfileLinks:
    def __init__(self, links: list[_FakeProfileLink]) -> None:
        self.links = links

    async def count(self) -> int:
        return len(self.links)

    def nth(self, index: int) -> _FakeProfileLink:
        return self.links[index]


class _FakeTikTokPage:
    def __init__(self, links: list[_FakeProfileLink]) -> None:
        self.links = _FakeProfileLinks(links)

    def locator(self, selector: str) -> _FakeProfileLinks:
        self.selector = selector
        return self.links


class TikTokIdentityTests(unittest.TestCase):
    def test_normalize_handle_accepts_profile_url_and_at_prefix(self):
        self.assertEqual(normalize_tiktok_handle("https://www.tiktok.com/@Test.User"), "test.user")
        self.assertEqual(normalize_tiktok_handle("@Test.User"), "test.user")

    def test_existing_binding_rejects_another_logged_in_handle(self):
        account = {"accountReference": "expected.user"}
        identity = TikTokIdentity("other.user", "Other", "https://www.tiktok.com/@other.user")
        with self.assertRaises(TikTokIdentityError) as raised:
            validate_identity_binding(account, identity, allow_initial_bind=False)
        self.assertEqual(raised.exception.error_code, "tiktok_account_identity_mismatch")

    def test_empty_legacy_binding_can_be_initialized_once(self):
        identity = TikTokIdentity("expected.user", "Expected", "https://www.tiktok.com/@expected.user")
        self.assertEqual(
            validate_identity_binding({"accountReference": ""}, identity, allow_initial_bind=True),
            "expected.user",
        )

    def test_nonempty_invalid_legacy_binding_cannot_be_initialized(self):
        identity = TikTokIdentity("expected.user", "Expected", "https://www.tiktok.com/@expected.user")
        with self.assertRaises(TikTokIdentityError) as raised:
            validate_identity_binding(
                {"accountReference": "not a valid handle!"},
                identity,
                allow_initial_bind=True,
            )
        self.assertEqual(raised.exception.error_code, "tiktok_account_identity_mismatch")

    def test_existing_binding_accepts_the_same_normalized_handle(self):
        identity = TikTokIdentity(
            "@Expected.User", "Expected", "https://www.tiktok.com/@expected.user"
        )
        self.assertEqual(
            validate_identity_binding(
                {"accountReference": "https://www.tiktok.com/@EXPECTED.USER"},
                identity,
                allow_initial_bind=False,
            ),
            "expected.user",
        )

    def test_read_identity_uses_the_single_visible_stable_profile_link(self):
        page = _FakeTikTokPage(
            [_FakeProfileLink("/@expected.user", "Expected", visible=True)]
        )
        identity = asyncio.run(read_tiktok_identity(page))
        self.assertEqual(identity.handle, "expected.user")
        self.assertEqual(identity.display_name, "Expected")
        self.assertEqual(identity.profile_url, "https://www.tiktok.com/@expected.user")

    def test_read_identity_rejects_when_no_visible_profile_handle_exists(self):
        page = _FakeTikTokPage(
            [_FakeProfileLink("/@expected.user", "Expected", visible=False)]
        )
        with self.assertRaises(TikTokIdentityError) as raised:
            asyncio.run(read_tiktok_identity(page))
        self.assertEqual(raised.exception.error_code, "tiktok_account_invalid")

    def test_read_identity_rejects_conflicting_visible_profile_handles(self):
        page = _FakeTikTokPage(
            [
                _FakeProfileLink("/@expected.user", "Expected"),
                _FakeProfileLink("/@other.user", "Other"),
            ]
        )
        with self.assertRaises(TikTokIdentityError) as raised:
            asyncio.run(read_tiktok_identity(page))
        self.assertEqual(raised.exception.error_code, "tiktok_account_identity_ambiguous")


if __name__ == "__main__":
    unittest.main()
