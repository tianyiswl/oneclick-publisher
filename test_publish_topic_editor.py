# -*- coding: utf-8 -*-
"""发布中心结构化话题编辑器的离线回归测试。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from ui.douyin_commerce_page import DouyinCommercePage
from ui.publish_page import PublishPage
from ui.topic_tag_editor import TopicTagEditor


class PublishTopicEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_common_and_platform_topics_use_structured_editor(self) -> None:
        with patch(
            "ui.topic_tag_editor.douyin_commerce_draft_service.list_tag_history",
            return_value=["常用话题"],
        ):
            page = PublishPage()
        try:
            self.assertIsInstance(page.tags_input, TopicTagEditor)
            self.assertTrue(
                all(
                    isinstance(editor, TopicTagEditor)
                    for editor in page.platform_tags.values()
                )
            )
            self.assertEqual(len(page.platform_tags), 10)
            self.assertEqual(page.tags_input.history_host.height(), 126)
            self.assertTrue(
                all(
                    editor.history_host.height() == 86
                    for editor in page.platform_tags.values()
                )
            )
            self.assertEqual(page.tags_input.history_title.text(), "历史标签")
            self.assertEqual(page.tags_input.topic_title.text(), "话题标签")
            content_tab_bar = page.content_tabs.tabBar()
            self.assertFalse(content_tab_bar.usesScrollButtons())
            self.assertTrue(content_tab_bar.expanding())
            self.assertEqual(content_tab_bar.minimumWidth(), 236)
            self.assertEqual(content_tab_bar.count(), 2)
            text_margins = page.title_input.contentsMargins()
            expected_text_height = (
                page.title_input.fontMetrics().lineSpacing() * 8
                + text_margins.top()
                + text_margins.bottom()
                + int(round(page.title_input.document().documentMargin() * 2))
            )
            self.assertEqual(page.title_input.height(), expected_text_height)
            for platform_text in page.platform_texts.values():
                platform_margins = platform_text.contentsMargins()
                expected_platform_height = (
                    platform_text.fontMetrics().lineSpacing() * 5
                    + platform_margins.top()
                    + platform_margins.bottom()
                    + int(round(platform_text.document().documentMargin() * 2))
                )
                self.assertEqual(platform_text.height(), expected_platform_height)
            self.assertFalse(
                any(
                    "点击添加" in label.text() or "删除" in label.text()
                    for editor in [page.tags_input, *page.platform_tags.values()]
                    for label in editor.findChildren(QLabel)
                )
            )

            page.tags_input.setPlainText("#探店 本地团购")
            page.tags_input.input.setText("效率工具、AI编程")
            page.tags_input.add_from_input()
            self.assertEqual(
                page.tags_input.tags(),
                ["探店", "本地团购", "效率工具", "AI编程"],
            )
            self.assertEqual(
                page.tags_input.toPlainText(),
                "#探店 #本地团购 #效率工具 #AI编程",
            )

            page.platform_tags[3].add_history_tag("常用话题")
            self.assertEqual(page.platform_tags[3].tags(), ["常用话题"])
            page.platform_tags[3].remove_tag("常用话题")
            self.assertEqual(page.platform_tags[3].tags(), [])
        finally:
            page.close()

    def test_commerce_recent_topics_reserve_three_rows(self) -> None:
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_draft_service.list_tag_history",
            return_value=["常用话题"],
        ):
            page = DouyinCommercePage()
        try:
            self.assertEqual(page.tag_history_host.height(), 126)
            self.assertFalse(
                any(
                    label.text() == "话题标签（选填）"
                    for label in page.findChildren(QLabel)
                )
            )
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
