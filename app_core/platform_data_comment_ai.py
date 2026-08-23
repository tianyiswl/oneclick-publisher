# -*- coding: utf-8 -*-
"""OpenAI 兼容评论洞察适配器，仅输出经证据校验的本地模型。"""

from __future__ import annotations

import asyncio
import json
import math
import re

import requests

from .platform_data_comment_models import (
    ALLOWED_COMMENT_LABELS,
    CommentClassification,
    CommentInsightFailure,
    CommentRecord,
    InsightResult,
    TopicCandidate,
)
from .platform_data_comment_settings import CommentAiSettings


PROMPT_VERSION = "comment-insight-v1"
SCHEMA_VERSION = 1

_MAX_TITLE_CHARACTERS = 500
_MAX_BODY_BYTES = 16_384
_MAX_REQUEST_BYTES = 262_144
_MAX_RESPONSE_BYTES = 262_144
_MAX_CANDIDATE_TITLE_CHARACTERS = 200
_MAX_CANDIDATE_REASON_CHARACTERS = 1000
_MAX_SECRET_BYTES = 8192
_MAX_COMMENTS = 100
_PROCESS_CONTROL = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
_PUBLIC_AI_ERRORS = frozenset(
    {
        "comment_ai_not_configured",
        "comment_ai_timeout",
        "comment_ai_service_unavailable",
        "comment_ai_response_invalid",
        "comment_ai_evidence_invalid",
    }
)
_REF_RE = re.compile(r"^C(?:00[1-9]|0[1-9][0-9]|100)$")
_CONTENT_TYPE_RE = re.compile(
    r"^application/(?:json|[a-z0-9!#$&^_.+-]+\+json)"
    r"(?:\s*;\s*charset\s*=\s*(?:utf-8|\"utf-8\"))?$",
    re.IGNORECASE,
)

_OUTER_KEYS = frozenset(
    {
        "id",
        "object",
        "created",
        "model",
        "choices",
        "usage",
        "system_fingerprint",
        "service_tier",
    }
)
_CHOICE_KEYS = frozenset({"index", "message", "finish_reason", "logprobs"})
_MESSAGE_KEYS = frozenset({"role", "content", "refusal"})
_CONTRACT_KEYS = frozenset({"classifications", "candidates"})
_CLASSIFICATION_KEYS = frozenset({"ref", "labels"})
_CANDIDATE_KEYS = frozenset({"title", "reason", "evidenceRefs"})


def _failure(error_code: str) -> CommentInsightFailure:
    return CommentInsightFailure(error_code)


def _response_invalid() -> CommentInsightFailure:
    return _failure("comment_ai_response_invalid")


def _evidence_invalid() -> CommentInsightFailure:
    return _failure("comment_ai_evidence_invalid")


def _plain_text(
    value: object,
    *,
    maximum: int,
    allow_newlines: bool = False,
) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
    ):
        raise _response_invalid()
    if not allow_newlines and any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise _response_invalid()
    return value


def _exact_dict(value: object, keys: frozenset[str]) -> dict:
    if type(value) is not dict or frozenset(value.keys()) != keys:
        raise _response_invalid()
    return value


def _safe_close(session) -> None:
    if session is None:
        return
    try:
        close = getattr(session, "close", None)
        if callable(close):
            close()
    except _PROCESS_CONTROL:
        raise
    except BaseException:
        return


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("invalid JSON object")
        result[key] = value
    return result


def _loads_json(value: str | bytes):
    parsed = json.loads(
        value,
        object_pairs_hook=_json_pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("invalid JSON number")
        ),
    )
    _validate_json_tree(parsed)
    return parsed


