import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from app_core.wechat_verification import verification_broker
from ui.publish_page import PublishPage


class PublishPageWechatDraftQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dispose_page(self, page: PublishPage) -> None:
        page.wechat_draft_queue_timer.stop()
        page.task_timer.stop()
        page.close()
        page.deleteLater()
        self.app.processEvents()

    def test_wechat_draft_queue_is_explicit_and_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = PublishPage()
            page.configure_wechat_draft_queue(Path(directory))

            self.assertEqual(
                page.wechat_draft_queue_enabled.text(),
                "兼容通道：硅基进化公众号只保存草稿（不会发表）",
            )
            self.assertEqual(
                page.content_project_gateway_status.text(),
                "内容项目主通道：本机受控接口（默认预检）",
            )
            self.assertFalse(page.wechat_draft_queue_enabled.isChecked())
            self.assertFalse(page.wechat_draft_queue_timer.isActive())

            with (
                patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch.object(page, "_poll_wechat_draft_queue"),
            ):
                page.wechat_draft_queue_enabled.setChecked(True)
            self.app.processEvents()
            self.assertTrue(page.wechat_draft_queue_timer.isActive())

            page.wechat_draft_queue_enabled.setChecked(False)
            self.assertFalse(page.wechat_draft_queue_timer.isActive())
            self._dispose_page(page)

    def test_compatibility_gateway_bar_is_hidden_from_publish_center(self) -> None:
        page = PublishPage()

        gateway_bar = page.content_project_gateway_status.parentWidget()

        self.assertIsNotNone(gateway_bar)
        self.assertTrue(gateway_bar.isHidden())
        self._dispose_page(page)

    def test_running_draft_queue_surfaces_pending_native_wechat_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = PublishPage()
            page.configure_wechat_draft_queue(Path(directory))
            page.wechat_draft_queue_enabled.blockSignals(True)
            page.wechat_draft_queue_enabled.setChecked(True)
            page.wechat_draft_queue_enabled.blockSignals(False)
            with (
                patch.object(page.wechat_draft_queue_tasks, "is_running", return_value=True),
                patch.object(verification_broker, "pending_task_ids", return_value=(41,)),
                patch.object(page, "_show_wechat_verification_for_task") as show,
            ):
                page._poll_wechat_draft_queue()
            show.assert_called_once_with(41)
            self._dispose_page(page)


if __name__ == "__main__":
    unittest.main()
