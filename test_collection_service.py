# -*- coding: utf-8 -*-
"""平台合集同步与闪退防护的离线回归测试。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from app_core import collection_service
from ui.publish_page import PublishPage


class CollectionServiceTests(unittest.TestCase):
    def test_common_collections_keeps_first_account_order(self) -> None:
        result = collection_service.common_collections(
            {
                1: ["合集 B", "合集 A", "合集 B"],
                2: ["合集 A", "合集 B", "合集 C"],
            }
        )
        self.assertEqual(result, ["合集 B", "合集 A"])

    def test_sync_returns_stable_result_and_refreshes_each_account_cache(self) -> None:
        accounts = [
            {"id": 1, "type": 3, "filePath": "first.json"},
            {"id": 2, "type": 3, "filePath": "second.json"},
        ]

        async def fake_sync(_accounts: list[dict], _platform_type: int):
            return {1: ["共同合集", "账号一"], 2: ["共同合集", "账号二"]}

        with patch.object(
            collection_service, "_sync_accounts", side_effect=fake_sync
        ), patch.object(collection_service, "_replace_cached_collections") as replace:
            result = collection_service.sync_accounts_collections(accounts)

        self.assertEqual(result["platformType"], 3)
        self.assertEqual(result["collections"], ["共同合集"])
        self.assertEqual(result["byAccount"][1], ["共同合集", "账号一"])
        self.assertEqual(replace.call_count, 2)

    def test_unsupported_platform_reports_error_instead_of_fake_success(self) -> None:
        with self.assertRaisesRegex(
            collection_service.CollectionSyncError,
            "当前仅抖音支持",
        ):
            collection_service.sync_accounts_collections(
                [{"id": 7, "type": 1, "filePath": "xhs.json"}]
            )


class CollectionSyncUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_invalid_worker_result_shows_error_without_raising(self) -> None:
        page = PublishPage()
        account = {"id": 1, "type": 3, "filePath": "douyin.json"}

        def run_now(
            _key,
            fn,
            *,
            on_started=None,
            on_success=None,
            on_error=None,
            on_finished=None,
            **_kwargs,
        ) -> bool:
            if on_started:
                on_started()
            try:
                if on_success:
                    on_success(fn())
            except Exception as exc:  # pragma: no cover - 失败即回归
                if on_error:
                    on_error(str(exc))
            finally:
                if on_finished:
                    on_finished()
            return True

        try:
            with patch.object(
                page, "_platform_accounts", return_value=[account]
            ), patch.object(
                collection_service, "sync_accounts_collections", return_value=[]
            ), patch.object(
                page.collection_tasks, "run", side_effect=run_now
            ), patch.object(QMessageBox, "warning") as warning:
                page.sync_platform_collections(3)

            warning.assert_called_once()
            self.assertIn("返回了无效数据", warning.call_args.args[2])
            sync_button = page.platform_collection_buttons[3]
            self.assertTrue(sync_button.isEnabledTo(sync_button.parentWidget()))
            self.assertEqual(sync_button.text(), "同步合集")
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
