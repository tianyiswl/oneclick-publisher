# -*- coding: utf-8 -*-
"""受控发布任务在异常退出后的终态与稳定错误码回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app_core import database, task_service
from app_core.controlled_publish import project_task
from app_core.douyin_graphic_matrix_service import prepare_matrix


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


if __name__ == "__main__":
    unittest.main()
