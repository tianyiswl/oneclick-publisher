# -*- coding: utf-8 -*-
"""发布中心素材选择语义与执行日志可读性的离线回归测试。"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from ui.publish_page import PublishPage


def _media(media_id: int, media_type: str) -> dict:
    suffix = "mp4" if media_type == "视频" else "jpg"
    return {
        "id": media_id,
        "typeText": media_type,
        "filename": f"素材-{media_id}.{suffix}",
        "file_path": f"F:/素材/素材-{media_id}.{suffix}",
        "storedPath": f"F:/素材/素材-{media_id}.{suffix}",
        "filesize": 1,
        "remark": "",
        "mediaCategory": "默认",
    }


class PublishMediaSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = PublishPage()
        self.page.media_category_combo.clear()
        self.page.media_category_combo.addItem("全部", "全部")

    def tearDown(self) -> None:
        self.page.close()

    def test_video_mode_hides_bulk_actions_and_keeps_only_latest_selection(self) -> None:
        self.page.content_type = "video"
        self.page._sync_content_type_interface()
        self.page.refresh_media([_media(1, "视频"), _media(2, "视频")])

        self.assertTrue(self.page.media_actions_widget.isHidden())
        self.page.media_list.item(0).setCheckState(Qt.CheckState.Checked)
        self.page.media_list.item(1).setCheckState(Qt.CheckState.Checked)

        self.assertEqual(self.page._selected_media_ids, {2})
        self.assertEqual(
            self.page.media_list.item(0).checkState(), Qt.CheckState.Unchecked
        )
        self.assertEqual(
            self.page.media_list.item(1).checkState(), Qt.CheckState.Checked
        )

    def test_article_mode_shows_bulk_actions_and_allows_multiple_images(self) -> None:
        self.page.content_type = "article"
        self.page._sync_content_type_interface()
        self.page.refresh_media([_media(1, "图片"), _media(2, "图片")])

        self.assertFalse(self.page.media_actions_widget.isHidden())
        self.page._set_list_checked(self.page.media_list, True)

        self.assertEqual(self.page._selected_media_ids, {1, 2})

    def test_video_mode_normalizes_legacy_multiple_selection(self) -> None:
        self.page.content_type = "video"
        self.page._selected_media_ids = {1, 2}

        self.page.refresh_media([_media(1, "视频"), _media(2, "视频")])

        self.assertEqual(self.page._selected_media_ids, {1})
        self.assertEqual(
            self.page.media_list.item(0).checkState(), Qt.CheckState.Checked
        )
        self.assertEqual(
            self.page.media_list.item(1).checkState(), Qt.CheckState.Unchecked
        )

    def test_execution_log_uses_larger_font(self) -> None:
        self.assertEqual(self.page.log.font().pixelSize(), 13)


if __name__ == "__main__":
    unittest.main()
