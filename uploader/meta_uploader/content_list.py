# -*- coding: utf-8 -*-
"""Read-only Facebook Page content-list baseline and unique Reel matching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Literal
from urllib.parse import urlsplit

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
_CONTENT_LIST_URL = "https://business.facebook.com/latest/content?asset_id={}"
_MAX_PAGES = 50
_DEFAULT_SETTLEMENT_ATTEMPTS = 4


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


@dataclass(frozen=True, slots=True, init=False)
class FacebookPlatformDecision:
    """Trusted decision value; instances are created only by the DOM parser."""

    kind: Literal["accepted", "rejected_no_creation", "unknown"]
    page_id: str
    observed_at: str
    evidence_sha256: str


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
    parsed = urlsplit(value.strip())
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
            and published >= clicked
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
    ) -> None:
        if context is None or not callable(getattr(context, "new_page", None)):
            raise TypeError("context must be a browser context")
        if not callable(wait_for_verification):
            raise TypeError("wait_for_verification must be callable")
        self.context = context
        self._wait_for_verification = wait_for_verification
        self._list_page: Any | None = None

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
                if exc.error_code == "facebook_page_baseline_read_failed":
                    raise
                raise _baseline_failed(expected) from exc
            except Exception as exc:
                raise _baseline_failed(expected) from exc
            if match.status != "none" or attempt + 1 == _DEFAULT_SETTLEMENT_ATTEMPTS:
                return match
            await self._bounded_pause()
        return FacebookReelMatch("none", None, 0, 0)

    async def read_platform_decision(
        self,
        expected_page_id: str,
    ) -> FacebookPlatformDecision:
        """Parse one explicit platform decision from the current Page DOM."""

        expected = _normalized_page_id(expected_page_id)
        page = await self._prepare_list_page(expected)
        try:
            decisions = await self._visible(
                page.locator("[data-meta-platform-decision]")
            )
            if len(decisions) != 1:
                raise _baseline_failed(expected)
            element = decisions[0]
            page_id = _normalized_page_id(
                await element.get_attribute("data-page-id")
            )
            kind = str(
                await element.get_attribute("data-meta-platform-decision") or ""
            ).strip()
            observed_at = _canonical_timestamp(
                await element.get_attribute("data-observed-at")
            )
            if page_id != expected or kind not in {
                "accepted",
                "rejected_no_creation",
                "unknown",
            }:
                raise _baseline_failed(expected)
            evidence_text = canonical_meta_caption(await element.inner_text())
            evidence_hash = _hash_json(
                {
                    "pageId": page_id,
                    "kind": kind,
                    "observedAt": observed_at,
                    "evidence": evidence_text,
                }
            )
            decision = object.__new__(FacebookPlatformDecision)
            object.__setattr__(decision, "kind", kind)
            object.__setattr__(decision, "page_id", page_id)
            object.__setattr__(decision, "observed_at", observed_at)
            object.__setattr__(decision, "evidence_sha256", evidence_hash)
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
        rows: list[FacebookReelRow] = []
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            await self._recheck_page(page, expected_page_id)
            roots = await self._visible(
                page.locator("[data-meta-page-content-list]")
            )
            if len(roots) != 1:
                raise _baseline_failed(expected_page_id)
            root_page_id = _normalized_page_id(
                await roots[0].get_attribute("data-page-id")
            )
            if root_page_id != expected_page_id:
                raise _baseline_failed(expected_page_id)

            row_elements = await self._visible(page.locator("[data-meta-reel-row]"))
            for element in row_elements:
                try:
                    page_id = _normalized_page_id(
                        await element.get_attribute("data-page-id")
                    )
                    reel_id = _normalized_reel_id(
                        await element.get_attribute("data-reel-id")
                    )
                    url = _canonical_reel_url(
                        await element.get_attribute("data-reel-url"),
                        expected_reel_id=reel_id,
                    )
                    published_at = _canonical_timestamp(
                        await element.get_attribute("data-published-at")
                    )
                except (FacebookPagePublishError, TypeError, ValueError) as exc:
                    raise _baseline_failed(expected_page_id) from exc
                if page_id != expected_page_id or reel_id in seen:
                    raise _baseline_failed(expected_page_id)
                seen.add(reel_id)
                rows.append(
                    FacebookReelRow(
                        page_id=page_id,
                        reel_id=reel_id,
                        url=url,
                        caption_sha256="",
                        published_at=published_at,
                    )
                )

            terminal = await self._visible(
                page.locator('[data-meta-pagination-complete="true"]')
            )
            if len(terminal) > 1:
                raise _baseline_failed(expected_page_id)
            if len(terminal) == 1:
                empty = await self._visible(
                    page.locator('[data-meta-content-empty="true"]')
                )
                if not rows and len(empty) != 1:
                    raise _baseline_failed(expected_page_id)
                if rows and empty:
                    raise _baseline_failed(expected_page_id)
                return tuple(sorted(rows, key=lambda item: item.reel_id))

            next_buttons = await self._visible(
                page.locator("[data-meta-pagination-next]")
            )
            if len(next_buttons) != 1 or not await next_buttons[0].is_enabled():
                raise _baseline_failed(expected_page_id)
            await next_buttons[0].click()
            await self._wait_for_verification(page)
            await self._recheck_page(page, expected_page_id)
        raise _baseline_failed(expected_page_id)

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
            await self._recheck_page(detail, expected_page_id)
            roots = await self._visible(detail.locator("[data-meta-reel-detail]"))
            if len(roots) != 1:
                raise _baseline_failed(expected_page_id)
            actual_page_id = _normalized_page_id(
                await roots[0].get_attribute("data-page-id")
            )
            actual_reel_id = _normalized_reel_id(
                await roots[0].get_attribute("data-reel-id")
            )
            if (
                actual_page_id != expected_page_id
                or actual_reel_id != row.reel_id
            ):
                raise _baseline_failed(expected_page_id)
            captions = await self._visible(
                detail.locator(
                    '[data-meta-reel-detail-caption][data-caption-complete="true"]'
                )
            )
            if len(captions) != 1:
                return None
            caption = canonical_meta_caption(await captions[0].inner_text())
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
            await page.goto(
                _CONTENT_LIST_URL.format(expected_page_id),
                wait_until="domcontentloaded",
            )
            await self._wait_for_verification(page)
            await self._recheck_page(page, expected_page_id)
            return page
        except FacebookPagePublishError as exc:
            if exc.error_code == "facebook_page_baseline_read_failed":
                raise
            raise _baseline_failed(expected_page_id) from exc
        except Exception as exc:
            raise _baseline_failed(expected_page_id) from exc

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
            if exc.error_code == "facebook_page_baseline_read_failed":
                raise
            raise _baseline_failed(expected_page_id) from exc
        except Exception as exc:
            raise _baseline_failed(expected_page_id) from exc

    async def _bounded_pause(self) -> None:
        page = await self._get_list_page()
        wait = getattr(page, "wait_for_timeout", None)
        if callable(wait):
            await wait(250)
        else:
            await asyncio.sleep(0.25)

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