def _validate_json_tree(root) -> None:
    stack = [(root, 0)]
    node_count = 0
    while stack:
        value, depth = stack.pop()
        node_count += 1
        if node_count > 5000 or depth > 12:
            raise ValueError("JSON bounds exceeded")
        if value is None or type(value) is bool:
            continue
        if type(value) is int:
            if abs(value) > 9_223_372_036_854_775_807:
                raise ValueError("JSON integer out of range")
            continue
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("JSON number out of range")
            continue
        if type(value) is str:
            if len(value.encode("utf-8")) > _MAX_RESPONSE_BYTES:
                raise ValueError("JSON string too large")
            continue
        if type(value) is list:
            if len(value) > 1000:
                raise ValueError("JSON array too large")
            stack.extend((item, depth + 1) for item in value)
            continue
        if type(value) is dict:
            if len(value) > 1000:
                raise ValueError("JSON object too large")
            for key, item in value.items():
                if type(key) is not str or not key or len(key) > 128:
                    raise ValueError("invalid JSON key")
                stack.append((item, depth + 1))
            continue
        raise ValueError("non-built-in JSON value")


class OpenAiCompatibleCommentProvider:
    """发起一次受控请求，并在完整校验后才恢复本地评论键。"""

    prompt_version = PROMPT_VERSION
    schema_version = SCHEMA_VERSION

    def __init__(
        self,
        *,
        settings: CommentAiSettings,
        secret: str,
        session_factory=requests.Session,
    ) -> None:
        if type(settings) is not CommentAiSettings or not callable(session_factory):
            raise _failure("comment_ai_not_configured")
        if (
            type(secret) is not str
            or not secret
            or not secret.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in secret)
        ):
            raise _failure("comment_ai_not_configured")
        try:
            secret_buffer = bytearray(secret, "utf-8")
        except _PROCESS_CONTROL:
            raise
        except BaseException:
            raise _failure("comment_ai_not_configured") from None
        if not secret_buffer or len(secret_buffer) > _MAX_SECRET_BYTES:
            for index in range(len(secret_buffer)):
                secret_buffer[index] = 0
            raise _failure("comment_ai_not_configured")
        self._settings = settings
        self._secret = secret_buffer
        self._session_factory = session_factory
        self.model_name = settings.model

    def analyze(
        self, title: str, comments: tuple[CommentRecord, ...]
    ) -> InsightResult:
        session = None
        headers = None
        payload = None
        response = None
        raw_response = None
        outer = None
        contract = None
        secret_text = None
        try:
            refs, ref_to_key, request_comments = self._validate_input(title, comments)
            payload = self._request_payload(title, request_comments)
            encoded_payload = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded_payload) > _MAX_REQUEST_BYTES:
                raise _response_invalid()
            if not any(self._secret):
                raise _failure("comment_ai_not_configured")
            try:
                secret_text = self._secret.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                raise _failure("comment_ai_not_configured") from None
            headers = {
                "Authorization": f"Bearer {secret_text}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            session = self._session_factory()
            response = session.post(
                f"{self._settings.normalized_base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=(10, 45),
                stream=True,
            )
            raw_response = self._validated_response_bytes(response)
            try:
                outer = _loads_json(raw_response.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                raise _response_invalid() from None
            contract = self._extract_contract(outer)
            validated = self._validate_contract(contract, refs)
            _safe_close(response)
            response = raw_response = outer = contract = None
            # No local comment key is restored before every response item has passed.
            classifications = tuple(
                CommentClassification(ref_to_key[ref], labels)
                for ref, labels in validated[0]
            )
            candidates = tuple(
                TopicCandidate(
                    candidate_title,
                    reason,
                    tuple(ref_to_key[ref] for ref in evidence_refs),
                )
                for candidate_title, reason, evidence_refs in validated[1]
            )
            return InsightResult(
                classifications=classifications,
                candidates=candidates,
                known_comment_keys=frozenset(ref_to_key.values()),
            )
        except _PROCESS_CONTROL:
            raise
        except CommentInsightFailure as exc:
            if exc.error_code in _PUBLIC_AI_ERRORS:
                raise
            raise _response_invalid() from None
        except (requests.exceptions.Timeout, TimeoutError):
            raise _failure("comment_ai_timeout") from None
        except requests.exceptions.RequestException:
            raise _failure("comment_ai_service_unavailable") from None
        except BaseException:
            raise _failure("comment_ai_service_unavailable") from None
        finally:
            if type(headers) is dict:
                headers.clear()
            if type(payload) is dict:
                payload.clear()
            for index in range(len(self._secret)):
                self._secret[index] = 0
            secret_text = None
            try:
                _safe_close(response)
            finally:
                _safe_close(session)
            response = raw_response = outer = contract = None

    def _validate_input(self, title, comments):
        title = _plain_text(title, maximum=_MAX_TITLE_CHARACTERS)
        try:
            title.encode("utf-8")
        except (UnicodeEncodeError, ValueError):
            raise _response_invalid() from None
        if (
            type(comments) is not tuple
            or not comments
            or len(comments) > _MAX_COMMENTS
            or not all(type(item) is CommentRecord for item in comments)
        ):
            raise _response_invalid()
        keys = tuple(item.comment_key for item in comments)
        if len(set(keys)) != len(keys):
            raise _response_invalid()
        refs = tuple(f"C{index:03d}" for index in range(1, len(comments) + 1))
        request_comments = []
        for ref, item in zip(refs, comments, strict=True):
            if type(item.body) is not str:
                raise _response_invalid()
            try:
                body_size = len(item.body.encode("utf-8"))
            except (UnicodeEncodeError, ValueError):
                raise _response_invalid() from None
            if body_size <= 0 or body_size > _MAX_BODY_BYTES:
                raise _response_invalid()
            request_comments.append({"ref": ref, "body": item.body})
        return refs, dict(zip(refs, keys, strict=True)), request_comments

    def _request_payload(self, title: str, request_comments: list[dict]) -> dict:
        labels = ["质疑", "认同", "真实经历", "追问", "选题建议", "其他"]
        refs = [item["ref"] for item in request_comments]
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["classifications", "candidates"],
            "properties": {
                "classifications": {
                    "type": "array",
                    "minItems": len(refs),
                    "maxItems": len(refs),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["ref", "labels"],
                        "properties": {
                            "ref": {"type": "string", "enum": refs},
                            "labels": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": len(labels),
                                "uniqueItems": True,
                                "items": {"type": "string", "enum": labels},
                            },
                        },
                    },
                },
                "candidates": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title", "reason", "evidenceRefs"],
                        "properties": {
                            "title": {"type": "string", "minLength": 1},
                            "reason": {"type": "string", "minLength": 1},
                            "evidenceRefs": {
                                "type": "array",
                                "minItems": 1,
                                "uniqueItems": True,
                                "items": {"type": "string", "enum": refs},
                            },
                        },
                    },
                },
            },
        }
        instruction = {
            "title": title,
            "comments": request_comments,
            "schema": schema,
        }
        return {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        instruction, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            ],
            "response_format": {"type": "json_object"},
        }

    def _validated_response_bytes(self, response) -> bytes:
        try:
            status = response.status_code
            headers = response.headers
        except _PROCESS_CONTROL:
            raise
        except BaseException:
            raise _failure("comment_ai_service_unavailable") from None
        if type(status) is not int:
            raise _failure("comment_ai_service_unavailable")
        if status < 200 or status >= 300:
            if status in {408, 504}:
                raise _failure("comment_ai_timeout")
            raise _failure("comment_ai_service_unavailable")
        if type(headers) is not dict:
            try:
                content_type = headers.get("Content-Type")
            except _PROCESS_CONTROL:
                raise
            except BaseException:
                raise _response_invalid() from None
        else:
            content_type = headers.get("Content-Type")
        if (
            type(content_type) is not str
            or len(content_type) > 128
            or _CONTENT_TYPE_RE.fullmatch(content_type.strip()) is None
        ):
            raise _response_invalid()
        try:
            content_length = headers.get("Content-Length")
        except _PROCESS_CONTROL:
            raise
        except BaseException:
            raise _response_invalid() from None
        if content_length is not None:
            if (
                type(content_length) is not str
                or not content_length.isascii()
                or not content_length.isdigit()
                or int(content_length) > _MAX_RESPONSE_BYTES
            ):
                raise _response_invalid()
        try:
            iterator = response.iter_content
        except _PROCESS_CONTROL:
            raise
        except BaseException:
            raise _failure("comment_ai_service_unavailable") from None
        if not callable(iterator):
            raise _failure("comment_ai_service_unavailable")
        collected = bytearray()
        try:
            for chunk in iterator(chunk_size=16_384):
                if type(chunk) is not bytes:
                    raise _response_invalid()
                if not chunk:
                    continue
                if len(collected) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise _response_invalid()
                collected.extend(chunk)
            if not collected:
                raise _response_invalid()
            raw = bytes(collected)
        finally:
            for index in range(len(collected)):
                collected[index] = 0
        if not raw:
            raise _response_invalid()
        return raw

    def _extract_contract(self, outer) -> dict:
        if type(outer) is not dict or not set(outer).issubset(_OUTER_KEYS):
            raise _response_invalid()
        choices = outer.get("choices")
        if type(choices) is not list or len(choices) != 1:
            raise _response_invalid()
        choice = choices[0]
        if type(choice) is not dict or not set(choice).issubset(_CHOICE_KEYS):
            raise _response_invalid()
        if choice.get("index") != 0 or choice.get("finish_reason") != "stop":
            raise _response_invalid()
        message = choice.get("message")
        if type(message) is not dict or not set(message).issubset(_MESSAGE_KEYS):
            raise _response_invalid()
        if message.get("role") != "assistant":
            raise _response_invalid()
        content = message.get("content")
        if type(content) is not str or not content or len(content.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise _response_invalid()
        try:
            contract = _loads_json(content)
        except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError):
            raise _response_invalid() from None
        return _exact_dict(contract, _CONTRACT_KEYS)

    def _validate_contract(self, contract: dict, refs: tuple[str, ...]):
        expected_refs = frozenset(refs)
        classifications_raw = contract["classifications"]
        candidates_raw = contract["candidates"]
        if (
            type(classifications_raw) is not list
            or len(classifications_raw) != len(refs)
            or type(candidates_raw) is not list
            or len(candidates_raw) > 5
        ):
            raise _response_invalid()
        classifications = []
        seen_classification_refs = set()
        for item in classifications_raw:
            item = _exact_dict(item, _CLASSIFICATION_KEYS)
            ref = item["ref"]
            labels = item["labels"]
            if type(ref) is not str:
                raise _response_invalid()
            if ref not in expected_refs:
                raise _evidence_invalid()
            if _REF_RE.fullmatch(ref) is None:
                raise _response_invalid()
            if ref in seen_classification_refs:
                raise _response_invalid()
            if (
                type(labels) is not list
                or not labels
                or len(labels) > len(ALLOWED_COMMENT_LABELS)
                or not all(type(label) is str for label in labels)
                or any(label not in ALLOWED_COMMENT_LABELS for label in labels)
                or len(set(labels)) != len(labels)
            ):
                raise _response_invalid()
            seen_classification_refs.add(ref)
            classifications.append((ref, tuple(labels)))
        if seen_classification_refs != expected_refs:
            raise _response_invalid()
        candidates = []
        for item in candidates_raw:
            item = _exact_dict(item, _CANDIDATE_KEYS)
            title = _plain_text(
                item["title"], maximum=_MAX_CANDIDATE_TITLE_CHARACTERS
            )
            reason = _plain_text(
                item["reason"], maximum=_MAX_CANDIDATE_REASON_CHARACTERS
            )
            evidence = item["evidenceRefs"]
            if (
                type(evidence) is not list
                or not evidence
                or not all(type(ref) is str for ref in evidence)
            ):
                raise _evidence_invalid()
            if len(set(evidence)) != len(evidence):
                raise _evidence_invalid()
            if any(
                _REF_RE.fullmatch(ref) is None or ref not in expected_refs
                for ref in evidence
            ):
                raise _evidence_invalid()
            candidates.append((title, reason, tuple(evidence)))
        return tuple(classifications), tuple(candidates)
