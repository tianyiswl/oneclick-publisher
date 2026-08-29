# -*- coding: utf-8 -*-
"""TikTok Studio 平台原生定时表单的语义边界测试。"""

from __future__ import annotations

import hashlib
import unittest
import unicodedata
from datetime import datetime, timedelta
from typing import Callable
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app_core.overseas_tiktok_errors import TikTokPublishError
from uploader.tk_uploader.schedule_form import (
    ACCEPTANCE_FEEDBACK_SELECTORS,
    FINAL_ACTION_SELECTORS,
    SCHEDULE_DATE_SELECTORS,
    SCHEDULE_TIME_SELECTORS,
    SCHEDULE_TOGGLE_SELECTORS,
    SCHEDULED_ROW_SELECTORS,
    TikTokScheduledContentExpectation,
    TikTokScheduleForm,
    TikTokScheduleTarget,
    _normalized_label,
)


class FakeScheduleLocator:
    def __init__(self, controls: list[FakeScheduleControl]) -> None:
        self._controls = controls

    async def count(self) -> int:
        return len(self._controls)

    def nth(self, index: int) -> FakeScheduleControl:
        return self._controls[index]

    @property
    def first(self) -> FakeScheduleControl:
        return self._controls[0]


class FakeScheduleControl:
    def __init__(
        self,
        node_id: str,
        semantic_role: str,
        *,
        label: str = "",
        value: str = "",
        visible: bool = True,
        enabled: bool = True,
        editable: bool = False,
        read_only: bool = False,
        checked: bool = False,
        attributes: dict[str, str | None] | None = None,
        fill_transform: Callable[[str], str] | None = None,
        raise_if_clicked: bool = False,
        visibility_failures: int = 0,
        text_failures: int = 0,
    ) -> None:
        self.node_id = node_id
        self.semantic_role = semantic_role
        self.label = label
        self.value = value
        self.visible = visible
        self.enabled = enabled
        self.editable = editable
        self.read_only = read_only
        self.checked = checked
        self.attributes = dict(attributes or {})
        self.fill_transform = fill_transform
        self.raise_if_clicked = raise_if_clicked
        self.visibility_failures = visibility_failures
        self.text_failures = text_failures
        self.click_count = 0
        self.fill_count = 0
        self.detached = False
        self.on_click: Callable[[FakeScheduleControl], None] | None = None
        self.on_fill: Callable[[FakeScheduleControl, str], None] | None = None
        self._children: dict[str, list[FakeScheduleControl]] = {}

    def register(self, selector: str, *controls: FakeScheduleControl) -> None:
        self._children.setdefault(selector, []).extend(controls)

    def locator(self, selector: str) -> FakeScheduleLocator:
        return FakeScheduleLocator(_deduplicated(self._children.get(selector, [])))

    def _assert_attached(self) -> None:
        if self.detached:
            raise RuntimeError(f"detached control: {self.node_id}")

    async def click(self) -> None:
        self._assert_attached()
        if self.raise_if_clicked:
            raise AssertionError(f"forbidden row action clicked: {self.semantic_role}")
        if not self.visible or not self.enabled:
            raise RuntimeError(f"unusable control: {self.node_id}")
        self.click_count += 1
        if self.semantic_role == "switch":
            self.checked = not self.checked
        if self.on_click is not None:
            self.on_click(self)

    async def fill(self, value: str) -> None:
        self._assert_attached()
        if not self.visible or not self.enabled or not self.editable or self.read_only:
            raise RuntimeError(f"uneditable control: {self.node_id}")
        self.fill_count += 1
        self.value = self.fill_transform(value) if self.fill_transform else value
        if self.on_fill is not None:
            self.on_fill(self, self.value)

    async def input_value(self) -> str:
        self._assert_attached()
        return self.value

    async def inner_text(self) -> str:
        self._assert_attached()
        if self.text_failures:
            self.text_failures -= 1
            raise RuntimeError("transient feedback text failure")
        return self.label

    async def get_attribute(self, name: str) -> str | None:
        self._assert_attached()
        if name == "aria-checked":
            return "true" if self.checked else "false"
        if name == "checked":
            return "true" if self.checked else None
        if name == "readonly":
            return "true" if self.read_only else None
        if name == "disabled":
            return None if self.enabled else "true"
        if name == "aria-label":
            return self.label or None
        return self.attributes.get(name)

    async def is_visible(self) -> bool:
        self._assert_attached()
        if self.visibility_failures:
            self.visibility_failures -= 1
            raise RuntimeError("transient feedback visibility failure")
        return self.visible

    async def is_enabled(self) -> bool:
        self._assert_attached()
        return self.enabled

    async def is_editable(self) -> bool:
        self._assert_attached()
        return self.editable and not self.read_only


