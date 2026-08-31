# -*- coding: utf-8 -*-
"""Instagram V1 发布意图、预检快照和回读合同的离线测试。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app_core import overseas_instagram_publish as instagram_publish
from app_core.overseas_instagram_identity import InstagramIdentity


class InstagramPublishIntentTests(unittest.TestCase):
    def test_intent_canonicalizes_caption_and_hashes_exact_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "reel.mp4"
            cover = root / "cover.png"
            video.write_bytes(b"video-bytes")
            cover.write_bytes(b"cover-bytes")
            intent = instagram_publish.prepare_instagram_publish_intent(
                {
                    "type": 8,
                    "instagramControlledPublish": True,
                    "accountIds": [7],
                    "instagramExpectedUserId": "17841400000000000",
                    "title": "  Reel title\r\nsecond line  ",
                    "description": " Body\u00a0text \rlast line ",
                    "tags": [" TopicOne ", "#TopicOne", "话题二"],
                    "fileList": [str(video)],
                    "coverPath": str(cover),
                    "visibility": "public",
                    "shareToFeed": False,
                    "scheduleMode": "platform_native",
                    "scheduledAt": "2026-09-01T10:30:00+08:00",
                    "scheduleTimezone": "Asia/Shanghai",
                    "accountList": ["runtime-session.json"],
                    "backgroundMode": False,
                }
            )

        expected_caption = (
            "Reel title\nsecond line\n\n"
            "Body text\nlast line\n\n"
            "#TopicOne #话题二"
        )
        self.assertEqual(intent.caption, expected_caption)
        self.assertEqual(
            intent.caption_sha256,
            hashlib.sha256(expected_caption.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            intent.video_sha256,
            hashlib.sha256(b"video-bytes").hexdigest(),
        )
        self.assertEqual(
            intent.cover_sha256,
            hashlib.sha256(b"cover-bytes").hexdigest(),
        )
        self.assertEqual(intent.topics, ("TopicOne", "话题二"))
        self.assertEqual(intent.schedule_mode, "platform_native")
        self.assertEqual(intent.scheduled_at, "2026-09-01T10:30:00+08:00")
        self.assertFalse(intent.share_to_feed)

    def test_form_snapshot_requires_exact_readback_and_drops_raw_form_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "reel.mp4"
            cover = root / "cover.png"
            video.write_bytes(b"video")
            cover.write_bytes(b"cover")
            intent = instagram_publish.prepare_instagram_publish_intent(
                {
                    "type": 8,
                    "instagramControlledPublish": True,
                    "accountIds": [7],
                    "instagramExpectedUserId": "17841400000000000",
                    "title": "Title",
                    "description": "Body",
                    "tags": ["Topic"],
                    "fileList": [str(video)],
                    "coverPath": str(cover),
                    "visibility": "public",
                    "shareToFeed": True,
                    "scheduleMode": "immediate",
                    "scheduledAt": None,
                    "scheduleTimezone": "",
                }
            )
        identity = InstagramIdentity(
            user_id="17841400000000000",
            username="creator.one",
            display_name="Creator One",
            avatar_url="",
            account_type="business",
            linked_page_id="1001",
            linked_page_name="Main Page",
            can_manage_content=True,
        )
        evidence = {
            "instagramUserId": identity.user_id,
            "username": identity.username,
            "accountType": identity.account_type,
            "linkedPageId": identity.linked_page_id,
            "videoSha256": intent.video_sha256,
            "caption": intent.caption,
            "captionSha256": intent.caption_sha256,
            "topics": ["Topic"],
            "coverSha256": intent.cover_sha256,
            "visibility": "public",
            "shareToFeed": True,
            "scheduleMode": "immediate",
            "scheduledAt": None,
            "scheduleTimezone": "",
            "baselineHash": "b" * 64,
            "finalButtonCount": 1,
            "finalButtonEnabled": True,
            "platformWriteOccurred": True,
            "finalActionTriggered": False,
            "cookie": "must-not-persist",
            "rawHtml": "must-not-persist",
        }

        receipt = instagram_publish.verify_instagram_form_snapshot(
            intent,
            identity,
            evidence,
        )

        self.assertEqual(receipt["phase"], "platform_form_verified")
        self.assertEqual(receipt["instagramUserId"], identity.user_id)
        self.assertEqual(receipt["videoSha256"], intent.video_sha256)
        self.assertEqual(receipt["coverSha256"], intent.cover_sha256)
        self.assertEqual(receipt["captionSha256"], intent.caption_sha256)
        self.assertEqual(len(receipt["formSnapshotHash"]), 64)
        self.assertFalse(receipt["finalActionTriggered"])
        self.assertNotIn("caption", receipt)
        self.assertNotIn("cookie", receipt)
        self.assertNotIn("rawHtml", receipt)

    def test_unique_content_readback_returns_exact_media_receipt(self) -> None:
        intent = instagram_publish.InstagramPublishIntent(
            account_id=7,
            instagram_user_id="17841400000000000",
            video_path="reel.mp4",
            video_sha256="1" * 64,
            cover_path="cover.png",
            cover_sha256="2" * 64,
            caption="Caption",
            caption_sha256="3" * 64,
            topics=("Topic",),
            visibility="public",
            share_to_feed=True,
            schedule_mode="immediate",
            scheduled_at=None,
            schedule_timezone="",
            intent_fingerprint="4" * 64,
        )
        baseline = instagram_publish.capture_instagram_content_baseline(
            instagram_user_id=intent.instagram_user_id,
            content_ids=["old-media"],
            complete=True,
            captured_at="2026-08-31T15:20:00+08:00",
        )
        receipt = instagram_publish.match_unique_instagram_readback(
            baseline,
            intent,
            [
                {
                    "instagramUserId": intent.instagram_user_id,
                    "mediaId": "old-media",
                    "url": "https://www.instagram.com/reel/OLD/",
                    "captionSha256": "9" * 64,
                    "state": "published",
                    "publishedAt": "2026-08-30T10:00:00+08:00",
                    "scheduledAt": None,
                },
                {
                    "instagramUserId": intent.instagram_user_id,
                    "mediaId": "new-media",
                    "url": "https://www.instagram.com/reel/NEW/",
                    "captionSha256": intent.caption_sha256,
                    "state": "published",
                    "publishedAt": "2026-08-31T15:21:00+08:00",
                    "scheduledAt": None,
                },
            ],
        )

        self.assertEqual(receipt["phase"], "published_readback_confirmed")
        self.assertEqual(receipt["mediaId"], "new-media")
        self.assertEqual(receipt["url"], "https://www.instagram.com/reel/NEW/")
        self.assertEqual(receipt["publishedAt"], "2026-08-31T15:21:00+08:00")
        self.assertIsNone(receipt["scheduledAt"])
        self.assertTrue(receipt["blocksReplay"])


if __name__ == "__main__":
    unittest.main()
