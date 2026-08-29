import os
import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app_core.tiktok_schedule_contract import (
    TikTokScheduleContractError,
    parse_tiktok_schedule_fields,
    validate_tiktok_schedule_window,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


class TikTokScheduleContractTests(unittest.TestCase):
    def test_immediate_and_scheduled_are_exactly_normalized(self):
        immediate = parse_tiktok_schedule_fields(
            enable_timer=False,
            schedule_time=None,
            schedule_timezone="Asia/Shanghai",
            daily_times=[],
        )
        self.assertEqual(immediate.mode, "immediate")
        self.assertIsNone(immediate.local_time)
        self.assertEqual(immediate.timezone, "Asia/Shanghai")
        self.assertIsNone(immediate.scheduled_at)

        scheduled = parse_tiktok_schedule_fields(
            enable_timer=True,
            schedule_time="2026-08-29 15:00",
            schedule_timezone="Asia/Shanghai",
            daily_times=["15:00"],
        )
        self.assertEqual(scheduled.mode, "platform_native")
        self.assertEqual(scheduled.local_time, "2026-08-29 15:00")
        self.assertEqual(scheduled.timezone, "Asia/Shanghai")
        self.assertEqual(scheduled.scheduled_at.tzinfo, SHANGHAI)
        self.assertEqual(scheduled.scheduled_at, datetime(2026, 8, 29, 15, 0, tzinfo=SHANGHAI))

    def test_creation_window_is_thirty_minutes_to_ten_days(self):
        now = datetime(2026, 8, 29, 14, 0, tzinfo=SHANGHAI)
        accepted = parse_tiktok_schedule_fields(
            enable_timer=True,
            schedule_time="2026-08-29 14:30",
            schedule_timezone="Asia/Shanghai",
            daily_times=["14:30"],
        )
        validate_tiktok_schedule_window(
            accepted,
            now=now,
            minimum_lead=timedelta(minutes=30),
        )

    def test_creation_window_rejects_twenty_nine_minutes(self):
        now = datetime(2026, 8, 29, 14, 0, tzinfo=SHANGHAI)
        intent = parse_tiktok_schedule_fields(
            enable_timer=True,
            schedule_time="2026-08-29 14:29",
            schedule_timezone="Asia/Shanghai",
            daily_times=["14:29"],
        )
        with self.assertRaises(TikTokScheduleContractError) as raised:
            validate_tiktok_schedule_window(
                intent,
                now=now,
                minimum_lead=timedelta(minutes=30),
            )
        self.assertEqual(raised.exception.error_code, "tiktok_schedule_out_of_range")

    def test_creation_window_accepts_exactly_ten_days(self):
        now = datetime(2026, 8, 29, 14, 0, tzinfo=SHANGHAI)
        intent = parse_tiktok_schedule_fields(
            enable_timer=True,
            schedule_time="2026-09-08 14:00",
            schedule_timezone="Asia/Shanghai",
            daily_times=["14:00"],
        )
        validate_tiktok_schedule_window(
            intent,
            now=now,
            minimum_lead=timedelta(minutes=30),
        )

    def test_creation_window_rejects_ten_days_plus_one_minute(self):
        now = datetime(2026, 8, 29, 14, 0, tzinfo=SHANGHAI)
        intent = parse_tiktok_schedule_fields(
            enable_timer=True,
            schedule_time="2026-09-08 14:01",
            schedule_timezone="Asia/Shanghai",
            daily_times=["14:01"],
        )
        with self.assertRaises(TikTokScheduleContractError) as raised:
            validate_tiktok_schedule_window(
                intent,
                now=now,
                minimum_lead=timedelta(minutes=30),
            )
        self.assertEqual(raised.exception.error_code, "tiktok_schedule_out_of_range")

    def test_naive_now_is_rejected(self):
        intent = parse_tiktok_schedule_fields(
            enable_timer=True,
            schedule_time="2026-08-29 15:00",
            schedule_timezone="Asia/Shanghai",
            daily_times=["15:00"],
        )
        with self.assertRaises(TikTokScheduleContractError) as raised:
            validate_tiktok_schedule_window(
                intent,
                now=datetime(2026, 8, 29, 14, 0),
                minimum_lead=timedelta(minutes=30),
            )
        self.assertEqual(raised.exception.error_code, "tiktok_schedule_invalid")

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(TikTokScheduleContractError) as raised:
            parse_tiktok_schedule_fields(
                enable_timer=True,
                schedule_time="2026-02-29 15:00",
                schedule_timezone="Asia/Shanghai",
                daily_times=["15:00"],
            )
        self.assertEqual(raised.exception.error_code, "tiktok_schedule_invalid")

    def test_duplicate_daily_times_are_rejected(self):
        with self.assertRaises(TikTokScheduleContractError) as raised:
            parse_tiktok_schedule_fields(
                enable_timer=True,
                schedule_time="2026-08-29 15:00",
                schedule_timezone="Asia/Shanghai",
                daily_times=["15:00", "15:00"],
            )
        self.assertEqual(raised.exception.error_code, "tiktok_schedule_invalid")

    def test_non_boolean_enable_timer_is_rejected(self):
        for value in (0, 1, "false", None):
            with self.subTest(value=value), self.assertRaises(TikTokScheduleContractError):
                parse_tiktok_schedule_fields(
                    enable_timer=value,
                    schedule_time=None,
                    schedule_timezone="Asia/Shanghai",
                    daily_times=[],
                )

    def test_conflicting_or_wrong_timezone_fields_are_rejected(self):
        cases = (
            (True, "2026-08-29 15:00", "UTC", ["15:00"]),
            (True, "2026-08-29 15:00", "Asia/Shanghai", []),
            (False, "2026-08-29 15:00", "Asia/Shanghai", ["15:00"]),
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(TikTokScheduleContractError):
                parse_tiktok_schedule_fields(
                    enable_timer=values[0],
                    schedule_time=values[1],
                    schedule_timezone=values[2],
                    daily_times=values[3],
                )

    def test_system_timezone_cannot_change_shanghai_result(self):
        original_tz = os.environ.get("TZ")
        observations = []
        try:
            for system_tz in ("UTC", "America/Los_Angeles"):
                os.environ["TZ"] = system_tz
                if hasattr(time, "tzset"):
                    time.tzset()
                intent = parse_tiktok_schedule_fields(
                    enable_timer=True,
                    schedule_time="2026-08-29 14:30",
                    schedule_timezone="Asia/Shanghai",
                    daily_times=["14:30"],
                )
                now = datetime(2026, 8, 29, 14, 0, tzinfo=SHANGHAI)
                validate_tiktok_schedule_window(
                    intent,
                    now=now,
                    minimum_lead=timedelta(minutes=30),
                )
                observations.append((intent.scheduled_at, intent.timezone))
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            if hasattr(time, "tzset"):
                time.tzset()
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(observations[0][0], datetime(2026, 8, 29, 14, 30, tzinfo=SHANGHAI))


if __name__ == "__main__":
    unittest.main()
