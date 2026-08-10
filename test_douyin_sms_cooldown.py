import math
import unittest

from app_core.douyin_sms_cooldown import (
    DouyinSmsCooldownError,
    DouyinSmsCooldownGate,
)


class DouyinSmsCooldownGateTests(unittest.TestCase):
    def test_waits_only_remaining_time_and_releases_at_60_seconds(self) -> None:
        now = [100.0]
        ticks: list[int] = []
        gate = DouyinSmsCooldownGate(
            clock=lambda: now[0],
            waiter=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        gate.record_trigger("account-a")
        now[0] = 133.0

        self.assertTrue(gate.wait_until_ready(
            "account-a", on_tick=ticks.append, cancelled=lambda: False
        ))
        self.assertEqual((ticks[0], ticks[-1], now[0]), (27, 1, 160.0))

    def test_boundary_accounts_restore_and_cancel(self) -> None:
        now = [10.0]
        waits: list[float] = []
        gate = DouyinSmsCooldownGate(clock=lambda: now[0], waiter=waits.append)
        gate.record_trigger("account-a")
        gate.restore_remaining("account-b", 8.0)
        now[0] = 69.999

        self.assertEqual(gate.remaining_seconds("account-a"), 1)
        self.assertEqual(gate.remaining_seconds("account-b"), 0)
        self.assertFalse(gate.wait_until_ready(
            "account-a", on_tick=lambda _: None, cancelled=lambda: True
        ))
        self.assertEqual(waits, [])
        now[0] = 70.0
        self.assertEqual(gate.remaining_seconds("account-a"), 0)

    def test_invalid_state_values_raise_fixed_error(self) -> None:
        gate = DouyinSmsCooldownGate(clock=lambda: math.nan, waiter=lambda _: None)

        for account_key in ("", "   ", None):
            with self.subTest(account_key=account_key):
                with self.assertRaisesRegex(
                    DouyinSmsCooldownError, "^verification_cooldown_state_invalid$"
                ):
                    gate.record_trigger(account_key)

        with self.assertRaisesRegex(
            DouyinSmsCooldownError, "^verification_cooldown_state_invalid$"
        ):
            gate.record_trigger("account-a")

        valid_clock_gate = DouyinSmsCooldownGate(clock=lambda: 1.0, waiter=lambda _: None)
        for remaining in (-0.1, 60.1):
            with self.subTest(remaining=remaining):
                with self.assertRaisesRegex(
                    DouyinSmsCooldownError, "^verification_cooldown_state_invalid$"
                ):
                    valid_clock_gate.restore_remaining("account-a", remaining)

    def test_waiter_exception_is_redacted_without_cause(self) -> None:
        def waiter(_: float) -> None:
            raise RuntimeError("sensitive account error text")

        now = [1.0]
        gate = DouyinSmsCooldownGate(clock=lambda: now[0], waiter=waiter)
        gate.record_trigger("account-a")

        with self.assertRaises(DouyinSmsCooldownError) as captured:
            gate.wait_until_ready(
                "account-a", on_tick=lambda _: None, cancelled=lambda: False
            )

        self.assertEqual(str(captured.exception), "verification_cooldown_failed")
        self.assertIsNone(captured.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
