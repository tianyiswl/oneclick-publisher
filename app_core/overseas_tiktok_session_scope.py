# -*- coding: utf-8 -*-
"""Pure TikTok-only filtering for browser storage state."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit



class TikTokSessionScopeError(RuntimeError):
    """A storage-state boundary violation with a stable public error code."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(self.public_message)


def is_tiktok_cookie_domain(value: object) -> bool:
    """Return whether *value* is TikTok's registrable domain or a subdomain."""

    host = str(value or "").strip().lower().lstrip(".").rstrip(".")
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def is_tiktok_https_origin(value: object) -> bool:
    """Return whether *value* is a credential-free TikTok HTTPS origin."""

    try:
        parsed = urlsplit(str(value or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in {"", "/"}
        and port in {None, 443}
        and (host == "tiktok.com" or host.endswith(".tiktok.com"))
    )


def _invalid(message: str) -> TikTokSessionScopeError:
    return TikTokSessionScopeError("tiktok_session_scope_invalid", message)


def sanitize_tiktok_storage_state(
    raw_state: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Keep only TikTok cookies and origins without mutating the input.

    Foreign entries are discarded.  The retained-cookie count is only a
    TikTok-state-presence signal, not authentication proof: callers must still
    prove one public handle in two independent blank contexts.  Shape
    violations use a fixed public message; no cookie or localStorage value is
    ever included in an error.
    """

    if not isinstance(raw_state, Mapping) or set(raw_state) != {"cookies", "origins"}:
        raise _invalid("TikTok 登录会话结构无效")

    raw_cookies = raw_state["cookies"]
    raw_origins = raw_state["origins"]
    if not isinstance(raw_cookies, list) or not isinstance(raw_origins, list):
        raise _invalid("TikTok 登录会话结构无效")

    kept_cookies: list[dict[str, Any]] = []
    for cookie in raw_cookies:
        if not isinstance(cookie, Mapping):
            raise _invalid("TikTok 登录会话结构无效")
        if "domain" not in cookie or not isinstance(cookie["domain"], str):
            raise _invalid("TikTok 登录会话结构无效")
        if is_tiktok_cookie_domain(cookie["domain"]):
            kept_cookies.append(copy.deepcopy(dict(cookie)))

    kept_origins: list[dict[str, Any]] = []
    for origin in raw_origins:
        if not isinstance(origin, Mapping):
            raise _invalid("TikTok 登录会话结构无效")
        if "origin" not in origin or not isinstance(origin["origin"], str):
            raise _invalid("TikTok 登录会话结构无效")
        if is_tiktok_https_origin(origin["origin"]):
            kept_origins.append(copy.deepcopy(dict(origin)))

    if not kept_cookies:
        raise TikTokSessionScopeError(
            "tiktok_session_missing", "TikTok 本地登录会话不存在"
        )
    return {"cookies": kept_cookies, "origins": kept_origins}
