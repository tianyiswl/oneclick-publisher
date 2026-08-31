# -*- coding: utf-8 -*-
"""Read-only Facebook Page content-list baseline and unique Reel matching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Awaitable, Callable, Iterable, Literal
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app_core.overseas_meta_errors import FacebookPagePublishError
from app_core.overseas_meta_page_identity import (
    activate_saved_facebook_page,
    normalize_facebook_page_id,
    validate_facebook_page_binding,
)
from uploader.meta_uploader.page_form import canonical_meta_caption


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REEL_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_CONTENT_HOME_URL = "https://business.facebook.com/latest/home/"
_LEGACY_CONTENT_LIST_URL = "https://business.facebook.com/latest/content?asset_id={}"
_CURRENT_CONTENT_ENTRY_PATH = "/latest/posts"
_CURRENT_CONTENT_LIST_PATH = "/latest/posts/published_posts"
_CONTENT_ENTRY_LABELS = ("内容", "Content")
_MAX_PAGES = 50
_DEFAULT_SETTLEMENT_ATTEMPTS = 4
_CURRENT_TABLE_MAX_SAMPLES = 20
_CURRENT_TABLE_STABLE_SAMPLES = 2
_CURRENT_EMPTY_MIN_SETTLEMENT_MS = 2_000
_CONTENT_SURFACE_WAIT_ATTEMPTS = 120
_CONTENT_SURFACE_WAIT_MS = 250
_CURRENT_TABLE_WAIT_MS = 500
_READBACK_SETTLEMENT_WAIT_MS = 5_000
_CURRENT_FILTER_WAIT_MS = 250
_CURRENT_FILTER_SETTLEMENT_ATTEMPTS = 20
_CURRENT_FILTER_REOPEN_AFTER_ATTEMPTS = 4
_CONTENT_ROW_ID = re.compile(r"(?<!\d)(\d{6,32})(?!\d)")
_CURRENT_REEL_MEDIA_LABELS = frozenset(
    {"reel", "reels", "video", "videos", "视频"}
)
_CURRENT_NON_REEL_MEDIA_LABELS = frozenset(
    {"photo", "photos", "post", "posts", "照片", "帖子"}
)
_TERMINAL_STATUS_TEXTS = frozenset(
    {
        "no more results",
        "you've reached the end",
        "没有更多内容",
        "已显示全部内容",
    }
)
_EMPTY_STATUS_TEXTS = frozenset(
    {
        "no content yet",
        "no posts yet",
        "暂无内容",
        "还没有内容",
    }
)
_PAGINATION_LABELS = (
    "Load more",
    "Next",
    "加载更多",
    "下一页",
)
_ACCEPTED_DECISION_TEXTS = frozenset(
    {
        "your reel is being published",
        "your reel was submitted for publishing",
        "你的 reel 正在发布",
        "你的 reel 已提交发布",
    }
)
_REJECTED_DECISION_TEXTS = frozenset(
    {
        "your reel wasn't published",
        "we couldn't publish your reel",
        "你的 reel 未发布",
        "无法发布你的 reel",
    }
)
_CURRENT_SEARCH_HINTS = (
    ("编号", "配文"),
    ("id", "caption"),
)
_CURRENT_DATE_BOUNDED_PREFIXES = (
    "过去90天",
    "过去 90 天",
    "past 90 days",
)
_CURRENT_DATE_ALL_TIME_PREFIXES = (
    "创建至今",
    "all time",
    "lifetime",
)
_CURRENT_DATE_ALL_TIME_LABELS = (
    "创建至今",
    "All time",
    "Lifetime",
)
_META_TIMEZONE_NAMES = {
    "太平洋时间": "America/Los_Angeles",
    "pacific time": "America/Los_Angeles",
    "pacific standard time": "America/Los_Angeles",
    "北京时间": "Asia/Shanghai",
    "china standard time": "Asia/Shanghai",
}


@dataclass(frozen=True, slots=True)
class FacebookReelRow:
    page_id: str
    reel_id: str
    url: str
    caption_sha256: str
    published_at: str


@dataclass(frozen=True, slots=True)
class FacebookPageContentBaseline:
    page_id: str
    rows: tuple[FacebookReelRow, ...]
    captured_at: str
    snapshot_sha256: str

    def __post_init__(self) -> None:
        if type(self.rows) is not tuple:
            raise TypeError("rows must be an immutable tuple")


@dataclass(frozen=True, slots=True)
class FacebookReelReceipt:
    page_id: str
    reel_id: str
    url: str
    published_at: str


@dataclass(frozen=True, slots=True)
class FacebookReelMatch:
    status: Literal["none", "mismatch", "unique"]
    receipt: FacebookReelReceipt | None
    new_count: int
    matching_count: int


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class FacebookPlatformDecision:
    """Trusted decision value; instances are created only by the DOM parser."""

    _kind: Literal["accepted", "rejected_no_creation", "unknown"] = field(
        repr=False
    )
    _page_id: str = field(repr=False)
    _observed_at: str = field(repr=False)
    _evidence_sha256: str = field(repr=False)

    @property
    def kind(self) -> Literal["accepted", "rejected_no_creation", "unknown"]:
        return _trusted_decision_values(self)[0]

    @property
    def page_id(self) -> str:
        return _trusted_decision_values(self)[1]

    @property
    def observed_at(self) -> str:
        return _trusted_decision_values(self)[2]

    @property
    def evidence_sha256(self) -> str:
        return _trusted_decision_values(self)[3]


_DecisionValues = tuple[str, str, str, str]
_TRUSTED_DECISIONS: dict[
    int,
    tuple[weakref.ReferenceType[FacebookPlatformDecision], _DecisionValues],
] = {}


def _trusted_decision_values(value: FacebookPlatformDecision) -> _DecisionValues:
    record = _TRUSTED_DECISIONS.get(id(value))
    if record is None or record[0]() is not value:
        raise ValueError("untrusted Facebook platform decision")
    actual = (
        object.__getattribute__(value, "_kind"),
        object.__getattribute__(value, "_page_id"),
        object.__getattribute__(value, "_observed_at"),
        object.__getattribute__(value, "_evidence_sha256"),
    )
    if actual != record[1]:
        raise ValueError("mutated Facebook platform decision")
    return record[1]


def _baseline_failed(page_id: object = "") -> FacebookPagePublishError:
    receipt: dict[str, object] = {
        "phase": "content_list_readback",
        "platformWriteOccurred": False,
        "finalActionTriggered": False,
    }
    candidate = str(page_id or "").strip()
    if candidate.isascii() and candidate.isdigit():
        receipt["pageId"] = candidate
    return FacebookPagePublishError(
        "facebook_page_baseline_read_failed",
        "Facebook Page 内容列表无法证明完整且主体一致，已停止。",
        receipt=receipt,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _hash_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_page_id(value: object) -> str:
    try:
        return normalize_facebook_page_id(value)
    except FacebookPagePublishError as exc:
        raise _baseline_failed(value) from exc


def _normalized_reel_id(value: object) -> str:
    if type(value) is not str:
        raise ValueError("invalid Reel ID")
    reel_id = value.strip()
    if _REEL_ID.fullmatch(reel_id) is None:
        raise ValueError("invalid Reel ID")
    return reel_id


def _canonical_reel_url(value: object, *, expected_reel_id: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("missing Reel URL")
    parsed = urlsplit(urljoin("https://www.facebook.com", value.strip()))
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname not in {"facebook.com", "www.facebook.com"}
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("non-canonical Reel URL")
    path = [part for part in parsed.path.split("/") if part]
    if len(path) != 2 or path[0].casefold() != "reel":
        raise ValueError("non-canonical Reel URL")
    reel_id = _normalized_reel_id(path[1])
    if reel_id != expected_reel_id:
        raise ValueError("Reel URL identity mismatch")
    return f"https://www.facebook.com/reel/{reel_id}"


def _page_url(page: Any) -> str:
    value = getattr(page, "url", "")
    if callable(value):
        value = value()
    return str(value or "")


def _is_exact_content_url(value: object, *, expected_page_id: str) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and parsed.hostname == "business.facebook.com"
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/")
        in {"/latest/content", _CURRENT_CONTENT_LIST_PATH}
        and set(query) == {"asset_id"}
        and query.get("asset_id") == [expected_page_id]
        and not parsed.fragment
    )


def _is_exact_content_entry_url(value: object, *, expected_page_id: str) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and parsed.hostname == "business.facebook.com"
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == _CURRENT_CONTENT_ENTRY_PATH
        and set(query) == {"asset_id"}
        and query.get("asset_id") == [expected_page_id]
        and not parsed.fragment
    )


def _is_current_content_url(value: object, *, expected_page_id: str) -> bool:
    if not _is_exact_content_url(value, expected_page_id=expected_page_id):
        return False
    return urlsplit(str(value)).path.rstrip("/") == _CURRENT_CONTENT_LIST_PATH


def _current_table_media_kind(value: object) -> str:
    text = canonical_meta_caption(value)
    lines = {line.strip().casefold() for line in text.splitlines() if line.strip()}
    if lines & _CURRENT_REEL_MEDIA_LABELS:
        return "reel"
    if lines & _CURRENT_NON_REEL_MEDIA_LABELS:
        return "other"
    return "unknown"


def _current_table_row_id(value: object) -> str:
    text = canonical_meta_caption(value)
    matches = _CONTENT_ROW_ID.findall(text)
    if len(matches) != 1:
        raise ValueError("ambiguous content row identity")
    return _normalized_reel_id(matches[0])


def _current_table_timestamp(
    value: object,
    *,
    observed_at: datetime | None = None,
    display_timezone: tzinfo | None = None,
) -> str:
    text = canonical_meta_caption(value)
    if not text:
        raise ValueError("missing content timestamp")
    try:
        return _canonical_timestamp(text)
    except ValueError:
        pass

    now = observed_at or datetime.now().astimezone()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("content timestamp observation must be timezone-aware")
    relative_anchor = now.replace(second=0, microsecond=0)
    folded = text.casefold()
    relative = re.fullmatch(r"(\d+)\s*(?:分钟|分鐘|min(?:ute)?s?)\s*(?:前|ago)?", folded)
    if relative:
        try:
            relative_anchor -= timedelta(minutes=int(relative.group(1)))
        except OverflowError as exc:
            raise ValueError("unsupported content timestamp") from exc
        return relative_anchor.astimezone(timezone.utc).isoformat()
    relative = re.fullmatch(r"(\d+)\s*(?:小时|小時|h(?:ou)?rs?)\s*(?:前|ago)?", folded)
    if relative:
        try:
            relative_anchor -= timedelta(hours=int(relative.group(1)))
        except OverflowError as exc:
            raise ValueError("unsupported content timestamp") from exc
        return relative_anchor.astimezone(timezone.utc).isoformat()
    if folded in {"刚刚", "剛剛", "just now"}:
        return relative_anchor.astimezone(timezone.utc).isoformat()

    chinese = re.fullmatch(
        r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})",
        text,
    )
    if chinese:
        if display_timezone is None:
            raise ValueError("timezone-less content timestamp lacks page timezone")
        explicit_year, month, day, hour, minute = chinese.groups()
        local_now = now.astimezone(display_timezone)
        year = int(explicit_year) if explicit_year else local_now.year
        parsed = datetime(
            year,
            int(month),
            int(day),
            int(hour),
            int(minute),
            tzinfo=display_timezone,
        )
        if explicit_year is None and parsed - local_now > timedelta(days=2):
            parsed = parsed.replace(year=year - 1)
        return parsed.astimezone(timezone.utc).isoformat()
    raise ValueError("unsupported content timestamp")


def _canonical_timestamp(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _normalized_hash(value: object, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError("invalid sha256")
    return value


def _normalized_row(
    value: object,
    *,
    require_caption_hash: bool,
) -> FacebookReelRow:
    if type(value) is not FacebookReelRow:
        raise ValueError("row is not an exact FacebookReelRow")
    page_id = _normalized_page_id(value.page_id)
    reel_id = _normalized_reel_id(value.reel_id)
    return FacebookReelRow(
        page_id=page_id,
        reel_id=reel_id,
        url=_canonical_reel_url(value.url, expected_reel_id=reel_id),
        caption_sha256=_normalized_hash(
            value.caption_sha256,
            allow_empty=not require_caption_hash,
        ),
        published_at=_canonical_timestamp(value.published_at),
    )


def _baseline_projection(
    *,
    page_id: str,
    rows: Iterable[FacebookReelRow],
    captured_at: str,
) -> dict[str, object]:
    safe_rows = [
        {
            "pageId": row.page_id,
            "reelId": row.reel_id,
            "url": row.url,
            "captionSha256": row.caption_sha256,
            "publishedAt": row.published_at,
        }
        for row in sorted(rows, key=lambda item: item.reel_id)
    ]
    return {
        "pageId": page_id,
        "rows": safe_rows,
        "capturedAt": captured_at,
    }


def _build_baseline(
    *,
    page_id: str,
    rows: Iterable[FacebookReelRow],
    captured_at: str,
) -> FacebookPageContentBaseline:
    normalized_page_id = _normalized_page_id(page_id)
    normalized_captured_at = _canonical_timestamp(captured_at)
    normalized_rows = tuple(
        sorted(
            (
                _normalized_row(row, require_caption_hash=True)
                for row in rows
            ),
            key=lambda item: item.reel_id,
        )
    )
    seen: set[str] = set()
    for row in normalized_rows:
        if row.page_id != normalized_page_id or row.reel_id in seen:
            raise _baseline_failed(normalized_page_id)
        seen.add(row.reel_id)
    projection = _baseline_projection(
        page_id=normalized_page_id,
        rows=normalized_rows,
        captured_at=normalized_captured_at,
    )
    return FacebookPageContentBaseline(
        page_id=normalized_page_id,
        rows=normalized_rows,
        captured_at=normalized_captured_at,
        snapshot_sha256=_hash_json(projection),
    )


def _validated_baseline(
    value: object,
    *,
    expected_page_id: str,
) -> FacebookPageContentBaseline:
    if type(value) is not FacebookPageContentBaseline:
        raise _baseline_failed(expected_page_id)
    try:
        rebuilt = _build_baseline(
            page_id=value.page_id,
            rows=value.rows,
            captured_at=value.captured_at,
        )
    except FacebookPagePublishError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise _baseline_failed(expected_page_id) from exc
    if (
        rebuilt.page_id != expected_page_id
        or type(value.snapshot_sha256) is not str
        or value.snapshot_sha256 != rebuilt.snapshot_sha256
    ):
        raise _baseline_failed(expected_page_id)
    return rebuilt


def match_unique_new_facebook_reel(
    *,
    baseline: FacebookPageContentBaseline,
    current_rows: Iterable[FacebookReelRow],
    expected_page_id: str,
    expected_caption_sha256: str,
    clicked_at: str,
) -> FacebookReelMatch:
    """Match exactly one target among complete visible rows after the click."""

    expected_page = _normalized_page_id(expected_page_id)
    valid_baseline = _validated_baseline(
        baseline,
        expected_page_id=expected_page,
    )
    try:
        expected_caption = _normalized_hash(expected_caption_sha256)
        clicked = datetime.fromisoformat(_canonical_timestamp(clicked_at))
        candidates = list(current_rows)
    except (TypeError, ValueError) as exc:
        raise _baseline_failed(expected_page) from exc

    old_ids = {row.reel_id for row in valid_baseline.rows}
    visible_new: list[FacebookReelRow | None] = []
    current_seen: set[str] = set()
    invalid_existing = False
    unsafe_new = False
    for raw_row in candidates:
        try:
            row = _normalized_row(raw_row, require_caption_hash=False)
        except (FacebookPagePublishError, TypeError, ValueError):
            visible_new.append(None)
            unsafe_new = True
            continue
        if row.reel_id in current_seen:
            if row.reel_id not in old_ids:
                visible_new.append(None)
                unsafe_new = True
            else:
                invalid_existing = True
            continue
        current_seen.add(row.reel_id)
        if row.reel_id not in old_ids:
            visible_new.append(row)
            if row.page_id != expected_page or not row.caption_sha256:
                unsafe_new = True

    if not visible_new:
        if invalid_existing:
            return FacebookReelMatch("mismatch", None, 0, 0)
        return FacebookReelMatch("none", None, 0, 0)

    matching: list[FacebookReelRow] = []
    for row in visible_new:
        if row is None or row.page_id != expected_page:
            continue
        try:
            published = datetime.fromisoformat(row.published_at)
        except ValueError:
            continue
        if (
            row.caption_sha256 == expected_caption
            and (
                published >= clicked
                or (
                    published.second == 0
                    and published.microsecond == 0
                    and published == clicked.replace(second=0, microsecond=0)
                )
            )
            and row.reel_id not in old_ids
        ):
            matching.append(row)

    if unsafe_new or len(matching) != 1:
        return FacebookReelMatch(
            "mismatch",
            None,
            len(visible_new),
            len(matching),
        )
    target = matching[0]
    return FacebookReelMatch(
        "unique",
        FacebookReelReceipt(
            page_id=target.page_id,
            reel_id=target.reel_id,
            url=target.url,
            published_at=target.published_at,
        ),
        len(visible_new),
        1,
    )


class FacebookPageContentReader:
    """Read an explicitly complete Page list in one authenticated context."""

    def __init__(
        self,
        context,
        *,
        wait_for_verification: Callable[..., Awaitable[None]],
        trusted_display_timezone: tzinfo | None = None,
    ) -> None:
        if context is None or not callable(getattr(context, "new_page", None)):
            raise TypeError("context must be a browser context")
        if not callable(wait_for_verification):
            raise TypeError("wait_for_verification must be callable")
        if trusted_display_timezone is not None:
            try:
                offset = datetime.now(trusted_display_timezone).utcoffset()
            except Exception as exc:
                raise TypeError(
                    "trusted_display_timezone must be timezone-aware"
                ) from exc
            if offset is None:
                raise TypeError("trusted_display_timezone must be timezone-aware")
        self.context = context
        self._wait_for_verification = wait_for_verification
        self._trusted_display_timezone = trusted_display_timezone
        self._list_page: Any | None = None
        self._activated_page_id = ""

    async def capture_baseline(
        self,
        expected_page_id: str,
    ) -> FacebookPageContentBaseline:
        expected = _normalized_page_id(expected_page_id)
        try:
            summaries = await self._read_complete_list(expected)
            rows: list[FacebookReelRow] = []
            for summary in summaries:
                caption_hash = await self._read_full_caption_hash(
                    summary,
                    expected_page_id=expected,
                )
                if caption_hash is None:
                    raise _baseline_failed(expected)
                rows.append(
                    FacebookReelRow(
                        page_id=summary.page_id,
                        reel_id=summary.reel_id,
                        url=summary.url,
                        caption_sha256=caption_hash,
                        published_at=summary.published_at,
                    )
                )
            return _build_baseline(
                page_id=expected,
                rows=rows,
                captured_at=datetime.now(timezone.utc).isoformat(),
            )
        except FacebookPagePublishError as exc:
            if exc.error_code == "facebook_page_baseline_read_failed":
                raise
            raise _baseline_failed(expected) from exc
        except Exception as exc:
            raise _baseline_failed(expected) from exc

    async def readback_unique_reel(
        self,
        *,
        baseline: FacebookPageContentBaseline,
        expected_page_id: str,
        expected_caption_sha256: str,
        clicked_at: str,
    ) -> FacebookReelMatch:
        expected = _normalized_page_id(expected_page_id)
        valid_baseline = _validated_baseline(
            baseline,
            expected_page_id=expected,
        )
        old_by_id = {row.reel_id: row for row in valid_baseline.rows}

        for attempt in range(_DEFAULT_SETTLEMENT_ATTEMPTS):
            try:
                summaries = await self._read_complete_list(expected)
                current_rows: list[FacebookReelRow] = []
                for summary in summaries:
                    old = old_by_id.get(summary.reel_id)
                    if old is not None:
                        caption_hash = old.caption_sha256
                    else:
                        caption_hash = await self._read_full_caption_hash(
                            summary,
                            expected_page_id=expected,
                        )
                        if caption_hash is None:
                            caption_hash = ""
                    current_rows.append(
                        FacebookReelRow(
                            page_id=summary.page_id,
                            reel_id=summary.reel_id,
                            url=summary.url,
                            caption_sha256=caption_hash,
                            published_at=summary.published_at,
                        )
                    )
                match = match_unique_new_facebook_reel(
                    baseline=valid_baseline,
                    current_rows=current_rows,
                    expected_page_id=expected,
                    expected_caption_sha256=expected_caption_sha256,
                    clicked_at=clicked_at,
                )
            except FacebookPagePublishError as exc:
                failure = (
                    exc
                    if exc.error_code == "facebook_page_baseline_read_failed"
                    else _baseline_failed(expected)
                )
                if attempt + 1 == _DEFAULT_SETTLEMENT_ATTEMPTS:
                    if failure is exc:
                        raise
                    raise failure from exc
                await self._bounded_pause()
                continue
            except Exception as exc:
                if attempt + 1 == _DEFAULT_SETTLEMENT_ATTEMPTS:
                    raise _baseline_failed(expected) from exc
                await self._bounded_pause()
                continue
            if match.status != "none" or attempt + 1 == _DEFAULT_SETTLEMENT_ATTEMPTS:
                return match
            await self._bounded_pause()
        return FacebookReelMatch("none", None, 0, 0)

    async def read_platform_decision(
        self,
        expected_page_id: str,
        *,
        page: Any | None = None,
    ) -> FacebookPlatformDecision:
        """Seal one decision parsed from an explicit, unique DOM announcement."""

        expected = _normalized_page_id(expected_page_id)
        try:
            if page is None:
                page = await self._get_list_page()
            self._require_same_context(page)
            await self._wait_for_verification(page)
            await self._recheck_page(page, expected)

            messages: list[str] = []
            for role in ("alert", "status"):
                elements = await self._visible(page.get_by_role(role))
                for element in elements:
                    text = canonical_meta_caption(await element.inner_text())
                    if text:
                        messages.append(text.casefold())

            kind: Literal["accepted", "rejected_no_creation", "unknown"]
            kind = "unknown"
            if len(messages) == 1:
                if messages[0] in _ACCEPTED_DECISION_TEXTS:
                    kind = "accepted"
                elif messages[0] in _REJECTED_DECISION_TEXTS:
                    kind = "rejected_no_creation"
            observed_at = datetime.now(timezone.utc).isoformat()
            evidence_hash = _hash_json(
                {
                    "pageId": expected,
                    "kind": kind,
                    "observedAt": observed_at,
                    "messages": messages,
                }
            )
            decision = object.__new__(FacebookPlatformDecision)
            values: _DecisionValues = (
                kind,
                expected,
                observed_at,
                evidence_hash,
            )
            for name, value in zip(
                ("_kind", "_page_id", "_observed_at", "_evidence_sha256"),
                values,
            ):
                object.__setattr__(decision, name, value)
            decision_id = id(decision)

            def discard(
                reference: weakref.ReferenceType[FacebookPlatformDecision],
                *,
                identity: int = decision_id,
            ) -> None:
                record = _TRUSTED_DECISIONS.get(identity)
                if record is not None and record[0] is reference:
                    _TRUSTED_DECISIONS.pop(identity, None)

            reference = weakref.ref(decision, discard)
            _TRUSTED_DECISIONS[decision_id] = (reference, values)
            return decision
        except FacebookPagePublishError:
            raise
        except Exception as exc:
            raise _baseline_failed(expected) from exc

    async def _read_complete_list(
        self,
        expected_page_id: str,
    ) -> tuple[FacebookReelRow, ...]:
        page = await self._prepare_list_page(expected_page_id)
        if _is_current_content_url(
            _page_url(page),
            expected_page_id=expected_page_id,
        ):
            return await self._read_current_posts_table(page, expected_page_id)

        rows_by_id: dict[str, FacebookReelRow] = {}
        previous_signature: tuple[tuple[str, str, str], ...] | None = None
        allow_persisted_prefix = False
        for _ in range(_MAX_PAGES):
            await self._recheck_page(page, expected_page_id)
            if not _is_exact_content_url(
                _page_url(page),
                expected_page_id=expected_page_id,
            ):
                raise _baseline_failed(expected_page_id)
            roots = await self._visible(page.locator("main"))
            if len(roots) != 1:
                raise _baseline_failed(expected_page_id)

            row_elements = await self._visible(
                roots[0].locator('a[href*="/reel/"]')
            )
            page_seen: set[str] = set()
            signature: list[tuple[str, str, str]] = []
            new_count = 0
            for row_index, element in enumerate(row_elements):
                try:
                    href = await element.get_attribute("href")
                    parsed = urlsplit(
                        urljoin("https://www.facebook.com", str(href or ""))
                    )
                    path = [part for part in parsed.path.split("/") if part]
                    if len(path) != 2 or path[0].casefold() != "reel":
                        raise ValueError("missing canonical Reel identity")
                    reel_id = _normalized_reel_id(path[1])
                    url = _canonical_reel_url(
                        href,
                        expected_reel_id=reel_id,
                    )
                    timestamps = await self._visible(
                        element.locator("time[datetime]")
                    )
                    if len(timestamps) != 1:
                        raise ValueError("ambiguous Reel timestamp")
                    published_at = _canonical_timestamp(
                        await timestamps[0].get_attribute("datetime")
                    )
                except (FacebookPagePublishError, TypeError, ValueError) as exc:
                    raise _baseline_failed(expected_page_id) from exc
                if reel_id in page_seen:
                    raise _baseline_failed(expected_page_id)
                page_seen.add(reel_id)
                row = FacebookReelRow(
                    page_id=expected_page_id,
                    reel_id=reel_id,
                    url=url,
                    caption_sha256="",
                    published_at=published_at,
                )
                previous = rows_by_id.get(reel_id)
                if previous is not None and previous != row:
                    raise _baseline_failed(expected_page_id)
                if previous is not None and not (
                    allow_persisted_prefix
                    and previous_signature is not None
                    and row_index < len(previous_signature)
                    and previous_signature[row_index]
                    == (reel_id, url, published_at)
                ):
                    raise _baseline_failed(expected_page_id)
                if previous is None:
                    rows_by_id[reel_id] = row
                    new_count += 1
                signature.append((reel_id, url, published_at))

            signature_tuple = tuple(signature)
            if (
                allow_persisted_prefix
                and previous_signature is not None
                and signature_tuple[: len(previous_signature)]
                != previous_signature
            ):
                raise _baseline_failed(expected_page_id)
            no_progress = (
                previous_signature == signature_tuple and new_count == 0
            )

            statuses = await self._visible(page.get_by_role("status"))
            terminal_count = 0
            empty_count = 0
            for element in statuses:
                status = canonical_meta_caption(
                    await element.inner_text()
                ).casefold()
                terminal_count += int(status in _TERMINAL_STATUS_TEXTS)
                empty_count += int(status in _EMPTY_STATUS_TEXTS)
            if terminal_count > 1 or empty_count > 1:
                raise _baseline_failed(expected_page_id)

            next_buttons: list[tuple[str, Any]] = []
            for label in _PAGINATION_LABELS:
                next_buttons.extend(
                    (label, button)
                    for button in await self._visible(
                        page.get_by_role("button", name=label, exact=True)
                    )
                )
            if len(next_buttons) > 1:
                raise _baseline_failed(expected_page_id)

            if empty_count == 1:
                if row_elements or rows_by_id or terminal_count or next_buttons:
                    raise _baseline_failed(expected_page_id)
                return ()
            if terminal_count == 1:
                if not rows_by_id or next_buttons:
                    raise _baseline_failed(expected_page_id)
                return tuple(
                    sorted(rows_by_id.values(), key=lambda item: item.reel_id)
                )
            if no_progress:
                raise _baseline_failed(expected_page_id)
            if len(next_buttons) != 1:
                raise _baseline_failed(expected_page_id)
            next_label, next_button = next_buttons[0]
            if not await next_button.is_enabled():
                raise _baseline_failed(expected_page_id)
            previous_signature = signature_tuple
            allow_persisted_prefix = next_label in {"Load more", "加载更多"}
            await next_button.click()
            await self._wait_for_verification(page)
            await self._recheck_page(page, expected_page_id)
        raise _baseline_failed(expected_page_id)

    @staticmethod
    def _current_date_scope(value: object) -> str:
        text = " ".join(canonical_meta_caption(value).casefold().split())
        compact = text.replace(" ", "")
        if any(
            text.startswith(prefix) or compact.startswith(prefix.replace(" ", ""))
            for prefix in _CURRENT_DATE_ALL_TIME_PREFIXES
        ):
            return "all_time"
        if any(
            text.startswith(prefix) or compact.startswith(prefix.replace(" ", ""))
            for prefix in _CURRENT_DATE_BOUNDED_PREFIXES
        ):
            return "bounded"
        return ""

    async def _current_posts_display_timezone(self, page: Any) -> tzinfo | None:
        candidates: set[str] = set()
        timezone_nodes = await self._elements(
            page.locator("[data-meta-timezone], [data-timezone]")
        )
        for node in timezone_nodes:
            for attribute in ("data-meta-timezone", "data-timezone"):
                value = canonical_meta_caption(await node.get_attribute(attribute))
                if value:
                    candidates.add(value)

        body_text = ""
        bodies = await self._visible(page.locator("body"))
        if len(bodies) > 1:
            raise _baseline_failed()
        if bodies:
            body_text = canonical_meta_caption(await bodies[0].inner_text()).casefold()
        for marker, timezone_name in _META_TIMEZONE_NAMES.items():
            if marker.casefold() in body_text:
                candidates.add(timezone_name)

        offset_matches = set(
            re.findall(r"(?:utc|gmt)\s*([+-]\d{1,2})(?::?(\d{2}))?", body_text)
        )
        if len(offset_matches) > 1:
            raise _baseline_failed()
        if offset_matches:
            hours_text, minutes_text = next(iter(offset_matches))
            hours = int(hours_text)
            minutes = int(minutes_text or "0")
            if abs(hours) > 14 or minutes > 59:
                raise _baseline_failed()
            sign = -1 if hours < 0 else 1
            offset = timedelta(hours=hours, minutes=sign * minutes)
            offset_name = f"UTC{hours:+03d}:{minutes:02d}"
            candidates.add(offset_name)

        resolved: list[tzinfo] = []
        for candidate in sorted(candidates):
            if candidate.startswith("UTC+") or candidate.startswith("UTC-"):
                matched = re.fullmatch(r"UTC([+-]\d{2}):(\d{2})", candidate)
                if matched is None:
                    raise _baseline_failed()
                hours = int(matched.group(1))
                minutes = int(matched.group(2))
                sign = -1 if hours < 0 else 1
                resolved.append(
                    timezone(timedelta(hours=hours, minutes=sign * minutes))
                )
                continue
            try:
                resolved.append(ZoneInfo(candidate))
            except ZoneInfoNotFoundError as exc:
                raise _baseline_failed() from exc
        unique = {str(item) for item in resolved}
        if len(unique) > 1:
            raise _baseline_failed()
        return resolved[0] if resolved else self._trusted_display_timezone

    async def _clear_current_posts_search(self, page: Any) -> None:
        candidates = await self._visible(
            page.locator('[role="searchbox"], input[type="search"]')
        )
        matching: list[Any] = []
        for candidate in candidates:
            hints = " ".join([
                canonical_meta_caption(await candidate.get_attribute(attribute))
                for attribute in ("aria-label", "placeholder")
            ]).casefold()
            if any(all(part in hints for part in pair) for pair in _CURRENT_SEARCH_HINTS):
                matching.append(candidate)
        if len(matching) > 1:
            raise _baseline_failed()
        if not matching:
            return
        search = matching[0]
        input_value = getattr(search, "input_value", None)
        value = (
            canonical_meta_caption(await input_value())
            if callable(input_value)
            else canonical_meta_caption(await search.get_attribute("value"))
        )
        if not value:
            return
        fill = getattr(search, "fill", None)
        if not callable(fill):
            raise _baseline_failed()
        await fill("")
        await self._wait_current_filter(page)
        value = (
            canonical_meta_caption(await input_value())
            if callable(input_value)
            else canonical_meta_caption(await search.get_attribute("value"))
        )
        if value:
            raise _baseline_failed()

    async def _current_date_button(self, page: Any) -> tuple[str, Any] | None:
        matching: list[tuple[str, Any]] = []
        for button in await self._visible(page.get_by_role("button")):
            label = canonical_meta_caption(await button.get_attribute("aria-label"))
            text = canonical_meta_caption(await button.inner_text())
            scope = self._current_date_scope(label or text)
            if scope:
                matching.append((scope, button))
        if len(matching) > 1:
            raise _baseline_failed()
        return matching[0] if matching else None

    async def _all_time_option(self, page: Any) -> Any:
        for attempt in range(_CURRENT_FILTER_SETTLEMENT_ATTEMPTS):
            for role in ("option", "menuitem", "button"):
                found: list[Any] = []
                for label in _CURRENT_DATE_ALL_TIME_LABELS:
                    found.extend(
                        await self._visible(
                            page.get_by_role(role, name=label, exact=True)
                        )
                    )
                if len(found) > 1:
                    raise _baseline_failed()
                if len(found) == 1:
                    return found[0]
            found = []
            for label in _CURRENT_DATE_ALL_TIME_LABELS:
                found.extend(
                    await self._visible(page.get_by_text(label, exact=True))
                )
            if len(found) > 1:
                raise _baseline_failed()
            if len(found) == 1:
                return found[0]
            if attempt + 1 == _CURRENT_FILTER_REOPEN_AFTER_ATTEMPTS:
                refreshed = await self._current_date_button(page)
                if refreshed is None or refreshed[0] != "bounded":
                    raise _baseline_failed()
                date_button = refreshed[1]
                expanded = canonical_meta_caption(
                    await date_button.get_attribute("aria-expanded")
                ).casefold()
                if expanded not in {"true", "1"}:
                    await date_button.click()
            if attempt + 1 < _CURRENT_FILTER_SETTLEMENT_ATTEMPTS:
                await self._wait_current_filter(page)
        raise _baseline_failed()

    async def _normalize_current_posts_view(
        self,
        page: Any,
        expected_page_id: str,
    ) -> tzinfo | None:
        """Remove known search/date filters before treating the table as complete."""

        await self._clear_current_posts_search(page)
        date_control = await self._current_date_button(page)
        display_timezone = await self._current_posts_display_timezone(page)
        if date_control is None:
            return display_timezone
        scope, button = date_control
        if scope == "bounded":
            await button.click()
            await self._wait_current_filter(page)
            display_timezone = (
                await self._current_posts_display_timezone(page)
                or display_timezone
            )
            option = await self._all_time_option(page)
            await option.click()
            for attempt in range(_CURRENT_FILTER_SETTLEMENT_ATTEMPTS):
                await self._wait_current_filter(page)
                await self._recheck_page(page, expected_page_id)
                refreshed = await self._current_date_button(page)
                if refreshed is not None and refreshed[0] == "all_time":
                    break
                if attempt + 1 == _CURRENT_FILTER_SETTLEMENT_ATTEMPTS:
                    raise _baseline_failed(expected_page_id)
        return (
            await self._current_posts_display_timezone(page)
            or display_timezone
        )

    @staticmethod
    async def _wait_current_filter(page: Any) -> None:
        wait = getattr(page, "wait_for_timeout", None)
        if callable(wait):
            await wait(_CURRENT_FILTER_WAIT_MS)
        else:
            await asyncio.sleep(_CURRENT_FILTER_WAIT_MS / 1000)

    async def _read_current_posts_table(
        self,
        page: Any,
        expected_page_id: str,
    ) -> tuple[FacebookReelRow, ...]:
        """Read Meta's current published-posts grid until its end is stable."""

        rows_by_id: dict[str, FacebookReelRow] = {}
        seen_content_rows: dict[str, tuple[str, str]] = {}
        previous_signature: tuple[tuple[str, str, str], ...] | None = None
        stable_at_bottom = 0
        stable_empty_status = 0
        empty_started_at: float | None = None
        display_timezone = await self._normalize_current_posts_view(
            page,
            expected_page_id,
        )
        observed_at = datetime.now(timezone.utc)

        for sample_index in range(_CURRENT_TABLE_MAX_SAMPLES):
            await self._recheck_page(page, expected_page_id)
            if not _is_current_content_url(
                _page_url(page),
                expected_page_id=expected_page_id,
            ):
                raise _baseline_failed(expected_page_id)

            title_headers = await self._visible(
                page.get_by_role("columnheader", name="标题", exact=True)
            )
            if not title_headers:
                title_headers = await self._visible(
                    page.get_by_role("columnheader", name="Title", exact=True)
                )
            date_headers = await self._visible(
                page.get_by_role("columnheader", name="发布日期", exact=True)
            )
            if not date_headers:
                date_headers = await self._visible(
                    page.get_by_role(
                        "columnheader",
                        name="Publish date",
                        exact=True,
                    )
                )
            if len(title_headers) != 1 or len(date_headers) != 1:
                if sample_index + 1 == _CURRENT_TABLE_MAX_SAMPLES:
                    raise _baseline_failed(expected_page_id)
                await self._wait_current_table_sample(page)
                continue

            data_rows = await self._visible(
                page.locator('tr[role="row"][data-index]')
            )
            parsed_rows: list[tuple[str, str, str]] = []
            parse_failure: Exception | None = None
            for row in data_rows:
                try:
                    checkboxes = await self._visible(
                        row.locator('input[type="checkbox"][aria-label]')
                    )
                    title_cells = await self._visible(
                        row.locator('td[aria-colindex="2"]')
                    )
                    date_cells = await self._visible(
                        row.locator('td[aria-colindex="3"]')
                    )
                    if (
                        len(checkboxes) != 1
                        or len(title_cells) != 1
                        or len(date_cells) != 1
                    ):
                        raise ValueError("ambiguous current content row")
                    content_id = _current_table_row_id(
                        await checkboxes[0].get_attribute("aria-label")
                    )
                    title_text = canonical_meta_caption(
                        await title_cells[0].inner_text()
                    )
                    media_kind = _current_table_media_kind(title_text)
                    if media_kind == "unknown":
                        raise ValueError("unknown current content kind")

                    timestamps = await self._visible(
                        date_cells[0].locator("time[datetime]")
                    )
                    if len(timestamps) > 1:
                        raise ValueError("ambiguous current content timestamp")
                    raw_timestamp = (
                        await timestamps[0].get_attribute("datetime")
                        if len(timestamps) == 1
                        else await date_cells[0].inner_text()
                    )
                    published_at = _current_table_timestamp(
                        canonical_meta_caption(raw_timestamp),
                        observed_at=observed_at,
                        display_timezone=display_timezone,
                    )
                    parsed_rows.append((content_id, media_kind, published_at))
                except Exception as exc:
                    parse_failure = exc
                    break
            if parse_failure is not None:
                if sample_index + 1 == _CURRENT_TABLE_MAX_SAMPLES:
                    raise _baseline_failed(expected_page_id) from parse_failure
                await self._wait_current_table_sample(page)
                continue

            signature: list[tuple[str, str, str]] = []
            new_content_count = 0
            for content_id, media_kind, published_at in parsed_rows:
                # Business Suite adds transient row actions (for example
                # “创建广告”) after the content identity has rendered.  The
                # exact ID, media kind and canonical publish time are stable;
                # Reel captions are verified separately on the canonical
                # public detail URL.
                projection = (media_kind, published_at)
                previous = seen_content_rows.get(content_id)
                if previous is not None and previous != projection:
                    raise _baseline_failed(expected_page_id)
                if previous is None:
                    seen_content_rows[content_id] = projection
                    new_content_count += 1
                signature.append((content_id, media_kind, published_at))
                if media_kind == "reel":
                    reel_url = f"https://www.facebook.com/reel/{content_id}"
                    reel_row = FacebookReelRow(
                        page_id=expected_page_id,
                        reel_id=content_id,
                        url=reel_url,
                        caption_sha256="",
                        published_at=published_at,
                    )
                    existing_reel = rows_by_id.get(content_id)
                    if existing_reel is None:
                        rows_by_id[content_id] = reel_row

            statuses = await self._visible(page.get_by_role("status"))
            empty_count = 0
            for element in statuses:
                status = canonical_meta_caption(
                    await element.inner_text()
                ).casefold()
                empty_count += int(status in _EMPTY_STATUS_TEXTS)
            if empty_count > 1:
                raise _baseline_failed(expected_page_id)
            if empty_count == 1:
                if data_rows or seen_content_rows:
                    raise _baseline_failed(expected_page_id)
                if empty_started_at is None:
                    empty_started_at = time.monotonic()
                stable_empty_status += 1
                settled_ms = (time.monotonic() - empty_started_at) * 1000
                if (
                    stable_empty_status >= _CURRENT_TABLE_STABLE_SAMPLES
                    and settled_ms >= _CURRENT_EMPTY_MIN_SETTLEMENT_MS
                ):
                    return ()
                await self._wait_current_table_sample(page)
                continue
            stable_empty_status = 0
            empty_started_at = None

            signature_tuple = tuple(signature)
            try:
                at_bottom = await self._scroll_current_table_to_bottom(page)
            except FacebookPagePublishError:
                if sample_index + 1 == _CURRENT_TABLE_MAX_SAMPLES:
                    raise
                await self._wait_current_table_sample(page)
                continue
            if (
                at_bottom
                and previous_signature == signature_tuple
                and new_content_count == 0
            ):
                stable_at_bottom += 1
            else:
                stable_at_bottom = 0
            # The current Business Suite table renders its headers before its
            # data rows.  An empty signature therefore does not prove that the
            # content list is complete; accepting it here can miss a Reel that
            # appears a moment later.  Photo/post rows are still valid evidence
            # that the table has hydrated, even when the resulting Reel
            # baseline is intentionally empty.
            if (
                seen_content_rows
                and stable_at_bottom >= _CURRENT_TABLE_STABLE_SAMPLES
            ):
                return tuple(
                    sorted(rows_by_id.values(), key=lambda item: item.reel_id)
                )
            previous_signature = signature_tuple
            await self._wait_current_table_sample(page)
        raise _baseline_failed(expected_page_id)

    @staticmethod
    async def _wait_current_table_sample(page: Any) -> None:
        wait = getattr(page, "wait_for_timeout", None)
        if callable(wait):
            await wait(_CURRENT_TABLE_WAIT_MS)
        else:
            await asyncio.sleep(_CURRENT_TABLE_WAIT_MS / 1000)

    @staticmethod
    async def _scroll_current_table_to_bottom(page: Any) -> bool:
        try:
            result = await page.evaluate(
                """
                () => {
                  const visible = element => {
                    if (!(element instanceof HTMLElement)) return false;
                    const style = getComputedStyle(element);
                    return style.display !== 'none' &&
                      style.visibility !== 'hidden' &&
                      element.getClientRects().length > 0;
                  };
                  const rows = Array.from(
                    document.querySelectorAll('tr[role="row"][data-index]')
                  ).filter(visible);
                  const headers = Array.from(
                    document.querySelectorAll('[role="columnheader"]')
                  ).filter(visible);
                  const element = rows.at(-1) || headers[0];
                  if (!element) return false;
                  const containers = [];
                  let node = element;
                  while (node && node.parentElement) {
                    node = node.parentElement;
                    const style = getComputedStyle(node);
                    if (
                      node.scrollHeight > node.clientHeight + 2 &&
                      /(auto|scroll)/.test(style.overflowY || '')
                    ) {
                      containers.push(node);
                    }
                  }
                  for (const container of containers) {
                    container.scrollTop = container.scrollHeight;
                  }
                  window.scrollTo(0, document.documentElement.scrollHeight);
                  const allContainersAtBottom = containers.every(container =>
                    container.scrollTop + container.clientHeight >=
                    container.scrollHeight - 2
                  );
                  const documentAtBottom =
                    window.scrollY + window.innerHeight >=
                    document.documentElement.scrollHeight - 2;
                  return allContainersAtBottom && documentAtBottom;
                }
                """
            )
            return result is True
        except Exception as exc:
            raise _baseline_failed() from exc

    async def _read_full_caption_hash(
        self,
        row: FacebookReelRow,
        *,
        expected_page_id: str,
    ) -> str | None:
        detail = await self.context.new_page()
        self._require_same_context(detail)
        try:
            await detail.goto(row.url, wait_until="domcontentloaded")
            await self._wait_for_verification(detail)
            if _canonical_reel_url(
                _page_url(detail),
                expected_reel_id=row.reel_id,
            ) != row.url:
                raise _baseline_failed(expected_page_id)
            canonical_urls = await self._elements(
                detail.locator('meta[property="og:url"][content]')
            )
            if len(canonical_urls) != 1:
                raise _baseline_failed(expected_page_id)
            canonical_url = _canonical_reel_url(
                await canonical_urls[0].get_attribute("content"),
                expected_reel_id=row.reel_id,
            )
            if canonical_url != row.url:
                raise _baseline_failed(expected_page_id)
            captions = await self._elements(
                detail.locator('meta[property="og:description"][content]')
            )
            if len(captions) != 1:
                return None
            caption = canonical_meta_caption(
                await captions[0].get_attribute("content")
            )
            if not caption:
                return None
            return hashlib.sha256(caption.encode("utf-8")).hexdigest()
        except FacebookPagePublishError:
            raise
        except Exception as exc:
            raise _baseline_failed(expected_page_id) from exc
        finally:
            close = getattr(detail, "close", None)
            if callable(close):
                await close()

    async def _prepare_list_page(self, expected_page_id: str):
        page = await self._get_list_page()
        try:
            await page.goto(_CONTENT_HOME_URL, wait_until="domcontentloaded")
            await self._wait_for_verification(page)
            selected = await activate_saved_facebook_page(page, expected_page_id)
            if _normalized_page_id(selected.page_id) != expected_page_id:
                raise _baseline_failed(expected_page_id)
            self._activated_page_id = expected_page_id

            destination = await self._wait_for_content_entry(
                page,
                expected_page_id,
            )

            await page.goto(destination, wait_until="domcontentloaded")
            await self._wait_for_verification(page)
            await self._wait_for_content_surface(page, expected_page_id)
            await self._recheck_page(page, expected_page_id)
            if not _is_exact_content_url(
                _page_url(page),
                expected_page_id=expected_page_id,
            ):
                raise _baseline_failed(expected_page_id)
            return page
        except FacebookPagePublishError as exc:
            if exc.error_code == "facebook_page_baseline_read_failed":
                raise
            raise _baseline_failed(expected_page_id) from exc
        except Exception as exc:
            raise _baseline_failed(expected_page_id) from exc

    async def _wait_for_content_entry(
        self,
        page: Any,
        expected_page_id: str,
    ) -> str:
        """Wait for the exact Page-bound Content link rendered by Business Suite."""

        for _ in range(_CONTENT_SURFACE_WAIT_ATTEMPTS):
            entry_links: list[Any] = []
            for label in _CONTENT_ENTRY_LABELS:
                entry_links.extend(
                    await self._visible(
                        page.get_by_role("link", name=label, exact=True)
                    )
                )
            if len(entry_links) > 1:
                raise _baseline_failed(expected_page_id)
            if len(entry_links) == 1:
                entry_url = await entry_links[0].get_attribute("href")
                if _is_exact_content_entry_url(
                    entry_url,
                    expected_page_id=expected_page_id,
                ) or _is_exact_content_url(
                    entry_url,
                    expected_page_id=expected_page_id,
                ):
                    return str(entry_url)
                raise _baseline_failed(expected_page_id)
            wait = getattr(page, "wait_for_timeout", None)
            if callable(wait):
                await wait(_CONTENT_SURFACE_WAIT_MS)
            else:
                await asyncio.sleep(_CONTENT_SURFACE_WAIT_MS / 1000)
        raise _baseline_failed(expected_page_id)

    async def _wait_for_content_surface(
        self,
        page: Any,
        expected_page_id: str,
    ) -> None:
        for _ in range(_CONTENT_SURFACE_WAIT_ATTEMPTS):
            if _is_exact_content_url(
                _page_url(page),
                expected_page_id=expected_page_id,
            ):
                if _is_current_content_url(
                    _page_url(page),
                    expected_page_id=expected_page_id,
                ):
                    title_headers = await self._visible(
                        page.get_by_role(
                            "columnheader",
                            name="标题",
                            exact=True,
                        )
                    )
                    if not title_headers:
                        title_headers = await self._visible(
                            page.get_by_role(
                                "columnheader",
                                name="Title",
                                exact=True,
                            )
                        )
                    date_headers = await self._visible(
                        page.get_by_role(
                            "columnheader",
                            name="发布日期",
                            exact=True,
                        )
                    )
                    if not date_headers:
                        date_headers = await self._visible(
                            page.get_by_role(
                                "columnheader",
                                name="Publish date",
                                exact=True,
                            )
                        )
                    if len(title_headers) == 1 and len(date_headers) == 1:
                        return
                elif await self._visible(page.locator("main")):
                    return
            wait = getattr(page, "wait_for_timeout", None)
            if callable(wait):
                await wait(_CONTENT_SURFACE_WAIT_MS)
            else:
                await asyncio.sleep(_CONTENT_SURFACE_WAIT_MS / 1000)
        raise _baseline_failed(expected_page_id)

    async def _get_list_page(self):
        if self._list_page is None:
            self._list_page = await self.context.new_page()
            self._require_same_context(self._list_page)
        return self._list_page

    def _require_same_context(self, page: Any) -> None:
        actual = getattr(page, "context", self.context)
        if callable(actual):
            actual = actual()
        if actual is not self.context:
            raise _baseline_failed()

    async def _recheck_page(self, page: Any, expected_page_id: str) -> None:
        try:
            selected = await validate_facebook_page_binding(
                page,
                {"accountReference": expected_page_id},
            )
            if _normalized_page_id(selected.page_id) != expected_page_id:
                raise _baseline_failed(expected_page_id)
        except FacebookPagePublishError as exc:
            if (
                exc.error_code == "facebook_page_not_found"
                and self._activated_page_id == expected_page_id
                and _is_exact_content_url(
                    _page_url(page),
                    expected_page_id=expected_page_id,
                )
            ):
                return
            if exc.error_code == "facebook_page_baseline_read_failed":
                raise
            raise _baseline_failed(expected_page_id) from exc
        except Exception as exc:
            raise _baseline_failed(expected_page_id) from exc

    async def _bounded_pause(self) -> None:
        page = await self._get_list_page()
        wait = getattr(page, "wait_for_timeout", None)
        if callable(wait):
            await wait(_READBACK_SETTLEMENT_WAIT_MS)
        else:
            await asyncio.sleep(_READBACK_SETTLEMENT_WAIT_MS / 1000)

    @staticmethod
    async def _elements(locator: Any) -> list[Any]:
        return [locator.nth(index) for index in range(int(await locator.count()))]

    @staticmethod
    async def _visible(locator: Any) -> list[Any]:
        visible: list[Any] = []
        count = int(await locator.count())
        for index in range(count):
            element = locator.nth(index)
            try:
                if await element.is_visible():
                    visible.append(element)
            except AttributeError:
                visible.append(element)
        return visible
