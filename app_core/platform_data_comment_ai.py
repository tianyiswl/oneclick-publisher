# -*- coding: utf-8 -*-
"""OpenAI 兼容评论洞察适配器，仅输出经证据校验的本地模型。"""

from __future__ import annotations

import asyncio
import json
import math
import re
import unicodedata

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
_OUTCOME_OK = "ok"
_OUTCOME_FAILURE = "failure"
_OUTCOME_CONTROL = "control"
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


def _control_outcome_value(error: BaseException):
    if isinstance(error, asyncio.CancelledError):
        return ("cancelled", None)
    if isinstance(error, KeyboardInterrupt):
        return ("keyboard_interrupt", None)
    code = error.code if isinstance(error, SystemExit) else None
    if type(code) is not int or not -2_147_483_648 <= code <= 2_147_483_647:
        code = 1
    return ("system_exit", code)


def _raise_clean_outcome(outcome) -> None:
    kind, value = outcome
    if kind == _OUTCOME_CONTROL:
        control, code = value
        if control == "cancelled":
            error = asyncio.CancelledError()
        elif control == "keyboard_interrupt":
            error = KeyboardInterrupt()
        else:
            error = SystemExit(code)
    else:
        error = _failure(value)
    error.__traceback__ = None
    error.__context__ = None
    error.__cause__ = None
    raise error from None


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
        unicodedata.category(character) == "Cc" for character in value
    ):
        raise _response_invalid()
    try:
        value.encode("utf-8")
    except (UnicodeEncodeError, ValueError):
        raise _response_invalid() from None
    return value


def _exact_dict(value: object, keys: frozenset[str]) -> dict:
    if type(value) is not dict or frozenset(value.keys()) != keys:
        raise _response_invalid()
    return value


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("invalid JSON object")
        result[key] = value
    return result


def _loads_json(value: str | bytes):
    parsed = None
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_json_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("invalid JSON number")
            ),
        )
        _validate_json_tree(parsed)
        return parsed
    except BaseException:
        parsed = None
        raise
    finally:
        value = None


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


