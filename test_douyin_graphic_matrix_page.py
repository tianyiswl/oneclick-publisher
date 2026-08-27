# -*- coding: utf-8 -*-
"""抖音图文矩阵页面的离屏契约测试。"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from ui.douyin_graphic_matrix_page import DouyinGraphicMatrixPage


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


if __name__ == "__main__":
    unittest.main()
