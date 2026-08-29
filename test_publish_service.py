import asyncio
import hashlib
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app_core import (
    account_service,
    controlled_publish,
    database,
    publish_service,
    task_service,
)
from app_core.publish_service import _validate_payloads
from app_core.overseas_meta_errors import FacebookPagePublishError
from uploader.meta_uploader.content_list import (
    FacebookPageContentReader,
    FacebookReelMatch,
    FacebookReelReceipt,
)


class PublishServiceWechatDraftTests(unittest.TestCase):
    def _payload(self, root: Path) -> dict:
        cover = root / "cover.png"
        cover.write_bytes(b"png")
        return {
            "type": 10,
            "contentType": "text",
            "title": "测试标题",
            "description": "测试摘要",
            "contentHtml": "<p>测试正文</p>",
            "coverPath": str(cover),
            "fileList": [],
            "runtimeMode": "wechat_draft",
            "debugDryRun": True,
            "wechatGroupNotification": False,
            "enableTimer": False,
            "scheduleTime": None,
            "originalDeclaration": True,
        }

    def test_wechat_draft_task_requires_exactly_one_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = self._payload(Path(directory))

            with self.assertRaisesRegex(ValueError, "一个公众号账号"):
                _validate_payloads([payload, dict(payload)])


class DouyinGraphicMatrixLocalCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.tempdir.name) / "database.db"
        )
        self.db_patch.start()
        database.ensure_schema()
        self.first_id = account_service.save_oneclick_authorized_account(
            3, "矩阵账号一", "matrix-31.json"
        )
        self.second_id = account_service.save_oneclick_authorized_account(
            3, "矩阵账号二", "matrix-32.json"
        )
        self.image = Path(self.tempdir.name) / "matrix.jpg"
        self.image.write_bytes(b"matrix")

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tempdir.cleanup()

    def _matrix(self) -> dict:
        return {
            "schemaVersion": "oneclick-douyin-graphic-matrix/v1",
            "workflow": "douyin-graphic-matrix",
            "runtimeMode": "local_check",
            "content": {
                "images": [str(self.image)],
                "common": {
                    "title": "矩阵标题",
                    "body": "矩阵正文",
                    "tags": ["图文矩阵"],
                },
            },
            "targets": [
                {"accountId": self.first_id, "itemIndex": 1, "overrides": {}},
                {"accountId": self.second_id, "itemIndex": 2, "overrides": {}},
            ],
        }

    def test_matrix_local_check_never_calls_platform_preflight(self) -> None:
        with patch.object(
            publish_service.oneclick_preflight, "run_preflight_sync"
        ) as platform_preflight:
            task = publish_service.start_douyin_graphic_matrix(self._matrix())
            for _ in range(100):
                saved = task_service.get_task(task["id"])
                if saved and saved["status"] not in {"pending", "running"}:
                    break
                time.sleep(0.01)

        platform_preflight.assert_not_called()
        self.assertEqual(saved["status"], "success")
        self.assertEqual([row["status"] for row in saved["items"]], ["success", "success"])
        self.assertTrue(
            all("本地批量检查" in row["message"] for row in saved["items"])
        )


class PublishServiceTikTokScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.tempdir.name) / "database.db"
        )
        self.db_patch.start()
        database.ensure_schema()

    def tearDown(self):
        self.db_patch.stop()
        self.tempdir.cleanup()

    def test_scheduled_result_is_persisted_without_published_at(self):
        payload = {
            "type": 6,
            "contentType": "video",
            "runtimeMode": "publish",
            "accountList": ["tiktok.json"],
            "scheduleMode": "platform_native",
            "scheduledAt": "2026-08-30 09:00",
            "scheduleTimezone": "Asia/Shanghai",
        }
        task = task_service.create_pending_task([payload], mode="oneclick_publish")
        result = {
            "ok": True,
            "status": "scheduled",
            "phase": "scheduled_readback_confirmed",
            "message": "TikTok 定时内容已唯一回读",
            "receipt": {
                "scheduleMode": "platform_native",
                "scheduledAt": "2026-08-30 09:00",
                "scheduleTimezone": "Asia/Shanghai",
                "platformAccepted": True,
                "scheduledReadbackConfirmed": True,
                "contentId": None,
                "contentUrl": None,
                "publishedAt": None,
            },
        }
        with patch.object(
            publish_service.overseas_tiktok_publish,
            "run_tiktok_platform_sync",
            return_value=result,
        ) as run_tiktok:
            publish_service._run_publish(task, [payload])
        saved = task_service.get_task(task["id"])
        item = saved["items"][0]
        receipt = json.loads(item["receiptJson"])
        run_tiktok.assert_called_once_with(
            payload, mode="formal", task_id=int(task["id"])
        )
        self.assertEqual(saved["status"], "success")
        self.assertEqual(receipt["scheduleMode"], "platform_native")
        self.assertEqual(receipt["scheduledAt"], "2026-08-30 09:00")
        self.assertEqual(receipt["scheduleTimezone"], "Asia/Shanghai")
        self.assertTrue(receipt["platformAccepted"])
        self.assertTrue(receipt["scheduledReadbackConfirmed"])
        self.assertEqual(item["publishedAt"], "")


class PublishServiceTikTokFormCheckClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.tempdir.name) / "database.db"
        )
        self.db_patch.start()
        database.ensure_schema()
        self.payload = {
            "type": 6,
            "contentType": "video",
            "runtimeMode": "platform_form_check",
            "accountList": ["tiktok.json"],
            "fileList": ["video.mp4"],
            "debugDryRun": False,
            "tiktokControlledPublish": True,
            "tiktokExpectedAccountReference": "expected.user",
            "tiktokExecutionIntent": "platform_form_check",
        }

    def tearDown(self) -> None:
        if publish_service._publish_lock.locked():
            publish_service._publish_lock.release()
        self.db_patch.stop()
        self.tempdir.cleanup()

    def _started_task(self) -> dict:
        task = task_service.create_pending_task(
            [self.payload],
            mode="oneclick_platform_form_check",
        )
        with database.connect() as conn:
            controlled_publish._ensure_tiktok_claim_schema(conn)
            conn.execute(
                """
                INSERT INTO tiktok_controlled_execution_claims
                    (scopeFingerprint, taskId, mode, state, createdAt)
                VALUES (?, ?, 'platform_form_check', 'started',
                        '2026-08-29T12:00:00+00:00')
                """,
                (f"worker-scope-{task['id']}", int(task["id"])),
            )
            conn.commit()
        return task

    @staticmethod
    def _claim_count(task_id: int) -> int:
        with database.connect() as conn:
            return int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM tiktok_controlled_execution_claims
                    WHERE taskId = ?
                    """,
                    (int(task_id),),
                ).fetchone()[0]
            )

    def test_publish_lock_busy_terminal_failure_releases_started_form_check_claim(self) -> None:
        task = self._started_task()
        self.assertTrue(publish_service._publish_lock.acquire(blocking=False))

        publish_service._run_platform_form_check(task, [self.payload])

        saved = task_service.get_task(task["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["items"][0]["errorCode"], "controlled_publish_busy")
        self.assertEqual(self._claim_count(task["id"]), 0)

    def test_normal_form_check_success_releases_started_claim_in_worker_finally(self) -> None:
        task = self._started_task()
        result = {
            "ok": True,
            "phase": "platform_form_verified",
            "message": "TikTok 表单检查通过；未点击 Schedule",
            "receipt": {
                "accountId": 61,
                "visibility": "public",
                "platformWriteOccurred": True,
                "finalActionTriggered": False,
                "phase": "platform_form_verified",
            },
        }
        with patch.object(
            publish_service.overseas_tiktok_publish,
            "run_tiktok_platform_sync",
            return_value=result,
        ):
            publish_service._run_platform_form_check(task, [self.payload])

        saved = task_service.get_task(task["id"])
        self.assertEqual(saved["status"], "success")
        self.assertEqual(self._claim_count(task["id"]), 0)

    def test_normal_form_check_prefinal_exception_releases_started_claim(self) -> None:
        task = self._started_task()
        with patch.object(
            publish_service.overseas_tiktok_publish,
            "run_tiktok_platform_sync",
            side_effect=RuntimeError("offline form check failure"),
        ):
            publish_service._run_platform_form_check(task, [self.payload])

        saved = task_service.get_task(task["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["items"][0]["errorCode"], "tiktok_publish_failed")
        self.assertEqual(self._claim_count(task["id"]), 0)

    def test_claim_cleanup_failure_preserves_terminal_result_and_records_safe_diagnostic(self) -> None:
        task = self._started_task()
        result = {
            "ok": True,
            "phase": "platform_form_verified",
            "message": "TikTok 表单检查通过；未点击 Schedule",
            "receipt": {
                "accountId": 61,
                "visibility": "public",
                "platformWriteOccurred": True,
                "finalActionTriggered": False,
                "phase": "platform_form_verified",
            },
        }
        with patch.object(
            publish_service.overseas_tiktok_publish,
            "run_tiktok_platform_sync",
            return_value=result,
        ), patch.object(
            publish_service,
            "_release_terminal_tiktok_form_check_claim",
            side_effect=RuntimeError("private database detail"),
            create=True,
        ):
            publish_service._run_platform_form_check(task, [self.payload])

        saved = task_service.get_task(task["id"])
        self.assertEqual(saved["status"], "success")
        self.assertEqual(self._claim_count(task["id"]), 1)
        diagnostics = [
            event
            for event in saved["events"]
            if event["eventType"] == "tiktok_form_check_claim_release_failed"
        ]
        self.assertEqual(len(diagnostics), 1)
        self.assertNotIn("private database detail", diagnostics[0]["message"])


class FacebookPageAuthorizedSubmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.tempdir.name) / "database.db",
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.ensure_schema()
        publish_service._active_threads.clear()
        self.addCleanup(publish_service._active_threads.clear)
        self.video = Path(self.tempdir.name) / "facebook.mp4"
        self.video.write_bytes(b"facebook-authorized-submit-video")

    def _preflight_payload(self) -> dict:
        caption = "Facebook Page exact caption\n\n#OneClick"
        return {
            "type": 9,
            "contentType": "video",
            "title": "Facebook Page exact title",
            "description": "Facebook Page exact caption",
            "tags": ["OneClick"],
            "fileList": [str(self.video)],
            "accountList": ["facebook-page-41.json"],
            "accountIds": [41],
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "backgroundMode": True,
            "facebookControlledPublish": True,
            "facebookExpectedPageReference": "1001",
            "facebookFinalCaption": caption,
            "facebookCaptionSha256": hashlib.sha256(
                caption.encode("utf-8")
            ).hexdigest(),
            "facebookVideoSha256": hashlib.sha256(
                self.video.read_bytes()
            ).hexdigest(),
            "facebookManifestIntentSha256": "d" * 64,
            "visibility": "public",
            "scheduleMode": "immediate",
            "scheduledAt": "",
            "scheduleTime": "",
            "scheduleTimezone": "Asia/Shanghai",
            "enableTimer": False,
        }

    def _authorized_preflight(self) -> tuple[int, str, dict]:
        payload = self._preflight_payload()
        task = task_service.create_pending_task(
            [payload],
            mode="oneclick_preflight",
        )
        task_service.mark_task_running(task["id"], "Facebook Page preflight")
        task_service.mark_platform_result(
            task["id"],
            9,
            ok=True,
            message="Facebook Page form verified",
            content_type="video",
            event_type="facebook_platform_form_verified",
            receipt={
                "accountId": 41,
                "pageId": "1001",
                "pageName": "Saved Facebook Page",
                "videoName": self.video.name,
                "videoSize": self.video.stat().st_size,
                "videoSha256": payload["facebookVideoSha256"],
                "captionSha256": payload["facebookCaptionSha256"],
                "visibility": "public",
                "phase": "platform_form_verified",
                "platformWriteOccurred": True,
                "finalActionTriggered": False,
                "finalButtonEnabled": True,
                "formSnapshotHash": "f" * 64,
            },
        )
        authorization = controlled_publish.authorize_completed_check(task["id"])
        return task["id"], str(authorization["authorizationId"]), payload

    @staticmethod
    def _formal_payload(preflight_payload: dict) -> dict:
        return {
            **preflight_payload,
            "runtimeMode": "publish",
            "debugDryRun": False,
            "backgroundMode": False,
        }

    def _create_leased_formal(
        self,
        preflight_task_id: int,
        authorization_id: str,
        formal_payload: dict,
    ) -> dict:
        def lease_without_thread(task_id: int) -> dict:
            stored = task_service.get_task(int(task_id))
            payloads = json.loads(str(stored["payloadJson"]))
            controlled_publish.require_facebook_page_execution_claim(
                int(task_id),
                payloads,
            )
            return stored

        with patch.object(
            publish_service,
            "start_controlled_facebook_publish",
            side_effect=lease_without_thread,
        ):
            return controlled_publish._create_claimed_facebook_page_task(
                [formal_payload],
                preflight_task_id=preflight_task_id,
                authorization_id=authorization_id,
            )

    @staticmethod
    def _claim(task_id: int) -> dict:
        with database.connect() as conn:
            return dict(
                conn.execute(
                    """
                    SELECT * FROM facebook_page_publish_claims WHERE taskId = ?
                    """,
                    (int(task_id),),
                ).fetchone()
            )

    def _checkpoint_receipt(self, payload: dict) -> dict:
        return {
            "accountId": 41,
            "pageId": "1001",
            "videoName": self.video.name,
            "videoSize": self.video.stat().st_size,
            "videoSha256": payload["facebookVideoSha256"],
            "captionSha256": payload["facebookCaptionSha256"],
            "visibility": "public",
            "phase": "final_action_claimed",
            "platformWriteOccurred": True,
            "finalActionTriggered": False,
            "finalButtonEnabled": True,
            "baseline": {"pageId": "1001", "rows": []},
            "formSnapshot": {
                "pageId": "1001",
                "videoName": self.video.name,
                "videoSize": self.video.stat().st_size,
                "videoSha256": payload["facebookVideoSha256"],
                "captionSha256": payload["facebookCaptionSha256"],
                "visibility": "public",
                "finalButtonLabel": "Publish",
                "finalButtonReady": True,
            },
        }

    def _unique_runner(
        self,
        payload: dict,
        *,
        result_reel_id: str = "new-reel-1",
    ):
        def runner(current, *, task_id, progress):
            progress("final_action_claimed", self._checkpoint_receipt(payload))
            progress("final_action_clicked", {"pageId": "1001"})
            clicked_at = str(self._claim(task_id)["clickedAt"])
            published_at = (
                datetime.fromisoformat(clicked_at) + timedelta(seconds=1)
            ).isoformat()
            progress(
                "readback_unique",
                {
                    "reelMatch": FacebookReelMatch(
                        status="unique",
                        receipt=FacebookReelReceipt(
                            page_id="1001",
                            reel_id="new-reel-1",
                            url="https://www.facebook.com/reel/new-reel-1",
                            published_at=published_at,
                        ),
                        new_count=1,
                        matching_count=1,
                    )
                },
            )
            return {
                "ok": True,
                "message": "Facebook Page unique Reel confirmed",
                "receipt": {
                    "accountId": 41,
                    "pageId": "1001",
                    "videoName": self.video.name,
                    "videoSize": self.video.stat().st_size,
                    "videoSha256": payload["facebookVideoSha256"],
                    "captionSha256": payload["facebookCaptionSha256"],
                    "visibility": "public",
                    "phase": "published_readback_confirmed",
                    "platformWriteOccurred": True,
                    "finalActionTriggered": True,
                    "reelId": result_reel_id,
                    "url": f"https://www.facebook.com/reel/{result_reel_id}",
                    "publishedAt": published_at,
                },
            }

        return runner

    @staticmethod
    def _sealed_rejected_decision(page_id: str):
        class Element:
            async def is_visible(self) -> bool:
                return True

            async def inner_text(self) -> str:
                return "We couldn't publish your reel"

        class Locator:
            def __init__(self, elements: list[object]) -> None:
                self.elements = elements

            async def count(self) -> int:
                return len(self.elements)

            def nth(self, index: int):
                return self.elements[index]

        class Context:
            async def new_page(self):
                return page

        context = Context()

        class Page:
            def context(self):
                return context

            def get_by_role(self, role: str):
                return Locator([Element()] if role == "alert" else [])

        page = Page()

        async def no_verification(_page) -> None:
            return None

        async def same_page(_page, _account):
            return SimpleNamespace(page_id=page_id)

        reader = FacebookPageContentReader(
            context,
            wait_for_verification=no_verification,
        )
        with patch(
            "uploader.meta_uploader.content_list.validate_facebook_page_binding",
            side_effect=same_page,
        ):
            return asyncio.run(
                reader.read_platform_decision(
                    expected_page_id=page_id,
                    page=page,
                )
            )

    def test_authorized_submit_commits_claim_before_starting_controlled_worker(self) -> None:
        preflight_task_id, authorization_id, preflight_payload = (
            self._authorized_preflight()
        )
        formal_payload = self._formal_payload(preflight_payload)
        committed: list[dict] = []

        def start_controlled(task_id: int) -> dict:
            with database.connect() as conn:
                claim = conn.execute(
                    """
                    SELECT state, preflightTaskId, workerStartedAt
                    FROM facebook_page_publish_claims WHERE taskId = ?
                    """,
                    (int(task_id),),
                ).fetchone()
                authorization = conn.execute(
                    """
                    SELECT consumedAt FROM controlled_publish_authorizations
                    WHERE authorizationId = ?
                    """,
                    (authorization_id,),
                ).fetchone()
            committed.append(
                {
                    "taskId": int(task_id),
                    "state": str(claim["state"]),
                    "preflightTaskId": int(claim["preflightTaskId"]),
                    "workerStartedAt": claim["workerStartedAt"],
                    "authorizationConsumed": bool(authorization["consumedAt"]),
                }
            )
            return task_service.get_task(int(task_id))

        with (
            patch.object(
                controlled_publish,
                "build_controlled_payloads",
                return_value=[formal_payload],
            ),
            patch.object(
                publish_service,
                "start_controlled_facebook_publish",
                side_effect=start_controlled,
            ),
        ):
            returned = controlled_publish.submit_request(
                {
                    "mode": "formal",
                    "confirmedPreflightTaskId": preflight_task_id,
                    "authorizationId": authorization_id,
                }
            )

        self.assertEqual(
            committed,
            [
                {
                    "taskId": returned["taskId"],
                    "state": "reserved",
                    "preflightTaskId": preflight_task_id,
                    "workerStartedAt": None,
                    "authorizationConsumed": True,
                }
            ],
        )
        self.assertEqual(returned["stage"], "preparing")

    def test_authorized_submit_start_failure_safely_closes_reserved_history(
        self,
    ) -> None:
        preflight_task_id, authorization_id, preflight_payload = (
            self._authorized_preflight()
        )
        formal_payload = self._formal_payload(preflight_payload)

        class Worker:
            def __init__(self, *, target, args, daemon, name) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("offline worker startup failure")

        with (
            patch.object(
                controlled_publish,
                "build_controlled_payloads",
                return_value=[formal_payload],
            ),
            patch.object(
                publish_service,
                "_validate_payloads",
                return_value=[dict(formal_payload)],
            ),
            patch.object(publish_service.threading, "Thread", Worker),
            self.assertRaisesRegex(RuntimeError, "offline worker startup failure"),
        ):
            controlled_publish.submit_request(
                {
                    "mode": "formal",
                    "confirmedPreflightTaskId": preflight_task_id,
                    "authorizationId": authorization_id,
                }
            )

        with database.connect() as conn:
            formal_rows = conn.execute(
                """
                SELECT id, status FROM publish_tasks
                WHERE mode = 'oneclick_publish'
                """
            ).fetchall()
            claim = dict(
                conn.execute(
                    "SELECT * FROM facebook_page_publish_claims"
                ).fetchone()
            )
            consumed_at = conn.execute(
                """
                SELECT consumedAt FROM controlled_publish_authorizations
                WHERE authorizationId = ?
                """,
                (authorization_id,),
            ).fetchone()["consumedAt"]
        self.assertEqual(len(formal_rows), 1)
        self.assertEqual(str(formal_rows[0]["status"]), "failed")
        self.assertEqual(claim["taskId"], formal_rows[0]["id"])
        self.assertEqual(claim["state"], "safe_failed")
        self.assertEqual(claim["blocksReplay"], 0)
        self.assertTrue(consumed_at)
        self.assertEqual(
            task_service.get_task(preflight_task_id)["status"],
            "success",
        )
        self.assertNotIn(claim["taskId"], publish_service._active_threads)

    def test_authorized_submit_lease_failure_closes_only_unleased_claim(self) -> None:
        preflight_task_id, authorization_id, preflight_payload = (
            self._authorized_preflight()
        )
        formal_payload = self._formal_payload(preflight_payload)

        class Worker:
            def __init__(self, *, target, args, daemon, name) -> None:
                pass

            def start(self) -> None:  # pragma: no cover - must stop before start
                raise AssertionError("worker start crossed failed lease")

        with (
            patch.object(
                controlled_publish,
                "build_controlled_payloads",
                return_value=[formal_payload],
            ),
            patch.object(
                publish_service,
                "_validate_payloads",
                return_value=[dict(formal_payload)],
            ),
            patch.object(
                controlled_publish,
                "require_facebook_page_execution_claim",
                side_effect=RuntimeError("offline lease storage failure"),
            ),
            patch.object(publish_service.threading, "Thread", Worker),
            self.assertRaisesRegex(RuntimeError, "offline lease storage failure"),
        ):
            controlled_publish.submit_request(
                {
                    "mode": "formal",
                    "confirmedPreflightTaskId": preflight_task_id,
                    "authorizationId": authorization_id,
                }
            )

        with database.connect() as conn:
            claim = dict(
                conn.execute(
                    "SELECT * FROM facebook_page_publish_claims"
                ).fetchone()
            )
        self.assertEqual(claim["state"], "safe_failed")
        self.assertEqual(claim["blocksReplay"], 0)
        self.assertIsNone(claim["workerStartedAt"])
        self.assertEqual(task_service.get_task(claim["taskId"])["status"], "failed")
        self.assertNotIn(claim["taskId"], publish_service._active_threads)

    def test_authorized_receipt_hash_and_form_snapshot_share_one_sqlite_snapshot(
        self,
    ) -> None:
        preflight_task_id, authorization_id, preflight_payload = (
            self._authorized_preflight()
        )
        formal_payload = self._formal_payload(preflight_payload)
        formal_task = self._create_leased_formal(
            preflight_task_id,
            authorization_id,
            formal_payload,
        )

        with database.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")

        original_hash = controlled_publish.facebook_preflight_receipt_hash
        writer_commits: list[str] = []

        def hash_then_commit_drift(conn, task_id, payloads):
            receipt_hash = original_hash(conn, task_id, payloads)
            with database.open_connection(
                database.DB_PATH,
                row_factory=True,
            ) as writer:
                row = writer.execute(
                    """
                    SELECT receiptJson FROM publish_task_items
                    WHERE taskId = ? AND platformType = 9
                    """,
                    (preflight_task_id,),
                ).fetchone()
                receipt = json.loads(str(row["receiptJson"]))
                receipt["formSnapshotHash"] = "a" * 64
                writer.execute(
                    """
                    UPDATE publish_task_items SET receiptJson = ?
                    WHERE taskId = ? AND platformType = 9
                    """,
                    (
                        json.dumps(
                            receipt,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        preflight_task_id,
                    ),
                )
                writer.commit()
            writer_commits.append("committed")
            return receipt_hash

        with patch.object(
            controlled_publish,
            "facebook_preflight_receipt_hash",
            side_effect=hash_then_commit_drift,
        ):
            authorized_receipt = (
                publish_service.overseas_browser_publish
                ._load_authorized_preflight_receipt(
                    formal_task["id"],
                    formal_payload,
                )
            )

        self.assertEqual(writer_commits, ["committed"])
        self.assertEqual(authorized_receipt["formSnapshotHash"], "f" * 64)
        with database.connect() as conn:
            persisted = json.loads(
                str(
                    conn.execute(
                        """
                        SELECT receiptJson FROM publish_task_items
                        WHERE taskId = ? AND platformType = 9
                        """,
                        (preflight_task_id,),
                    ).fetchone()["receiptJson"]
                )
            )
        self.assertEqual(persisted["formSnapshotHash"], "a" * 64)

    def test_succeeded_claim_repairs_transient_task_success_write_failure(self) -> None:
        preflight_task_id, authorization_id, preflight_payload = (
            self._authorized_preflight()
        )
        payload = self._formal_payload(preflight_payload)
        task = self._create_leased_formal(
            preflight_task_id,
            authorization_id,
            payload,
        )

        real_mark_result = task_service.mark_platform_result
        result_writes: list[bool] = []

        def fail_first_success_write(*args, **kwargs):
            result_writes.append(bool(kwargs["ok"]))
            if result_writes == [True]:
                raise RuntimeError("injected task result write failure")
            return real_mark_result(*args, **kwargs)

        with (
            patch.object(
                publish_service.overseas_browser_publish,
                "run_facebook_page_publish_sync",
                side_effect=self._unique_runner(payload),
            ),
            patch.object(
                task_service,
                "mark_platform_result",
                side_effect=fail_first_success_write,
            ),
        ):
            publish_service._run_facebook_page_publish(task, [payload])

        saved = task_service.get_task(task["id"])
        self.assertEqual(result_writes, [True, True])
        self.assertEqual(self._claim(task["id"])["state"], "succeeded")
        self.assertEqual(saved["status"], "success")

        replay_authorization = controlled_publish.authorize_completed_check(
            preflight_task_id
        )
        with self.assertRaises(controlled_publish.ControlledPublishError) as replayed:
            self._create_leased_formal(
                preflight_task_id,
                str(replay_authorization["authorizationId"]),
                payload,
            )
        self.assertEqual(
            replayed.exception.error_code,
            "facebook_duplicate_submit_blocked",
        )

    def test_task_success_uses_hashed_succeeded_claim_receipt_not_runner_copy(
        self,
    ) -> None:
        preflight_task_id, authorization_id, preflight_payload = (
            self._authorized_preflight()
        )
        payload = self._formal_payload(preflight_payload)
        task = self._create_leased_formal(
            preflight_task_id,
            authorization_id,
            payload,
        )

        with patch.object(
            publish_service.overseas_browser_publish,
            "run_facebook_page_publish_sync",
            side_effect=self._unique_runner(
                payload,
                result_reel_id="runner-copy-mismatch",
            ),
        ):
            publish_service._run_facebook_page_publish(task, [payload])

        saved = task_service.get_task(task["id"])
        task_receipt = json.loads(saved["items"][0]["receiptJson"])
        claim = self._claim(task["id"])
        claim_receipt = json.loads(str(claim["receiptJson"]))
        self.assertEqual(saved["status"], "success")
        self.assertEqual(claim_receipt["reelId"], "new-reel-1")
        self.assertEqual(task_receipt["reelId"], "new-reel-1")
        self.assertNotIn("runner-copy-mismatch", task_receipt["url"])

    def test_persistent_task_success_write_failure_stays_repairable_and_blocked(
        self,
    ) -> None:
        preflight_task_id, authorization_id, preflight_payload = (
            self._authorized_preflight()
        )
        payload = self._formal_payload(preflight_payload)
        task = self._create_leased_formal(
            preflight_task_id,
            authorization_id,
            payload,
        )
        result_writes: list[bool] = []

        def reject_success_write(*args, **kwargs):
            result_writes.append(bool(kwargs["ok"]))
            raise RuntimeError("private injected database detail")

        with (
            patch.object(
                publish_service.overseas_browser_publish,
                "run_facebook_page_publish_sync",
                side_effect=self._unique_runner(payload),
            ),
            patch.object(
                task_service,
                "mark_platform_result",
                side_effect=reject_success_write,
            ),
        ):
            publish_service._run_facebook_page_publish(task, [payload])

        saved = task_service.get_task(task["id"])
        repair_events = [
            event
            for event in saved["events"]
            if event["eventType"]
            == "facebook_success_persistence_repair_required"
        ]
        self.assertEqual(result_writes, [True, True])
        self.assertEqual(self._claim(task["id"])["state"], "succeeded")
        self.assertEqual(saved["status"], "running")
        self.assertEqual(len(repair_events), 1)
        self.assertNotIn(
            "private injected database detail",
            repair_events[0]["message"],
        )

        replay_authorization = controlled_publish.authorize_completed_check(
            preflight_task_id
        )
        with self.assertRaises(controlled_publish.ControlledPublishError) as replayed:
            self._create_leased_formal(
                preflight_task_id,
                str(replay_authorization["authorizationId"]),
                payload,
            )
        self.assertEqual(
            replayed.exception.error_code,
            "facebook_duplicate_submit_blocked",
        )

    def test_typed_decision_is_durable_before_readback_and_progress_events_are_unique(
        self,
    ) -> None:
        preflight_task_id, authorization_id, preflight_payload = (
            self._authorized_preflight()
        )
        payload = self._formal_payload(preflight_payload)
        task = self._create_leased_formal(
            preflight_task_id,
            authorization_id,
            payload,
        )
        decision = self._sealed_rejected_decision("1001")
        durable_before_readback: list[tuple[str, str, bool]] = []

        def runner(_payload, *, task_id, progress):
            progress("final_action_claimed", self._checkpoint_receipt(payload))
            progress("final_action_clicked", {"pageId": "1001"})
            progress(
                "platform_decision_observed",
                {"pageId": "1001", "platformDecision": decision},
            )
            claim = self._claim(task_id)
            persisted = json.loads(str(claim["platformDecisionJson"]))
            expected_hash = hashlib.sha256(
                json.dumps(
                    persisted,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            durable_before_readback.append(
                (
                    str(claim["state"]),
                    str(persisted.get("kind") or ""),
                    str(claim["platformDecisionHash"]) == expected_hash,
                )
            )
            raise FacebookPagePublishError(
                "facebook_page_baseline_read_failed",
                "readback crashed after sealed decision",
                receipt={"pageId": "1001"},
                outcome_ambiguous=True,
            )

        with patch.object(
            publish_service.overseas_browser_publish,
            "run_facebook_page_publish_sync",
            side_effect=runner,
        ):
            publish_service._run_facebook_page_publish(task, [payload])

        self.assertEqual(
            durable_before_readback,
            [("final_action_clicked", "rejected_no_creation", True)],
        )
        claim = self._claim(task["id"])
        self.assertEqual((claim["state"], claim["blocksReplay"]), ("ambiguous", 1))
        evidence = controlled_publish._validated_facebook_page_claim_evidence(
            claim
        )
        self.assertEqual(evidence["decision"]["kind"], "rejected_no_creation")
        event_types = [
            event["eventType"]
            for event in task_service.get_task(task["id"])["events"]
        ]
        self.assertEqual(event_types.count("facebook_final_action_claimed"), 1)
        self.assertEqual(event_types.count("facebook_final_action_clicked"), 1)
        self.assertEqual(event_types.count("facebook_platform_decision_observed"), 1)

    def test_concurrent_controlled_starts_grant_one_lease_and_one_thread(self) -> None:
        preflight_task_id, authorization_id, preflight_payload = (
            self._authorized_preflight()
        )
        payload = self._formal_payload(preflight_payload)
        with patch.object(
            publish_service,
            "start_controlled_facebook_publish",
            side_effect=lambda task_id: task_service.get_task(int(task_id)),
        ):
            task = controlled_publish._create_claimed_facebook_page_task(
                [payload],
                preflight_task_id=preflight_task_id,
                authorization_id=authorization_id,
            )

        caller_barrier = threading.Barrier(2)
        constructor_barrier = threading.Barrier(2)
        outcome_lock = threading.Lock()
        starts: list[str] = []
        outcomes: list[tuple[str, object]] = []
        real_thread = threading.Thread

        class Worker:
            def __init__(self, *, target, args, daemon, name) -> None:
                self.name = str(name)
                constructor_barrier.wait(timeout=5)

            def start(self) -> None:
                starts.append(self.name)

            def is_alive(self) -> bool:
                return True

        def caller() -> None:
            caller_barrier.wait(timeout=5)
            try:
                returned = publish_service.start_controlled_facebook_publish(
                    task["id"]
                )
            except Exception as exc:
                outcome = ("error", getattr(exc, "error_code", ""))
            else:
                outcome = ("ok", int(returned["id"]))
            with outcome_lock:
                outcomes.append(outcome)

        with (
            patch.object(
                publish_service,
                "_validate_payloads",
                return_value=[dict(payload)],
            ),
            patch.object(publish_service.threading, "Thread", Worker),
        ):
            callers = [real_thread(target=caller) for _ in range(2)]
            for current in callers:
                current.start()
            for current in callers:
                current.join(timeout=10)

        self.assertFalse(any(current.is_alive() for current in callers))
        self.assertEqual(outcomes.count(("ok", task["id"])), 1)
        self.assertEqual(
            outcomes.count(("error", "facebook_publish_authorization_invalid")),
            1,
        )
        self.assertEqual(
            starts,
            [f"oneclick-facebook-page-publish-{task['id']}"],
        )
        self.assertEqual(list(publish_service._active_threads), [task["id"]])
        claim = self._claim(task["id"])
        self.assertEqual(claim["state"], "reserved")
        self.assertTrue(claim["workerStartedAt"])
        with database.connect() as conn:
            claim_count = conn.execute(
                """
                SELECT COUNT(*) FROM facebook_page_publish_claims
                WHERE taskId = ?
                """,
                (task["id"],),
            ).fetchone()[0]
        self.assertEqual(claim_count, 1)

    def test_constructor_failure_cannot_close_concurrent_winner_lease(self) -> None:
        preflight_task_id, authorization_id, preflight_payload = (
            self._authorized_preflight()
        )
        payload = self._formal_payload(preflight_payload)
        with patch.object(
            publish_service,
            "start_controlled_facebook_publish",
            side_effect=lambda task_id: task_service.get_task(int(task_id)),
        ):
            task = controlled_publish._create_claimed_facebook_page_task(
                [payload],
                preflight_task_id=preflight_task_id,
                authorization_id=authorization_id,
            )

        winner_lease_barrier = threading.Barrier(2)
        release_winner = threading.Event()
        constructor_lock = threading.Lock()
        outcome_lock = threading.Lock()
        constructor_count = 0
        starts: list[str] = []
        outcomes: dict[str, tuple[str, str, str]] = {}
        real_thread = threading.Thread

        class Worker:
            def __init__(self, *, target, args, daemon, name) -> None:
                nonlocal constructor_count
                with constructor_lock:
                    constructor_count += 1
                    ordinal = constructor_count
                if ordinal == 2:
                    raise RuntimeError("offline loser constructor failure")
                self.name = str(name)

            def start(self) -> None:
                starts.append(self.name)
                winner_lease_barrier.wait(timeout=5)
                if not release_winner.wait(timeout=5):
                    raise AssertionError("winner was not released")
                # Model the already-started worker reaching its normal cleanup.
                publish_service._active_threads.pop(task["id"], None)

            def is_alive(self) -> bool:
                return False

        def caller(label: str) -> None:
            try:
                returned = publish_service.start_controlled_facebook_publish(
                    task["id"]
                )
            except Exception as exc:
                outcome = (
                    "error",
                    str(getattr(exc, "error_code", "")),
                    str(exc),
                )
            else:
                outcome = ("ok", "", str(returned["id"]))
            with outcome_lock:
                outcomes[label] = outcome

        with (
            patch.object(
                publish_service,
                "_validate_payloads",
                return_value=[dict(payload)],
            ),
            patch.object(publish_service.threading, "Thread", Worker),
        ):
            winner = real_thread(target=caller, args=("winner",))
            winner.start()
            winner_lease_barrier.wait(timeout=5)
            loser = real_thread(target=caller, args=("loser",))
            loser.start()
            loser.join(timeout=10)
            try:
                self.assertFalse(loser.is_alive())
                self.assertEqual(
                    outcomes.get("loser"),
                    ("error", "", "offline loser constructor failure"),
                )
                claim = self._claim(task["id"])
                self.assertEqual(claim["state"], "reserved")
                self.assertTrue(claim["workerStartedAt"])
                self.assertNotEqual(
                    task_service.get_task(task["id"])["status"],
                    "failed",
                )
                self.assertEqual(
                    starts,
                    [f"oneclick-facebook-page-publish-{task['id']}"],
                )
                self.assertEqual(
                    list(publish_service._active_threads),
                    [task["id"]],
                )
            finally:
                release_winner.set()
                winner.join(timeout=10)

        self.assertFalse(winner.is_alive())
        self.assertEqual(outcomes.get("winner"), ("ok", "", str(task["id"])))
        self.assertEqual(publish_service._active_threads, {})


class FacebookPageLegacyStartBoundaryTests(unittest.TestCase):
    def test_generic_desktop_publish_rejects_formal_facebook_without_claim(self) -> None:
        payload = {
            "type": 9,
            "contentType": "video",
            "runtimeMode": "publish",
            "debugDryRun": False,
            "fileList": ["facebook.mp4"],
            "accountList": ["facebook.json"],
            "accountIds": [91],
            "facebookExpectedPageReference": "1001",
        }
        with (
            patch.object(publish_service.task_service, "create_pending_task") as create,
            patch.object(publish_service.threading, "Thread") as thread,
            self.assertRaises(publish_service.PublishServiceError) as raised,
        ):
            publish_service.start_desktop_publish([payload])

        self.assertEqual(
            raised.exception.error_code,
            "facebook_publish_authorization_invalid",
        )
        create.assert_not_called()
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
