# -*- coding: utf-8 -*-
"""One canonical Facebook Page caption and its byte-level fingerprint."""

from __future__ import annotations

import hashlib
from typing import Iterable

from .overseas_meta_errors import FacebookPagePublishError


def _content_invalid() -> FacebookPagePublishError:
    return FacebookPagePublishError(
        "facebook_page_content_invalid", "Facebook Page 文案无效，已停止操作。"
    )


def build_facebook_page_caption(*, title: str, body: str, topics: Iterable[str]) -> str:
    """Compose title, body, and structured topics exactly once for all phases."""

    if type(title) is not str or not title or type(body) is not str or not body:
        raise _content_invalid()
    if type(topics) is str:
        raise _content_invalid()
    try:
        source_topics = tuple(topics)
    except TypeError as exc:
        raise _content_invalid() from exc

    normalized_topics: list[str] = []
    seen: set[str] = set()
    for topic in source_topics:
        if type(topic) is not str or "\r" in topic or "\n" in topic:
            raise _content_invalid()
        normalized = topic.strip()
        if normalized.startswith("#"):
            normalized = normalized[1:].strip()
        if not normalized or normalized in seen:
            if not normalized:
                raise _content_invalid()
            continue
        seen.add(normalized)
        normalized_topics.append(normalized)

    parts = [title, body]
    if normalized_topics:
        parts.append(" ".join(f"#{topic}" for topic in normalized_topics))
    return "\n\n".join(parts)


def facebook_page_caption_sha256(caption: str) -> str:
    if type(caption) is not str:
        raise _content_invalid()
    return hashlib.sha256(caption.encode("utf-8")).hexdigest()
