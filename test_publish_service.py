import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import (
    account_service,
    controlled_publish,
    database,
    publish_service,
    task_service,
)
from app_core.publish_service import _validate_payloads


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


if __name__ == "__main__":
    unittest.main()
