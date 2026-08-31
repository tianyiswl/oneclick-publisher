# -*- coding: utf-8 -*-
"""Instagram V1 的稳定公开错误边界。"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Mapping
from urllib.parse import urlsplit


_PUBLIC_RECEIPT_KEYS = frozenset(
    {
        "phase",
        "accountId",
        "instagramUserId",
        "username",
        "accountType",
        "linkedPageId",
        "linkedPageName",
        "videoSha256",
        "coverSha256",
        "captionSha256",
        "topics",
        "visibility",
        "shareToFeed",
        "scheduleMode",
        "scheduledAt",
        "scheduleTimezone",
        "platformWriteOccurred",
        "finalActionTriggered",
        "finalButtonEnabled",
        "baselineHash",
        "formSnapshotHash",
        "mediaId",
        "url",
        "publishedAt",
        "blocksReplay",
        "errorCode",
        "platformErrorText",
    }
)
_SAFE_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_SAFE_USERNAME = re.compile(r"[a-z0-9._]{1,30}\Z")
_SAFE_CODE = re.compile(r"[a-z0-9_]{1,96}\Z")


def _safe_text(value: object, *, maximum: int = 512) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and "\r" not in value
        and "\n" not in value
    )


def _safe_time(value: object) -> bool:
    if value is None:
        return True
    if type(value) is not str or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _safe_url(value: object) -> bool:
    if value is None:
        return True
    if type(value) is not str or len(value) > 512:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and host in {"instagram.com", "www.instagram.com"}
        and parsed.path.startswith(("/reel/", "/p/"))
    )


def _safe_value(key: str, value: object) -> bool:
    if key == "accountId":
        return type(value) is int and value > 0
    if key in {"instagramUserId", "linkedPageId"}:
        return type(value) is str and value.isascii() and value.isdigit() and bool(value)
    if key == "username":
        return type(value) is str and _SAFE_USERNAME.fullmatch(value) is not None
    if key == "accountType":
        return value in {"business", "creator"}
    if key in {"linkedPageName", "platformErrorText"}:
        if not _safe_text(value):
            return False
        folded = str(value).casefold()
        return not any(
            marker in folded
            for marker in ("cookie=", "access_token", "authorization:")
        )
    if key in {
        "videoSha256",
        "coverSha256",
        "captionSha256",
        "baselineHash",
        "formSnapshotHash",
    }:
        return type(value) is str and _SAFE_HASH.fullmatch(value) is not None
    if key == "topics":
        return isinstance(value, list) and all(
            type(item) is str
            and 0 < len(item) <= 100
            and all(char.isalnum() or char == "_" for char in item)
            for item in value
        )
    if key == "visibility":
        return value == "public"
    if key == "scheduleMode":
        return value in {"immediate", "platform_native"}
    if key == "scheduleTimezone":
        return type(value) is str and len(value) <= 64 and "\n" not in value
    if key in {"scheduledAt", "publishedAt"}:
        return _safe_time(value)
    if key in {
        "shareToFeed",
        "platformWriteOccurred",
        "finalActionTriggered",
        "finalButtonEnabled",
        "blocksReplay",
    }:
        return type(value) is bool
    if key == "mediaId":
        return value is None or (
            type(value) is str and _SAFE_ID.fullmatch(value) is not None
        )
    if key == "url":
        return _safe_url(value)
    if key in {"phase", "errorCode"}:
        return type(value) is str and _SAFE_CODE.fullmatch(value) is not None
    return False


def project_instagram_receipt(receipt: object) -> dict[str, object]:
    if not isinstance(receipt, Mapping):
        raise ValueError("Instagram 回执必须是映射")
    projected: dict[str, object] = {}
    for key, value in receipt.items():
        if key not in _PUBLIC_RECEIPT_KEYS:
            continue
        if not _safe_value(key, value):
            raise ValueError(f"Instagram 回执字段无效：{key}")
        projected[key] = value
    return projected


class InstagramPublishError(RuntimeError):
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
        self.receipt = project_instagram_receipt(receipt or {})
        self.outcome_ambiguous = bool(outcome_ambiguous)
        super().__init__(self.public_message)
