# -*- coding: utf-8 -*-
"""TikTok Studio 平台原生定时表单的语义边界测试。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unittest
import unicodedata
from datetime import datetime, timedelta
from typing import Callable
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from playwright.async_api import Error as PlaywrightError

from app_core.overseas_tiktok_errors import TikTokPublishError
from uploader.tk_uploader import schedule_form as tiktok_schedule_form
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


TEST_PAGE_ACCOUNT_REFERENCE_SELECTORS = (
    'a[data-e2e="nav-profile"][href*="/@"]',
    'a[data-e2e="profile-link"][href*="/@"]',
    '[data-e2e="user-avatar"] a[href*="/@"]',
)
TEST_ROW_ACCOUNT_REFERENCE_SELECTORS = (
    'a[data-e2e="content-row-account"][href*="/@"]',
    'a[data-e2e="account-link"][href*="/@"]',
)
EXACT_SCHEDULE_RADIO_SELECTOR = (
    '[data-e2e="schedule-settings"] [role="radio"][aria-label="Schedule"]'
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
        checked_attribute_tracks_state: bool = True,
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
        self.checked_attribute_tracks_state = checked_attribute_tracks_state
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
        if self.semantic_role in {"switch", "radio", "checkbox"}:
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
            if not self.checked_attribute_tracks_state:
                return None
            return "true" if self.checked else "false"
        if name == "checked":
            if not self.checked_attribute_tracks_state:
                return None
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

    async def is_checked(self) -> bool:
        self._assert_attached()
        return self.checked

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
        self._accessible_labels: dict[str, str] = {}

    def register(self, selector: str, *controls: FakeScheduleControl) -> None:
        self._registered.setdefault(selector, []).extend(controls)
        self.controls.extend(control for control in controls if control not in self.controls)

    def locator(self, selector: str) -> FakeScheduleLocator:
        if self.locator_failures.get(selector, 0):
            self.locator_failures[selector] -= 1
            raise RuntimeError("transient feedback locator failure")
        return FakeScheduleLocator(_deduplicated(self._registered.get(selector, [])))

    def set_accessible_label(self, label_id: str, value: str) -> None:
        self._accessible_labels[label_id] = value

    def get_by_role(
        self,
        semantic_role: str,
        *,
        name: str,
        exact: bool,
    ) -> FakeScheduleLocator:
        def accessible_name(control: FakeScheduleControl) -> str:
            labelled_by = str(control.attributes.get("aria-labelledby") or "")
            labelled = " ".join(
                self._accessible_labels.get(label_id, "")
                for label_id in labelled_by.split()
            ).strip()
            return labelled or str(control.attributes.get("aria-label") or control.label)

        return FakeScheduleLocator(
            [
                control
                for control in self.by_role(semantic_role)
                if (accessible_name(control) == name if exact else name in accessible_name(control))
            ]
        )

    def by_role(self, semantic_role: str) -> list[FakeScheduleControl]:
        return [
            control
            for control in _deduplicated(self.controls)
            if control.semantic_role == semantic_role
        ]

    def detach(self) -> None:
        for control in _deduplicated(self.controls):
            control.detached = True


class LiveScheduleLocator:
    """A locator that re-resolves to a different DOM node on each handle read."""

    def __init__(self, snapshots: list[FakeScheduleControl]) -> None:
        self._snapshots = snapshots
        self._position = 0

    async def count(self) -> int:
        return 1

    def nth(self, index: int) -> LiveScheduleLocator:
        if index != 0:
            raise IndexError(index)
        return self

    @property
    def node_id(self) -> str:
        return self._snapshots[min(self._position, len(self._snapshots) - 1)].node_id

    async def element_handle(self) -> FakeScheduleControl:
        snapshot = self._snapshots[min(self._position, len(self._snapshots) - 1)]
        self._position += 1
        return snapshot

    async def is_visible(self) -> bool:
        return await self._snapshots[min(self._position, len(self._snapshots) - 1)].is_visible()

    async def is_enabled(self) -> bool:
        return await self._snapshots[min(self._position, len(self._snapshots) - 1)].is_enabled()

    async def get_attribute(self, name: str) -> str | None:
        return await self._snapshots[min(self._position, len(self._snapshots) - 1)].get_attribute(name)


class HangingScheduleLocator:
    async def count(self) -> int:
        await asyncio.Future()
        raise AssertionError("unreachable")


class TransientCountThenEmptyLocator:
    """A real Playwright transient while a later selector is enumerated."""

    def __init__(self, failures: int) -> None:
        self._failures = failures

    async def count(self) -> int:
        if self._failures:
            self._failures -= 1
            raise PlaywrightError(
                "Execution context was destroyed, most likely because of a navigation"
            )
        return 0

    def nth(self, index: int) -> object:
        raise IndexError(index)


class TransientVisibilityScheduleControl(FakeScheduleControl):
    """A real Playwright transient while the second candidate is checked."""

    def __init__(self, *args: object, visibility_failures: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._playwright_visibility_failures = visibility_failures

    async def is_visible(self) -> bool:
        if self._playwright_visibility_failures:
            self._playwright_visibility_failures -= 1
            raise PlaywrightError(
                "Execution context was destroyed, most likely because of a navigation"
            )
        return await super().is_visible()


class HandleRecord:
    def __init__(self, identity: str) -> None:
        self.identity = identity


class HandleWrapper(FakeScheduleControl):
    """Different Python wrappers for one immutable ElementHandle identity."""

    def __init__(self, record: HandleRecord) -> None:
        super().__init__("wrapper", "switch", checked=True)
        del self.node_id
        self._record = record

    async def evaluate(self, expression: str, other: HandleWrapper) -> bool:
        if expression != "(element, other) => element === other":
            raise RuntimeError("comparison protocol bug")
        return self._record is other._record


class BrokenCompareHandle(HandleWrapper):
    async def evaluate(self, expression: str, other: HandleWrapper) -> bool:
        raise RuntimeError("comparison protocol bug")


class ContextHandle(HandleWrapper):
    def __init__(self, record: HandleRecord, context: str) -> None:
        super().__init__(record)
        self.context = context

    async def evaluate(self, expression: str, other: HandleWrapper) -> bool:
        if not isinstance(other, ContextHandle) or self.context != other.context:
            raise RuntimeError("cross execution context compare")
        return await super().evaluate(expression, other)


class UploadRemountHandle(HandleWrapper):
    def __init__(self, record: HandleRecord, message: str = "JSHandles can be evaluated only in the context they were created") -> None:
        super().__init__(record)
        self._message = message

    async def evaluate(self, expression: str, other: HandleWrapper) -> bool:
        if not isinstance(other, UploadRemountHandle) or self._record is not other._record:
            raise PlaywrightError(self._message)
        return True


class OtherPlaywrightErrorHandle(HandleWrapper):
    def __init__(self, record: HandleRecord, message: str = "unrelated Playwright comparison failure") -> None:
        super().__init__(record)
        self._message = message

    async def evaluate(self, expression: str, other: HandleWrapper) -> bool:
        raise PlaywrightError(self._message)


class WrapperScheduleLocator:
    def __init__(self, factories: list[Callable[[], HandleWrapper]]) -> None:
        self._factories = factories
        self._index = 0

    async def count(self) -> int:
        return 1

    def nth(self, index: int) -> WrapperScheduleLocator:
        if index != 0:
            raise IndexError(index)
        return self

    async def element_handle(self) -> HandleWrapper:
        factory = self._factories[min(self._index, len(self._factories) - 1)]
        self._index += 1
        return factory()


class LiveScheduleBase(FakeScheduleBase):
    def __init__(self, live_locators: dict[str, object]) -> None:
        super().__init__("live-base")
        self._live_locators = live_locators

    def locator(self, selector: str) -> object:
        if selector in self._live_locators:
            return self._live_locators[selector]
        return super().locator(selector)


class FakeSchedulePage(FakeScheduleBase):
    def __init__(self) -> None:
        super().__init__("page")
        self.url = "https://www.tiktok.com/tiktokstudio/upload"
        self.goto_calls: list[str] = []
        self.goto_failures: set[str] = set()
        self._rows_after_next_navigation: list[FakeScheduleControl] | None = None

    async def goto(self, route: str) -> None:
        self.goto_calls.append(route)
        if route in self.goto_failures:
            raise RuntimeError("bounded route unavailable")
        self.url = route
        if self._rows_after_next_navigation is not None:
            self.replace_rows(*self._rows_after_next_navigation)
            self._rows_after_next_navigation = None

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

    def replace_rows(self, *rows: FakeScheduleControl) -> None:
        for selector in SCHEDULED_ROW_SELECTORS:
            self._registered[selector] = list(rows)

    def reveal_rows_on_next_navigation(self, *rows: FakeScheduleControl) -> None:
        self._rows_after_next_navigation = list(rows)

    def set_page_account_references(self, *account_references: str) -> None:
        links = [
            FakeScheduleControl(
                f"page-account-{index}-{reference}",
                "page_account_link",
                label=f"@{reference}",
                attributes={"href": f"https://www.tiktok.com/@{reference}"},
            )
            for index, reference in enumerate(account_references)
        ]
        for selector in TEST_PAGE_ACCOUNT_REFERENCE_SELECTORS:
            self._registered[selector] = list(links)
        self.controls.extend(link for link in links if link not in self.controls)


class ContextSchedulePage(FakeSchedulePage):
    def __init__(self, live_locators: dict[str, object]) -> None:
        super().__init__()
        self._live_locators = live_locators

    def locator(self, selector: str) -> object:
        if selector in self._live_locators:
            return self._live_locators[selector]
        return super().locator(selector)


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.current = datetime(2026, 8, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[float], None] | None = None

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds
        self.current += timedelta(seconds=seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)


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
    page.set_page_account_references("expected.user")
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
    account_reference: str | tuple[str, ...] | None = "expected.user",
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
            "data-caption": caption,
            "data-schedule-time": scheduled_at,
            "data-schedule-timezone": timezone,
            "data-content-id": content_id,
            "data-content-url": content_url,
        },
    )
    account_references = (
        account_reference
        if isinstance(account_reference, tuple)
        else (account_reference,)
        if account_reference is not None
        else ()
    )
    account_links = [
        FakeScheduleControl(
            f"{node_id}-account-{index}-{reference}",
            "row_account_link",
            label=f"@{reference}",
            attributes={"href": f"https://www.tiktok.com/@{reference}"},
        )
        for index, reference in enumerate(account_references)
    ]
    for selector in TEST_ROW_ACCOUNT_REFERENCE_SELECTORS:
        row.register(selector, *account_links)
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
        control_timeout_seconds: float | None = None,
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
        if control_timeout_seconds is not None:
            kwargs["control_timeout_seconds"] = control_timeout_seconds
        return (
            TikTokScheduleForm(
                page,
                resolve_base=resolver,
                wait_for_manual_intervention=intervention,
                readback_page_factory=AsyncMock(return_value=page),
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
        self.assertFalse(snapshot.final_action_ready)
        self.assertEqual(
            snapshot.as_dict(),
            {
                "scheduleMode": "platform_native",
                "scheduledAt": "2026-08-29 15:00",
                "scheduleTimezone": "Asia/Shanghai",
                "scheduleToggleEnabled": True,
                "finalActionLabel": "Schedule",
                "finalActionReady": False,
            },
        )
        self.assertEqual(bases[0].by_role("final_action")[0].click_count, 0)

    async def test_missing_schedule_control_never_falls_back_to_post(self) -> None:
        page, bases = scheduled_form_page(toggle_count=0, final_button="Post")
        form, _, _ = self.form(page, bases, clock=FakeClock())

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
        form, _, _ = self.form(page, bases, clock=FakeClock())

        with self.assertRaises(TikTokPublishError) as raised:
            await form.configure(self.target)

        self.assertEqual(raised.exception.error_code, "tiktok_schedule_unavailable")

    async def test_configure_accepts_an_exact_schedule_radio_choice(self) -> None:
        page, bases = scheduled_form_page(toggle_count=0)
        radio = FakeScheduleControl(
            "schedule-radio",
            "radio",
            label="Schedule",
            checked=False,
        )
        bases[0].register(EXACT_SCHEDULE_RADIO_SELECTOR, radio)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)

        await form.configure(self.target)

        self.assertTrue(radio.checked)
        self.assertEqual(radio.click_count, 1)
        self.assertEqual(bases[0].by_role("final_action")[0].click_count, 0)

    async def test_configure_uses_exact_role_name_and_native_checked_property(
        self,
    ) -> None:
        page, bases = scheduled_form_page(toggle_count=0)
        bases[0].set_accessible_label("schedule-choice-label", "Schedule")
        radio = FakeScheduleControl(
            "labelled-schedule-radio",
            "radio",
            attributes={"aria-labelledby": "schedule-choice-label"},
            checked_attribute_tracks_state=False,
        )
        bases[0].register("[data-test-only='radio']", radio)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)

        await form.configure(self.target)

        self.assertTrue(radio.checked)
        self.assertEqual(radio.click_count, 1)
        self.assertEqual(bases[0].by_role("final_action")[0].click_count, 0)

    async def test_live_locator_remount_requires_the_new_node_twice(self) -> None:
        first = FakeScheduleControl("live-radio-a", "switch", checked=True)
        replacement = FakeScheduleControl("live-radio-b", "switch", checked=True)
        base = LiveScheduleBase(
            {SCHEDULE_TOGGLE_SELECTORS[0]: LiveScheduleLocator([first, replacement, replacement])}
        )
        page = FakeSchedulePage()
        clock = FakeClock()
        form, _, _ = self.form(page, [base] * 8, clock=clock)

        control = await form._schedule_control(
            SCHEDULE_TOGGLE_SELECTORS,
            setting="schedule choice",
            editable=False,
        )

        self.assertEqual(control.node_id, "live-radio-b")
        self.assertEqual(clock.sleeps, [0.25, 0.25])

    async def test_live_multiple_sets_must_be_stable_before_ambiguity(self) -> None:
        first_a = FakeScheduleControl("set-a", "switch", checked=True)
        first_b = FakeScheduleControl("set-b", "switch", checked=True)
        second_c = FakeScheduleControl("set-c", "switch", checked=True)
        second_d = FakeScheduleControl("set-d", "switch", checked=True)
        base = LiveScheduleBase(
            {
                SCHEDULE_TOGGLE_SELECTORS[0]: LiveScheduleLocator(
                    [first_a, second_c, second_c]
                ),
                SCHEDULE_TOGGLE_SELECTORS[1]: LiveScheduleLocator(
                    [first_b, second_d, second_d]
                ),
            }
        )
        page = FakeSchedulePage()
        clock = FakeClock()
        form, _, _ = self.form(page, [base] * 8, clock=clock)

        with self.assertRaises(TikTokPublishError) as raised:
            await form._schedule_control(
                SCHEDULE_TOGGLE_SELECTORS,
                setting="schedule choice",
                editable=False,
            )

        self.assertEqual(raised.exception.error_code, "tiktok_schedule_control_ambiguous")
        self.assertEqual(clock.sleeps, [0.25, 0.25])

    async def test_later_selector_transient_discards_the_whole_observation(self) -> None:
        record = HandleRecord("upload-schedule")
        base = LiveScheduleBase(
            {
                SCHEDULE_TOGGLE_SELECTORS[0]: WrapperScheduleLocator(
                    [lambda: HandleWrapper(record)]
                ),
                SCHEDULE_TOGGLE_SELECTORS[1]: TransientCountThenEmptyLocator(2),
            }
        )
        page = FakeSchedulePage()
        clock = FakeClock()
        form, _, _ = self.form(page, [base] * 8, clock=clock)

        control = await form._schedule_control(
            SCHEDULE_TOGGLE_SELECTORS,
            setting="schedule choice",
            editable=False,
        )

        self.assertIsInstance(control, HandleWrapper)
        self.assertEqual(clock.sleeps, [0.25, 0.25, 0.25])

    async def test_top_page_transient_discards_upload_scope_observation(self) -> None:
        record = HandleRecord("upload-schedule")
        base = LiveScheduleBase(
            {
                SCHEDULE_TOGGLE_SELECTORS[0]: WrapperScheduleLocator(
                    [lambda: HandleWrapper(record)]
                ),
            }
        )
        page = ContextSchedulePage(
            {SCHEDULE_TOGGLE_SELECTORS[1]: TransientCountThenEmptyLocator(2)}
        )
        clock = FakeClock()
        form, _, _ = self.form(page, [base] * 8, clock=clock)

        control = await form._schedule_control(
            SCHEDULE_TOGGLE_SELECTORS,
            setting="schedule choice",
            editable=False,
        )

        self.assertIsInstance(control, HandleWrapper)
        self.assertEqual(clock.sleeps, [0.25, 0.25, 0.25])

    async def test_second_candidate_transient_discards_the_whole_observation(self) -> None:
        first = FakeScheduleControl("first-schedule", "switch", checked=True)
        second = TransientVisibilityScheduleControl(
            "second-schedule",
            "switch",
            checked=True,
            visibility_failures=2,
        )
        base = LiveScheduleBase(
            {
                SCHEDULE_TOGGLE_SELECTORS[0]: LiveScheduleLocator([first]),
                SCHEDULE_TOGGLE_SELECTORS[1]: LiveScheduleLocator([second]),
            }
        )
        page = FakeSchedulePage()
        clock = FakeClock()
        form, _, _ = self.form(page, [base] * 8, clock=clock)

        with self.assertRaises(TikTokPublishError) as raised:
            await form._schedule_control(
                SCHEDULE_TOGGLE_SELECTORS,
                setting="schedule choice",
                editable=False,
            )

        self.assertEqual(raised.exception.error_code, "tiktok_schedule_control_ambiguous")
        self.assertEqual(clock.sleeps, [0.25, 0.25, 0.25])

    async def test_distinct_handle_wrappers_for_one_dom_node_are_stable(self) -> None:
        record = HandleRecord("same-dom")
        base = LiveScheduleBase(
            {SCHEDULE_TOGGLE_SELECTORS[0]: WrapperScheduleLocator([lambda: HandleWrapper(record)])}
        )
        page = FakeSchedulePage()
        clock = FakeClock()
        form, _, _ = self.form(page, [base] * 8, clock=clock)

        control = await form._schedule_control(
            SCHEDULE_TOGGLE_SELECTORS,
            setting="schedule choice",
            editable=False,
        )

        self.assertIsInstance(control, HandleWrapper)
        self.assertEqual(clock.sleeps, [0.25])

    async def test_same_handle_node_from_two_selectors_is_deduplicated(self) -> None:
        record = HandleRecord("same-dom")
        base = LiveScheduleBase(
            {
                SCHEDULE_TOGGLE_SELECTORS[0]: WrapperScheduleLocator([lambda: HandleWrapper(record)]),
                SCHEDULE_TOGGLE_SELECTORS[1]: WrapperScheduleLocator([lambda: HandleWrapper(record)]),
            }
        )
        page = FakeSchedulePage()
        clock = FakeClock()
        form, _, _ = self.form(page, [base] * 8, clock=clock)

        await form._schedule_control(
            SCHEDULE_TOGGLE_SELECTORS,
            setting="schedule choice",
            editable=False,
        )

        self.assertEqual(clock.sleeps, [0.25])

    async def test_stable_distinct_handle_nodes_are_ambiguous(self) -> None:
        first = HandleRecord("one")
        second = HandleRecord("two")
        base = LiveScheduleBase(
            {
                SCHEDULE_TOGGLE_SELECTORS[0]: WrapperScheduleLocator([lambda: HandleWrapper(first)]),
                SCHEDULE_TOGGLE_SELECTORS[1]: WrapperScheduleLocator([lambda: HandleWrapper(second)]),
            }
        )
        page = FakeSchedulePage()
        clock = FakeClock()
        form, _, _ = self.form(page, [base] * 8, clock=clock)

        with self.assertRaises(TikTokPublishError) as raised:
            await form._schedule_control(
                SCHEDULE_TOGGLE_SELECTORS,
                setting="schedule choice",
                editable=False,
            )

        self.assertEqual(raised.exception.error_code, "tiktok_schedule_control_ambiguous")

    async def test_distinct_scope_handles_are_ambiguous_without_cross_context_compare(self) -> None:
        upload_record = HandleRecord("upload")
        page_record = HandleRecord("page")
        base = LiveScheduleBase(
            {SCHEDULE_TOGGLE_SELECTORS[0]: WrapperScheduleLocator([lambda: ContextHandle(upload_record, "upload")])}
        )
        page = ContextSchedulePage(
            {SCHEDULE_TOGGLE_SELECTORS[0]: WrapperScheduleLocator([lambda: ContextHandle(page_record, "page")])}
        )
        clock = FakeClock()
        form, _, _ = self.form(page, [base] * 8, clock=clock)

        with self.assertRaises(TikTokPublishError) as raised:
            await form._schedule_control(
                SCHEDULE_TOGGLE_SELECTORS,
                setting="schedule choice",
                editable=False,
            )

        self.assertEqual(raised.exception.error_code, "tiktok_schedule_control_ambiguous")

    async def test_upload_frame_remount_requires_new_handle_twice(self) -> None:
        for message in (
            "JSHandles can be evaluated only in the context they were created",
            "JSHandles can be evaluated only in the context they were created!",
            "ElementHandle.evaluate: JSHandles can be evaluated only in the context they were created!",
        ):
            with self.subTest(message=message):
                before = HandleRecord("upload-a")
                after = HandleRecord("upload-b")
                base = LiveScheduleBase(
                    {SCHEDULE_TOGGLE_SELECTORS[0]: WrapperScheduleLocator([
                        lambda: UploadRemountHandle(before, message),
                        lambda: UploadRemountHandle(after, message),
                        lambda: UploadRemountHandle(after, message),
                    ])}
                )
                page = FakeSchedulePage()
                clock = FakeClock()
                form, _, _ = self.form(page, [base] * 8, clock=clock)

                control = await form._schedule_control(
                    SCHEDULE_TOGGLE_SELECTORS,
                    setting="schedule choice",
                    editable=False,
                )

                self.assertIsInstance(control, UploadRemountHandle)
                self.assertEqual(clock.sleeps, [0.25, 0.25])

    async def test_other_playwright_comparison_error_propagates(self) -> None:
        for message in (
            "unrelated Playwright comparison failure",
            "NotElementHandle.evaluate: JSHandles can be evaluated only in the context they were created",
            "JSHandles can be evaluated only in the context they were created! extra",
        ):
            with self.subTest(message=message):
                record = HandleRecord("other-error")
                base = LiveScheduleBase(
                    {SCHEDULE_TOGGLE_SELECTORS[0]: WrapperScheduleLocator([lambda: OtherPlaywrightErrorHandle(record, message)])}
                )
                page = FakeSchedulePage()
                clock = FakeClock()
                form, _, _ = self.form(page, [base] * 8, clock=clock)

                with self.assertRaisesRegex(PlaywrightError, re.escape(message)):
                    await form._schedule_control(
                        SCHEDULE_TOGGLE_SELECTORS,
                        setting="schedule choice",
                        editable=False,
                    )

    async def test_handle_comparison_protocol_error_is_not_silently_transient(self) -> None:
        record = HandleRecord("broken")
        base = LiveScheduleBase(
            {SCHEDULE_TOGGLE_SELECTORS[0]: WrapperScheduleLocator([lambda: BrokenCompareHandle(record)])}
        )
        page = FakeSchedulePage()
        clock = FakeClock()
        form, _, _ = self.form(page, [base] * 8, clock=clock)

        with self.assertRaisesRegex(RuntimeError, "comparison protocol bug"):
            await form._schedule_control(
                SCHEDULE_TOGGLE_SELECTORS,
                setting="schedule choice",
                editable=False,
            )

    async def test_control_sleep_does_not_exceed_real_deadline(self) -> None:
        page, bases = scheduled_form_page(toggle_count=0)
        form, _, _ = self.form(
            page,
            bases,
            control_timeout_seconds=0.01,
        )
        started = time.monotonic()

        with self.assertRaises(TikTokPublishError):
            await form._schedule_control(
                SCHEDULE_TOGGLE_SELECTORS,
                setting="schedule choice",
                editable=False,
            )

        self.assertLess(time.monotonic() - started, 0.1)

    async def test_hung_dom_read_hits_schedule_control_deadline(self) -> None:
        base = LiveScheduleBase({SCHEDULE_TOGGLE_SELECTORS[0]: HangingScheduleLocator()})
        page = FakeSchedulePage()
        form, _, _ = self.form(
            page,
            [base] * 8,
            control_timeout_seconds=0.01,
        )

        with self.assertRaises(TikTokPublishError) as raised:
            await form._schedule_control(
                SCHEDULE_TOGGLE_SELECTORS,
                setting="schedule choice",
                editable=False,
            )

        self.assertEqual(raised.exception.error_code, "tiktok_schedule_unavailable")

    async def test_configure_waits_for_delayed_exact_schedule_radio_choice(
        self,
    ) -> None:
        page, bases = scheduled_form_page(toggle_count=0)
        radio = FakeScheduleControl(
            "delayed-schedule-radio",
            "radio",
            label="Schedule",
        )
        clock = FakeClock()
        clock.on_sleep = lambda _seconds: bases[0].register(
            EXACT_SCHEDULE_RADIO_SELECTOR,
            radio,
        )
        form, _, _ = self.form(page, bases, clock=clock)

        await form.configure(self.target)

        self.assertGreaterEqual(len(clock.sleeps), 2)
        self.assertEqual(radio.click_count, 1)

    async def test_configure_waits_for_disabled_radio_then_rechecks_its_choice(
        self,
    ) -> None:
        page, bases = scheduled_form_page(toggle_count=0)
        radio = FakeScheduleControl(
            "disabled-then-enabled-radio",
            "radio",
            label="Schedule",
            enabled=False,
        )
        bases[0].register(EXACT_SCHEDULE_RADIO_SELECTOR, radio)
        clock = FakeClock()
        clock.on_sleep = lambda _seconds: setattr(radio, "enabled", True)
        form, _, _ = self.form(page, bases, clock=clock)

        await form.configure(self.target)

        self.assertTrue(radio.checked)
        self.assertEqual(radio.click_count, 1)

    async def test_configure_discards_detached_radio_and_uses_stable_remount(
        self,
    ) -> None:
        page, bases = scheduled_form_page(toggle_count=0)
        first = FakeScheduleControl("schedule-radio-before-remount", "radio", label="Schedule")
        replacement = FakeScheduleControl(
            "schedule-radio-after-remount",
            "radio",
            label="Schedule",
        )
        bases[0].register(EXACT_SCHEDULE_RADIO_SELECTOR, first)
        clock = FakeClock()

        def remount(_seconds: float) -> None:
            first.detached = True
            bases[0]._registered[EXACT_SCHEDULE_RADIO_SELECTOR] = [replacement]
            bases[0].controls = [
                replacement if control is first else control
                for control in bases[0].controls
            ]

        clock.on_sleep = remount
        form, _, _ = self.form(page, bases, clock=clock)

        await form.configure(self.target)

        self.assertEqual(first.click_count, 0)
        self.assertEqual(replacement.click_count, 1)

    async def test_configure_resolves_unique_top_page_radio_when_upload_base_is_iframe(
        self,
    ) -> None:
        page, bases = scheduled_form_page(toggle_count=0)
        radio = FakeScheduleControl("top-page-schedule-radio", "radio", label="Schedule")
        page.register(EXACT_SCHEDULE_RADIO_SELECTOR, radio)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)

        await form.configure(self.target)

        self.assertEqual(radio.click_count, 1)
        self.assertEqual(bases[0].by_role("final_action")[0].click_count, 0)

    async def test_same_fake_node_in_both_scopes_is_ambiguous(self) -> None:
        page, bases = scheduled_form_page(toggle_count=0)
        radio = FakeScheduleControl("shared-schedule-radio", "radio", label="Schedule")
        bases[0].register(EXACT_SCHEDULE_RADIO_SELECTOR, radio)
        page.register(EXACT_SCHEDULE_RADIO_SELECTOR, radio)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)

        with self.assertRaises(TikTokPublishError) as raised:
            await form.configure(self.target)

        self.assertEqual(raised.exception.error_code, "tiktok_schedule_control_ambiguous")
        self.assertEqual(radio.click_count, 0)

    async def test_distinct_schedule_radios_across_base_and_top_page_are_ambiguous(
        self,
    ) -> None:
        page, bases = scheduled_form_page(toggle_count=0)
        bases[0].register(
            EXACT_SCHEDULE_RADIO_SELECTOR,
            FakeScheduleControl("iframe-schedule-radio", "radio", label="Schedule"),
        )
        page.register(
            EXACT_SCHEDULE_RADIO_SELECTOR,
            FakeScheduleControl("top-page-schedule-radio", "radio", label="Schedule"),
        )
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)

        with self.assertRaises(TikTokPublishError) as raised:
            await form.configure(self.target)

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_schedule_control_ambiguous",
        )

    async def test_configure_allows_final_action_to_become_ready_after_upload_processing(self) -> None:
        page, bases = scheduled_form_page()
        final_button = bases[0].by_role("final_action")[0]
        final_button.enabled = False
        form, _, _ = self.form(page, bases)

        configured = await form.configure(self.target)

        self.assertFalse(configured.final_action_ready)
        self.assertEqual(final_button.click_count, 0)
        final_button.enabled = True
        verified = await form.verify(self.target)
        self.assertTrue(verified.final_action_ready)
        self.assertEqual(final_button.click_count, 0)

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
        stable_bases = [
            bases[index]
            for index in (0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3)
        ]
        form, resolver, _ = self.form(page, stable_bases)

        snapshot = await form.configure(self.target)

        self.assertEqual(snapshot.scheduled_at, "2026-08-29 15:00")
        self.assertEqual(resolver.await_count, 12)
        self.assertEqual(bases[0].by_role("switch")[0].click_count, 1)
        self.assertEqual(bases[1].by_role("date")[0].fill_count, 1)
        self.assertEqual(bases[2].by_role("time")[0].fill_count, 1)
        used_node_ids = {
            bases[0].by_role("switch")[0].node_id,
            bases[1].by_role("date")[0].node_id,
            bases[2].by_role("time")[0].node_id,
        }
        self.assertEqual(len(used_node_ids), 3)

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
        baseline_row_keys: frozenset[str] | None = None,
    ) -> TikTokScheduledContentExpectation:
        normalized = normalized_caption(caption)
        values = {
            "account_reference": account_reference,
            "expected_caption": caption,
            "caption_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "target": target or self.target,
            "submitted_after": submitted_after
            or datetime(2026, 8, 29, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
        }
        if baseline_row_keys is not None:
            values["baseline_row_keys"] = baseline_row_keys
        return TikTokScheduledContentExpectation(**values)

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
                    page.set_page_account_references()
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
        page.set_page_account_references("wrong.page")
        row, _ = scheduled_row()
        page.set_rows(row)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)
        self.assertIsNone(
            await form.readback_scheduled_content(self.expectation())
        )

    async def test_readback_rejects_ambiguous_page_or_row_dom_identity(self) -> None:
        for ambiguity in ("page", "row"):
            with self.subTest(ambiguity=ambiguity):
                page, bases = scheduled_form_page(toggle_enabled=True)
                if ambiguity == "page":
                    page.set_page_account_references("expected.user", "other.user")
                    row, _ = scheduled_row()
                else:
                    row, _ = scheduled_row(
                        account_reference=("expected.user", "other.user")
                    )
                page.set_rows(row)
                clock = FakeClock()
                form, _, _ = self.form(page, bases, clock=clock)

                self.assertIsNone(
                    await form.readback_scheduled_content(self.expectation())
                )

    async def test_baseline_allows_only_one_exact_new_row(self) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True)
        old, _ = scheduled_row(
            node_id="row-old",
            content_id="old-7654321",
            content_url="https://www.tiktok.com/@expected.user/video/old-7654321",
        )
        page.replace_rows(old)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)

        baseline = await form.capture_scheduled_content_baseline("expected.user")
        new, _ = scheduled_row(
            node_id="row-new",
            content_id="new-7654321",
            content_url="https://www.tiktok.com/@expected.user/video/new-7654321",
        )
        page.replace_rows(old, new)

        readback = await form.readback_scheduled_content(
            self.expectation(baseline_row_keys=baseline.row_keys)
        )

        self.assertIsNotNone(readback)
        assert readback is not None
        self.assertEqual(readback.content_id, "new-7654321")

    async def test_readback_refreshes_after_baseline_and_accepts_only_new_exact_row(
        self,
    ) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True)
        old, old_actions = scheduled_row(
            node_id="row-old",
            content_id="old-7654321",
            content_url="https://www.tiktok.com/@expected.user/video/old-7654321",
        )
        page.replace_rows(old)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)

        baseline = await form.capture_scheduled_content_baseline("expected.user")
        new, new_actions = scheduled_row(
            node_id="row-new-after-refresh",
            content_id="new-after-refresh-7654321",
            content_url=(
                "https://www.tiktok.com/@expected.user/video/new-after-refresh-7654321"
            ),
        )
        page.reveal_rows_on_next_navigation(old, new)

        readback = await form.readback_scheduled_content(
            self.expectation(baseline_row_keys=baseline.row_keys)
        )

        self.assertIsNotNone(readback)
        assert readback is not None
        self.assertEqual(readback.content_id, "new-after-refresh-7654321")
        self.assertEqual(
            page.goto_calls,
            [
                "https://www.tiktok.com/tiktokstudio/content",
                "https://www.tiktok.com/tiktokstudio/content",
            ],
        )
        self.assertEqual(
            [action.click_count for action in old_actions + new_actions],
            [0] * 8,
        )

    async def test_old_exact_row_is_not_accepted_while_new_row_is_delayed(self) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True)
        old, _ = scheduled_row(
            node_id="row-old",
            content_id="old-7654321",
            content_url="https://www.tiktok.com/@expected.user/video/old-7654321",
        )
        page.replace_rows(old)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)
        baseline = await form.capture_scheduled_content_baseline("expected.user")
        new, _ = scheduled_row(
            node_id="row-new-delayed",
            content_id="new-delayed-7654321",
            content_url=(
                "https://www.tiktok.com/@expected.user/video/new-delayed-7654321"
            ),
        )
        readback_sleep_count = 0

        def reveal_new_row(_seconds: float) -> None:
            nonlocal readback_sleep_count
            readback_sleep_count += 1
            if readback_sleep_count == 1:
                page.replace_rows(old, new)

        clock.on_sleep = reveal_new_row

        readback = await form.readback_scheduled_content(
            self.expectation(baseline_row_keys=baseline.row_keys)
        )

        self.assertIsNotNone(readback)
        assert readback is not None
        self.assertEqual(readback.content_id, "new-delayed-7654321")
        self.assertEqual(readback_sleep_count, 1)

    async def test_zero_or_multiple_exact_new_rows_never_succeed(self) -> None:
        page, bases = scheduled_form_page(toggle_enabled=True)
        old, _ = scheduled_row(node_id="row-old", content_id="old")
        page.replace_rows(old)
        clock = FakeClock()
        form, _, _ = self.form(page, bases, clock=clock)
        baseline = await form.capture_scheduled_content_baseline("expected.user")

        self.assertIsNone(
            await form.readback_scheduled_content(
                self.expectation(baseline_row_keys=baseline.row_keys)
            )
        )

        first, _ = scheduled_row(node_id="row-new-1", content_id="new-1")
        second, _ = scheduled_row(node_id="row-new-2", content_id="new-2")
        page.replace_rows(old, first, second)
        self.assertIsNone(
            await form.readback_scheduled_content(
                self.expectation(baseline_row_keys=baseline.row_keys)
            )
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
