# -*- coding: utf-8 -*-
"""Instagram UI/CLI 共用服务边界的离线测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app_core import overseas_instagram_service as instagram_service
from app_core.overseas_instagram_errors import InstagramPublishError
from app_core.overseas_instagram_identity import InstagramIdentity


class InstagramServiceTests(unittest.TestCase):
    def _payload(self, root: Path) -> dict[str, object]:
        video = root / "reel.mp4"
        cover = root / "cover.png"
        video.write_bytes(b"offline-video")
        cover.write_bytes(b"offline-cover")
        return {
            "type": 8,
            "instagramControlledPublish": True,
            "accountIds": [7],
            "instagramExpectedUserId": "17841400000000000",
            "title": "Local title",
            "description": "Local body",
            "tags": ["Topic"],
            "fileList": [str(video)],
            "coverPath": str(cover),
            "visibility": "public",
            "shareToFeed": True,
            "scheduleMode": "immediate",
            "scheduledAt": None,
            "scheduleTimezone": "",
            "Cookie": "must-not-leak",
            "accessToken": "must-not-leak",
            "rawHtml": "must-not-leak",
        }

    def _identity(self, *, user_id: str = "17841400000000000") -> InstagramIdentity:
        return InstagramIdentity(
            user_id=user_id,
            username="creator.one",
            display_name="Creator One",
            avatar_url="https://example.invalid/avatar?token=secret",
            account_type="business",
            linked_page_id="1001",
            linked_page_name="Main Page",
            can_manage_content=True,
        )

    def test_local_preflight_uses_binding_and_returns_only_public_receipt(self) -> None:
        loaded: list[int] = []

        def load_identity(account_id: int) -> InstagramIdentity:
            loaded.append(account_id)
            return self._identity()

        with tempfile.TemporaryDirectory() as temporary:
            result = instagram_service.run_instagram_local_preflight_sync(
                self._payload(Path(temporary)),
                identity_loader=load_identity,
            )

        self.assertEqual(loaded, [7])
        self.assertTrue(result["ok"])
        self.assertEqual(result["phase"], "local_preflight_passed")
        receipt = result["receipt"]
        self.assertEqual(receipt["phase"], "local_preflight_passed")
        self.assertEqual(receipt["instagramUserId"], "17841400000000000")
        self.assertEqual(receipt["username"], "creator.one")
        self.assertEqual(receipt["accountType"], "business")
        self.assertEqual(receipt["linkedPageId"], "1001")
        self.assertEqual(receipt["linkedPageName"], "Main Page")
        self.assertEqual(receipt["topics"], ["Topic"])
        self.assertFalse(receipt["platformWriteOccurred"])
        self.assertFalse(receipt["finalActionTriggered"])
        self.assertFalse(receipt["blocksReplay"])
        self.assertNotIn("caption", receipt)
        self.assertNotIn("avatarUrl", receipt)
        self.assertNotIn("Cookie", receipt)
        self.assertNotIn("accessToken", receipt)
        self.assertNotIn("rawHtml", receipt)

    def test_local_preflight_fails_closed_when_saved_subject_differs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(InstagramPublishError) as raised:
                instagram_service.run_instagram_local_preflight_sync(
                    self._payload(Path(temporary)),
                    identity_loader=lambda _account_id: self._identity(
                        user_id="17841499999999999"
                    ),
                )

        self.assertEqual(raised.exception.error_code, "instagram_identity_mismatch")


if __name__ == "__main__":
    unittest.main()
