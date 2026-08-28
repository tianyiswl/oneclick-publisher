# -*- coding: utf-8 -*-
"""TikTok 发布合同与上传器共用的中立错误类。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlsplit


_PUBLIC_RECEIPT_KEYS = frozenset(
    {
        "accountId",
        "visibility",
        "mode",
        "phase",
        "platformWriteOccurred",
        "finalActionTriggered",
        "contentId",
        "contentUrl",
        "publishedAt",
    }
)
_PUBLIC_MODES = frozenset({"preflight", "platform_form_check", "formal"})
_PUBLIC_PHASES = frozenset(
    {
        "local_preflight_passed",
        "platform_form_verified",
        "failed_before_final_action",
        "final_action_triggered",
        "platform_accepted",
        "published_readback_confirmed",
        "ambiguous",
    }
)
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_TIKTOK_VIDEO_PATH = re.compile(
    r"/@[A-Za-z0-9._-]{1,64}/video/[0-9]{1,32}/?\Z"
)
_TIKTOK_SHORT_PATH = re.compile(r"/[A-Za-z0-9]{1,64}/?\Z")
_SAFE_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


def _safe_tiktok_url(value: object) -> bool:
    if type(value) is not str or len(value) > 512:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not (host == "tiktok.com" or host.endswith(".tiktok.com"))
    ):
        return False
    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        return _TIKTOK_SHORT_PATH.fullmatch(parsed.path) is not None
    return _TIKTOK_VIDEO_PATH.fullmatch(parsed.path) is not None


def _safe_timestamp(value: object) -> bool:
    if type(value) is not str or _SAFE_TIME.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _safe_receipt_value(key: str, value: object) -> bool:
    if key == "accountId":
        return type(value) is int and value > 0
    if key == "visibility":
        return type(value) is str and value == "public"
    if key == "mode":
        return type(value) is str and value in _PUBLIC_MODES
    if key == "phase":
        return type(value) is str and value in _PUBLIC_PHASES
    if key in {"platformWriteOccurred", "finalActionTriggered"}:
        return type(value) is bool
    if key == "contentId":
        return value is None or (
            type(value) is str and _SAFE_IDENTIFIER.fullmatch(value) is not None
        )
    if key == "contentUrl":
        return value is None or _safe_tiktok_url(value)
    if key == "publishedAt":
        return value is None or _safe_timestamp(value)
    return False


class TikTokPublishError(RuntimeError):
    """TikTok 受控发布在稳定边界内无法继续。"""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        receipt: Mapping[str, Any] | None = None,
        outcome_ambiguous: bool = False,
    ) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        self.receipt = {
            key: value
            for key, value in dict(receipt or {}).items()
            if key in _PUBLIC_RECEIPT_KEYS and _safe_receipt_value(key, value)
        }
        self.outcome_ambiguous = bool(outcome_ambiguous)
        super().__init__(self.public_message)
