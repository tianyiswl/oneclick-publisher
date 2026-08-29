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
from app_core.overseas_tiktok_identity import normalize_tiktok_handle


SCHEDULE_TOGGLE_SELECTORS = (
    '[data-e2e*="schedule" i] [role="switch"]',
    '[role="switch"][aria-label*="schedule" i]',
    '[role="switch"][aria-label*="定时"]',
)
SCHEDULE_CHOICE_SELECTORS = SCHEDULE_TOGGLE_SELECTORS
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
PAGE_ACCOUNT_REFERENCE_SELECTORS = (
    'a[data-e2e="nav-profile"][href*="/@"]',
    'a[data-e2e="profile-link"][href*="/@"]',
    '[data-e2e="user-avatar"] a[href*="/@"]',
)
ROW_ACCOUNT_REFERENCE_SELECTORS = (
    'a[data-e2e="content-row-account"][href*="/@"]',
    'a[data-e2e="account-link"][href*="/@"]',
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
_CONTROL_POLL_INTERVAL_SECONDS = 0.25
_CONTROL_POLL_OBSERVATIONS = 64
_CONTROL_TIMEOUT_SECONDS = 15.0


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
    baseline_row_keys: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class TikTokScheduledContentBaseline:
    row_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class TikTokScheduledContentReadback:
    content_id: str | None
    content_url: str | None
    scheduled_at: str
    schedule_timezone: Literal["Asia/Shanghai"]
    evidence: str


@dataclass(frozen=True, slots=True)
class _FrozenScheduleCandidate:
    node: Any
    kind: Literal["switch", "radio", "checkbox", "field"]


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _normalized_label(value: object) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).split()
    ).casefold()