def _zero_buffer(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def _valid_secret_text(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and bool(value.strip())
        and not any(
            unicodedata.category(character) == "Cc" for character in value
        )
    )


def _provider_secret_buffer(value: object) -> bytearray | None:
    mutable = None
    try:
        if not _valid_secret_text(value):
            return None
        try:
            mutable = bytearray(value, "utf-8")
        except _PROCESS_CONTROL:
            raise
        except BaseException:
            return None
        if not mutable or len(mutable) > _MAX_SECRET_BYTES:
            return None
        result = mutable
        mutable = None
        return result
    finally:
        value = None
        if type(mutable) is bytearray:
            _zero_buffer(mutable)


def _sanitize_prepared_request(request) -> None:
    if not isinstance(request, requests.PreparedRequest):
        return
    try:
        headers = request.headers
        for key in tuple(headers.keys()):
            if type(key) is str and key.lower() in {
                "authorization",
                "proxy-authorization",
            }:
                headers.pop(key, None)
        request.body = None
        request._body_position = None
    except BaseException:
        return


def _sanitize_outbound_carriers(*, error=None, response=None) -> None:
    requests_to_clear = []
    if isinstance(error, requests.exceptions.RequestException):
        requests_to_clear.append(error.request)
        external_response = error.response
        if isinstance(external_response, requests.Response):
            requests_to_clear.append(external_response.request)
    if isinstance(response, requests.Response):
        requests_to_clear.append(response.request)
    seen = set()
    for request in requests_to_clear:
        identity = id(request)
        if identity in seen:
            continue
        seen.add(identity)
        _sanitize_prepared_request(request)


def _parse_json_outcome(raw_response):
    decoded = None
    try:
        decoded = raw_response.decode("utf-8")
        return (_OUTCOME_OK, _loads_json(decoded))
    except _PROCESS_CONTROL as error:
        return (_OUTCOME_CONTROL, _control_outcome_value(error))
    except json.JSONDecodeError as error:
        # JSONDecodeError.doc is our decoded response carrier; its other state
        # belongs to the external exception and is deliberately untouched.
        error.doc = ""
        return (_OUTCOME_FAILURE, "comment_ai_response_invalid")
    except (UnicodeDecodeError, RecursionError, ValueError):
        return (_OUTCOME_FAILURE, "comment_ai_response_invalid")
    except BaseException:
        return (_OUTCOME_FAILURE, "comment_ai_service_unavailable")
    finally:
        raw_response = None
        decoded = None


def _close_resource_outcome(resource):
    try:
        if resource is None:
            return None
        close = getattr(resource, "close", None)
        if callable(close):
            close()
    except _PROCESS_CONTROL as error:
        return (_OUTCOME_CONTROL, _control_outcome_value(error))
    except BaseException:
        return None
    finally:
        resource = None
    return None


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
        self._settings = None
        self._secret = None
        self._session_factory = None
        self._initial_outcome = (
            _OUTCOME_FAILURE,
            "comment_ai_not_configured",
        )
        self.model_name = "unconfigured"
        mutable = None
        try:
            if type(settings) is CommentAiSettings:
                self.model_name = settings.model
            if (
                type(settings) is not CommentAiSettings
                or not callable(session_factory)
            ):
                return
            mutable = _provider_secret_buffer(secret)
            if type(mutable) is not bytearray:
                return
            self._settings = settings
            self._secret = mutable
            mutable = None
            self._session_factory = session_factory
            self._initial_outcome = None
            self.model_name = settings.model
        except _PROCESS_CONTROL as error:
            self._initial_outcome = (
                _OUTCOME_CONTROL,
                _control_outcome_value(error),
            )
        except BaseException:
            self._initial_outcome = (
                _OUTCOME_FAILURE,
                "comment_ai_not_configured",
            )
        finally:
            settings = None
            secret = None
            session_factory = None
            if type(mutable) is bytearray:
                _zero_buffer(mutable)

    def analyze(
        self, title: str, comments: tuple[CommentRecord, ...]
    ) -> InsightResult:
        outcome = self._analyze_outcome(title, comments)
        title = None
        comments = None
        if outcome[0] == _OUTCOME_OK:
            return outcome[1]
        self = None
        _raise_clean_outcome(outcome)

    def _analyze_outcome(self, title, comments):
        try:
            if self._initial_outcome is not None:
                outcome = self._initial_outcome
                self._initial_outcome = (
                    _OUTCOME_FAILURE,
                    "comment_ai_not_configured",
                )
                return outcome
            return self._analyze_worker_outcome(title, comments)
        except _PROCESS_CONTROL as error:
            return (_OUTCOME_CONTROL, _control_outcome_value(error))
        except CommentInsightFailure as error:
            code = (
                error.error_code
                if error.error_code in _PUBLIC_AI_ERRORS
                else "comment_ai_response_invalid"
            )
            return (_OUTCOME_FAILURE, code)
        except BaseException:
            return (_OUTCOME_FAILURE, "comment_ai_service_unavailable")
        finally:
            title = None
            comments = None
            self = None

    def _analyze_worker_outcome(self, title, comments):
        outcome = (_OUTCOME_FAILURE, "comment_ai_service_unavailable")
        session = None
        headers = None
        payload = None
        response = None
        error_response = None
        raw_response = None
        outer = None
        contract = None
        secret_text = None
        refs = None
        ref_to_key = None
        request_comments = None
        encoded_payload = None
        parsed_outcome = None
        validated = None
        classifications = None
        candidates = None
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
            session.trust_env = False
            response = session.post(
                f"{self._settings.normalized_base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=(10, 45),
                stream=True,
                allow_redirects=False,
            )
            raw_response = self._validated_response_bytes(response)
            parsed_outcome = _parse_json_outcome(raw_response)
            raw_response = None
            if parsed_outcome[0] != _OUTCOME_OK:
                outcome = parsed_outcome
            else:
                outer = parsed_outcome[1]
                parsed_outcome = None
                contract = self._extract_contract(outer)
                validated = self._validate_contract(contract, refs)
                # No local comment key is restored before every response item
                # has passed the complete response contract.
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
                result = InsightResult(
                    classifications=classifications,
                    candidates=candidates,
                    known_comment_keys=frozenset(ref_to_key.values()),
                )
                outcome = (_OUTCOME_OK, result)
                result = None
        except _PROCESS_CONTROL as error:
            outcome = (_OUTCOME_CONTROL, _control_outcome_value(error))
        except CommentInsightFailure as exc:
            code = (
                exc.error_code
                if exc.error_code in _PUBLIC_AI_ERRORS
                else "comment_ai_response_invalid"
            )
            outcome = (_OUTCOME_FAILURE, code)
        except (requests.exceptions.Timeout, TimeoutError) as error:
            if isinstance(error, requests.exceptions.RequestException):
                _sanitize_outbound_carriers(error=error)
                if isinstance(error.response, requests.Response):
                    error_response = error.response
            outcome = (_OUTCOME_FAILURE, "comment_ai_timeout")
        except requests.exceptions.RequestException as error:
            _sanitize_outbound_carriers(error=error)
            if isinstance(error.response, requests.Response):
                error_response = error.response
            outcome = (_OUTCOME_FAILURE, "comment_ai_service_unavailable")
        except BaseException:
            outcome = (_OUTCOME_FAILURE, "comment_ai_service_unavailable")
        finally:
            _sanitize_outbound_carriers(response=response)
            if type(headers) is dict:
                headers.clear()
            if type(payload) is dict:
                payload.clear()
            if type(request_comments) is list:
                for item in request_comments:
                    if type(item) is dict:
                        item.clear()
                request_comments.clear()
            if type(ref_to_key) is dict:
                ref_to_key.clear()
            if type(self._secret) is bytearray:
                _zero_buffer(self._secret)
            close_control = None
            for resource in (response, error_response, session):
                current = _close_resource_outcome(resource)
                if close_control is None and current is not None:
                    close_control = current
            if close_control is not None:
                outcome = close_control
            title = None
            comments = None
            self = None
            session = None
            headers = None
            payload = None
            response = None
            error_response = None
            raw_response = None
            outer = None
            contract = None
            secret_text = None
            refs = None
            ref_to_key = None
            request_comments = None
            encoded_payload = None
            parsed_outcome = None
            validated = None
            classifications = None
            candidates = None
            close_control = None
            resource = None
            current = None
        return outcome

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
            if type(item.body) is not str or any(
                unicodedata.category(character) == "Cc"
                for character in item.body
            ):
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
        for key, maximum in (
            ("id", 512),
            ("object", 64),
            ("model", 256),
        ):
            if key in outer:
                _plain_text(outer[key], maximum=maximum)
        if "created" in outer and (
            type(outer["created"]) is not int or outer["created"] < 0
        ):
            raise _response_invalid()
        if "usage" in outer and type(outer["usage"]) is not dict:
            raise _response_invalid()
        for key in ("system_fingerprint", "service_tier"):
            if key in outer and outer[key] is not None:
                _plain_text(outer[key], maximum=256)
        choices = outer.get("choices")
        if type(choices) is not list or len(choices) != 1:
            raise _response_invalid()
        choice = choices[0]
        if type(choice) is not dict or not set(choice).issubset(_CHOICE_KEYS):
            raise _response_invalid()
        if (
            type(choice.get("index")) is not int
            or choice["index"] != 0
            or type(choice.get("finish_reason")) is not str
            or choice["finish_reason"] != "stop"
        ):
            raise _response_invalid()
        if "logprobs" in choice and choice["logprobs"] is not None:
            if type(choice["logprobs"]) is not dict:
                raise _response_invalid()
        message = choice.get("message")
        if type(message) is not dict or not set(message).issubset(_MESSAGE_KEYS):
            raise _response_invalid()
        if (
            type(message.get("role")) is not str
            or message["role"] != "assistant"
        ):
            raise _response_invalid()
        if "refusal" in message and message["refusal"] is not None:
            _plain_text(message["refusal"], maximum=1000)
        content = message.get("content")
        if type(content) is not str or not content:
            raise _response_invalid()
        try:
            content_size = len(content.encode("utf-8"))
        except (UnicodeEncodeError, ValueError):
            raise _response_invalid() from None
        if content_size > _MAX_RESPONSE_BYTES:
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
        seen_candidates = set()
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
            evidence_refs = tuple(evidence)
            identity = (title, reason, frozenset(evidence_refs))
            if identity in seen_candidates:
                raise _response_invalid()
            seen_candidates.add(identity)
            candidates.append((title, reason, evidence_refs))
        return tuple(classifications), tuple(candidates)