def _deduplicated(
    controls: list[FakeScheduleControl],
) -> list[FakeScheduleControl]:
    unique: dict[str, FakeScheduleControl] = {}
    for control in controls:
        unique.setdefault(control.node_id, control)
    return list(unique.values())


class FakeScheduleBase:
    def __init__(self, base_id: str) -> None:
        self.base_id = base_id
        self._registered: dict[str, list[FakeScheduleControl]] = {}
        self.controls: list[FakeScheduleControl] = []
        self.locator_failures: dict[str, int] = {}

    def register(self, selector: str, *controls: FakeScheduleControl) -> None:
        self._registered.setdefault(selector, []).extend(controls)
        self.controls.extend(control for control in controls if control not in self.controls)

    def locator(self, selector: str) -> FakeScheduleLocator:
        if self.locator_failures.get(selector, 0):
            self.locator_failures[selector] -= 1
            raise RuntimeError("transient feedback locator failure")
        return FakeScheduleLocator(_deduplicated(self._registered.get(selector, [])))

    def by_role(self, semantic_role: str) -> list[FakeScheduleControl]:
        return [
            control
            for control in _deduplicated(self.controls)
            if control.semantic_role == semantic_role
        ]

    def detach(self) -> None:
        for control in _deduplicated(self.controls):
            control.detached = True


