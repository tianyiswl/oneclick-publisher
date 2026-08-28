# -*- coding: utf-8 -*-
"""TikTok 发布合同与上传器共用的中立错误类。"""

from __future__ import annotations

from typing import Any, Mapping


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
            if key in _PUBLIC_RECEIPT_KEYS
        }
        self.outcome_ambiguous = bool(outcome_ambiguous)
        super().__init__(self.public_message)