def canonicalize_tiktok_caption(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


_normalized_caption = canonicalize_tiktok_caption


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


async def _unique_dom_account_reference(
    base: Any,
    selectors: tuple[str, ...],
) -> str | None:
    references: set[str] = set()
    try:
        candidates = await _bounded_candidates(base, selectors)
    except Exception:
        return None
    for candidate in candidates:
        try:
            if not await candidate.is_visible():
                continue
            reference = normalize_tiktok_handle(
                await candidate.get_attribute("href")
            )
        except Exception:
            continue
        if reference:
            references.add(reference)
    if len(references) != 1:
        return None
    return next(iter(references))


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


async def _switch_enabled(control: Any) -> bool:
    aria_checked = _normalized_label(await control.get_attribute("aria-checked"))
    if aria_checked in {"true", "false"}:
        return aria_checked == "true"
    checked = _normalized_label(await control.get_attribute("checked"))
    return checked in {"true", "checked"}


async def _schedule_choice_enabled(control: Any) -> bool:
    is_checked = getattr(control, "is_checked", None)
    if callable(is_checked):
        try:
            return bool(await is_checked())
        except Exception:
            pass
    return await _switch_enabled(control)


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
        readback_page_factory: Callable[[], Awaitable[Any]] | None = None,
        control_timeout_seconds: float = _CONTROL_TIMEOUT_SECONDS,
    ) -> None:
        self._page = page
        self._resolve_base = resolve_base
        self._wait_for_manual_intervention = wait_for_manual_intervention
        self._monotonic = monotonic or time.monotonic
        self._now = now or _shanghai_now
        self._sleep = sleep or asyncio.sleep
        self._readback_page_factory = readback_page_factory
        self._control_timeout_seconds = control_timeout_seconds
        self._readback_page: Any | None = None
        self._readback_route_loaded = False

    async def _await_control(self, operation: Awaitable[Any], deadline: float) -> Any:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            close = getattr(operation, "close", None)
            if callable(close):
                close()
            raise asyncio.TimeoutError()
        return await asyncio.wait_for(operation, timeout=remaining)

    @staticmethod
    def _is_transient_control_error(exc: BaseException) -> bool:
        if isinstance(exc, asyncio.TimeoutError):
            return True
        message = str(exc).casefold()
        return any(
            phrase in message
            for phrase in (
                "detached",
                "not attached",
                "execution context was destroyed",
                "re-mounted",
            )
        )

    async def _schedule_scopes(self, deadline: float) -> tuple[Any, ...]:
        upload_base = await self._await_control(self._resolve_base(), deadline)
        if upload_base is self._page:
            return (upload_base,)
        return (upload_base, self._page)

    async def _freeze_locator_candidates(
        self,
        locator: Any,
        *,
        kind: Literal["switch", "radio", "checkbox", "field"],
        deadline: float,
    ) -> list[_FrozenScheduleCandidate]:
        frozen: list[_FrozenScheduleCandidate] = []
        count = await self._await_control(locator.count(), deadline)
        for index in range(count):
            live = locator.nth(index)
            element_handle = getattr(live, "element_handle", None)
            node = (
                await self._await_control(element_handle(), deadline)
                if callable(element_handle)
                else live
            )
            if node is not None:
                frozen.append(_FrozenScheduleCandidate(node, kind))
        return frozen

    async def _same_frozen_node(
        self,
        left: _FrozenScheduleCandidate,
        right: _FrozenScheduleCandidate,
        deadline: float,
    ) -> bool:
        left_id = getattr(left.node, "node_id", None)
        right_id = getattr(right.node, "node_id", None)
        if left_id is not None and right_id is not None:
            return left_id == right_id
        evaluate = getattr(left.node, "evaluate", None)
        if not callable(evaluate):
            raise RuntimeError("frozen schedule node lacks comparison protocol")
        return bool(
            await self._await_control(
                evaluate("(element, other) => element === other", right.node),
                deadline,
            )
        )

    async def _append_unique_frozen(
        self,
        items: list[_FrozenScheduleCandidate],
        candidate: _FrozenScheduleCandidate,
        deadline: float,
    ) -> None:
        for existing in items:
            if await self._same_frozen_node(existing, candidate, deadline):
                return
        items.append(candidate)

    async def _observed_schedule_candidates(
        self,
        scopes: tuple[Any, ...],
        selectors: tuple[str, ...],
        *,
        choice: bool,
        deadline: float,
    ) -> list[_FrozenScheduleCandidate]:
        candidates: list[_FrozenScheduleCandidate] = []
        for scope in scopes:
            try:
                for selector in selectors:
                    locator = scope.locator(selector)
                    kind: Literal["switch", "radio", "checkbox", "field"] = (
                        "switch" if choice else "field"
                    )
                    for candidate in await self._freeze_locator_candidates(
                        locator, kind=kind, deadline=deadline
                    ):
                        await self._append_unique_frozen(candidates, candidate, deadline)
                if choice:
                    get_by_role = getattr(scope, "get_by_role", None)
                    if callable(get_by_role):
                        for role in ("radio", "checkbox"):
                            for name in ("Schedule", "定时发布", "排期"):
                                locator = get_by_role(role, name=name, exact=True)
                                for candidate in await self._freeze_locator_candidates(
                                    locator, kind=role, deadline=deadline
                                ):
                                    await self._append_unique_frozen(candidates, candidate, deadline)
            except BaseException as exc:
                if not self._is_transient_control_error(exc):
                    raise
                continue
        return candidates

    async def _choice_checked(
        self,
        candidate: _FrozenScheduleCandidate,
        deadline: float,
    ) -> bool:
        if candidate.kind in {"radio", "checkbox"}:
            is_checked = getattr(candidate.node, "is_checked", None)
            if not callable(is_checked):
                raise RuntimeError("native schedule choice lacks is_checked")
            return bool(await self._await_control(is_checked(), deadline))
        return await self._await_control(_switch_enabled(candidate.node), deadline)

    async def _usable_schedule_candidates(
        self,
        candidates: list[_FrozenScheduleCandidate],
        *,
        editable: bool,
        checked: bool | None,
        deadline: float,
    ) -> list[_FrozenScheduleCandidate]:
        usable: list[_FrozenScheduleCandidate] = []
        for candidate in candidates:
            try:
                if not await self._await_control(candidate.node.is_visible(), deadline):
                    continue
                if not await self._await_control(candidate.node.is_enabled(), deadline):
                    continue
                if editable:
                    if not await self._await_control(candidate.node.is_editable(), deadline):
                        continue
                    if await self._await_control(candidate.node.get_attribute("readonly"), deadline) is not None:
                        continue
                if checked is not None and await self._choice_checked(candidate, deadline) != checked:
                    continue
            except BaseException as exc:
                if not self._is_transient_control_error(exc):
                    raise
                continue
            usable.append(candidate)
        return usable

    async def _same_candidate_sets(
        self,
        left: list[_FrozenScheduleCandidate],
        right: list[_FrozenScheduleCandidate],
        deadline: float,
    ) -> bool:
        if len(left) != len(right):
            return False
        unmatched = list(right)
        for candidate in left:
            for index, other in enumerate(unmatched):
                if await self._same_frozen_node(candidate, other, deadline):
                    unmatched.pop(index)
                    break
            else:
                return False
        return not unmatched

    async def _schedule_control(
        self,
        selectors: tuple[str, ...],
        *,
        setting: Literal["schedule choice", "date", "time"],
        editable: bool,
        checked: bool | None = None,
    ) -> Any:
        deadline = self._monotonic() + self._control_timeout_seconds
        previous_usable: list[_FrozenScheduleCandidate] = []
        last_scope_count = 0
        last_candidate_count = 0
        last_usable_count = 0
        for observation in range(_CONTROL_POLL_OBSERVATIONS):
            if self._monotonic() >= deadline:
                break
            try:
                await self._await_control(self._wait_for_manual_intervention(self._page), deadline)
                scopes = await self._schedule_scopes(deadline)
                candidates = await self._observed_schedule_candidates(
                    scopes, selectors, choice=setting == "schedule choice", deadline=deadline
                )
                usable = await self._usable_schedule_candidates(
                    candidates, editable=editable, checked=checked, deadline=deadline
                )
            except BaseException as exc:
                if not self._is_transient_control_error(exc):
                    raise
                scopes = ()
                candidates = []
                usable = []
            last_scope_count = len(scopes)
            last_candidate_count = len(candidates)
            last_usable_count = len(usable)
            if len(usable) == 1:
                if await self._same_candidate_sets(previous_usable, usable, deadline):
                    return usable[0].node
                previous_usable = usable
            elif len(usable) > 1:
                if await self._same_candidate_sets(previous_usable, usable, deadline):
                    raise TikTokPublishError(
                        "tiktok_schedule_control_ambiguous",
                        f"TikTok {setting} ambiguous (scopes={last_scope_count}, "
                        f"candidates={last_candidate_count}, usable={last_usable_count})",
                    )
                previous_usable = usable
            else:
                previous_usable = []
            if observation + 1 < _CONTROL_POLL_OBSERVATIONS:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break
                try:
                    await self._await_control(
                        self._sleep(min(_CONTROL_POLL_INTERVAL_SECONDS, remaining)),
                        deadline,
                    )
                except asyncio.TimeoutError:
                    break
        raise TikTokPublishError(
            "tiktok_schedule_unavailable",
            f"TikTok {setting} unavailable (scopes={last_scope_count}, "
            f"candidates={last_candidate_count}, usable={last_usable_count})",
        )

    async def configure(
        self,
        target: TikTokScheduleTarget,
    ) -> TikTokScheduleFormSnapshot:
        date_value, time_value = self._target_values(target)

        toggle = await self._schedule_control(
            SCHEDULE_CHOICE_SELECTORS,
            setting="schedule choice",
            editable=False,
        )
        if not await _schedule_choice_enabled(toggle):
            await toggle.click()

        toggle = await self._schedule_control(
            SCHEDULE_CHOICE_SELECTORS,
            setting="schedule choice",
            editable=False,
            checked=True,
        )

        date_control = await self._schedule_control(
            SCHEDULE_DATE_SELECTORS,
            setting="date",
            editable=True,
        )
        await date_control.fill(date_value)

        date_control = await self._schedule_control(
            SCHEDULE_DATE_SELECTORS,
            setting="date",
            editable=True,
        )
        if str(await date_control.input_value()) != date_value:
            raise TikTokPublishError(
                "tiktok_schedule_readback_mismatch",
                "TikTok 定时日期写入后回读不一致",
            )

        time_control = await self._schedule_control(
            SCHEDULE_TIME_SELECTORS,
            setting="time",
            editable=True,
        )
        await time_control.fill(time_value)

        time_control = await self._schedule_control(
            SCHEDULE_TIME_SELECTORS,
            setting="time",
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

        await self._schedule_control(
            SCHEDULE_CHOICE_SELECTORS,
            setting="schedule choice",
            editable=False,
            checked=True,
        )

        date_control = await self._schedule_control(
            SCHEDULE_DATE_SELECTORS,
            setting="date",
            editable=True,
        )
        if str(await date_control.input_value()) != date_value:
            raise TikTokPublishError(
                "tiktok_schedule_readback_mismatch",
                "TikTok 定时日期回读不一致",
            )

        time_control = await self._schedule_control(
            SCHEDULE_TIME_SELECTORS,
            setting="time",
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
        page = await self._load_scheduled_content_page(refresh=True)
        if page is None:
            return None

        deadline = self._monotonic() + _OUTCOME_TIMEOUT_SECONDS
        while self._monotonic() < deadline:
            await self._wait_for_manual_intervention(page)
            observed_at = self._now()
            page_account_reference = await _unique_dom_account_reference(
                page,
                PAGE_ACCOUNT_REFERENCE_SELECTORS,
            )
            exact: list[TikTokScheduledContentReadback] = []
            try:
                rows = await _bounded_candidates(page, SCHEDULED_ROW_SELECTORS)
            except Exception:
                rows = []
            for row in rows:
                try:
                    if not await row.is_visible():
                        continue
                    row_key = await self._stable_row_key(row)
                    if (
                        row_key is None
                        or row_key in expected.baseline_row_keys
                    ):
                        continue
                    readback = await self._matching_row(
                        row,
                        expected,
                        page_account_reference=page_account_reference,
                        observed_at=observed_at,
                    )
                except Exception:
                    continue
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

    async def capture_scheduled_content_baseline(
        self,
        expected_account_reference: str,
    ) -> TikTokScheduledContentBaseline:
        expected_reference = normalize_tiktok_handle(expected_account_reference)
        page = await self._load_scheduled_content_page()
        if page is None or not expected_reference:
            raise TikTokPublishError(
                "tiktok_schedule_baseline_unavailable",
                "TikTok 定时列表基线无法安全读取",
            )
        snapshots: list[frozenset[str]] = []
        for index in range(2):
            await self._wait_for_manual_intervention(page)
            page_reference = await _unique_dom_account_reference(
                page,
                PAGE_ACCOUNT_REFERENCE_SELECTORS,
            )
            if page_reference != expected_reference:
                raise TikTokPublishError(
                    "tiktok_schedule_readback_identity_mismatch",
                    "TikTok 定时列表账号身份无法唯一匹配",
                )
            try:
                rows = await _bounded_candidates(page, SCHEDULED_ROW_SELECTORS)
            except Exception as exc:
                raise TikTokPublishError(
                    "tiktok_schedule_baseline_unavailable",
                    "TikTok 定时列表基线无法安全读取",
                ) from exc
            row_keys: set[str] = set()
            for row in rows:
                try:
                    if not await row.is_visible():
                        continue
                    row_key = await self._stable_row_key(row)
                except Exception as exc:
                    raise TikTokPublishError(
                        "tiktok_schedule_baseline_unavailable",
                        "TikTok 定时列表基线无法安全读取",
                    ) from exc
                if row_key is None:
                    raise TikTokPublishError(
                        "tiktok_schedule_baseline_unavailable",
                        "TikTok 定时列表存在无法唯一标识的内容",
                    )
                row_keys.add(row_key)
            snapshots.append(frozenset(row_keys))
            if index == 0:
                await self._sleep(_POLL_INTERVAL_SECONDS)
        if snapshots[0] != snapshots[1]:
            raise TikTokPublishError(
                "tiktok_schedule_baseline_unstable",
                "TikTok 定时列表基线尚未稳定",
            )
        return TikTokScheduledContentBaseline(row_keys=snapshots[1])

    async def _load_scheduled_content_page(self, *, refresh: bool = False) -> Any | None:
        if self._readback_page is None:
            try:
                if self._readback_page_factory is not None:
                    self._readback_page = await self._readback_page_factory()
                else:
                    context = getattr(self._page, "context", None)
                    if callable(context):
                        context = context()
                    self._readback_page = await context.new_page()
            except Exception:
                return None
        if self._readback_route_loaded and not refresh:
            return self._readback_page
        loaded = False
        for route in SCHEDULED_CONTENT_ROUTES:
            try:
                await self._readback_page.goto(route)
            except Exception:
                continue
            loaded = True
            break
        if not loaded:
            return None
        self._readback_route_loaded = True
        return self._readback_page

    @staticmethod
    async def _stable_row_key(row: Any) -> str | None:
        content_id = str(await row.get_attribute("data-content-id") or "").strip()
        if content_id:
            return f"content-id:{content_id}"
        content_url = str(
            await row.get_attribute("data-content-url") or ""
        ).strip()
        if content_url:
            return f"content-url:{content_url}"
        row_reference = await _unique_dom_account_reference(
            row,
            ROW_ACCOUNT_REFERENCE_SELECTORS,
        )
        caption = await row.get_attribute("data-caption")
        if caption is None:
            caption = await row.inner_text()
        scheduled_at = str(
            await row.get_attribute("data-schedule-time") or ""
        ).strip()
        timezone = str(
            await row.get_attribute("data-schedule-timezone") or ""
        ).strip()
        caption_hash = hashlib.sha256(
            canonicalize_tiktok_caption(caption).encode("utf-8")
        ).hexdigest()
        if not row_reference or not scheduled_at or not timezone:
            return None
        material = "\x1f".join(
            (row_reference, caption_hash, scheduled_at, timezone)
        )
        return "content-key:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

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
        caption = canonicalize_tiktok_caption(expected.expected_caption)
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
        page_account_reference: str | None,
        observed_at: datetime,
    ) -> TikTokScheduledContentReadback | None:
        expected_account_reference = normalize_tiktok_handle(
            expected.account_reference
        )
        row_account_reference = await _unique_dom_account_reference(
            row,
            ROW_ACCOUNT_REFERENCE_SELECTORS,
        )
        caption = await row.get_attribute("data-caption")
        if caption is None:
            caption = await row.inner_text()
        caption_hash = hashlib.sha256(
            canonicalize_tiktok_caption(caption).encode("utf-8")
        ).hexdigest()
        scheduled_at = await row.get_attribute("data-schedule-time")
        timezone = await row.get_attribute("data-schedule-timezone")
        if (
            not expected_account_reference
            or page_account_reference != expected_account_reference
            or row_account_reference != expected_account_reference
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
