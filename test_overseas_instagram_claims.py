# -*- coding: utf-8 -*-
"""Instagram 一次性最终动作与禁止重放的离线数据库测试。"""

from __future__ import annotations

import sqlite3
import unittest

from app_core import overseas_instagram_claims as claims
from app_core.overseas_instagram_errors import InstagramPublishError
from app_core.overseas_instagram_publish import InstagramPublishIntent


class InstagramClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.intent = InstagramPublishIntent(
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
        self.preflight_receipt = {
            "phase": "platform_form_verified",
            "accountId": 7,
            "instagramUserId": "17841400000000000",
            "videoSha256": "1" * 64,
            "coverSha256": "2" * 64,
            "captionSha256": "3" * 64,
            "formSnapshotHash": "5" * 64,
            "baselineHash": "6" * 64,
            "finalActionTriggered": False,
        }

    def tearDown(self) -> None:
        self.conn.close()

    def test_post_click_unknown_blocks_second_click_and_duplicate_reservation(self) -> None:
        with self.conn:
            claims.reserve_instagram_claim(
                self.conn,
                task_id=101,
                preflight_task_id=100,
                intent=self.intent,
                preflight_receipt=self.preflight_receipt,
                created_at="2026-08-31T15:10:00+08:00",
            )
            claims.claim_instagram_final_action(
                self.conn,
                task_id=101,
                form_snapshot_hash="5" * 64,
                claimed_at="2026-08-31T15:11:00+08:00",
            )
            claims.record_instagram_final_action_clicked(
                self.conn,
                task_id=101,
                clicked_at="2026-08-31T15:12:00+08:00",
            )
            claims.mark_instagram_outcome_unknown(
                self.conn,
                task_id=101,
                error_code="instagram_publish_outcome_unknown",
                platform_error_text="The result could not be confirmed",
                observed_at="2026-08-31T15:13:00+08:00",
            )

        claim = claims.get_instagram_claim(self.conn, 101)
        self.assertEqual(claim["state"], "ambiguous")
        self.assertEqual(claim["blocksReplay"], 1)
        self.assertEqual(claim["finalActionTriggered"], 1)
        with self.assertRaises(InstagramPublishError) as clicked:
            claims.record_instagram_final_action_clicked(
                self.conn,
                task_id=101,
                clicked_at="2026-08-31T15:14:00+08:00",
            )
        self.assertEqual(
            clicked.exception.error_code,
            "instagram_final_action_already_triggered",
        )
        with self.assertRaises(InstagramPublishError) as duplicate:
            with self.conn:
                claims.reserve_instagram_claim(
                    self.conn,
                    task_id=102,
                    preflight_task_id=100,
                    intent=self.intent,
                    preflight_receipt=self.preflight_receipt,
                    created_at="2026-08-31T15:15:00+08:00",
                )
        self.assertEqual(duplicate.exception.error_code, "instagram_duplicate_blocked")

    def test_unique_readback_closes_claim_with_media_receipt(self) -> None:
        with self.conn:
            claims.reserve_instagram_claim(
                self.conn,
                task_id=201,
                preflight_task_id=200,
                intent=self.intent,
                preflight_receipt=self.preflight_receipt,
                created_at="2026-08-31T15:20:00+08:00",
            )
            claims.claim_instagram_final_action(
                self.conn,
                task_id=201,
                form_snapshot_hash="5" * 64,
                claimed_at="2026-08-31T15:21:00+08:00",
            )
            claims.record_instagram_final_action_clicked(
                self.conn,
                task_id=201,
                clicked_at="2026-08-31T15:22:00+08:00",
            )
            claims.mark_instagram_readback_success(
                self.conn,
                task_id=201,
                receipt={
                    "phase": "published_readback_confirmed",
                    "instagramUserId": self.intent.instagram_user_id,
                    "mediaId": "new-media",
                    "url": "https://www.instagram.com/reel/NEW/",
                    "publishedAt": "2026-08-31T15:23:00+08:00",
                    "scheduledAt": None,
                    "finalActionTriggered": True,
                    "blocksReplay": True,
                },
                observed_at="2026-08-31T15:23:00+08:00",
            )

        claim = claims.get_instagram_claim(self.conn, 201)
        self.assertEqual(claim["state"], "succeeded")
        self.assertEqual(claim["blocksReplay"], 1)
        self.assertEqual(claim["mediaId"], "new-media")
        self.assertEqual(claim["url"], "https://www.instagram.com/reel/NEW/")
        self.assertEqual(claim["publishedAt"], "2026-08-31T15:23:00+08:00")

    def test_crash_after_click_claim_blocks_replay_and_allows_readback_repair(self) -> None:
        with self.conn:
            claims.reserve_instagram_claim(
                self.conn,
                task_id=301,
                preflight_task_id=300,
                intent=self.intent,
                preflight_receipt=self.preflight_receipt,
                created_at="2026-08-31T15:30:00+08:00",
            )
            claims.claim_instagram_final_action(
                self.conn,
                task_id=301,
                form_snapshot_hash="5" * 64,
                claimed_at="2026-08-31T15:31:00+08:00",
            )
            # 模拟适配器可能已点击，但进程来不及写 clickedAt。
            claims.mark_instagram_outcome_unknown(
                self.conn,
                task_id=301,
                error_code="instagram_publish_outcome_unknown",
                platform_error_text="Worker ended after the click claim",
                observed_at="2026-08-31T15:32:00+08:00",
            )

        ambiguous = claims.get_instagram_claim(self.conn, 301)
        self.assertEqual(ambiguous["state"], "ambiguous")
        self.assertEqual(ambiguous["blocksReplay"], 1)
        self.assertEqual(ambiguous["finalActionTriggered"], 0)
        with self.assertRaises(InstagramPublishError) as replay:
            claims.record_instagram_final_action_clicked(
                self.conn,
                task_id=301,
                clicked_at="2026-08-31T15:33:00+08:00",
            )
        self.assertEqual(
            replay.exception.error_code,
            "instagram_final_action_already_triggered",
        )
        self.assertTrue(replay.exception.outcome_ambiguous)

        with self.conn:
            claims.mark_instagram_readback_success(
                self.conn,
                task_id=301,
                receipt={
                    "phase": "published_readback_confirmed",
                    "instagramUserId": self.intent.instagram_user_id,
                    "mediaId": "recovered-media",
                    "url": "https://www.instagram.com/reel/RECOVERED/",
                    "publishedAt": "2026-08-31T15:34:00+08:00",
                    "scheduledAt": None,
                    "finalActionTriggered": True,
                    "blocksReplay": True,
                },
                observed_at="2026-08-31T15:34:00+08:00",
            )

        repaired = claims.get_instagram_claim(self.conn, 301)
        self.assertEqual(repaired["state"], "succeeded")
        self.assertEqual(repaired["finalActionTriggered"], 1)
        self.assertEqual(repaired["mediaId"], "recovered-media")


if __name__ == "__main__":
    unittest.main()
