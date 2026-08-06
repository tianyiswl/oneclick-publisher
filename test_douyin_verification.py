# -*- coding: utf-8 -*-
"""抖音无头验证协调器的离线安全契约测试。"""

from __future__ import annotations

from io import BytesIO
import math
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

    def test_rejects_non_finite_verification_expiry(self):
        broker = DouyinVerificationBroker()

        for label, expires_in_seconds in (
            ("nan", math.nan),
            ("positive-infinity", math.inf),
            ("negative-infinity", -math.inf),
        ):
            with self.subTest(kind=label):
                with self.assertRaises(DouyinVerificationError):
                    broker.create_sms(
                        task_id=50,
                        message="需要短信验证",
                        expires_in_seconds=expires_in_seconds,
                    )

    def test_untrusted_request_or_error_message_never_enters_snapshot(self):
        broker = DouyinVerificationBroker()
        untrusted_message = (
            "Cookie=session=private; 手机号=138-0013-8000; "
            "<form action='/verify?token=private'>"
        )
        request_id = broker.create_sms(
            task_id=51,
            message=untrusted_message,
        )

        waiting_snapshot = broker.snapshot(request_id)
        broker.fail(request_id, untrusted_message)
        failed_snapshot = broker.snapshot(request_id)

        for snapshot in (waiting_snapshot, failed_snapshot):
            rendered = str(snapshot)
            self.assertNotIn("Cookie", rendered)
            self.assertNotIn("session=private", rendered)
            self.assertNotIn("138-0013-8000", rendered)
            self.assertNotIn("<form", rendered)
            self.assertNotIn("token=private", rendered)
        self.assertEqual(waiting_snapshot["message"], "请在一键发客户端输入短信验证码")
        self.assertEqual(failed_snapshot["message"], "抖音验证失败，发布已安全停止")

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

    def test_claimed_sms_code_stops_when_cancelled_or_expired_before_page_write(self):
        """领取、取消和超时必须有可检测的状态边界，不能静默标记成功。"""

        now = [100.0]
        broker = DouyinVerificationBroker(clock=lambda: now[0])
        cancelled_id = broker.create_sms(task_id=52, message="需要短信验证")
        broker.submit_code(cancelled_id, "123456")
        broker.cancel(cancelled_id)

        with self.assertRaises(DouyinVerificationError):
            broker.claim_code(cancelled_id)
        with self.assertRaises(DouyinVerificationError):
            broker.succeed(cancelled_id)

        expired_id = broker.create_sms(
            task_id=53,
            message="需要短信验证",
            expires_in_seconds=1,
        )
        broker.submit_code(expired_id, "123456")
        now[0] = 102.0

        with self.assertRaises(DouyinVerificationError):
            broker.claim_code(expired_id)
        with self.assertRaises(DouyinVerificationError):
            broker.succeed(expired_id)

    def test_invalid_sms_code_never_enters_pending_submission(self):
        broker = DouyinVerificationBroker()
        request_id = broker.create_sms(task_id=54, message="需要短信验证")

        with self.assertRaises(DouyinVerificationError):
            broker.submit_code(request_id, "invalid")
        self.assertIsNone(broker.claim_code(request_id))

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

    def test_wait_timeout_cannot_override_processing_qr_request(self):
        """扫码已进入处理态后，观察者超时不能把执行器的临界态改写为失败。"""

        broker = DouyinVerificationBroker()
        request_id = broker.create_qr(
            task_id=55,
            qr_image=_qr_bytes(),
            expires_in_seconds=30,
        )
        broker.begin_processing(request_id)
        observed_states: list[str] = []

        waiter = threading.Thread(
            target=lambda: observed_states.append(
                broker.wait(request_id, timeout_seconds=0.01)["state"]
            ),
        )
        waiter.start()
        waiter.join(timeout=1)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(observed_states, ["processing"])
        self.assertEqual(broker.snapshot(request_id)["state"], "processing")

        broker.succeed(request_id)
        self.assertEqual(broker.snapshot(request_id)["state"], "success")

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
