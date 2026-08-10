# -*- coding: utf-8 -*-
"""抖音无头验证协调器的离线安全契约测试。"""

from __future__ import annotations

from io import BytesIO
import math
from pathlib import Path
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


class ReadmeContractTests(unittest.TestCase):
    def test_readme_states_douyin_verification_stays_in_native_client(self):
        content = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("抖音带货无头验证由客户端原生弹窗承接，浏览器不前置", content)


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

    def test_platform_rejected_sms_returns_to_same_request_for_reentry(self):
        """重输不创建第二个请求，也不绕过原短信重发冷却。"""

        broker = DouyinVerificationBroker()
        request_id = broker.create_sms(task_id=154, message="需要短信验证")
        broker.submit_code(request_id, "123456")
        self.assertEqual(broker.claim_code(request_id), "123456")

        broker.retry_sms_input(request_id)

        snapshot = broker.snapshot(request_id)
        self.assertEqual(snapshot["state"], "waiting")
        self.assertEqual(snapshot["message"], "验证码未通过或已过期，请检查后重新输入。")
        self.assertFalse(snapshot["canResend"])
        broker.submit_code(request_id, "654321")
        self.assertEqual(broker.claim_code(request_id), "654321")

    def test_sms_resend_uses_same_active_request_and_enforces_monotonic_60_second_cooldown(self):
        now = [100.0]
        calls: list[str] = []
        confirmations: list[str] = []
        broker = DouyinVerificationBroker(clock=lambda: now[0])
        request_id = broker.create_sms(
            task_id=88,
            message="需要短信验证",
            resend_handler=lambda: calls.append("resend") or True,
        )
        broker.register_sms_resend_confirmed_observer(
            request_id,
            confirmations.append,
        )

        first = broker.snapshot(request_id)
        self.assertFalse(first["canResend"])
        self.assertEqual(first["resendInSeconds"], 60)
        with self.assertRaisesRegex(DouyinVerificationError, "60 秒后"):
            broker.request_sms_resend(request_id)
        self.assertEqual(confirmations, [])

        now[0] = 160.0
        broker.request_sms_resend(request_id)
        second = broker.snapshot(request_id)
        self.assertEqual(calls, ["resend"])
        self.assertEqual(confirmations, [request_id])
        self.assertFalse(second["canResend"])
        self.assertEqual(second["resendInSeconds"], 60)
        # 重新发送不创建新的 request，也不能用第二次点击绕过新的冷却期。
        self.assertEqual(second["requestId"], request_id)
        with self.assertRaisesRegex(DouyinVerificationError, "60 秒后"):
            broker.request_sms_resend(request_id)
        self.assertEqual(confirmations, [request_id])

        now[0] = 220.0
        broker.request_sms_resend(request_id)
        self.assertEqual(calls, ["resend", "resend"])
        self.assertEqual(confirmations, [request_id, request_id])

    def test_sms_resend_rejects_terminal_request_and_handler_failure_stops_safely(self):
        now = [200.0]
        confirmations: list[str] = []
        broker = DouyinVerificationBroker(clock=lambda: now[0])
        request_id = broker.create_sms(
            task_id=89,
            message="需要短信验证",
            resend_handler=lambda: False,
        )
        broker.register_sms_resend_confirmed_observer(
            request_id,
            confirmations.append,
        )
        now[0] = 260.0
        with self.assertRaisesRegex(DouyinVerificationError, "未确认"):
            broker.request_sms_resend(request_id)
        self.assertEqual(broker.snapshot(request_id)["state"], "failed")
        self.assertEqual(confirmations, [])
        with self.assertRaisesRegex(DouyinVerificationError, "不能重新发送"):
            broker.request_sms_resend(request_id)

        cancelled = broker.create_sms(
            task_id=90,
            message="需要短信验证",
            resend_handler=lambda: True,
        )
        self.assertTrue(broker.cancel(cancelled))
        now[0] = 320.0
        with self.assertRaisesRegex(DouyinVerificationError, "不能重新发送"):
            broker.request_sms_resend(cancelled)

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

    def test_request_for_task_only_exposes_an_active_request_id(self):
        """页面轮询只能按任务取得无敏感信息的活动请求标识。"""

        broker = DouyinVerificationBroker()
        request_id = broker.create_sms(
            task_id=56,
            message="Cookie=session-private; 手机号=13800138000",
        )

        self.assertEqual(broker.request_for_task(56), request_id)
        self.assertNotIn("Cookie", str(broker.request_for_task(56)))
        broker.cancel(request_id)
        self.assertIsNone(broker.request_for_task(56))
        broker.clear(request_id)
        self.assertIsNone(broker.request_for_task(56))


if __name__ == "__main__":
    unittest.main()
