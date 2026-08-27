import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import account_service, database, publish_service, task_service
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


if __name__ == "__main__":
    unittest.main()
