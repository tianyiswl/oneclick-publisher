# -*- coding: utf-8 -*-
"""TikTok Studio 平台原生定时表单的隔离语义适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from app_core.overseas_tiktok_errors import TikTokPublishError


SCHEDULE_TOGGLE_SELECTORS = (
    '[data-e2e*="schedule" i] [role="switch"]',
    '[role="switch"][aria-label*="schedule" i]',
    '[role="switch"][aria-label*="定时"]',
)
SCHEDULE_DATE_SELECTORS = (
    '[data-e2e*="schedule" i] input[type="date"]',
    'input[aria-label*="date" i]',
    'input[aria-label*="日期"]',
)
SCHEDULE_TIME_SELECTORS = (
    '[data-e2e*="schedule" i] input[type="time"]',
    'input[aria-label*="time" i]',
    'input[aria-label*="时间"]',
)
FINAL_ACTION_SELECTORS = (
    'button[data-e2e="schedule_video_button"]',
    'button[data-e2e="post_video_button"]',
    '[role="button"][aria-label="Schedule"]',
    '[role="button"][aria-label="Post"]',
    '[role="button"][aria-label="定时发布"]',
    '[role="button"][aria-label="排期"]',
    '[role="button"][aria-label="发布"]',
)
ACCEPTANCE_FEEDBACK_SELECTORS = (
    '[role="alert"][data-e2e*="schedule" i]',
    '[role="status"][data-e2e*="schedule" i]',
    '[role="alert"]',
    '[role="status"]',
)
SCHEDULED_CONTENT_ROUTES = (
    "https://www.tiktok.com/tiktokstudio/content",
    "https://www.tiktok.com/tiktokstudio/posts",
)
SCHEDULED_ROW_SELECTORS = (
    '[data-e2e="content-row"]',
    '[data-e2e="post-item"]',
    '[role="row"][data-schedule-time]',
)

_SCHEDULE_LABELS = frozenset({"schedule", "定时发布", "排期"})
_IMMEDIATE_LABELS = frozenset({"post", "发布"})
_SCHEDULE_SUCCESS_MESSAGES = frozenset(
    {
        "video scheduled successfully",
        "your video has been scheduled",
        "your video has been scheduled successfully",
        "定时发布成功",
        "视频已定时发布",
        "已成功排期",
    }
)
_POLL_INTERVAL_SECONDS = 1.0
_OUTCOME_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class TikTokScheduleTarget:
    scheduled_at: str
    timezone: Literal["Asia/Shanghai"]


@dataclass(frozen=True, slots=True)
class TikTokScheduleFormSnapshot:
    schedule_mode: Literal["platform_native"]
    scheduled_at: str
    timezone: Literal["Asia/Shanghai"]
    toggle_enabled: bool
    final_action_label: Literal["Schedule"]
    final_action_ready: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "scheduleMode": self.schedule_mode,
            "scheduledAt": self.scheduled_at,
            "scheduleTimezone": self.timezone,
            "scheduleToggleEnabled": self.toggle_enabled,
            "finalActionLabel": self.final_action_label,
            "finalActionReady": self.final_action_ready,
        }


@dataclass(frozen=True, slots=True)
class TikTokScheduleAcceptance:
    evidence: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class TikTokScheduledContentExpectation:
    account_reference: str
    expected_caption: str
    caption_sha256: str
    target: TikTokScheduleTarget
    submitted_after: datetime


@dataclass(frozen=True, slots=True)
class TikTokScheduledContentReadback:
    content_id: str | None
    content_url: str | None
    scheduled_at: str
    schedule_timezone: Literal["Asia/Shanghai"]
    evidence: str


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _normalized_label(value: object) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).split()
    ).casefold()


def _normalized_caption(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


async def _same_dom_node(left: Any, right: Any) -> bool:
    if left is right:
        return True
    left_id = getattr(left, "node_id", None)
    right_id = getattr(right, "node_id", None)
    if left_id is not None and right_id is not None:
        return left_id == right_id
    evaluate = getattr(left, "evaluate", None)
    element_handle = getattr(right, "element_handle", None)
    if callable(evaluate) and callable(element_handle):
        try:
            handle = await element_handle()
            if handle is not None:
                return bool(
                    await evaluate(
                        "(element, other) => element === other",
                        handle,
                    )
                )
        except Exception:
            return False
    return False


async def _append_unique(items: list[Any], candidate: Any) -> None:
    for existing in items:
        if await _same_dom_node(existing, candidate):
            return
    items.append(candidate)


async def _bounded_candidates(base: Any, selectors: tuple[str, ...]) -> list[Any]:
    candidates: list[Any] = []
    for selector in selectors:
        locator = base.locator(selector)
        count = await locator.count()
        for index in range(count):
            await _append_unique(candidates, locator.nth(index))
    return candidates


async def _visible_enabled_final_buttons(base: Any) -> list[tuple[Any, str]]:
    buttons: list[tuple[Any, str]] = []
    for candidate in await _bounded_candidates(base, FINAL_ACTION_SELECTORS):
        if not await candidate.is_visible() or not await candidate.is_enabled():
            continue
        label = str(await candidate.inner_text() or "")
        if not _normalized_label(label):
            label = str(await candidate.get_attribute("aria-label") or "")
        buttons.append((candidate, label))
    return buttons


async def _one_schedule_control(
    base: Any,
    selectors: tuple[str, ...],
    *,
    editable: bool,
) -> Any:
    usable: list[Any] = []
    for candidate in await _bounded_candidates(base, selectors):
        if not await candidate.is_visible() or not await candidate.is_enabled():
            continue
        if editable:
            if not await candidate.is_editable():
                continue
            if await candidate.get_attribute("readonly") is not None:
                continue
        usable.append(candidate)
    if not usable:
        raise TikTokPublishError(
            "tiktok_schedule_unavailable",
            "TikTok 上传页没有唯一可用的定时控件",
        )
    if len(usable) != 1:
        raise TikTokPublishError(
            "tiktok_schedule_control_ambiguous",
            "TikTok 定时控件不唯一",
        )
    return usable[0]


async def _switch_enabled(control: Any) -> bool:
    aria_checked = _normalized_label(await control.get_attribute("aria-checked"))
    if aria_checked in {"true", "false"}:
        return aria_checked == "true"
    checked = _normalized_label(await control.get_attribute("checked"))
    return checked in {"true", "checked"}


async def _read_feedback(page: Any) -> list[str]:
    try:
        messages: list[str] = []
        candidates = await _bounded_candidates(
            page,
            ACCEPTANCE_FEEDBACK_SELECTORS,
        )
        for candidate in candidates:
            if not await candidate.is_visible():
                continue
            text = str(await candidate.inner_text() or "").strip()
            if text:
                messages.append(text)
        return messages
    except Exception:
        return []


def _is_scheduled_success(message: str) -> bool:
    normalized = _normalized_label(message).rstrip(".!。！")
    return normalized in _SCHEDULE_SUCCESS_MESSAGES


def _is_explicit_rejection(message: str) -> bool:
    normalized = _normalized_label(message)
    return any(
        phrase in normalized
        for phrase in (
            "couldn't schedule",
            "could not schedule",
            "unable to schedule",
            "failed to schedule",
            "schedule failed",
            "not been scheduled",
            "not scheduled",
            "failed",
            "retry",
            "please try again",
            "try again",
            "cancelled",
            "canceled",
            "rejected",
            "定时发布失败",
            "无法定时",
            "未能定时",
            "定时未成功",
            "已取消",
            "请重试",
        )
    )


class TikTokScheduleForm:
    def __init__(
        self,
        page: Any,
        *,
        resolve_base: Callable[[], Awaitable[Any]],
        wait_for_manual_intervention: Callable[[Any], Awaitable[None]],
        monotonic: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._page = page
        self._resolve_base = resolve_base
        self._wait_for_manual_intervention = wait_for_manual_intervention
        self._monotonic = monotonic or time.monotonic
        self._now = now or _shanghai_now
        self._sleep = sleep or asyncio.sleep

    async def configure(
        self,
        target: TikTokScheduleTarget,
    ) -> TikTokScheduleFormSnapshot:
        date_value, time_value = self._target_values(target)

        base = await self._resolve_base()
        toggle = await _one_schedule_control(
            base,
            SCHEDULE_TOGGLE_SELECTORS,
            editable=False,
        )
        if not await _switch_enabled(toggle):
            await toggle.click()

        base = await self._resolve_base()
        toggle = await _one_schedule_control(
            base,
            SCHEDULE_TOGGLE_SELECTORS,
            editable=False,
        )
        if not await _switch_enabled(toggle):
            raise TikTokPublishError(
                "tiktok_schedule_readback_mismatch",
                "TikTok 定时开关写入后回读不一致",
            )

        base = await self._resolve_base()
        date_control = await _one_schedule_control(
            base,
            SCHEDULE_DATE_SELECTORS,
            editable=True,
        )
        await date_control.fill(date_value)

        base = await self._resolve_base()
        date_control = await _one_schedule_control(
            base,
            SCHEDULE_DATE_SELECTORS,
            editable=True,
        )
        if str(await date_control.input_value()) != date_value:
            raise TikTokPublishError(
                "tiktok_schedule_readback_mismatch",
                "TikTok 定时日期写入后回读不一致",
            )

        base = await self._resolve_base()
        time_control = await _one_schedule_control(
            base,
            SCHEDULE_TIME_SELECTORS,
            editable=True,
        )
        await time_control.fill(time_value)

        base = await self._resolve_base()
        time_control = await _one_schedule_control(
            base,
            SCHEDULE_TIME_SELECTORS,
            editable=True,
        )
        if str(await time_control.input_value()) != time_value:
            raise TikTokPublishError(
                "tiktok_schedule_readback_mismatch",
                "TikTok 定时时间写入后回读不一致",
            )
        return TikTokScheduleFormSnapshot(
            schedule_mode="platform_native",
            scheduled_at=target.scheduled_at,
            timezone=target.timezone,
            toggle_enabled=True,
            final_action_label="Schedule",
            final_action_ready=False,
        )

    async def verify(
        self,
        target: TikTokScheduleTarget,
    ) -> TikTokScheduleFormSnapshot:
        date_value, time_value = self._target_values(target)

        base = await self._resolve_base()
        toggle = await _one_schedule_control(
            base,
            SCHEDULE_TOGGLE_SELECTORS,
            editable=False,
        )
        if not await _switch_enabled(toggle):
            raise TikTokPublishError(
                "tiktok_schedule_readback_mismatch",
                "TikTok 定时开关回读未开启",
            )

        base = await self._resolve_base()
        date_control = await _one_schedule_control(
            base,
            SCHEDULE_DATE_SELECTORS,
            editable=True,
        )
        if str(await date_control.input_value()) != date_value:
            raise TikTokPublishError(
                "tiktok_schedule_readback_mismatch",
                "TikTok 定时日期回读不一致",
            )

        base = await self._resolve_base()
        time_control = await _one_schedule_control(
            base,
            SCHEDULE_TIME_SELECTORS,
            editable=True,
        )
        if str(await time_control.input_value()) != time_value:
            raise TikTokPublishError(
                "tiktok_schedule_readback_mismatch",
                "TikTok 定时时间回读不一致",
            )

        await self.final_button(target)
        return TikTokScheduleFormSnapshot(
            schedule_mode="platform_native",
            scheduled_at=target.scheduled_at,
            timezone=target.timezone,
            toggle_enabled=True,
            final_action_label="Schedule",
            final_action_ready=True,
        )

    async def final_button(self, target: TikTokScheduleTarget) -> Any:
        self._target_values(target)
        base = await self._resolve_base()
        candidates = await _visible_enabled_final_buttons(base)
        matches = [
            button
            for button, label in candidates
            if _normalized_label(label) in _SCHEDULE_LABELS
        ]
        immediate_matches = [
            button
            for button, label in candidates
            if _normalized_label(label) in _IMMEDIATE_LABELS
        ]
        if not matches:
            raise TikTokPublishError(
                "tiktok_schedule_final_action_unavailable",
                "TikTok 定时请求没有可用的 Schedule 按钮",
            )
        if len(matches) > 1 or immediate_matches:
            raise TikTokPublishError(
                "tiktok_schedule_control_ambiguous",
                "TikTok 定时请求的最终动作不唯一",
            )
        return matches[0]

    async def wait_for_acceptance(
        self,
        target: TikTokScheduleTarget,
    ) -> TikTokScheduleAcceptance:
        self._target_values(target)
        deadline = self._monotonic() + _OUTCOME_TIMEOUT_SECONDS
        while self._monotonic() < deadline:
            await self._wait_for_manual_intervention(self._page)
            for message in await _read_feedback(self._page):
                if _is_explicit_rejection(message):
                    raise TikTokPublishError(
                        "tiktok_publish_rejected",
                        "TikTok 平台明确拒绝了定时发布",
                    )
                if _is_scheduled_success(message):
                    return TikTokScheduleAcceptance(
                        evidence=message,
                        accepted_at=self._now(),
                    )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            await self._sleep(min(_POLL_INTERVAL_SECONDS, remaining))
        raise TikTokPublishError(
            "tiktok_schedule_outcome_unknown",
            "TikTok 定时提交后缺少明确平台回执",
            outcome_ambiguous=True,
        )

    async def readback_scheduled_content(
        self,
        expected: TikTokScheduledContentExpectation,
    ) -> TikTokScheduledContentReadback | None:
        if not self._expectation_is_coherent(expected):
            return None
        loaded = False
        for route in SCHEDULED_CONTENT_ROUTES:
            try:
                await self._page.goto(route)
            except Exception:
                continue
            loaded = True
            break
        if not loaded:
            return None

        deadline = self._monotonic() + _OUTCOME_TIMEOUT_SECONDS
        while self._monotonic() < deadline:
            await self._wait_for_manual_intervention(self._page)
            observed_at = self._now()
            exact: list[TikTokScheduledContentReadback] = []
            rows = await _bounded_candidates(self._page, SCHEDULED_ROW_SELECTORS)
            for row in rows:
                if not await row.is_visible():
                    continue
                readback = await self._matching_row(
                    row,
                    expected,
                    observed_at=observed_at,
                )
                if readback is not None:
                    exact.append(readback)
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                return None
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            await self._sleep(min(_POLL_INTERVAL_SECONDS, remaining))
        return None

    @staticmethod
    def _target_values(target: TikTokScheduleTarget) -> tuple[str, str]:
        if target.timezone != "Asia/Shanghai":
            raise TikTokPublishError(
                "tiktok_schedule_unavailable",
                "TikTok 定时只允许使用北京时间",
            )
        try:
            parsed = datetime.strptime(target.scheduled_at, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise TikTokPublishError(
                "tiktok_schedule_unavailable",
                "TikTok 定时时间格式无效",
            ) from exc
        if parsed.strftime("%Y-%m-%d %H:%M") != target.scheduled_at:
            raise TikTokPublishError(
                "tiktok_schedule_unavailable",
                "TikTok 定时时间格式无效",
            )
        return target.scheduled_at[:10], target.scheduled_at[11:]

    @staticmethod
    def _expectation_is_coherent(
        expected: TikTokScheduledContentExpectation,
    ) -> bool:
        caption = _normalized_caption(expected.expected_caption)
        actual_hash = hashlib.sha256(caption.encode("utf-8")).hexdigest()
        return (
            expected.target.timezone == "Asia/Shanghai"
            and expected.caption_sha256 == actual_hash
            and expected.submitted_after.tzinfo is not None
            and expected.submitted_after.utcoffset() is not None
        )

    async def _matching_row(
        self,
        row: Any,
        expected: TikTokScheduledContentExpectation,
        *,
        observed_at: datetime,
    ) -> TikTokScheduledContentReadback | None:
        page_account_reference = getattr(self._page, "account_reference", None)
        row_account_reference = await row.get_attribute("data-account-reference")
        caption = await row.get_attribute("data-caption")
        if caption is None:
            caption = await row.inner_text()
        caption_hash = hashlib.sha256(
            _normalized_caption(caption).encode("utf-8")
        ).hexdigest()
        scheduled_at = await row.get_attribute("data-schedule-time")
        timezone = await row.get_attribute("data-schedule-timezone")
        if (
            page_account_reference != expected.account_reference
            or row_account_reference != expected.account_reference
            or caption_hash != expected.caption_sha256
            or scheduled_at != expected.target.scheduled_at
            or timezone != expected.target.timezone
            or observed_at < expected.submitted_after
        ):
            return None
        return TikTokScheduledContentReadback(
            content_id=await row.get_attribute("data-content-id"),
            content_url=await row.get_attribute("data-content-url"),
            scheduled_at=expected.target.scheduled_at,
            schedule_timezone="Asia/Shanghai",
            evidence="tiktok_studio_scheduled_content_exact_match",
        )
