# -*- coding: utf-8 -*-
"""抖音匿名评论、分页批次和评论洞察的严格公开模型。

模型只接受已经由平台合同校验过的 Python 内建标量。作者对象和平台评论
ID 不属于任何公开模型；平台 ID 只能通过 :func:`derive_comment_key` 在内存
中转换为不可逆的本地键。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import re

from .platform_data_collection_errors import CleanupReceipt


ALLOWED_COMMENT_LABELS = frozenset(
    {"质疑", "认同", "真实经历", "追问", "选题建议", "其他"}
)
ALLOWED_COMMENT_WARNING_CODES = frozenset({"", "comment_limit_reached"})
ALLOWED_COMMENT_ERROR_CODES = frozenset(
    {
        "comment_content_unavailable",
        "comment_login_required",
        "comment_verification_required",
        "comment_access_denied",
        "comment_payload_invalid",
        "comment_sync_timeout",
        "comment_sync_cancelled",
        "comment_ai_not_configured",
        "comment_ai_timeout",
        "comment_ai_service_unavailable",
        "comment_ai_response_invalid",
        "comment_ai_evidence_invalid",
    }
)
ALLOWED_COMMENT_SOURCE_MODES = frozenset({"direct_session", "browser_signed"})
ALLOWED_COMMENT_STOP_REASONS = frozenset(
    {"", "known_comment", "limit_reached", "platform_end"}
)
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class CommentInsightFailure(RuntimeError):
    """评论合同和洞察层只向上层公开固定错误码。"""

    def __init__(self, error_code: str, *, retryable: bool = False) -> None:
        code = (
            error_code
            if type(error_code) is str and error_code in ALLOWED_COMMENT_ERROR_CODES
            else "comment_payload_invalid"
        )
        self.error_code = code
        self.retryable = bool(retryable)
        super().__init__(code)


def _failure() -> None:
    raise CommentInsightFailure("comment_payload_invalid")


def _text(value: object, *, preserve: bool = False) -> str:
    if type(value) is not str or not value or not value.strip():
        _failure()
    if not preserve and value != value.strip():
        _failure()
    return value


def _timestamp(value: object) -> str:
    result = _text(value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        _failure()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _failure()
    return result


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        _failure()
    return value


def _key(value: object) -> str:
    if type(value) is not str or _KEY_RE.fullmatch(value) is None:
        _failure()
    return value


def derive_comment_key(account_id: int, content_id: str, platform_comment_id: str) -> str:
    """由账号、作品和平台评论 ID 生成确定性 SHA-256 匿名键。"""

    if type(account_id) is not int or account_id <= 0:
        _failure()
    if type(content_id) is not str or not content_id.strip():
        _failure()
    if type(platform_comment_id) is not str or not platform_comment_id.strip():
        _failure()
    source = f"{account_id}\x1f{content_id}\x1f{platform_comment_id}".encode(
        "utf-8"
    )
    return hashlib.sha256(source).hexdigest()


@dataclass(frozen=True, slots=True)
class CommentRecord:
    content_id: str
    comment_key: str
    body: str
    like_count: int
    reply_count: int
    commented_at: str
    observed_at: str

    def __post_init__(self) -> None:
        _text(self.content_id)
        _key(self.comment_key)
        _text(self.body, preserve=True)
        _count(self.like_count)
        _count(self.reply_count)
        _timestamp(self.commented_at)
        _timestamp(self.observed_at)


@dataclass(frozen=True, slots=True)
class CommentPage:
    comments: tuple[CommentRecord, ...]
    has_more: bool
    next_cursor: str | None = ""
    platform_end: bool = False

    def __post_init__(self) -> None:
        if type(self.comments) is not tuple:
            _failure()
        if not all(type(item) is CommentRecord for item in self.comments):
            _failure()
        keys = tuple(item.comment_key for item in self.comments)
        if len(set(keys)) != len(keys):
            _failure()
        if type(self.has_more) is not bool or type(self.platform_end) is not bool:
            _failure()
        if self.next_cursor is not None and type(self.next_cursor) is not str:
            _failure()
        if self.next_cursor is not None and self.next_cursor != self.next_cursor.strip():
            _failure()
        if self.has_more and not self.next_cursor:
            _failure()
        if self.platform_end and self.has_more:
            _failure()


@dataclass(frozen=True, slots=True)
class CommentCollectionBatch:
    platform_type: int
    source_mode: str
    content_id: str
    comments: tuple[CommentRecord, ...]
    accepted_count: int
    rejected_count: int
    page_count: int
    stop_reason: str
    warning_code: str
    platform_observed_at: str
    cleanup_receipt: CleanupReceipt | None

    def __post_init__(self) -> None:
        if type(self.platform_type) is not int or self.platform_type != 3:
            _failure()
        source_mode = _text(self.source_mode)
        if source_mode not in ALLOWED_COMMENT_SOURCE_MODES:
            _failure()
        _text(self.content_id)
        if type(self.comments) is not tuple:
            _failure()
        if not all(type(item) is CommentRecord for item in self.comments):
            _failure()
        keys = tuple(item.comment_key for item in self.comments)
        if len(set(keys)) != len(keys):
            _failure()
        if type(self.accepted_count) is not int or self.accepted_count < 0:
            _failure()
        if self.accepted_count != len(self.comments) or self.accepted_count > 100:
            _failure()
        _count(self.rejected_count)
        _count(self.page_count)
        if type(self.stop_reason) is not str:
            _failure()
        stop_reason = self.stop_reason
        if stop_reason not in ALLOWED_COMMENT_STOP_REASONS:
            _failure()
        if type(self.warning_code) is not str or self.warning_code not in ALLOWED_COMMENT_WARNING_CODES:
            _failure()
        _timestamp(self.platform_observed_at)
        if self.cleanup_receipt is not None and type(self.cleanup_receipt) is not CleanupReceipt:
            _failure()
        if self.warning_code == "comment_limit_reached":
            if self.accepted_count != 100 or stop_reason != "limit_reached":
                _failure()
        if stop_reason == "limit_reached" and self.accepted_count != 100:
            _failure()
        if stop_reason == "platform_end" and self.warning_code:
            _failure()
        if any(item.content_id != self.content_id for item in self.comments):
            _failure()


@dataclass(frozen=True, slots=True)
class CommentClassification:
    comment_key: str
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        _key(self.comment_key)
        if type(self.labels) is not tuple or not self.labels:
            _failure()
        if not all(type(label) is str and label in ALLOWED_COMMENT_LABELS for label in self.labels):
            _failure()
        if len(set(self.labels)) != len(self.labels):
            _failure()


@dataclass(frozen=True, slots=True)
class TopicCandidate:
    title: str
    reason: str
    evidence_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.title)
        _text(self.reason)
        if type(self.evidence_keys) is not tuple or not self.evidence_keys:
            _failure()
        if not all(_KEY_RE.fullmatch(key or "") is not None for key in self.evidence_keys):
            _failure()
        if len(set(self.evidence_keys)) != len(self.evidence_keys):
            _failure()


@dataclass(frozen=True, slots=True)
class InsightResult:
    classifications: tuple[CommentClassification, ...]
    candidates: tuple[TopicCandidate, ...]
    known_comment_keys: frozenset[str]

    def __post_init__(self) -> None:
        if type(self.classifications) is not tuple or type(self.candidates) is not tuple:
            _failure()
        if not all(type(item) is CommentClassification for item in self.classifications):
            _failure()
        if not all(type(item) is TopicCandidate for item in self.candidates):
            _failure()
        if len(self.candidates) > 5:
            raise CommentInsightFailure("comment_ai_response_invalid")
        if type(self.known_comment_keys) is not frozenset:
            _failure()
        if not all(type(key) is str and _KEY_RE.fullmatch(key) is not None for key in self.known_comment_keys):
            _failure()
        classification_keys = tuple(item.comment_key for item in self.classifications)
        if len(set(classification_keys)) != len(classification_keys):
            raise CommentInsightFailure("comment_ai_evidence_invalid")
        if any(key not in self.known_comment_keys for key in classification_keys):
            raise CommentInsightFailure("comment_ai_evidence_invalid")
        for candidate in self.candidates:
            if any(key not in self.known_comment_keys for key in candidate.evidence_keys):
                raise CommentInsightFailure("comment_ai_evidence_invalid")
