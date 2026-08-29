# -*- coding: utf-8 -*-
"""Stable public errors and non-sensitive Facebook Page receipts."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Mapping
from urllib.parse import urlsplit


_PUBLIC_RECEIPT_KEYS = frozenset(
    {
        "accountId",
        "pageId",
        "pageName",
        "videoName",
        "videoSize",
        "videoSha256",
        "captionSha256",
        "visibility",
        "phase",
        "platformWriteOccurred",
        "finalActionTriggered",
        "finalButtonEnabled",
        "reelId",
        "url",
        "publishedAt",
        "baselineHash",
        "formSnapshotHash",
    }
)
_SAFE_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_SAFE_PHASE = re.compile(r"[a-z0-9_]{1,64}\Z")


def _safe_page_id(value: object) -> bool:
    return type(value) is str and value.isascii() and value.isdigit() and bool(value)


def _safe_text(value: object, *, maximum_length: int = 512) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum_length
        and "\r" not in value
        and "\n" not in value
    )


def _safe_facebook_url(value: object) -> bool:
    if type(value) is not str or len(value) > 512:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    return (
        parsed.scheme in {"http", "https"}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and (host == "facebook.com" or host.endswith(".facebook.com"))
        and bool(parsed.path)
    )


def _safe_published_at(value: object) -> bool:
    if type(value) is not str or len(value) > 64:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _safe_receipt_value(key: str, value: object) -> bool:
    if key == "accountId":
        return type(value) is int and value > 0
    if key == "pageId":
        return _safe_page_id(value)
    if key in {"pageName", "videoName"}:
        return _safe_text(value)
    if key == "videoSize":
        return type(value) is int and value >= 0
    if key in {"videoSha256", "captionSha256", "baselineHash", "formSnapshotHash"}:
        return type(value) is str and _SAFE_HASH.fullmatch(value) is not None
    if key == "visibility":
        return type(value) is str and value == "public"
    if key == "phase":
        return type(value) is str and _SAFE_PHASE.fullmatch(value) is not None
    if key in {
        "platformWriteOccurred",
        "finalActionTriggered",
        "finalButtonEnabled",
    }:
        return type(value) is bool
    if key == "reelId":
        return value is None or (
            type(value) is str and _SAFE_IDENTIFIER.fullmatch(value) is not None
        )
    if key == "url":
        return value is None or _safe_facebook_url(value)
    if key == "publishedAt":
        return value is None or _safe_published_at(value)
    return False


def project_facebook_page_receipt(receipt: object) -> dict[str, object]:
    """Return the deliberately small receipt safe for task/UI exposure.

    Unknown fields are ignored.  A caller cannot hide malformed data in a
    known field: known values must be scalar and satisfy their public shape.
    """

    if not isinstance(receipt, Mapping):
        raise ValueError("Facebook Page 回执必须是映射")
    projected: dict[str, object] = {}
    for key, value in receipt.items():
        if key not in _PUBLIC_RECEIPT_KEYS:
            continue
        if not _safe_receipt_value(key, value):
            raise ValueError(f"Facebook Page 回执字段无效：{key}")
        projected[key] = value
    return projected


class FacebookPagePublishError(RuntimeError):
    """Facebook Page controlled publishing cannot safely continue."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        receipt: Mapping[str, object] | None = None,
        outcome_ambiguous: bool = False,
    ) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        self.receipt = project_facebook_page_receipt(receipt or {})
        self.outcome_ambiguous = bool(outcome_ambiguous)
        super().__init__(self.public_message)
