# -*- coding: utf-8 -*-
"""后台公众号微信验证交互的离线测试。"""

from __future__ import annotations

from io import BytesIO
import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import qrcode
from PIL import Image, ImageDraw
from PyQt6.QtWidgets import QApplication

from app_core.wechat_verification import (
    WechatVerificationBroker,
    WechatVerificationError,
    validate_qr_image_bytes,
)
from ui.wechat_verification_dialog import WechatVerificationDialog


def _qr_bytes(text: str = "oneclick-offline-verification") -> bytes:
    image = qrcode.make(text)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _blank_prompt_bytes() -> bytes:
    image = Image.new("RGB", (520, 520), "white")
    drawer = ImageDraw.Draw(image)
    drawer.text((180, 400), "scan placeholder", fill="#444444")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class WechatVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_simulated_qr_stays_in_memory_and_dialog_can_render_it(self):
        events: list[tuple[str, str, str]] = []
        broker = WechatVerificationBroker(
            event_callback=lambda *event: events.append(event)
        )
        request_id = broker.create(task_id=7, qr_image=_qr_bytes())
        snapshot = broker.snapshot(request_id)
        self.assertTrue(snapshot["hasQrImage"])
        self.assertNotIn("qrImage", snapshot)
        self.assertNotIn("oneclick-offline-verification", str(events))

        dialog = WechatVerificationDialog(request_id, broker=broker)
        dialog.poll_state()
        self.assertIsNotNone(dialog.qr_label.pixmap())
        self.assertFalse(dialog.qr_label.pixmap().isNull())
        self.assertIn("二维码已从本机内存载入", dialog.qr_label.toolTip())
        dialog.reject()

    def test_expired_qr_can_refresh_without_replacing_request(self):
        now = [10.0]
        refreshed = _qr_bytes("refreshed")
        broker = WechatVerificationBroker(clock=lambda: now[0])
        request_id = broker.create(
            task_id=8,
            qr_image=_qr_bytes("first"),
            expires_in_seconds=2,
            refresh_callback=lambda: refreshed,
        )
        now[0] = 13.0
        self.assertEqual(broker.snapshot(request_id)["state"], "expired")
        broker.refresh(request_id, expires_in_seconds=5)
        self.assertEqual(broker.snapshot(request_id)["state"], "waiting")
        self.assertEqual(broker.qr_image(request_id), refreshed)

    def test_blank_placeholder_and_unreadable_image_are_rejected(self):
        broker = WechatVerificationBroker()
        with self.assertRaises(WechatVerificationError):
            broker.create(task_id=81, qr_image=b"not-an-image")
        with self.assertRaises(WechatVerificationError):
            broker.create(task_id=82, qr_image=_blank_prompt_bytes())

    def test_expired_dialog_refreshes_to_new_scannable_pixels(self):
        now = [20.0]
        broker = WechatVerificationBroker(clock=lambda: now[0])
        refreshed = _qr_bytes("dialog-refreshed")
        request_id = broker.create(
            task_id=83,
            qr_image=_qr_bytes("dialog-first"),
            expires_in_seconds=1,
            refresh_callback=lambda: refreshed,
        )
        dialog = WechatVerificationDialog(request_id, broker=broker)
        now[0] = 22.0
        dialog.poll_state()
        self.assertTrue(dialog.refresh_btn.isEnabled())
        dialog.refresh_qr()
        self.assertEqual(broker.qr_image(request_id), refreshed)
        self.assertFalse(dialog.qr_label.pixmap().isNull())
        dialog.reject()

    def test_refresh_failure_stops_safely(self):
        broker = WechatVerificationBroker()
        request_id = broker.create(
            task_id=9,
            qr_image=_qr_bytes(),
            refresh_callback=lambda: b"",
        )
        broker.refresh(request_id)
        snapshot = broker.snapshot(request_id)
        self.assertEqual(snapshot["state"], "failed")
        self.assertIn("刷新失败", snapshot["message"])

    def test_cancel_unblocks_waiter_with_cancelled_state(self):
        broker = WechatVerificationBroker()
        request_id = broker.create(task_id=10, qr_image=_qr_bytes())
        result: list[dict] = []
        waiter = threading.Thread(
            target=lambda: result.append(
                broker.wait(request_id, timeout_seconds=2)
            )
        )
        waiter.start()
        broker.cancel(request_id)
        waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result[0]["state"], "cancelled")

    def test_success_continues_same_waiting_request(self):
        broker = WechatVerificationBroker()
        request_id = broker.create(task_id=11, qr_image=_qr_bytes())
        result: list[dict] = []
        waiter = threading.Thread(
            target=lambda: result.append(
                broker.wait(request_id, timeout_seconds=2)
            )
        )
        waiter.start()
        broker.mark_verifying(request_id)
        self.assertEqual(broker.snapshot(request_id)["state"], "verifying")
        broker.succeed(request_id)
        waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result[0]["requestId"], request_id)
        self.assertEqual(result[0]["state"], "success")

    def test_duplicate_active_request_and_invalid_image_are_rejected(self):
        broker = WechatVerificationBroker()
        broker.create(task_id=12, qr_image=_qr_bytes())
        with self.assertRaises(WechatVerificationError):
            broker.create(task_id=12, qr_image=_qr_bytes("duplicate"))
        with self.assertRaises(WechatVerificationError):
            broker.create(task_id=13, qr_image=b"")

    def test_pending_task_ids_exposes_only_active_in_memory_requests(self):
        broker = WechatVerificationBroker()
        first = broker.create(task_id=21, qr_image=_qr_bytes("first"))
        second = broker.create(task_id=22, qr_image=_qr_bytes("second"))
        self.assertEqual(broker.pending_task_ids(), (21, 22))

        broker.succeed(first)
        self.assertEqual(broker.pending_task_ids(), (22,))
        broker.clear(second)
        self.assertEqual(broker.pending_task_ids(), ())

    def test_qr_pixel_validation_never_decodes_or_logs_payload(self):
        data = _qr_bytes("sensitive-local-only-payload")
        self.assertEqual(validate_qr_image_bytes(data), data)
        with self.assertRaises(WechatVerificationError):
            validate_qr_image_bytes(_blank_prompt_bytes())


if __name__ == "__main__":
    unittest.main()
