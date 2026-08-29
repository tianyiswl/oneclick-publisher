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


def canonical_facebook_page_caption(value: object) -> str:
    """Normalize only Facebook editor whitespace, preserving content order."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def build_facebook_page_caption(*, title: str, body: str, topics: Iterable[str]) -> str:
    """Compose title, body, and structured topics exactly once for all phases."""

    if type(title) is not str or type(body) is not str:
        raise _content_invalid()
    canonical_title = canonical_facebook_page_caption(title)
    canonical_body = canonical_facebook_page_caption(body)
    if not canonical_title or not canonical_body:
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
        normalized = canonical_facebook_page_caption(topic)
        if normalized.startswith("#"):
            normalized = canonical_facebook_page_caption(normalized[1:])
        if not normalized or normalized in seen:
            if not normalized:
                raise _content_invalid()
            continue
        seen.add(normalized)
        normalized_topics.append(normalized)

    parts = [canonical_title, canonical_body]
    if normalized_topics:
        parts.append(" ".join(f"#{topic}" for topic in normalized_topics))
    return canonical_facebook_page_caption("\n\n".join(parts))


def facebook_page_caption_sha256(caption: str) -> str:
    if type(caption) is not str:
        raise _content_invalid()
    canonical = canonical_facebook_page_caption(caption)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
