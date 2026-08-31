# -*- coding: utf-8 -*-
"""受控发布任务在异常退出后的终态与稳定错误码回归测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app_core import controlled_publish, database, task_service
from app_core.controlled_publish import ControlledPublishError, project_task
from app_core.douyin_graphic_matrix_service import prepare_matrix
from uploader.meta_uploader.content_list import FacebookReelMatch, FacebookReelReceipt


class ControlledPublishResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "database.db"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        database.ensure_schema()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temporary.cleanup()

    def _task(self) -> dict:
        task = task_service.create_pending_task(
            [
                {
                    "type": 3,
                    "contentType": "video",
                    "title": "测试",
                    "accountList": ["douyin.json"],
                    "fileList": ["video.mp4"],
                },
                {
                    "type": 1,
                    "contentType": "video",
                    "title": "测试",
                    "accountList": ["xhs.json"],
                    "fileList": ["video.mp4"],
                },
            ],
            mode="oneclick_publish",
        )
        task_service.mark_task_running(task["id"], "开始正式任务")
        return task

    def _matrix_task(self) -> dict:
        image = Path(self.temporary.name) / "matrix.jpg"
        image.write_bytes(b"matrix")
        matrix = prepare_matrix(
            {
                "schemaVersion": "oneclick-douyin-graphic-matrix/v1",
                "workflow": "douyin-graphic-matrix",
                "runtimeMode": "publish",
                "content": {
                    "images": [str(image)],
                    "common": {
                        "title": "矩阵标题",
                        "body": "矩阵正文",
                        "tags": ["图文矩阵"],
                    },
                },
                "targets": [
                    {"itemIndex": 1, "accountId": 31, "overrides": {}},
                    {"itemIndex": 2, "accountId": 32, "overrides": {}},
                ],
            },
            accounts=[
                {"id": 31, "type": 3, "profileName": "账号一"},
                {"id": 32, "type": 3, "profileName": "账号二"},
            ],
        )
        task = task_service.create_douyin_graphic_matrix_task(
            matrix, mode="oneclick_matrix_publish"
        )
        first = task_service.matrix_item_for_index(task["id"], 1)
        task_service.start_matrix_item(task["id"], first["id"])
        return task

    def test_fail_active_task_closes_pending_platform_and_task(self) -> None:
        task = self._task()
        task_service.mark_platform_result(
            task["id"],
            3,
            ok=False,
            message="抖音话题候选暂不可用（错误码 douyin_topic_candidates_unavailable）",
            content_type="video",
            event_type="platform_publish",
        )

        task_service.fail_active_task(
            task["id"],
            error_code="controlled_worker_ended_without_terminal_result",
            message="受控发布进程结束，未取得平台最终回执",
        )

        detail = task_service.get_task(task["id"])
        self.assertEqual(detail["status"], "failed")
        self.assertEqual([item["status"] for item in detail["items"]], ["failed", "failed"])
        projected = project_task(detail)
        self.assertEqual(
            projected["platforms"][1]["errorCode"],
            "controlled_worker_ended_without_terminal_result",
        )

    def test_stale_matrix_worker_never_remains_running_and_projects_codes(self) -> None:
        task = self._matrix_task()
        stale = datetime.now() - timedelta(minutes=2)
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET workerPid = 99999999, workerHeartbeatAt = ? WHERE id = ?",
                (stale.strftime("%Y-%m-%d %H:%M:%S"), task["id"]),
            )
            conn.commit()

        reconciled = task_service.reconcile_stale_controlled_task(
            task["id"], lease_seconds=30, now=datetime.now()
        )
        projected = project_task(task_service.get_task(task["id"]))

        self.assertTrue(reconciled)
        self.assertIn(projected["status"], {"failed", "partial_failed"})
        self.assertTrue(
            all(
                row["errorCode"] == "controlled_worker_lease_expired"
                for row in projected["platforms"]
            )
        )
        self.assertEqual([row["accountId"] for row in projected["platforms"]], [31, 32])

    def test_stale_controlled_task_is_reconciled_after_lease_expires(self) -> None:
        task = self._task()
        stale = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET workerPid = ?, workerHeartbeatAt = ? WHERE id = ?",
                (999_999_999, stale, task["id"]),
            )
            conn.commit()

        changed = task_service.reconcile_stale_controlled_task(
            task["id"], lease_seconds=30
        )

        self.assertTrue(changed)
        detail = task_service.get_task(task["id"])
        self.assertEqual(detail["status"], "failed")
        self.assertTrue(all(item["status"] == "failed" for item in detail["items"]))

    def test_stale_youtube_worker_with_known_video_requires_reconciliation(self) -> None:
        task = task_service.create_pending_task(
            [
                {
                    "type": 7,
                    "contentType": "video",
                    "title": "YouTube 失联测试",
                    "accountList": ["youtube-oauth:test"],
                    "accountIds": [71],
                    "fileList": ["video.mp4"],
                    "youtubeOfficialApi": True,
                }
            ],
            mode="oneclick_publish",
        )
        task_service.mark_task_running(task["id"], "YouTube 正式任务")
        stale = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE publish_task_items
                SET platformPostId = ?, receiptJson = ?
                WHERE taskId = ? AND platformType = 7
                """,
                (
                    "yt-known-2",
                    '{"videoId":"yt-known-2","visibility":"private"}',
                    task["id"],
                ),
            )
            conn.execute(
                "UPDATE publish_tasks SET workerPid = ?, workerHeartbeatAt = ? WHERE id = ?",
                (999_999_999, stale, task["id"]),
            )
            conn.commit()

        changed = task_service.reconcile_stale_controlled_task(
            task["id"], lease_seconds=30
        )
        projected = project_task(task_service.get_task(task["id"]))

        self.assertTrue(changed)
        self.assertEqual(
            projected["platforms"][0]["errorCode"],
            "youtube_manual_reconciliation_required",
        )
        self.assertEqual(projected["platforms"][0]["contentId"], "yt-known-2")

    def test_live_worker_pid_is_not_reconciled_during_native_verification(self) -> None:
        task = self._task()
        stale = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET workerHeartbeatAt = ? WHERE id = ?",
                (stale, task["id"]),
            )
            conn.commit()

        changed = task_service.reconcile_stale_controlled_task(
            task["id"], lease_seconds=30
        )

        self.assertFalse(changed)
        self.assertEqual(task_service.get_task(task["id"])["status"], "running")

    def test_facebook_claim_lifecycle_rejects_skipped_and_repeated_transitions(
        self,
    ) -> None:
        payload = {
            "type": 9,
            "contentType": "video",
            "title": "Facebook Page lifecycle",
            "accountList": ["facebook-page.json"],
            "accountIds": [41],
            "fileList": ["facebook.mp4"],
            "facebookExpectedPageReference": "1001",
            "facebookVideoSha256": "a" * 64,
            "facebookVideoSize": 123,
            "facebookCaptionSha256": "b" * 64,
            "visibility": "public",
        }
        preflight = task_service.create_pending_task(
            [payload], mode="oneclick_preflight"
        )
        formal_tasks = [
            task_service.create_pending_task([payload], mode="oneclick_publish")
            for _ in range(2)
        ]
        with database.connect() as conn:
            controlled_publish._ensure_facebook_page_claim_schema(conn)
            for index, task in enumerate(formal_tasks, start=1):
                conn.execute(
                    """
                    INSERT INTO facebook_page_publish_claims (
                        pageReference, publishIntentFingerprint,
                        replayFingerprint, preflightTaskId,
                        preflightReceiptHash, taskId, state, blocksReplay,
                        createdAt, updatedAt
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', 1, ?, ?)
                    """,
                    (
                        str(1000 + index),
                        f"intent-{index}",
                        f"replay-{index}",
                        preflight["id"],
                        "c" * 64,
                        task["id"],
                        "2026-08-30T10:00:00+00:00",
                        "2026-08-30T10:00:00+00:00",
                    ),
                )
            conn.commit()

        controlled_publish.mark_facebook_page_checkpoint(
            formal_tasks[0]["id"],
            expected_state="reserved",
            new_state="safe_failed",
            receipt={"pageId": "1001"},
        )
        with self.assertRaises(ControlledPublishError) as repeated:
            controlled_publish.mark_facebook_page_checkpoint(
                formal_tasks[0]["id"],
                expected_state="safe_failed",
                new_state="safe_failed",
                receipt={"pageId": "1001"},
            )
        with self.assertRaises(ControlledPublishError) as skipped:
            controlled_publish.mark_facebook_page_checkpoint(
                formal_tasks[1]["id"],
                expected_state="reserved",
                new_state="final_action_clicked",
                receipt={"pageId": "1002"},
            )

        self.assertEqual(
            repeated.exception.error_code,
            "facebook_claim_lifecycle_invalid",
        )
        self.assertEqual(
            skipped.exception.error_code,
            "facebook_claim_lifecycle_invalid",
        )
        with database.connect() as conn:
            rows = conn.execute(
                """
                SELECT taskId, state, blocksReplay
                FROM facebook_page_publish_claims ORDER BY taskId
                """
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (formal_tasks[0]["id"], "safe_failed", 0),
                (formal_tasks[1]["id"], "reserved", 1),
            ],
        )

    def test_dead_pid_repairs_succeeded_facebook_claim_instead_of_generic_failure(
        self,
    ) -> None:
        page_id = "1001"
        payload = {
            "type": 9,
            "contentType": "video",
            "title": "Facebook Page restart repair",
            "accountList": ["facebook-page.json"],
            "accountIds": [41],
            "fileList": ["facebook.mp4"],
            "facebookExpectedPageReference": page_id,
            "facebookVideoSha256": "a" * 64,
            "facebookVideoSize": 123,
            "facebookCaptionSha256": "b" * 64,
            "visibility": "public",
        }
        preflight = task_service.create_pending_task(
            [payload], mode="oneclick_preflight"
        )
        formal = task_service.create_pending_task(
            [payload], mode="oneclick_publish"
        )
        baseline = {"pageId": page_id, "rows": []}
        form_snapshot = {
            "pageId": page_id,
            "videoName": "facebook.mp4",
            "videoSize": 123,
            "videoSha256": "a" * 64,
            "captionSha256": "b" * 64,
            "visibility": "public",
            "finalButtonLabel": "Publish",
            "finalButtonReady": True,
        }
        claimed_receipt = {
            "pageId": page_id,
            "baseline": baseline,
            "formSnapshot": form_snapshot,
        }
        with database.connect() as conn:
            controlled_publish._ensure_facebook_page_claim_schema(conn)
            conn.execute(
                """
                INSERT INTO facebook_page_publish_claims (
                    pageReference, publishIntentFingerprint,
                    replayFingerprint, preflightTaskId,
                    preflightReceiptHash, taskId, state, blocksReplay,
                    workerStartedAt, createdAt, updatedAt
                ) VALUES (?, 'intent', 'replay', ?, ?, ?, 'reserved', 1,
                          ?, ?, ?)
                """,
                (
                    page_id,
                    preflight["id"],
                    "f" * 64,
                    formal["id"],
                    "2026-08-30T02:00:00+00:00",
                    "2026-08-30T02:00:00+00:00",
                    "2026-08-30T02:00:00+00:00",
                ),
            )
            conn.commit()
        controlled_publish.mark_facebook_page_checkpoint(
            formal["id"],
            expected_state="reserved",
            new_state="final_action_claimed",
            receipt=claimed_receipt,
        )
        controlled_publish.mark_facebook_page_checkpoint(
            formal["id"],
            expected_state="final_action_claimed",
            new_state="final_action_clicked",
            receipt={"pageId": page_id},
        )
        with database.connect() as conn:
            clicked_at = conn.execute(
                "SELECT clickedAt FROM facebook_page_publish_claims WHERE taskId = ?",
                (formal["id"],),
            ).fetchone()[0]
        published_at = (
            datetime.fromisoformat(clicked_at).astimezone(timezone.utc)
            + timedelta(seconds=1)
        ).isoformat()
        controlled_publish.mark_facebook_page_checkpoint(
            formal["id"],
            expected_state="final_action_clicked",
            new_state="succeeded",
            receipt={
                "reelMatch": FacebookReelMatch(
                    status="unique",
                    receipt=FacebookReelReceipt(
                        page_id=page_id,
                        reel_id="restart-reel",
                        url="https://www.facebook.com/reel/restart-reel",
                        published_at=published_at,
                    ),
                    new_count=1,
                    matching_count=1,
                )
            },
        )
        stale = (datetime.now() - timedelta(minutes=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE publish_tasks
                SET status = 'running', successCount = 0, failedCount = 0,
                    finishedAt = NULL, workerPid = ?, workerHeartbeatAt = ?
                WHERE id = ?
                """,
                (999_999_999, stale, formal["id"]),
            )
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = 'running', receiptJson = '', errorCode = '',
                    platformPostId = '', postUrl = '', publishedAt = ''
                WHERE taskId = ? AND platformType = 9
                """,
                (formal["id"],),
            )
            conn.execute(
                """
                INSERT INTO publish_task_events
                    (taskId, level, eventType, message, createdAt)
                VALUES (?, 'error',
                        'facebook_success_persistence_repair_required',
                        'repair from succeeded claim', ?)
                """,
                (formal["id"], "2026-08-30T02:01:00+00:00"),
            )
            conn.commit()

        changed = task_service.reconcile_stale_controlled_task(
            formal["id"], lease_seconds=30
        )

        self.assertTrue(changed)
        saved = task_service.get_task(formal["id"])
        receipt = json.loads(saved["items"][0]["receiptJson"])
        self.assertEqual(saved["status"], "success")
        self.assertEqual(saved["items"][0]["status"], "success")
        self.assertEqual(receipt["pageId"], page_id)
        self.assertEqual(receipt["reelId"], "restart-reel")
        self.assertEqual(
            hashlib.sha256(
                json.dumps(
                    receipt,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            self._facebook_claim_receipt_hash(formal["id"]),
        )

    @staticmethod
    def _facebook_claim_receipt_hash(task_id: int) -> str:
        with database.connect() as conn:
            return str(
                conn.execute(
                    "SELECT receiptHash FROM facebook_page_publish_claims WHERE taskId = ?",
                    (int(task_id),),
                ).fetchone()[0]
            )


if __name__ == "__main__":
    unittest.main()
