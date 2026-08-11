# -*- coding: utf-8 -*-
"""发布中心标准抖音验证窗口接入测试。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app_core.douyin_verification import verification_broker
from ui.publish_page import PublishPage


class PublishDouyinVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_publish_page_opens_douyin_dialog_for_active_task(self) -> None:
        task_id = 90211
        request_id = verification_broker.create_sms(
            task_id=task_id,
            message="请在一键发客户端输入短信验证码",
        )
        page = PublishPage()
        page.active_task_id = task_id
        dialog = MagicMock()
        try:
            with patch(
                "ui.publish_page.DouyinVerificationDialog",
                return_value=dialog,
            ) as dialog_type:
                page._show_douyin_verification()

            dialog_type.assert_called_once_with(
                request_id,
                page,
                broker=verification_broker,
            )
            dialog.exec.assert_called_once_with()
            self.assertIsNone(page._douyin_verification_dialog)
        finally:
            verification_broker.clear(request_id)
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
