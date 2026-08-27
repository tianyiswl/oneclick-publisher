import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from app_core import database, task_service
from app_core.douyin_graphic_editor import DouyinGraphicEditorError
from app_core.douyin_graphic_matrix_executor import (
    run_matrix_preflight_sync,
    run_matrix_sync,
)
from app_core.douyin_graphic_matrix_service import prepare_matrix


class FakePage:
    def __init__(self, account_id: int) -> None:
        self.account_id = account_id


class FakeAccountLauncher:
    def __init__(self) -> None:
        self.active = 0
        self.active_peak = 0
        self.opened_account_ids: list[int] = []

    @asynccontextmanager
    async def __call__(self, account: dict):
        account_id = int(account["id"])
        self.active += 1
        self.active_peak = max(self.active_peak, self.active)
        self.opened_account_ids.append(account_id)
        try:
            yield FakePage(account_id)
        finally:
            self.active -= 1


class FakeEditor:
    def __init__(self, *, fail_account_ids=(), no_receipt_ids=()) -> None:
        self.fail_account_ids = set(fail_account_ids)
        self.no_receipt_ids = set(no_receipt_ids)
        self.prepare_calls: list[int] = []
        self.submit_calls: list[int] = []

    async def prepare(self, page: FakePage, payload: dict) -> dict:
        self.prepare_calls.append(page.account_id)
        if page.account_id in self.fail_account_ids:
            raise DouyinGraphicEditorError(
                "douyin_topic_entity_missing", "官方话题未回读"
            )
        return {"ok": True}

    async def submit_and_read_receipt(self, page: FakePage, payload: dict):
        self.submit_calls.append(page.account_id)
        if page.account_id in self.no_receipt_ids:
            raise DouyinGraphicEditorError(
                "douyin_graphic_submit_receipt_missing", "平台回执缺失"
            )
        return {
            "platformPostId": f"post-{page.account_id}",
            "scheduledAt": payload["scheduleTime"],
            "timezone": "Asia/Shanghai",
        }


class DouyinGraphicMatrixExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.tempdir.name) / "database.db"
        )
        self.db_patch.start()
        database.ensure_schema()
        image = Path(self.tempdir.name) / "matrix.jpg"
        image.write_bytes(b"matrix")
        self.accounts = [
            {
                "id": account_id,
                "type": 3,
                "profileName": f"账号{account_id}",
                "userName": f"抖音{account_id}",
                "filePath": f"account-{account_id}.json",
            }
            for account_id in (31, 32, 33)
        ]
        self.matrix = prepare_matrix(
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
                    {"itemIndex": index, "accountId": account_id, "overrides": {}}
                    for index, account_id in enumerate((31, 32, 33), start=1)
                ],
            },
            accounts=self.accounts,
        )

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tempdir.cleanup()

    def _task(self, mode: str = "oneclick_matrix_publish") -> dict:
        return task_service.create_douyin_graphic_matrix_task(self.matrix, mode=mode)

    def test_accounts_run_serially_once_and_failure_does_not_block_next_account(self) -> None:
        task = self._task()
        editor = FakeEditor(fail_account_ids={32})
        launcher = FakeAccountLauncher()

        results = run_matrix_sync(
            self.matrix,
            task_id=task["id"],
            editor_factory=lambda **_kwargs: editor,
            browser_launcher=launcher,
            account_resolver=lambda account_id: next(
                row for row in self.accounts if row["id"] == account_id
            ),
        )

        self.assertEqual(launcher.active_peak, 1)
        self.assertEqual(launcher.opened_account_ids, [31, 32, 33])
        self.assertEqual([row["status"] for row in results], ["success", "failed", "success"])
        self.assertEqual(editor.prepare_calls, [31, 32, 33])
        self.assertEqual(editor.submit_calls, [31, 33])
        self.assertEqual(task_service.get_task(task["id"])["status"], "partial_failed")

    def test_success_requires_receipt_and_terminal_success_is_never_resubmitted(self) -> None:
        task = self._task()
        no_receipt = FakeEditor(no_receipt_ids={31})
        first = run_matrix_sync(
            self.matrix,
            task_id=task["id"],
            editor_factory=lambda **_kwargs: no_receipt,
            browser_launcher=FakeAccountLauncher(),
            account_resolver=lambda account_id: next(
                row for row in self.accounts if row["id"] == account_id
            ),
        )
        self.assertEqual(first[0]["errorCode"], "douyin_graphic_submit_receipt_missing")

        second_editor = FakeEditor()
        second = run_matrix_sync(
            self.matrix,
            task_id=task["id"],
            editor_factory=lambda **_kwargs: second_editor,
            browser_launcher=FakeAccountLauncher(),
            account_resolver=lambda account_id: next(
                row for row in self.accounts if row["id"] == account_id
            ),
        )
        self.assertEqual(second, [])
        self.assertEqual(second_editor.submit_calls, [])

    def test_optional_platform_preflight_uses_same_editor_without_submit(self) -> None:
        task = self._task("oneclick_matrix_preflight")
        editor = FakeEditor()
        results = run_matrix_preflight_sync(
            self.matrix,
            task_id=task["id"],
            editor_factory=lambda **_kwargs: editor,
            browser_launcher=FakeAccountLauncher(),
            account_resolver=lambda account_id: next(
                row for row in self.accounts if row["id"] == account_id
            ),
        )

        self.assertTrue(all(row["status"] == "success" for row in results))
        self.assertEqual(editor.prepare_calls, [31, 32, 33])
        self.assertEqual(editor.submit_calls, [])


if __name__ == "__main__":
    unittest.main()
