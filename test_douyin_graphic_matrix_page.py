# -*- coding: utf-8 -*-
"""抖音图文矩阵页面的离屏契约测试。"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from app_core import database
from ui.douyin_graphic_matrix_page import (
    DouyinGraphicMatrixPage,
    DouyinGraphicMediaDialog,
)


def _accounts(count: int = 3) -> list[dict]:
    return [
        {
            "id": index,
            "type": 3,
            "platformName": "抖音",
            "profileName": f"主体{index}",
            "userName": f"账号{index}",
            "status": 1,
        }
        for index in range(1, count + 1)
    ]


class DouyinGraphicMatrixPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = DouyinGraphicMatrixPage(accounts=_accounts())
        self.page.resize(1180, 720)
        self.page.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()

    def test_page_has_three_clear_steps_and_local_check_copy(self) -> None:
        labels = [label.text() for label in self.page.findChildren(QLabel)]
        buttons = [button.text() for button in self.page.findChildren(QPushButton)]
        self.assertTrue(any("内容准备" in text for text in labels))
        self.assertTrue(any("账号设置" in text for text in labels))
        self.assertTrue(any("检查与提交" in text for text in labels))
        self.assertIn("本地批量检查", buttons)
        self.assertNotIn("平台预检成功", " ".join(labels))

    def test_account_field_follows_common_until_overridden_then_can_restore(self) -> None:
        self.page.account_table.set_accounts(_accounts(2))
        self.page.set_common_content("通用标题", "通用正文", ["矩阵发布"])
        self.page.account_table.edit_title(2, "账号二标题")
        self.page.set_common_content("新通用标题", "新通用正文", ["新话题"])
        self.assertEqual(self.page.account_table.row_for(1).title(), "新通用标题")
        self.assertEqual(self.page.account_table.row_for(2).title(), "账号二标题")
        self.page.account_table.restore_common_field(2, "title")
        self.assertEqual(self.page.account_table.row_for(2).title(), "新通用标题")

    def test_only_douyin_accounts_are_accepted_and_selection_is_capped_at_twenty(self) -> None:
        mixed = _accounts(21) + [
            {"id": 90, "type": 1, "profileName": "小红书", "userName": "xhs"}
        ]
        self.page.set_available_accounts(mixed)
        self.assertEqual(len(self.page.available_accounts()), 21)
        with self.assertRaisesRegex(ValueError, "20"):
            self.page.select_account_ids(range(1, 22))
        self.page.select_account_ids(range(1, 21))
        self.assertEqual(len(self.page.selected_account_ids()), 20)

    def test_minimum_supported_window_keeps_primary_actions_visible(self) -> None:
        self.page.resize(1180, 720)
        self.app.processEvents()
        self.assertGreater(self.page.width(), 0)
        self.assertIsNotNone(self.page.findChild(QPushButton, "douyinGraphicNextButton"))
        self.assertIsNotNone(self.page.findChild(QPushButton, "douyinGraphicLocalCheckButton"))

    def test_account_select_all_and_cancel_all_respect_twenty_account_limit(self) -> None:
        self.page.set_available_accounts(_accounts(23))

        self.page.select_all_accounts_button.click()
        self.assertEqual(self.page.selected_account_ids(), list(range(1, 21)))
        self.assertIn("20", self.page.account_selection_status.text())

        self.page.deselect_all_accounts_button.click()
        self.assertEqual(self.page.selected_account_ids(), [])

    def test_manual_account_selection_cannot_exceed_twenty(self) -> None:
        self.page.set_available_accounts(_accounts(21))
        self.page.select_account_ids(range(1, 21))

        self.page.account_list.item(20).setCheckState(Qt.CheckState.Checked)

        self.assertEqual(self.page.selected_account_ids(), list(range(1, 21)))
        self.assertEqual(
            self.page.account_list.item(20).checkState(),
            Qt.CheckState.Unchecked,
        )


class DouyinGraphicMediaDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.rows: list[dict] = []
        for index in range(1, 41):
            path = root / f"image-{index:02}.jpg"
            path.write_bytes(b"image")
            self.rows.append(
                {
                    "id": index,
                    "filename": path.name,
                    "storedPath": str(path),
                    "typeText": "图片",
                    "mediaCategory": "广州" if index <= 20 else "江苏",
                    "remark": f"素材{index}",
                }
            )
        video = root / "video.mp4"
        video.write_bytes(b"video")
        self.rows.append(
            {
                "id": 99,
                "filename": video.name,
                "storedPath": str(video),
                "typeText": "视频",
                "mediaCategory": "广州",
                "remark": "不应显示",
            }
        )
        self.dialog = DouyinGraphicMediaDialog(self.rows)

    def tearDown(self) -> None:
        self.dialog.close()
        self.dialog.deleteLater()
        self.temporary.cleanup()

    def test_material_picker_lists_only_images_and_selects_at_most_thirty_five(self) -> None:
        self.assertEqual(self.dialog.media_list.count(), 40)

        self.dialog.select_all_button.click()

        self.assertEqual(len(self.dialog.selected_paths()), 35)
        self.assertTrue(all(path.endswith(".jpg") for path in self.dialog.selected_paths()))
        self.assertIn("35", self.dialog.selection_status.text())

    def test_filter_changes_keep_previous_selection_and_cancel_all_clears_everything(self) -> None:
        first = self.dialog.media_list.item(0)
        first.setCheckState(Qt.CheckState.Checked)
        first_path = str((first.data(Qt.ItemDataRole.UserRole) or {})["storedPath"])

        self.dialog.search_input.setText("image-40")
        self.assertIn(first_path, self.dialog.selected_paths())
        self.dialog.select_all_button.click()
        self.assertEqual(len(self.dialog.selected_paths()), 2)

        self.dialog.deselect_all_button.click()
        self.assertEqual(self.dialog.selected_paths(), [])


class DouyinGraphicMatrixDraftPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database_patch = patch.object(database, "DB_PATH", root / "database.db")
        self.database_patch.start()
        self.image_a = root / "a.jpg"
        self.image_b = root / "b.jpg"
        self.image_a.write_bytes(b"a")
        self.image_b.write_bytes(b"b")
        accounts = _accounts(2)
        accounts[0]["filePath"] = "oneclick_3_account-1.json"
        accounts[1]["filePath"] = "oneclick_3_account-2.json"
        self.page = DouyinGraphicMatrixPage(accounts=accounts)

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()
        self.database_patch.stop()
        self.temporary.cleanup()

    def test_save_clear_and_restore_keeps_full_matrix_without_touching_snapshot(self) -> None:
        self.page.set_images([str(self.image_a), str(self.image_b)])
        self.page.set_common_content("通用标题", "通用正文", ["矩阵", "图文"])
        self.page.select_account_ids([1, 2])
        self.page._go_account_step()
        self.page.account_table.edit_title(2, "账号二标题")
        self.page._set_step(1)

        saved = self.page.save_matrix_draft()
        self.page.clear_matrix_state()

        self.assertEqual(self.page.image_paths(), [])
        self.assertEqual(self.page.selected_account_ids(), [])
        self.assertEqual(self.page.common_title.text(), "")
        restored = self.page.restore_matrix_draft()

        self.assertEqual(restored["missingImages"], [])
        self.assertEqual(restored["missingAccounts"], [])
        self.assertEqual(
            self.page.image_paths(),
            [str(self.image_a.resolve()), str(self.image_b.resolve())],
        )
        self.assertEqual(self.page.selected_account_ids(), [1, 2])
        self.assertEqual(self.page.account_table.row_for(2).title(), "账号二标题")
        self.assertEqual(self.page._step, 1)
        self.assertEqual(saved["payload"]["common"]["title"], "通用标题")

    def test_restore_uses_account_file_when_database_id_changed(self) -> None:
        self.page.set_images([str(self.image_a)])
        self.page.set_common_content("通用标题", "通用正文", ["矩阵"])
        self.page.select_account_ids([1])
        self.page._go_account_step()
        self.page.account_table.edit_title(1, "账号一专属标题")
        self.page.save_matrix_draft()
        self.page.clear_matrix_state()
        self.page.set_available_accounts(
            [
                {
                    "id": 101,
                    "type": 3,
                    "profileName": "账号一",
                    "userName": "douyin-1",
                    "filePath": "oneclick_3_account-1.json",
                }
            ]
        )

        restored = self.page.restore_matrix_draft()

        self.assertEqual(restored["missingAccounts"], [])
        self.assertEqual(self.page.selected_account_ids(), [101])
        self.assertEqual(
            self.page.account_table.row_for(101).title(),
            "账号一专属标题",
        )


if __name__ == "__main__":
    unittest.main()