class FakeSchedulePage(FakeScheduleBase):
    def __init__(self) -> None:
        super().__init__("page")
        self.url = "https://www.tiktok.com/tiktokstudio/upload"
        self.goto_calls: list[str] = []
        self.goto_failures: set[str] = set()
        self.account_reference = "expected.user"

    async def goto(self, route: str) -> None:
        self.goto_calls.append(route)
        if route in self.goto_failures:
            raise RuntimeError("bounded route unavailable")
        self.url = route

    def set_feedback(self, *messages: str) -> None:
        controls = [
            FakeScheduleControl(
                f"feedback-{index}",
                "feedback",
                label=message,
            )
            for index, message in enumerate(messages)
        ]
        for selector in ACCEPTANCE_FEEDBACK_SELECTORS:
            self.register(selector, *controls)

    def set_rows(self, *rows: FakeScheduleControl) -> None:
        for selector in SCHEDULED_ROW_SELECTORS:
            self.register(selector, *rows)


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.current = datetime(2026, 8, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds
        self.current += timedelta(seconds=seconds)


def _register_form_controls(
    base: FakeScheduleBase,
    *,
    suffix: str,
    toggle_count: int,
    toggle_enabled: bool,
    date_value: str,
    time_value: str,
    final_labels: tuple[str, ...],
) -> None:
    toggles = [
        FakeScheduleControl(
            f"toggle-{suffix}-{index}",
            "switch",
            label="Schedule",
            checked=toggle_enabled,
        )
        for index in range(toggle_count)
    ]
    dates = [
        FakeScheduleControl(
            f"date-{suffix}",
            "date",
            label="Schedule date",
            value=date_value,
            editable=True,
        )
    ]
    times = [
        FakeScheduleControl(
            f"time-{suffix}",
            "time",
            label="Schedule time",
            value=time_value,
            editable=True,
        )
    ]
    buttons = [
        FakeScheduleControl(
            f"final-{suffix}-{index}",
            "final_action",
            label=label,
        )
        for index, label in enumerate(final_labels)
    ]
    for selector in SCHEDULE_TOGGLE_SELECTORS:
        base.register(selector, *toggles)
    for selector in SCHEDULE_DATE_SELECTORS:
        base.register(selector, *dates)
    for selector in SCHEDULE_TIME_SELECTORS:
        base.register(selector, *times)
    for selector in FINAL_ACTION_SELECTORS:
        base.register(selector, *buttons)


def scheduled_form_page(
    *,
    toggle_count: int = 1,
    toggle_enabled: bool = False,
    date_value: str = "2026-08-29",
    time_value: str = "15:00",
    final_button: str = "Schedule",
    remount_after_writes: bool = False,
) -> tuple[FakeSchedulePage, list[FakeScheduleBase]]:
    """Return one fake page plus the exact base sequence consumed by resolve_base."""
    page = FakeSchedulePage()
    base_count = 24 if remount_after_writes else 1
    bases: list[FakeScheduleBase] = []
    for index in range(base_count):
        base = FakeScheduleBase(f"base-{index}")
        _register_form_controls(
            base,
            suffix=str(index),
            toggle_count=toggle_count,
            toggle_enabled=toggle_enabled,
            date_value=date_value,
            time_value=time_value,
            final_labels=(final_button,),
        )
        bases.append(base)
    if not remount_after_writes:
        return page, bases * 64

    def propagate_switch(control: FakeScheduleControl) -> None:
        current_index = int(control.node_id.split("-")[1])
        bases[current_index].detach()
        for future in bases[current_index + 1 :]:
            for toggle in future.by_role("switch"):
                toggle.checked = control.checked

    def propagate_value(control: FakeScheduleControl, value: str) -> None:
        current_index = int(control.node_id.split("-")[1])
        bases[current_index].detach()
        for future in bases[current_index + 1 :]:
            for item in future.by_role(control.semantic_role):
                item.value = value

    for base in bases:
        for toggle in base.by_role("switch"):
            toggle.on_click = propagate_switch
        for date in base.by_role("date"):
            date.on_fill = propagate_value
        for time_control in base.by_role("time"):
            time_control.on_fill = propagate_value
    return page, bases


def scheduled_row(
    *,
    node_id: str = "row-1",
    account_reference: str | None = "expected.user",
    caption: str = "Hello\nWorld",
    scheduled_at: str = "2026-08-29 15:00",
    timezone: str = "Asia/Shanghai",
    content_id: str | None = "7654321",
    content_url: str | None = "https://www.tiktok.com/@expected.user/video/7654321",
) -> tuple[FakeScheduleControl, list[FakeScheduleControl]]:
    row = FakeScheduleControl(
        node_id,
        "scheduled_row",
        label=caption,
        attributes={
            "data-account-reference": account_reference,
            "data-caption": caption,
            "data-schedule-time": scheduled_at,
            "data-schedule-timezone": timezone,
            "data-content-id": content_id,
            "data-content-url": content_url,
        },
    )
    actions = [
        FakeScheduleControl(
            f"{node_id}-{name}",
            name,
            label=name,
            raise_if_clicked=True,
        )
        for name in ("edit", "publish", "retry", "delete")
    ]
    for action in actions:
        row.register(f'[data-action="{action.semantic_role}"]', action)
    return row, actions


def normalized_caption(value: str) -> str:
    return unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace(
        "\r", "\n"
    ).strip()


class TikTokScheduleFormTests(unittest.IsolatedAsyncioTestCase):
    target = TikTokScheduleTarget("2026-08-29 15:00", "Asia/Shanghai")

    def form(
        self,
        page: FakeSchedulePage,
        bases: list[FakeScheduleBase],
        *,
        clock: FakeClock | None = None,
    ) -> tuple[TikTokScheduleForm, AsyncMock, AsyncMock]:
        resolver = AsyncMock(side_effect=bases)
        intervention = AsyncMock()
        kwargs: dict[str, object] = {}
        if clock is not None:
            kwargs = {
                "monotonic": clock.monotonic,
                "now": clock.now,
                "sleep": clock.sleep,
            }
        return (
            TikTokScheduleForm(
                page,
                resolve_base=resolver,
                wait_for_manual_intervention=intervention,
                **kwargs,
            ),
            resolver,
            intervention,
        )

    async def test_configures_and_reads_exact_beijing_schedule(self) -> None:
        page, bases = scheduled_form_page()
        form, _, _ = self.form(page, bases)

        snapshot = await form.configure(self.target)

        self.assertEqual(snapshot.scheduled_at, "2026-08-29 15:00")
        self.assertEqual(snapshot.timezone, "Asia/Shanghai")
        self.assertTrue(snapshot.toggle_enabled)
        self.assertEqual(snapshot.final_action_label, "Schedule")
        self.assertTrue(snapshot.final_action_ready)
        self.assertEqual(
            snapshot.as_dict(),
            {
                "scheduleMode": "platform_native",
                "scheduledAt": "2026-08-29 15:00",
                "scheduleTimezone": "Asia/Shanghai",
                "scheduleToggleEnabled": True,
                "finalActionLabel": "Schedule",
                "finalActionReady": True,
            },
        )
        self.assertEqual(bases[0].by_role("final_action")[0].click_count, 0)

    async def test_missing_schedule_control_never_falls_back_to_post(self) -> None:
        page, bases = scheduled_form_page(toggle_count=0, final_button="Post")
        form, _, _ = self.form(page, bases)

        with self.assertRaises(TikTokPublishError) as raised:
            await form.configure(self.target)

        self.assertEqual(raised.exception.error_code, "tiktok_schedule_unavailable")

    async def test_two_visible_toggles_are_ambiguous(self) -> None:
        page, bases = scheduled_form_page(toggle_count=2)
        form, _, _ = self.form(page, bases)

        with self.assertRaises(TikTokPublishError) as raised:
            await form.configure(self.target)

        self.assertEqual(
            raised.exception.error_code, "tiktok_schedule_control_ambiguous"
        )

    async def test_disabled_schedule_control_is_unavailable(self) -> None:
        page, bases = scheduled_form_page()
        bases[0].by_role("switch")[0].enabled = False
        form, _, _ = self.form(page, bases)

        with self.assertRaises(TikTokPublishError) as raised:
            await form.configure(self.target)

        self.assertEqual(raised.exception.error_code, "tiktok_schedule_unavailable")

    async def test_ambiguous_date_time_and_final_controls_share_stable_code(self) -> None:
        for semantic_role, selectors in (
            ("date", SCHEDULE_DATE_SELECTORS),
            ("time", SCHEDULE_TIME_SELECTORS),
            ("final_action", FINAL_ACTION_SELECTORS),
        ):
            with self.subTest(semantic_role=semantic_role):
                page, bases = scheduled_form_page(toggle_enabled=True)
                first = bases[0].by_role(semantic_role)[0]
                duplicate = FakeScheduleControl(
                    f"{semantic_role}-duplicate",
                    semantic_role,
                    label=first.label,
                    value=first.value,
                    editable=first.editable,
                )
                for selector in selectors:
                    bases[0].register(selector, duplicate)
                form, _, _ = self.form(page, bases)
                with self.assertRaises(TikTokPublishError) as raised:
                    await form.verify(self.target)
                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_schedule_control_ambiguous",
                )

    async def test_date_time_normalization_and_minute_rewrite_are_rejected(self) -> None:
        for role, rewritten in (("date", "08/29/2026"), ("time", "15:01")):
            with self.subTest(role=role):
                page, bases = scheduled_form_page()
                control = bases[0].by_role(role)[0]
                control.fill_transform = lambda _value, result=rewritten: result
                form, _, _ = self.form(page, bases)
                with self.assertRaises(TikTokPublishError) as raised:
                    await form.configure(self.target)
                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_schedule_readback_mismatch",
                )

    async def test_post_or_mixed_final_action_is_never_accepted(self) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True, final_button="Post")
        form, _, _ = self.form(page, bases)
        with self.assertRaises(TikTokPublishError) as post_only:
            await form.verify(self.target)
        self.assertEqual(
            post_only.exception.error_code,
            "tiktok_schedule_final_action_unavailable",
        )

        page, bases = scheduled_form_page(toggle_enabled=True)
        post = FakeScheduleControl("post-extra", "final_action", label="Post")
        for selector in FINAL_ACTION_SELECTORS:
            bases[0].register(selector, post)
        form, _, _ = self.form(page, bases)
        with self.assertRaises(TikTokPublishError) as mixed:
            await form.verify(self.target)
        self.assertEqual(
            mixed.exception.error_code,
            "tiktok_schedule_control_ambiguous",
        )

    async def test_exact_english_and_chinese_schedule_labels_are_canonicalized(self) -> None:
        for label in ("Schedule", "定时发布", "排期"):
            with self.subTest(label=label):
                page, bases = scheduled_form_page(
                    toggle_enabled=True,
                    final_button=label,
                )
                form, _, _ = self.form(page, bases)
                snapshot = await form.verify(self.target)
                self.assertEqual(snapshot.final_action_label, "Schedule")
                self.assertEqual(
                    bases[0].by_role("final_action")[0].click_count,
                    0,
                )

    async def test_each_control_step_reacquires_after_react_remount(self) -> None:
        page, bases = scheduled_form_page(
            date_value="2026-08-01",
            time_value="15:59",
            remount_after_writes=True,
        )
        form, resolver, _ = self.form(page, bases)

        snapshot = await form.configure(self.target)

        self.assertEqual(snapshot.scheduled_at, "2026-08-29 15:00")
        self.assertEqual(resolver.await_count, 10)
        self.assertEqual(bases[0].by_role("switch")[0].click_count, 1)
        self.assertEqual(bases[2].by_role("date")[0].fill_count, 1)
        self.assertEqual(bases[4].by_role("time")[0].fill_count, 1)
        used_node_ids = {
            bases[0].by_role("switch")[0].node_id,
            bases[2].by_role("date")[0].node_id,
            bases[4].by_role("time")[0].node_id,
            bases[9].by_role("final_action")[0].node_id,
        }
        self.assertEqual(len(used_node_ids), 4)

    def test_label_normalization_is_nfkc_whitespace_and_casefolded(self) -> None:
        self.assertEqual(_normalized_label("  Ｓｃｈｅｄｕｌｅ\n"), "schedule")
        self.assertEqual(_normalized_label("  定时   发布  "), "定时 发布")

    async def test_wait_for_acceptance_requires_explicit_scheduled_signal(self) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True)
        page.set_feedback("Video scheduled successfully")
        clock = FakeClock()
        form, _, intervention = self.form(page, bases, clock=clock)

        acceptance = await form.wait_for_acceptance(self.target)

        self.assertEqual(acceptance.evidence, "Video scheduled successfully")
        self.assertEqual(acceptance.accepted_at, clock.current)
        intervention.assert_awaited_once_with(page)

    async def test_wait_for_acceptance_rejection_and_timeout_are_terminal(self) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True)
        page.set_feedback("Couldn't schedule this video")
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)
        with self.assertRaises(TikTokPublishError) as rejected:
            await form.wait_for_acceptance(self.target)
        self.assertEqual(rejected.exception.error_code, "tiktok_publish_rejected")
        self.assertFalse(rejected.exception.outcome_ambiguous)

        page, bases = scheduled_form_page(toggle_enabled=True)
        page.url = "https://www.tiktok.com/tiktokstudio/content"
        clock = FakeClock()
        form, _, intervention = self.form(page, bases, clock=clock)
        with self.assertRaises(TikTokPublishError) as timeout:
            await form.wait_for_acceptance(self.target)
        self.assertEqual(
            timeout.exception.error_code,
            "tiktok_schedule_outcome_unknown",
        )
        self.assertTrue(timeout.exception.outcome_ambiguous)
        self.assertEqual(clock.elapsed, 120.0)
        self.assertEqual(intervention.await_count, 120)

    async def test_wait_for_acceptance_rejects_negated_or_reversed_success_words(
        self,
    ) -> None:
        messages = (
            "Video has not been scheduled successfully",
            "This video has been scheduled successfully? No—please try again.",
            "Video scheduled successfully but failed",
            "Video scheduled successfully? Retry",
            "Video scheduled successfully but then cancelled",
        )
        for message in messages:
            with self.subTest(message=message):
                page, bases = scheduled_form_page(toggle_enabled=True)
                page.set_feedback(message)
                clock = FakeClock()
                form, _, _ = self.form(page, bases, clock=clock)
                with self.assertRaises(TikTokPublishError) as raised:
                    await form.wait_for_acceptance(self.target)
                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_publish_rejected",
                )
                self.assertFalse(raised.exception.outcome_ambiguous)

    async def test_wait_for_acceptance_ignores_stale_and_unrelated_alerts(
        self,
    ) -> None:
        messages = (
            "Your previous video has been scheduled successfully",
            "Upload completed successfully",
        )
        for message in messages:
            with self.subTest(message=message):
                page, bases = scheduled_form_page(toggle_enabled=True)
                page.set_feedback(message)
                clock = FakeClock()
                form, _, _ = self.form(page, bases, clock=clock)
                with self.assertRaises(TikTokPublishError) as raised:
                    await form.wait_for_acceptance(self.target)
                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_schedule_outcome_unknown",
                )
                self.assertTrue(raised.exception.outcome_ambiguous)
                self.assertEqual(clock.elapsed, 120.0)

    async def test_transient_feedback_read_errors_stay_inside_bounded_poll(
        self,
    ) -> None:
        for failure_kind in ("locator", "visibility", "text"):
            with self.subTest(failure_kind=failure_kind):
                page, bases = scheduled_form_page(toggle_enabled=True)
                page.set_feedback("Video scheduled successfully")
                feedback = page.by_role("feedback")[0]
                if failure_kind == "locator":
                    page.locator_failures[ACCEPTANCE_FEEDBACK_SELECTORS[0]] = 1
                elif failure_kind == "visibility":
                    feedback.visibility_failures = 1
                else:
                    feedback.text_failures = 1
                clock = FakeClock()
                form, _, _ = self.form(page, bases, clock=clock)

                acceptance = await form.wait_for_acceptance(self.target)

                self.assertEqual(
                    acceptance.evidence,
                    "Video scheduled successfully",
                )
                self.assertEqual(clock.elapsed, 1.0)
                self.assertEqual(clock.sleeps, [1.0])

    async def test_unreadable_feedback_until_deadline_is_outcome_unknown(
        self,
    ) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True)
        page.set_feedback("Video scheduled successfully")
        page.locator_failures[ACCEPTANCE_FEEDBACK_SELECTORS[0]] = 200
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)

        with self.assertRaises(TikTokPublishError) as raised:
            await form.wait_for_acceptance(self.target)

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_schedule_outcome_unknown",
        )
        self.assertTrue(raised.exception.outcome_ambiguous)
        self.assertEqual(clock.elapsed, 120.0)
        self.assertEqual(len(clock.sleeps), 120)

    def expectation(
        self,
        *,
        account_reference: str = "expected.user",
        caption: str = "Hello\nWorld",
        target: TikTokScheduleTarget | None = None,
        submitted_after: datetime | None = None,
    ) -> TikTokScheduledContentExpectation:
        normalized = normalized_caption(caption)
        return TikTokScheduledContentExpectation(
            account_reference=account_reference,
            expected_caption=caption,
            caption_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            target=target or self.target,
            submitted_after=submitted_after
            or datetime(2026, 8, 29, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    async def test_readback_returns_only_one_exact_row(self) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True)
        row, _ = scheduled_row(caption=" Ｈｅｌｌｏ\r\nWorld ")
        page.set_rows(row)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)

        readback = await form.readback_scheduled_content(
            self.expectation(caption="Hello\nWorld")
        )

        self.assertIsNotNone(readback)
        assert readback is not None
        self.assertEqual(readback.content_id, "7654321")
        self.assertEqual(
            readback.content_url,
            "https://www.tiktok.com/@expected.user/video/7654321",
        )
        self.assertEqual(readback.scheduled_at, "2026-08-29 15:00")
        self.assertEqual(readback.schedule_timezone, "Asia/Shanghai")
        self.assertEqual(page.goto_calls, ["https://www.tiktok.com/tiktokstudio/content"])

    async def test_readback_requires_both_page_and_row_account_references(
        self,
    ) -> None:
        for missing_reference in ("page", "row"):
            with self.subTest(missing_reference=missing_reference):
                page, bases = scheduled_form_page(toggle_enabled=True)
                row_account = (
                    None if missing_reference == "row" else "expected.user"
                )
                row, _ = scheduled_row(account_reference=row_account)
                page.set_rows(row)
                if missing_reference == "page":
                    del page.account_reference
                clock = FakeClock()
                form, _, _ = self.form(page, bases, clock=clock)

                self.assertIsNone(
                    await form.readback_scheduled_content(self.expectation())
                )
                self.assertEqual(clock.elapsed, 120.0)

    async def test_readback_zero_or_multiple_rows_is_not_success(self) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)
        self.assertIsNone(
            await form.readback_scheduled_content(self.expectation())
        )
        self.assertEqual(clock.elapsed, 120.0)

        page, bases = scheduled_form_page(toggle_enabled=True)
        first, _ = scheduled_row(node_id="row-1")
        second, _ = scheduled_row(node_id="row-2")
        page.set_rows(first, second)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)
        self.assertIsNone(
            await form.readback_scheduled_content(self.expectation())
        )
        self.assertEqual(clock.elapsed, 0.0)

    async def test_readback_rejects_each_mismatched_identity_field(self) -> None:
        cases = (
            {"account_reference": "wrong.user"},
            {"caption": "Different caption"},
            {"scheduled_at": "2026-08-29 15:01"},
            {"timezone": "UTC"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                page, bases = scheduled_form_page(toggle_enabled=True)
                row, _ = scheduled_row(**overrides)
                page.set_rows(row)
                clock = FakeClock()
                form, _, _ = self.form(page, bases, clock=clock)
                self.assertIsNone(
                    await form.readback_scheduled_content(self.expectation())
                )

        page, bases = scheduled_form_page(toggle_enabled=True)
        page.account_reference = "wrong.page"
        row, _ = scheduled_row()
        page.set_rows(row)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)
        self.assertIsNone(
            await form.readback_scheduled_content(self.expectation())
        )

        page, bases = scheduled_form_page(toggle_enabled=True)
        row, _ = scheduled_row()
        page.set_rows(row)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)
        self.assertIsNone(
            await form.readback_scheduled_content(
                self.expectation(submitted_after=clock.current + timedelta(minutes=5))
            )
        )

    async def test_readback_never_clicks_row_actions(self) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True)
        row, actions = scheduled_row()
        page.set_rows(row)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)

        readback = await form.readback_scheduled_content(self.expectation())

        self.assertIsNotNone(readback)
        self.assertEqual([action.click_count for action in actions], [0, 0, 0, 0])


if __name__ == "__main__":
    unittest.main()
