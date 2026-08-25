import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from ui.publish_page import PublishPage


class PublishPageWechatDraftQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_wechat_draft_queue_is_explicit_and_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = PublishPage()
            page.configure_wechat_draft_queue(Path(directory))

            self.assertEqual(
                page.wechat_draft_queue_enabled.text(),
                "启用硅基进化自动保存草稿（不会发表）",
            )
            self.assertFalse(page.wechat_draft_queue_enabled.isChecked())
            self.assertFalse(page.wechat_draft_queue_timer.isActive())

            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page.wechat_draft_queue_enabled.setChecked(True)
            self.app.processEvents()
            self.assertTrue(page.wechat_draft_queue_timer.isActive())

            page.wechat_draft_queue_enabled.setChecked(False)
            self.assertFalse(page.wechat_draft_queue_timer.isActive())
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
