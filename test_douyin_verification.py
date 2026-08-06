# -*- coding: utf-8 -*-
"""抖音无头验证协调器的离线安全契约测试。"""

from __future__ import annotations

from io import BytesIO
import threading
import unittest

import qrcode
from PIL import Image

from app_core.douyin_verification import (
    DouyinVerificationBroker,
    DouyinVerificationError,
)


def _qr_bytes() -> bytes:
    """生成仅用于离线测试的二维码图像字节。"""

    image = qrcode.make("offline-test")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _blank_image_bytes() -> bytes:
    """生成不可用的纯白占位图。"""

    output = BytesIO()
    Image.new("RGB", (240, 240), "white").save(output, format="PNG")
    return output.getvalue()


class DouyinVerificationBrokerTests(unittest.TestCase):
    def test_sms_code_is_consumed_once_and_never_exposed_by_snapshot(self):
        broker = DouyinVerificationBroker()
        request_id = broker.create_sms(task_id=41, message="需要短信验证")

        broker.submit_code(request_id, "123456")

        self.assertEqual(broker.consume_code(request_id), "123456")
        self.assertIsNone(broker.consume_code(request_id))
        self.assertNotIn("123456", str(broker.snapshot(request_id)))

    def test_qr_snapshot_never_exposes_image_bytes(self):
        broker = DouyinVerificationBroker()
        qr_image = _qr_bytes()

        request_id = broker.create_qr(
            task_id=42,
            qr_image=qr_image,
            expires_in_seconds=30,
        )
        snapshot = broker.snapshot(request_id)

        self.assertEqual(snapshot["kind"], "qr")
        self.assertTrue(snapshot["hasQrImage"])
        self.assertNotIn("qrImage", snapshot)
        self.assertNotIn(str(qr_image), str(snapshot))

    def test_rejects_empty_or_damaged_qr_images(self):
        broker = DouyinVerificationBroker()

        for label, qr_image in (
            ("empty", b""),
            ("damaged", b"not-an-image"),
            ("blank", _blank_image_bytes()),
        ):
            with self.subTest(kind=label):
                with self.assertRaises(DouyinVerificationError):
                    broker.create_qr(
                        task_id=43,
                        qr_image=qr_image,
                        expires_in_seconds=30,
                    )

    def test_rejects_duplicate_active_task_request(self):
        broker = DouyinVerificationBroker()
        broker.create_sms(task_id=44, message="需要短信验证")

        with self.assertRaises(DouyinVerificationError):
            broker.create_qr(
                task_id=44,
                qr_image=_qr_bytes(),
                expires_in_seconds=30,
            )

    def test_cancel_unblocks_waiter_and_prevents_later_code_submission(self):
        broker = DouyinVerificationBroker()
        request_id = broker.create_sms(task_id=45, message="需要短信验证")
        states: list[str] = []
        waiter = threading.Thread(
            target=lambda: states.append(broker.wait(request_id, timeout_seconds=2)["state"])
        )

        waiter.start()
        broker.cancel(request_id)
        waiter.join(timeout=2)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(states, ["cancelled"])
        with self.assertRaises(DouyinVerificationError):
            broker.submit_code(request_id, "123456")

    def test_expiry_and_error_are_safe_terminal_states(self):
        now = [100.0]
        broker = DouyinVerificationBroker(clock=lambda: now[0])
        request_id = broker.create_sms(
            task_id=46,
            message="需要短信验证",
            expires_in_seconds=5,
        )

        now[0] = 106.0
        self.assertEqual(broker.snapshot(request_id)["state"], "expired")
        with self.assertRaises(DouyinVerificationError):
            broker.submit_code(request_id, "123456")

        error_request_id = broker.create_sms(task_id=47, message="需要短信验证")
        broker.fail(error_request_id, "平台验证失败")
        self.assertEqual(broker.snapshot(error_request_id)["state"], "failed")
        self.assertNotIn("123456", str(broker.snapshot(error_request_id)))

    def test_expired_request_releases_task_for_a_fresh_safe_retry(self):
        now = [100.0]
        broker = DouyinVerificationBroker(clock=lambda: now[0])
        broker.create_sms(
            task_id=49,
            message="需要短信验证",
            expires_in_seconds=1,
        )

        now[0] = 102.0
        retry_request_id = broker.create_sms(task_id=49, message="重新验证")

        self.assertEqual(broker.snapshot(retry_request_id)["state"], "waiting")

    def test_clear_removes_request_and_releases_task_for_retry(self):
        broker = DouyinVerificationBroker()
        request_id = broker.create_sms(task_id=48, message="需要短信验证")

        broker.clear(request_id)

        with self.assertRaises(DouyinVerificationError):
            broker.snapshot(request_id)
        retry_request_id = broker.create_sms(task_id=48, message="重新验证")
        self.assertNotEqual(retry_request_id, request_id)


if __name__ == "__main__":
    unittest.main()
