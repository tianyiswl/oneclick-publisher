# -*- coding: utf-8 -*-
"""Instagram 专业账号稳定身份合同的离线测试。"""

from __future__ import annotations

import unittest

from app_core import overseas_instagram_identity as identity_service
from app_core.overseas_instagram_identity import normalize_instagram_user_id


class InstagramIdentityTests(unittest.TestCase):
    def test_stable_user_id_accepts_only_a_decimal_string(self) -> None:
        self.assertEqual(normalize_instagram_user_id(" 17841400000000000 "), "17841400000000000")
        for value in (17841400000000000, True, "@creator", "", "12.5"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                normalize_instagram_user_id(value)

    def test_two_page_identity_conflict_fails_closed(self) -> None:
        first = identity_service.InstagramIdentity(
            user_id="17841400000000000",
            username="creator.one",
            display_name="Creator",
            avatar_url="https://example.test/first.png",
            account_type="business",
            linked_page_id="1001",
            linked_page_name="Page",
            can_manage_content=True,
        )
        second = identity_service.InstagramIdentity(
            user_id="17841400000000000",
            username="creator.two",
            display_name="Creator",
            avatar_url="https://example.test/second.png",
            account_type="business",
            linked_page_id="1001",
            linked_page_name="Page",
            can_manage_content=True,
        )

        with self.assertRaises(identity_service.InstagramIdentityError) as raised:
            identity_service.confirm_two_page_identity(first, second)

        self.assertEqual(raised.exception.error_code, "instagram_identity_mismatch")

    def test_payload_parser_keeps_only_public_professional_identity(self) -> None:
        secret = "must-not-cross-identity-boundary"
        parsed = identity_service.parse_instagram_identity_payload(
            {
                "state": "ok",
                "instagramUserId": "17841400000000000",
                "username": "Creator.One",
                "displayName": " Creator One ",
                "avatarUrl": "https://example.test/avatar.png",
                "accountType": "BUSINESS",
                "linkedPageId": "1001",
                "linkedPageName": " Main Page ",
                "canManageContent": True,
                "cookie": secret,
                "accessToken": secret,
            }
        )

        self.assertEqual(
            parsed,
            identity_service.InstagramIdentity(
                user_id="17841400000000000",
                username="creator.one",
                display_name="Creator One",
                avatar_url="https://example.test/avatar.png",
                account_type="business",
                linked_page_id="1001",
                linked_page_name="Main Page",
                can_manage_content=True,
            ),
        )
        self.assertNotIn(secret, repr(parsed))


if __name__ == "__main__":
    unittest.main()
