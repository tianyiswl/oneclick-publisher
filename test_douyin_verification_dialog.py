# -*- coding: utf-8 -*-
"""抖音无头验证原生对话框的离线安全测试。"""

from __future__ import annotations

from io import BytesIO
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import qrcode
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLineEdit

from app_core.douyin_verification import DouyinVerificationBroker
from ui.douyin_verification_dialog import DouyinVerificationDialog


def _qr_bytes() -> bytes:
    image = qrcode.make("offline-dialog-verification")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class DouyinVerificationDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.broker = DouyinVerificationBroker()

    def tearDown(self) -> None:
        self.app.processEvents()

    def test_sms_dialog_submits_digits_to_broker_without_showing_sensitive_history(self):
        request_id = self.broker.create_sms(
            task_id=51,
            message="手机号=13800138000；请输入短信验证码",
        )
        dialog = DouyinVerificationDialog(request_id, broker=self.broker)

        dialog.code_input.setText("123456")
        dialog.submit_button.click()

        self.assertEqual(self.broker.consume_code(request_id), "123456")
        self.assertNotIn("123456", dialog.status_label.text())
        self.assertNotIn("13800138000", dialog.status_label.text())
        dialog.close()

    def test_sms_dialog_accepts_typed_and_pasted_digits_but_rejects_non_digits(self):
        """真实输入事件必须可提交数字，验证器不能把数字当作字面量拒绝。"""

        typed_id = self.broker.create_sms(task_id=56, message="需要短信验证")
        typed_dialog = DouyinVerificationDialog(typed_id, broker=self.broker)
        typed_dialog.code_input.setFocus()
        QTest.keyClicks(typed_dialog.code_input, "123456")
        typed_dialog.submit_button.click()
        self.assertEqual(self.broker.consume_code(typed_id), "123456")
        self.assertNotIn("123456", typed_dialog.status_label.text())
        typed_dialog.close()

        pasted_id = self.broker.create_sms(task_id=57, message="需要短信验证")
        pasted_dialog = DouyinVerificationDialog(pasted_id, broker=self.broker)
        QApplication.clipboard().setText("654321")
        pasted_dialog.code_input.setFocus()
        QTest.keyClick(
            pasted_dialog.code_input,
            Qt.Key.Key_V,
            Qt.KeyboardModifier.ControlModifier,
        )
        self.assertEqual(pasted_dialog.code_input.text(), "654321")
        pasted_dialog.submit_button.click()
        self.assertEqual(self.broker.consume_code(pasted_id), "654321")
        self.assertNotIn("654321", pasted_dialog.status_label.text())
        pasted_dialog.close()

        rejected_id = self.broker.create_sms(task_id=58, message="需要短信验证")
        rejected_dialog = DouyinVerificationDialog(rejected_id, broker=self.broker)
        rejected_dialog.code_input.setFocus()
        QTest.keyClicks(rejected_dialog.code_input, "abc")
        self.assertEqual(rejected_dialog.code_input.text(), "")
        rejected_dialog.close()

    def test_qr_dialog_renders_memory_bytes_without_sms_input_and_can_cancel(self):
        request_id = self.broker.create_qr(
            task_id=52,
            qr_image=_qr_bytes(),
            expires_in_seconds=30,
        )
        dialog = DouyinVerificationDialog(request_id, broker=self.broker)

        dialog.poll_state()

        self.assertIsNotNone(dialog.qr_label.pixmap())
        self.assertFalse(dialog.qr_label.pixmap().isNull())
        self.assertFalse(hasattr(dialog, "code_input"))
        self.assertEqual(dialog.findChildren(QLineEdit), [])
        dialog.cancel_button.click()
        self.assertEqual(self.broker.snapshot(request_id)["state"], "cancelled")
        self.assertIn("验证已取消", dialog.status_label.text())
        dialog.close()

    def test_processing_dialog_refuses_cancel_without_reporting_a_false_cancel(self):
        request_id = self.broker.create_sms(task_id=53, message="需要短信验证")
        dialog = DouyinVerificationDialog(request_id, broker=self.broker)
        self.broker.begin_processing(request_id)

        dialog.poll_state()
        dialog.cancel_button.click()

        self.assertEqual(self.broker.snapshot(request_id)["state"], "processing")
        self.assertIn("正在验证，无法取消", dialog.status_label.text())
        self.assertEqual(dialog.result(), 0)
        dialog.accept()

    def test_window_close_cancels_waiting_but_not_processing_request(self):
        waiting_id = self.broker.create_sms(task_id=54, message="需要短信验证")
        waiting_dialog = DouyinVerificationDialog(waiting_id, broker=self.broker)
        waiting_dialog.show()
        waiting_dialog.close()
        self.app.processEvents()
        self.assertEqual(self.broker.snapshot(waiting_id)["state"], "cancelled")

        processing_id = self.broker.create_sms(task_id=55, message="需要短信验证")
        processing_dialog = DouyinVerificationDialog(processing_id, broker=self.broker)
        self.broker.begin_processing(processing_id)
        processing_dialog.show()
        processing_dialog.close()
        self.app.processEvents()
        self.assertEqual(self.broker.snapshot(processing_id)["state"], "processing")
        self.assertIn("正在验证，无法取消", processing_dialog.status_label.text())
        self.assertTrue(processing_dialog.isVisible())
        processing_dialog.accept()


if __name__ == "__main__":
    unittest.main()
