# -*- coding: utf-8 -*-
"""Instagram 同一表单会话的离线受控执行测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app_core import overseas_instagram_claims as claims
from app_core import overseas_instagram_executor as executor_service
from app_core.overseas_instagram_errors import InstagramPublishError
from app_core.overseas_instagram_identity import InstagramIdentity


class FakeInstagramSession:
    def __init__(
        self,
        *,
        return_unique_result: bool = True,
        mutate_before_click: bool = False,
    ) -> None:
        self.return_unique_result = return_unique_result
        self.mutate_before_click = mutate_before_click
        self.identity_reads: list[str] = []
        self.baseline_reads = 0
        self.form_fills = 0
        self.current_form_reads = 0
        self.final_clicks = 0
        self.result_reads = 0

    def read_identity(self, surface: str) -> dict[str, object]:
        self.identity_reads.append(surface)
        return {
            "state": "ok",
            "instagramUserId": "17841400000000000",
            "username": "creator.one",
            "displayName": "Creator One",
            "avatarUrl": "https://example.invalid/avatar?token=not-persisted",
            "accountType": "business",
            "linkedPageId": "1001",
            "linkedPageName": "Main Page",
            "canManageContent": True,
            "Cookie": "must-not-persist",
        }

    def read_content_baseline(self, _intent) -> dict[str, object]:
        self.baseline_reads += 1
        return {
            "contentIds": ["old-media"],
            "complete": True,
            "capturedAt": "2026-08-31T16:00:00+08:00",
        }

    def _form_evidence(self, intent, identity) -> dict[str, object]:
        return {
            "instagramUserId": intent.instagram_user_id,
            "username": identity.username,
            "accountType": identity.account_type,
            "linkedPageId": identity.linked_page_id,
            "videoSha256": intent.video_sha256,
            "caption": intent.caption,
            "captionSha256": intent.caption_sha256,
            "topics": list(intent.topics),
            "coverSha256": intent.cover_sha256,
            "visibility": intent.visibility,
            "shareToFeed": intent.share_to_feed,
            "scheduleMode": intent.schedule_mode,
            "scheduledAt": intent.scheduled_at,
            "scheduleTimezone": intent.schedule_timezone,
            "finalButtonCount": 1,
            "finalButtonEnabled": True,
            "platformWriteOccurred": True,
            "finalActionTriggered": False,
            "rawHtml": "must-not-persist",
        }

    def fill_and_read_form(self, intent, identity) -> dict[str, object]:
        self.form_fills += 1
        return self._form_evidence(intent, identity)

    def read_current_form(self, intent, identity) -> dict[str, object]:
        self.current_form_reads += 1
        evidence = self._form_evidence(intent, identity)
        if self.mutate_before_click:
            evidence["shareToFeed"] = not intent.share_to_feed
        return evidence

    def click_final_action_once(self, _intent) -> dict[str, object]:
        self.final_clicks += 1
        return {
            "accepted": True,
            "observedAt": "2026-08-31T16:02:00+08:00",
        }

    def read_content_after_final(self, intent) -> list[dict[str, object]]:
        self.result_reads += 1
        if not self.return_unique_result:
            return []
        return [
            {
                "instagramUserId": intent.instagram_user_id,
                "mediaId": "new-media",
                "url": "https://www.instagram.com/reel/NEW/",
                "captionSha256": intent.caption_sha256,
                "state": "published",
                "publishedAt": "2026-08-31T16:03:00+08:00",
                "scheduledAt": None,
            }
        ]


class InstagramExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.identity = InstagramIdentity(
            user_id="17841400000000000",
            username="creator.one",
            display_name="Creator One",
            avatar_url="",
            account_type="business",
            linked_page_id="1001",
            linked_page_name="Main Page",
            can_manage_content=True,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _payload(self, root: Path) -> dict[str, object]:
        video = root / "reel.mp4"
        cover = root / "cover.png"
        video.write_bytes(b"video")
        cover.write_bytes(b"cover")
        return {
            "type": 8,
            "instagramControlledPublish": True,
            "accountIds": [7],
            "instagramExpectedUserId": self.identity.user_id,
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

    def _prepared(self, root: Path, session: FakeInstagramSession):
        executor = executor_service.InstagramControlledExecutor(
            self._payload(root),
            saved_identity=self.identity,
            session=session,
        )
        receipt = executor.run_platform_form_preflight()
        return executor, receipt

    def _reserve(self, executor, receipt, *, task_id: int) -> None:
        with self.conn:
            claims.reserve_instagram_claim(
                self.conn,
                task_id=task_id,
                preflight_task_id=task_id - 1,
                intent=executor.intent,
                preflight_receipt=receipt,
                created_at="2026-08-31T16:01:00+08:00",
            )

    def test_preflight_and_formal_share_one_session_and_click_at_most_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = FakeInstagramSession()
            executor, preflight = self._prepared(Path(temporary), session)
            self._reserve(executor, preflight, task_id=401)
            result = executor.run_authorized_final_action(
                self.conn,
                task_id=401,
                claimed_at="2026-08-31T16:01:30+08:00",
            )

            self.assertEqual(result["phase"], "published_readback_confirmed")
            self.assertEqual(result["mediaId"], "new-media")
            self.assertEqual(session.identity_reads, ["composer", "content"])
            self.assertEqual(session.baseline_reads, 1)
            self.assertEqual(session.form_fills, 1)
            self.assertEqual(session.current_form_reads, 1)
            self.assertEqual(session.final_clicks, 1)
            self.assertEqual(session.result_reads, 1)
            with self.assertRaises(InstagramPublishError):
                executor.run_authorized_final_action(
                    self.conn,
                    task_id=401,
                    claimed_at="2026-08-31T16:04:00+08:00",
                )
            self.assertEqual(session.final_clicks, 1)

        claim = claims.get_instagram_claim(self.conn, 401)
        self.assertEqual(claim["state"], "succeeded")
        self.assertEqual(claim["mediaId"], "new-media")

    def test_missing_unique_readback_is_ambiguous_and_never_reclicks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = FakeInstagramSession(return_unique_result=False)
            executor, preflight = self._prepared(Path(temporary), session)
            self._reserve(executor, preflight, task_id=501)
            with self.assertRaises(InstagramPublishError) as raised:
                executor.run_authorized_final_action(
                    self.conn,
                    task_id=501,
                    claimed_at="2026-08-31T16:01:30+08:00",
                )
            self.assertEqual(
                raised.exception.error_code,
                "instagram_readback_not_unique",
            )
            self.assertTrue(raised.exception.outcome_ambiguous)
            with self.assertRaises(InstagramPublishError):
                executor.run_authorized_final_action(
                    self.conn,
                    task_id=501,
                    claimed_at="2026-08-31T16:04:00+08:00",
                )
            self.assertEqual(session.final_clicks, 1)
            self.assertEqual(session.result_reads, 1)

        claim = claims.get_instagram_claim(self.conn, 501)
        self.assertEqual(claim["state"], "ambiguous")
        self.assertEqual(claim["blocksReplay"], 1)
        self.assertEqual(claim["finalActionTriggered"], 1)

    def test_form_mutation_after_preflight_safe_fails_before_click(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = FakeInstagramSession(mutate_before_click=True)
            executor, preflight = self._prepared(Path(temporary), session)
            self._reserve(executor, preflight, task_id=601)
            with self.assertRaises(InstagramPublishError) as raised:
                executor.run_authorized_final_action(
                    self.conn,
                    task_id=601,
                    claimed_at="2026-08-31T16:01:30+08:00",
                )

            self.assertEqual(
                raised.exception.error_code,
                "instagram_form_readback_failed",
            )
            self.assertFalse(raised.exception.outcome_ambiguous)
            self.assertEqual(session.current_form_reads, 1)
            self.assertEqual(session.final_clicks, 0)

        claim = claims.get_instagram_claim(self.conn, 601)
        self.assertEqual(claim["state"], "safe_failed")
        self.assertEqual(claim["blocksReplay"], 0)
        self.assertEqual(claim["finalActionTriggered"], 0)


if __name__ == "__main__":
    unittest.main()
