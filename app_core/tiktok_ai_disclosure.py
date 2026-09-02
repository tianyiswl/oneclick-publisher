# -*- coding: utf-8 -*-
"""Shared TikTok caption disclosure contract.

The current TikTok Studio web form does not expose a stable AI-label control
for every account.  A manifest may therefore authorize a truthful, explicit
caption disclosure without pretending that a platform toggle was selected.
"""

from __future__ import annotations


TIKTOK_AI_DISCLOSURE_MODE_CAPTION = "caption"
TIKTOK_AI_CAPTION_DISCLOSURE = "AI-generated content."


def append_tiktok_ai_caption_disclosure(body: object) -> str:
    """Append the canonical disclosure exactly once as the body's final line."""

    normalized = str(body or "").rstrip()
    lines = normalized.splitlines()
    if lines and lines[-1].strip() == TIKTOK_AI_CAPTION_DISCLOSURE:
        return normalized
    if not normalized:
        return TIKTOK_AI_CAPTION_DISCLOSURE
    return f"{normalized}\n\n{TIKTOK_AI_CAPTION_DISCLOSURE}"


def has_canonical_tiktok_ai_caption_disclosure(body: object) -> bool:
    """Return true only when the exact disclosure is the body's final line."""

    lines = str(body or "").rstrip().splitlines()
    return bool(lines and lines[-1].strip() == TIKTOK_AI_CAPTION_DISCLOSURE)
