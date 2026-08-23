# -*- coding: utf-8 -*-
"""评论洞察 AI 的非秘密设置。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from .platform_data_comment_models import CommentInsightFailure


SETTINGS_GROUP = "commentInsightAi"
BASE_URL_KEY = f"{SETTINGS_GROUP}/baseUrl"
MODEL_KEY = f"{SETTINGS_GROUP}/model"

_MAX_BASE_URL_LENGTH = 2048
_MAX_MODEL_LENGTH = 256
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_PROCESS_CONTROL = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


def _not_configured() -> CommentInsightFailure:
    return CommentInsightFailure("comment_ai_not_configured")


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _normalize_base_url(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_BASE_URL_LENGTH
        or _has_control(value)
        or "\\" in value
        or "%" in value
    ):
        raise _not_configured()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise _not_configured() from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None
    ):
        raise _not_configured()
    hostname = parsed.hostname
    if type(hostname) is not str:
        raise _not_configured()
    hostname = hostname.lower()
    if _HOST_RE.fullmatch(hostname) is None:
        raise _not_configured()
    # Reject noncanonical authorities (Unicode, IPv6, uppercase ports, or hidden text).
    if parsed.netloc.lower() != hostname:
        raise _not_configured()
    path = parsed.path or ""
    if path == "/":
        path = ""
    elif path:
        segments = path.split("/")
        if segments[-1] == "":
            segments = segments[:-1]
        if segments[0] or any(
            not segment
            or segment in {".", ".."}
            or _PATH_SEGMENT_RE.fullmatch(segment) is None
            for segment in segments[1:]
        ):
            raise _not_configured()
        path = "/".join(segments)
    normalized = urlunsplit(("https", hostname, path, "", ""))
    if len(normalized) > _MAX_BASE_URL_LENGTH:
        raise _not_configured()
    return normalized


def _normalize_model(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_MODEL_LENGTH
        or _has_control(value)
    ):
        raise _not_configured()
    try:
        value.encode("utf-8")
    except (UnicodeEncodeError, ValueError):
        raise _not_configured() from None
    return value


@dataclass(frozen=True, slots=True)
class CommentAiSettings:
    base_url: str
    model: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _normalize_base_url(self.base_url))
        object.__setattr__(self, "model", _normalize_model(self.model))

    @property
    def normalized_base_url(self) -> str:
        return self.base_url


def load_ai_settings(settings) -> CommentAiSettings | None:
    """从 QSettings 读取基础地址与模型；损坏或不全按未配置处理。"""

    try:
        base_url = settings.value(BASE_URL_KEY, None)
        model = settings.value(MODEL_KEY, None)
        if base_url is None or model is None:
            return None
        return CommentAiSettings(base_url, model)
    except _PROCESS_CONTROL:
        raise
    except BaseException:
        return None


def save_ai_settings(settings, value: CommentAiSettings) -> None:
    """只保存非秘密值；API Key 不属于此入口。"""

    if type(value) is not CommentAiSettings:
        raise _not_configured()
    try:
        settings.setValue(BASE_URL_KEY, value.normalized_base_url)
        settings.setValue(MODEL_KEY, value.model)
        settings.sync()
    except _PROCESS_CONTROL:
        raise
    except BaseException:
        raise _not_configured() from None
