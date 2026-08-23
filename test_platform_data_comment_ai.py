# -*- coding: utf-8 -*-
"""评论洞察 AI 边界、非秘密设置和系统凭据库测试。"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import requests

from app_core.platform_data_comment_ai import OpenAiCompatibleCommentProvider
from app_core.platform_data_comment_models import (
    CommentInsightFailure,
    CommentRecord,
)
from app_core.platform_data_comment_secret_store import CommentSecretStore
from app_core.platform_data_comment_settings import (
    BASE_URL_KEY,
    MODEL_KEY,
    CommentAiSettings,
    load_ai_settings,
    save_ai_settings,
)


OBSERVED = "2026-08-23T12:00:00+08:00"
COMMENT_KEY_A = "a" * 64
COMMENT_KEY_B = "b" * 64


def exception_trace_text(error: BaseException) -> str:
    """Collect every visible exception-chain frame local without hiding values."""

    pending = [error]
    seen = set()
    parts = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.append(repr(current))
        trace = current.__traceback__
        while trace is not None:
            filename = trace.tb_frame.f_code.co_filename.replace("\\", "/")
            if "/app_core/" in filename:
                parts.append(trace.tb_frame.f_code.co_name)
                for name, value in trace.tb_frame.f_locals.items():
                    parts.append(f"{name}={value!r}")
            trace = trace.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(parts)


def comment(
    key: str = COMMENT_KEY_A,
    body: str = "为什么会这样？",
    *,
    likes: int = 777,
    replies: int = 888,
) -> CommentRecord:
    return CommentRecord(
        content_id="private-work-id",
        comment_key=key,
        body=body,
        like_count=likes,
        reply_count=replies,
        commented_at=OBSERVED,
        observed_at=OBSERVED,
    )


def insight_contract(
    *,
    classifications=None,
    candidates=None,
    extra=None,
):
    value = {
        "classifications": classifications
        if classifications is not None
        else [{"ref": "C001", "labels": ["追问"]}],
        "candidates": candidates
        if candidates is not None
        else [
            {
                "title": "回答这个问题",
                "reason": "评论提出了具体追问",
                "evidenceRefs": ["C001"],
            }
        ],
    }
    if extra:
        value.update(extra)
    return value


def response_bytes(contract=None) -> bytes:
    content = json.dumps(
        insight_contract() if contract is None else contract,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    envelope = {
        "id": "chatcmpl-controlled",
        "object": "chat.completion",
        "created": 1,
        "model": "model-x",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


class FakeResponse:
    def __init__(
        self,
        content: bytes | object = None,
        *,
        status_code: int = 200,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.content = response_bytes() if content is None else content
        self.closed = False
        self.chunks_yielded = 0

    def iter_content(self, chunk_size):
        if type(self.content) is not bytes:
            self.chunks_yielded += 1
            yield self.content
            return
        for offset in range(0, len(self.content), chunk_size):
            self.chunks_yielded += 1
            yield self.content[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class RawMarker:
    def __init__(self, marker: str) -> None:
        self.marker = marker


class StreamingControlResponse(requests.Response):
    """Retain ``self`` in an external stream frame after a private chunk."""

    def __init__(self, marker: str, control: BaseException) -> None:
        super().__init__()
        self.status_code = 200
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self._content = marker.encode("utf-8")
        self._content_consumed = True
        self.raw = RawMarker(marker)
        self.control = control
        self.closed = False

    def iter_content(self, chunk_size):
        del chunk_size
        yield self._content
        raise self.control

    def close(self):
        self.closed = True


class RecordingSession:
    def __init__(self, outcome=None) -> None:
        self.outcome = FakeResponse() if outcome is None else outcome
        self.calls = []
        self.closed = False
        self.trust_env = True
        self.sent_request = None
        self.send_calls = 0
        self.post_calls = 0

    def post(self, *_args, **_kwargs):
        self.post_calls += 1
        raise AssertionError("provider must use one owned PreparedRequest")

    def send(self, request, *, timeout, stream, allow_redirects):
        self.send_calls += 1
        self.sent_request = request
        if isinstance(self.outcome, BaseException):
            self.calls.append(
                {
                    "request": request,
                    "url": request.url,
                    "timeout": timeout,
                    "stream": stream,
                    "allow_redirects": allow_redirects,
                }
            )
            raise self.outcome
        body = request.body
        if type(body) in (bytes, bytearray):
            decoded = body.decode("utf-8")
        else:
            decoded = body
        self.calls.append(
            {
                "request": request,
                "method": request.method,
                "url": request.url,
                "headers": dict(request.headers),
                "json": deepcopy(json.loads(decoded)),
                "timeout": timeout,
                "stream": stream,
                "allow_redirects": allow_redirects,
            }
        )
        object.__getattribute__(self.outcome, "__dict__")["request"] = request
        return self.outcome

    def close(self):
        self.closed = True


class RetainedRequestFailureSession:
    """Retain the external exception and request carriers for privacy assertions."""

    def __init__(self) -> None:
        self.closed = False
        self.trust_env = True
        self.error = None
        self.request = None
        self.response = None
        self.cause = ValueError("safe-request-cause")
        self.context = RuntimeError("safe-request-context")
        self.calls = 0

    def send(self, request, *, timeout, stream, allow_redirects):
        del timeout, stream, allow_redirects
        self.calls += 1
        response = requests.Response()
        response.status_code = 503
        response.__dict__["request"] = request
        response.closed_by_provider = False

        def close_response():
            response.closed_by_provider = True

        response.close = close_response
        error = requests.exceptions.ConnectionError(
            "safe-request-failure",
            request=request,
            response=response,
        )
        error.__cause__ = self.cause
        error.__context__ = self.context
        self.request = request
        self.response = response
        self.error = error
        raise error

    def close(self):
        self.closed = True


class CleanupControlResponse(requests.Response):
    """A real Response whose cleanup attributes can raise process control."""

    def __init__(
        self,
        *,
        request_control=None,
        request_control_permanent=False,
        request_set_control=None,
        close_control=None,
    ) -> None:
        self._request_control = None
        self._request_control_raised = False
        self._request_control_permanent = request_control_permanent
        self._request_set_control = None
        self.request_access_count = 0
        self.request_set_count = 0
        super().__init__()
        self.status_code = 200
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self._content = response_bytes()
        self._content_consumed = True
        self._request_control = request_control
        self._request_set_control = request_set_control
        self._close_control = close_control
        self.closed_by_provider = False
        self.raw = RawMarker("response-raw-private-marker")
        self.request_set_count = 0

    @property
    def request(self):
        self.request_access_count += 1
        if (
            self._request_control is not None
            and (
                self._request_control_permanent
                or not self._request_control_raised
            )
        ):
            self._request_control_raised = True
            raise self._request_control
        return self.__dict__.get("request")

    @request.setter
    def request(self, value):
        self.request_set_count += 1
        if self._request_set_control is not None:
            raise self._request_set_control
        self.__dict__["request"] = value

    def iter_content(self, chunk_size=1, decode_unicode=False):
        del chunk_size, decode_unicode
        yield self._content

    def close(self):
        self.closed_by_provider = True
        if self._close_control is not None:
            raise self._close_control


class CleanupControlSession:
    def __init__(
        self,
        response,
        *,
        send_control=None,
        close_control=None,
        request_mutator=None,
    ) -> None:
        self.response = response
        self.send_control = send_control
        self.close_control = close_control
        self.request_mutator = request_mutator
        self.closed = False
        self.calls = 0
        self.trust_env = True
        self.request = None
        self.response_request_before_cleanup = None
        self.headers = requests.structures.CaseInsensitiveDict(
            {"Authorization": "session-header-private-marker"}
        )
        self.auth = ("session-user", "session-auth-private-marker")
        self.proxies = {"https": "session-proxy-private-marker"}

    def post(self, *_args, **_kwargs):
        raise AssertionError("provider must not use post")

    def send(self, request, *, timeout, stream, allow_redirects):
        del timeout, stream, allow_redirects
        self.calls += 1
        self.request = request
        if self.send_control is not None:
            raise self.send_control
        request.headers["Proxy-Authorization"] = "proxy-private-marker"
        if self.request_mutator is not None:
            self.request_mutator(request)
        object.__getattribute__(self.response, "__dict__")["request"] = request
        self.response_request_before_cleanup = request
        return self.response

    def close(self):
        self.closed = True
        if self.close_control is not None:
            raise self.close_control


class ExplosiveScalar:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.comparisons = 0
        self.length_checks = 0

    def __eq__(self, _other):
        self.comparisons += 1
        raise KeyboardInterrupt()

    def __len__(self):
        self.length_checks += 1
        raise KeyboardInterrupt()


class FakeSettings:
    def __init__(self, initial=None) -> None:
        self.values = dict(initial or {})
        self.synced = 0

    def value(self, key, defaultValue=None):
        return self.values.get(key, defaultValue)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        self.synced += 1


class FakeFunction:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeMacSecurity:
    NOT_FOUND = -25300

    def __init__(self, *, fail=None) -> None:
        self.secret = None
        self.fail = fail
        self.returned = []
        self.freed = []
        self.mutable_views = []
        self.deleted = 0
        self.SecKeychainFindGenericPassword = FakeFunction(self._find)
        self.SecKeychainAddGenericPassword = FakeFunction(self._add)
        self.SecKeychainItemModifyAttributesAndData = FakeFunction(self._modify)
        self.SecKeychainItemDelete = FakeFunction(self._delete)
        self.SecKeychainItemFreeContent = FakeFunction(self._free)

    def _maybe_fail(self):
        if self.fail is not None:
            if isinstance(self.fail, BaseException):
                raise self.fail
            return self.fail
        return None

    def _find(
        self,
        _keychain,
        service_length,
        service_pointer,
        account_length,
        account_pointer,
        password_length_out,
        password_data_out,
        item_out,
    ):
        failure = self._maybe_fail()
        if failure is not None:
            return failure
        service = ctypes.string_at(service_pointer, service_length).decode()
        account = ctypes.string_at(account_pointer, account_length).decode()
        if service != "com.oneclickpublisher.comment-insight" or account != "default":
            return self.NOT_FOUND
        if self.secret is None:
            return self.NOT_FOUND
        if password_length_out is not None or password_data_out is not None:
            assert password_length_out is not None
            assert password_data_out is not None
            value = (
                self.secret
                if type(self.secret) is bytes
                else self.secret.encode("utf-8")
            )
            native = ctypes.create_string_buffer(value, len(value))
            self.returned.append(native)
            password_length_out._obj.value = len(value)
            password_data_out._obj.value = ctypes.addressof(native)
        item_out._obj.value = 0x1234
        return 0

    def _capture(self, length, pointer):
        view = ctypes.cast(
            pointer, ctypes.POINTER(ctypes.c_ubyte * int(length))
        ).contents
        self.mutable_views.append(view)
        return ctypes.string_at(pointer, length).decode("utf-8")

    def _add(
        self,
        _keychain,
        service_length,
        service_pointer,
        account_length,
        account_pointer,
        password_length,
        password_pointer,
        _item_out,
    ):
        failure = self._maybe_fail()
        if failure is not None:
            return failure
        self.assert_names(
            service_length,
            service_pointer,
            account_length,
            account_pointer,
        )
        self.secret = self._capture(password_length, password_pointer)
        return 0

    def _modify(self, _item, _attributes, password_length, password_pointer):
        failure = self._maybe_fail()
        if failure is not None:
            return failure
        self.secret = self._capture(password_length, password_pointer)
        return 0

    def _delete(self, _item):
        failure = self._maybe_fail()
        if failure is not None:
            return failure
        self.secret = None
        self.deleted += 1
        return 0

    def _free(self, _attributes, pointer):
        self.freed.append(pointer.value if hasattr(pointer, "value") else pointer)
        return 0

    @staticmethod
    def assert_names(service_length, service_pointer, account_length, account_pointer):
        service = ctypes.string_at(service_pointer, service_length).decode()
        account = ctypes.string_at(account_pointer, account_length).decode()
        assert service == "com.oneclickpublisher.comment-insight"
        assert account == "default"


class FakeCoreFoundation:
    def __init__(self) -> None:
        self.released = []
        self.CFRelease = FakeFunction(self._release)

    def _release(self, value):
        self.released.append(value.value if hasattr(value, "value") else value)


class FakeWindowsAdvapi:
    NOT_FOUND = 1168

    def __init__(self, facade, *, fail=None) -> None:
        self.facade = facade
        self.secret = None
        self.fail = fail
        self.last_error = 0
        self.mutable_views = []
        self.read_refs = []
        self.freed = []
        self.deleted = 0
        self.CredWriteW = FakeFunction(self._write)
        self.CredReadW = FakeFunction(self._read)
        self.CredDeleteW = FakeFunction(self._delete)
        self.CredFree = FakeFunction(self._free)

    def _maybe_fail(self):
        if self.fail is not None:
            if isinstance(self.fail, BaseException):
                raise self.fail
            self.last_error = int(self.fail)
            return True
        return False

    def _write(self, credential_pointer, _flags):
        if self._maybe_fail():
            return 0
        credential = credential_pointer._obj
        if credential.TargetName != "com.oneclickpublisher.comment-insight":
            self.last_error = 87
            return 0
        length = int(credential.CredentialBlobSize)
        view = ctypes.cast(
            credential.CredentialBlob,
            ctypes.POINTER(ctypes.c_ubyte * length),
        ).contents
        self.mutable_views.append(view)
        self.secret = ctypes.string_at(credential.CredentialBlob, length).decode(
            "utf-8"
        )
        self.last_error = 0
        return 1

    def _read(self, target, _credential_type, _flags, credential_out):
        if self._maybe_fail():
            return 0
        if target != "com.oneclickpublisher.comment-insight" or self.secret is None:
            self.last_error = self.NOT_FOUND
            return 0
        pointer_type = credential_out._obj
        credential_type = pointer_type._type_
        value = self.secret if type(self.secret) is bytes else self.secret.encode("utf-8")
        blob = (ctypes.c_ubyte * len(value))(*value)
        credential = credential_type()
        credential.Type = 1
        credential.TargetName = "com.oneclickpublisher.comment-insight"
        credential.CredentialBlobSize = len(value)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.UserName = "default"
        pointer = ctypes.pointer(credential)
        out_pointer = ctypes.cast(
            credential_out, ctypes.POINTER(type(pointer))
        )
        out_pointer[0] = pointer
        self.read_refs.append((blob, credential, pointer))
        self.last_error = 0
        return 1

    def _delete(self, target, _credential_type, _flags):
        if self._maybe_fail():
            return 0
        if target != "com.oneclickpublisher.comment-insight" or self.secret is None:
            self.last_error = self.NOT_FOUND
            return 0
        self.secret = None
        self.deleted += 1
        self.last_error = 0
        return 1

    def _free(self, pointer):
        self.freed.append(pointer)


class FakeCtypes:
    """保留真实 ctypes 指针语义，只把动态库入口换成可观测假实现。"""

    def __init__(self, *, mac_fail=None, windows_fail=None) -> None:
        self.mac = FakeMacSecurity(fail=mac_fail)
        self.core = FakeCoreFoundation()
        self.last_error = 0
        self.windows = FakeWindowsAdvapi(self, fail=windows_fail)
        for name in (
            "Structure",
            "POINTER",
            "byref",
            "cast",
            "pointer",
            "string_at",
            "create_string_buffer",
            "memset",
            "addressof",
            "sizeof",
            "c_bool",
            "c_byte",
            "c_char",
            "c_char_p",
            "c_int",
            "c_long",
            "c_size_t",
            "c_ubyte",
            "c_uint",
            "c_uint32",
            "c_void_p",
            "c_wchar_p",
        ):
            setattr(self, name, getattr(ctypes, name))
        self.wintypes = wintypes

    def CDLL(self, path):
        if "Security.framework" in path:
            return self.mac
        if "CoreFoundation.framework" in path:
            return self.core
        raise AssertionError(path)

    def WinDLL(self, name, use_last_error=True):
        if name != "Advapi32.dll" or use_last_error is not True:
            raise AssertionError((name, use_last_error))
        return self.windows

    def get_last_error(self):
        return self.windows.last_error


class CommentAiSettingsTests(unittest.TestCase):
    def test_qsettings_stores_only_normalized_nonsecret_values(self):
        settings = FakeSettings({"unrelated": "keep"})
        value = CommentAiSettings("https://AI.EXAMPLE.com/v1/", "model-x")

        save_ai_settings(settings, value)
        loaded = load_ai_settings(settings)

        self.assertEqual(
            settings.values,
            {
                "unrelated": "keep",
                BASE_URL_KEY: "https://ai.example.com/v1",
                MODEL_KEY: "model-x",
            },
        )
        self.assertEqual(loaded, CommentAiSettings("https://ai.example.com/v1", "model-x"))
        self.assertEqual(settings.synced, 1)
        serialized = repr(settings.values).lower()
        for forbidden in ("secret", "api_key", "apikey", "authorization", "bearer"):
            self.assertNotIn(forbidden, serialized)

    def test_invalid_or_partial_qsettings_are_treated_as_unconfigured(self):
        cases = (
            {},
            {BASE_URL_KEY: "https://ai.example.com/v1"},
            {MODEL_KEY: "model-x"},
            {BASE_URL_KEY: "http://ai.example.com/v1", MODEL_KEY: "model-x"},
            {BASE_URL_KEY: 3, MODEL_KEY: "model-x"},
        )
        for values in cases:
            with self.subTest(values=values):
                self.assertIsNone(load_ai_settings(FakeSettings(values)))

    def test_base_url_rejects_authority_smuggling_and_noncanonical_paths(self):
        invalid = (
            "http://ai.example.com/v1",
            "https://user@ai.example.com/v1",
            "https://user:pass@ai.example.com/v1",
            "https://ai.example.com/v1?token=x",
            "https://ai.example.com/v1#fragment",
            "https://ai.example.com:443/v1",
            "https://ai.example.com:8443/v1",
            "https://ai.example.com/v1//nested",
            "https://ai.example.com/v1/../other",
            "https://ai.example.com/v1%2fother",
            "https://ai.example.com/v1\\other",
            "https://",
            " https://ai.example.com/v1",
            "https://ai.example.com/v1\n",
            "https://ai.example.com/" + "x" * 2048,
            True,
            None,
        )
        for value in invalid:
            with self.subTest(value=repr(value)[:80]):
                with self.assertRaises(CommentInsightFailure) as raised:
                    CommentAiSettings(value, "model-x")
                self.assertEqual(raised.exception.error_code, "comment_ai_not_configured")

    def test_model_requires_a_bounded_plain_nonempty_string(self):
        invalid = (
            "",
            " ",
            " model",
            "model ",
            "model\n",
            "model\u0085private",
            "x" * 257,
            1,
            True,
            None,
        )
        for value in invalid:
            with self.subTest(value=repr(value)[:80]):
                with self.assertRaises(CommentInsightFailure) as raised:
                    CommentAiSettings("https://ai.example.com/v1", value)
                self.assertEqual(raised.exception.error_code, "comment_ai_not_configured")

    def test_model_rejects_isolated_surrogate_before_any_network_boundary(self):
        with self.assertRaises(CommentInsightFailure) as raised:
            CommentAiSettings("https://ai.example.com/v1", "model-\ud800")

        self.assertEqual(raised.exception.error_code, "comment_ai_not_configured")

    def test_qsettings_process_control_is_preserved(self):
        for control_type in (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            class InterruptingSettings(FakeSettings):
                def value(self, key, defaultValue=None):
                    raise control_type()

                def setValue(self, key, value):
                    raise control_type()

            with self.subTest(control=control_type.__name__):
                with self.assertRaises(control_type):
                    load_ai_settings(InterruptingSettings())
                with self.assertRaises(control_type):
                    save_ai_settings(
                        InterruptingSettings(),
                        CommentAiSettings("https://ai.example.com/v1", "model-x"),
                    )


class CommentSecretStoreTests(unittest.TestCase):
    def test_is_configured_checks_native_existence_without_copying_secret(self):
        """Existence probes must never copy or decode the stored credential blob."""

        for platform_name, native_attribute in (
            ("darwin", "mac"),
            ("win32", "windows"),
        ):
            native = FakeCtypes()
            getattr(native, native_attribute).secret = "status-private-marker"
            copied = []

            def forbidden_string_at(*args):
                copied.append(args)
                raise AssertionError("existence probe must not copy credential bytes")

            native.string_at = forbidden_string_at
            store = CommentSecretStore(
                platform_name=platform_name,
                ctypes_module=native,
            )

            with self.subTest(platform=platform_name):
                self.assertTrue(hasattr(store, "is_configured"))
                self.assertTrue(store.is_configured())
                self.assertEqual(copied, [])
                if platform_name == "darwin":
                    self.assertEqual(native.mac.returned, [])
                    self.assertEqual(native.mac.freed, [])
                    self.assertEqual(len(native.core.released), 1)
                else:
                    self.assertEqual(len(native.windows.read_refs), 1)
                    self.assertEqual(len(native.windows.freed), 1)

    def test_is_configured_returns_false_only_for_absent_native_credential(self):
        """A missing item is a safe false result; unsupported/native errors are fixed failures."""

        for platform_name in ("darwin", "win32"):
            store = CommentSecretStore(
                platform_name=platform_name,
                ctypes_module=FakeCtypes(),
            )
            with self.subTest(platform=platform_name):
                self.assertTrue(hasattr(store, "is_configured"))
                self.assertFalse(store.is_configured())

        failing = (
            CommentSecretStore(platform_name="linux", ctypes_module=FakeCtypes()),
            CommentSecretStore(
                platform_name="darwin",
                ctypes_module=FakeCtypes(mac_fail=-50),
            ),
            CommentSecretStore(
                platform_name="win32",
                ctypes_module=FakeCtypes(windows_fail=5),
            ),
        )
        for store in failing:
            with self.subTest(platform=store._platform):
                self.assertTrue(hasattr(store, "is_configured"))
                with self.assertRaises(CommentInsightFailure) as raised:
                    store.is_configured()
                self.assertEqual(
                    raised.exception.error_code,
                    "comment_ai_not_configured",
                )
                self.assertEqual(str(raised.exception), "comment_ai_not_configured")

    def test_is_configured_preserves_safe_process_control_after_cleanup(self):
        """Native process-control exceptions must be rebuilt only after owned handles close."""

        cases = (
            ("darwin", KeyboardInterrupt(), KeyboardInterrupt, None),
            ("darwin", SystemExit(37), SystemExit, 37),
            ("win32", KeyboardInterrupt(), KeyboardInterrupt, None),
            ("win32", SystemExit("private-status-code"), SystemExit, 1),
        )
        for platform_name, control, expected, expected_code in cases:
            kwargs = (
                {"mac_fail": control}
                if platform_name == "darwin"
                else {"windows_fail": control}
            )
            native = FakeCtypes(**kwargs)
            store = CommentSecretStore(
                platform_name=platform_name,
                ctypes_module=native,
            )
            with self.subTest(platform=platform_name, control=type(control).__name__):
                self.assertTrue(hasattr(store, "is_configured"))
                with self.assertRaises(expected) as raised:
                    store.is_configured()
                if expected is SystemExit:
                    self.assertEqual(raised.exception.code, expected_code)
                if platform_name == "darwin":
                    self.assertEqual(native.mac.freed, [])
                    self.assertEqual(native.core.released, [])
                else:
                    self.assertEqual(native.windows.freed, [])

    def test_macos_ctypes_backend_writes_reads_replaces_and_deletes(self):
        native = FakeCtypes()
        store = CommentSecretStore(platform_name="darwin", ctypes_module=native)

        self.assertIsNone(store.read())
        store.write("sk-first")
        self.assertEqual(native.mac.secret, "sk-first")
        self.assertTrue(all(bytes(view) == b"\0" * len(view) for view in native.mac.mutable_views))
        self.assertEqual(store.read(), "sk-first")
        self.assertTrue(native.mac.freed)
        for function in (
            native.mac.SecKeychainFindGenericPassword,
            native.mac.SecKeychainAddGenericPassword,
            native.mac.SecKeychainItemModifyAttributesAndData,
            native.mac.SecKeychainItemDelete,
            native.mac.SecKeychainItemFreeContent,
            native.core.CFRelease,
        ):
            self.assertIsNotNone(function.argtypes)
        store.write("sk-replacement")
        self.assertEqual(native.mac.secret, "sk-replacement")
        store.delete()
        self.assertIsNone(store.read())
        self.assertEqual(native.mac.deleted, 1)
        self.assertTrue(native.core.released)

    def test_windows_ctypes_backend_writes_reads_replaces_and_deletes(self):
        native = FakeCtypes()
        store = CommentSecretStore(platform_name="win32", ctypes_module=native)

        self.assertIsNone(store.read())
        store.write("sk-first")
        self.assertEqual(native.windows.secret, "sk-first")
        self.assertTrue(
            all(bytes(view) == b"\0" * len(view) for view in native.windows.mutable_views)
        )
        self.assertEqual(store.read(), "sk-first")
        self.assertEqual(len(native.windows.freed), 1)
        for function in (
            native.windows.CredWriteW,
            native.windows.CredReadW,
            native.windows.CredDeleteW,
            native.windows.CredFree,
        ):
            self.assertIsNotNone(function.argtypes)
        self.assertIs(native.windows.CredWriteW.restype, wintypes.BOOL)
        self.assertIs(native.windows.CredReadW.restype, wintypes.BOOL)
        self.assertIs(native.windows.CredDeleteW.restype, wintypes.BOOL)
        store.write("sk-replacement")
        self.assertEqual(native.windows.secret, "sk-replacement")
        store.delete()
        self.assertIsNone(store.read())
        self.assertEqual(native.windows.deleted, 1)

    def test_invalid_native_read_is_freed_before_fixed_failure(self):
        mac_native = FakeCtypes()
        mac_native.mac.secret = b"\xff"
        mac_store = CommentSecretStore(
            platform_name="darwin", ctypes_module=mac_native
        )
        with self.assertRaises(CommentInsightFailure) as mac_raised:
            mac_store.read()
        self.assertEqual(mac_raised.exception.error_code, "comment_ai_not_configured")
        self.assertEqual(len(mac_native.mac.freed), 1)
        self.assertEqual(len(mac_native.core.released), 1)

        windows_native = FakeCtypes()
        windows_native.windows.secret = b"\xff"
        windows_store = CommentSecretStore(
            platform_name="win32", ctypes_module=windows_native
        )
        with self.assertRaises(CommentInsightFailure) as windows_raised:
            windows_store.read()
        self.assertEqual(
            windows_raised.exception.error_code, "comment_ai_not_configured"
        )
        self.assertEqual(len(windows_native.windows.freed), 1)

    def test_native_failures_and_unsupported_platform_expose_only_fixed_code(self):
        marker = "native-private-error-and-secret"
        cases = (
            CommentSecretStore(platform_name="linux", ctypes_module=FakeCtypes()),
            CommentSecretStore(
                platform_name="darwin",
                ctypes_module=FakeCtypes(mac_fail=RuntimeError(marker)),
            ),
            CommentSecretStore(
                platform_name="win32",
                ctypes_module=FakeCtypes(windows_fail=5),
            ),
        )
        for store in cases:
            for operation in (
                lambda current=store: current.write("sk-private"),
                lambda current=store: current.read(),
                lambda current=store: current.delete(),
            ):
                with self.subTest(store=store, operation=operation):
                    with self.assertRaises(CommentInsightFailure) as raised:
                        operation()
                    self.assertEqual(
                        raised.exception.error_code, "comment_ai_not_configured"
                    )
                    self.assertEqual(str(raised.exception), "comment_ai_not_configured")
                    self.assertNotIn(marker, repr(raised.exception))
                    self.assertNotIn("sk-private", repr(raised.exception))

    def test_invalid_secret_is_rejected_without_native_call(self):
        native = FakeCtypes()
        store = CommentSecretStore(platform_name="darwin", ctypes_module=native)
        for value in (
            "",
            " ",
            "sk\nprivate",
            "sk\0private",
            "sk\x7fprivate",
            "sk\u0085private",
            "sk-\ud800",
            "x" * 8193,
            None,
            1,
            True,
        ):
            with self.subTest(value=repr(value)[:80]):
                with self.assertRaises(CommentInsightFailure) as raised:
                    store.write(value)
                self.assertEqual(raised.exception.error_code, "comment_ai_not_configured")
        self.assertIsNone(native.mac.secret)

    def test_write_failure_traceback_contains_no_secret_or_native_context(self):
        secret = "sk-traceback-private-marker"
        native_marker = "native-traceback-private-marker"
        native_error = RuntimeError(native_marker)
        store = CommentSecretStore(
            platform_name="darwin",
            ctypes_module=FakeCtypes(mac_fail=native_error),
        )

        caught = None
        try:
            store.write(secret)
        except CommentInsightFailure as error:
            caught = error

        self.assertIsNotNone(caught)
        visible = exception_trace_text(caught)
        self.assertNotIn(secret, visible)
        self.assertNotIn(native_marker, visible)
        self.assertIsNone(caught.__context__)
        self.assertIsNone(caught.__cause__)
        self.assertEqual(native_error.args, (native_marker,))
        self.assertIsNotNone(native_error.__traceback__)
        self.assertIsNone(native_error.__context__)
        self.assertIsNone(native_error.__cause__)
        self.assertNotIn(secret, exception_trace_text(native_error))
        trace = caught.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_name == "write":
                self.assertIsNone(trace.tb_frame.f_locals.get("self"))
            trace = trace.tb_next

    def test_macos_partial_find_is_always_freed_and_released(self):
        for terminal in (-50, KeyboardInterrupt()):
            native = FakeCtypes()
            native.mac.secret = b"partial-native-secret"
            original = native.mac.SecKeychainFindGenericPassword.callback

            def partial_find(*args, outcome=terminal):
                self.assertEqual(original(*args), 0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            native.mac.SecKeychainFindGenericPassword = FakeFunction(partial_find)
            store = CommentSecretStore(
                platform_name="darwin", ctypes_module=native
            )

            with self.subTest(terminal=type(terminal).__name__):
                expected = (
                    KeyboardInterrupt
                    if isinstance(terminal, KeyboardInterrupt)
                    else CommentInsightFailure
                )
                with self.assertRaises(expected):
                    store.read()
                self.assertEqual(len(native.mac.freed), 1)
                self.assertEqual(len(native.core.released), 1)

    def test_windows_partial_read_is_always_freed(self):
        for terminal in (5, KeyboardInterrupt()):
            native = FakeCtypes()
            native.windows.secret = b"partial-native-secret"
            original = native.windows.CredReadW.callback

            def partial_read(*args, outcome=terminal):
                self.assertEqual(original(*args), 1)
                if isinstance(outcome, BaseException):
                    raise outcome
                native.windows.last_error = outcome
                return 0

            native.windows.CredReadW = FakeFunction(partial_read)
            store = CommentSecretStore(
                platform_name="win32", ctypes_module=native
            )

            with self.subTest(terminal=type(terminal).__name__):
                expected = (
                    KeyboardInterrupt
                    if isinstance(terminal, KeyboardInterrupt)
                    else CommentInsightFailure
                )
                with self.assertRaises(expected):
                    store.read()
                self.assertEqual(len(native.windows.freed), 1)

    def test_native_reads_reject_control_text_after_releasing_memory(self):
        for platform_name, attribute in (
            ("darwin", "mac"),
            ("win32", "windows"),
        ):
            for secret in (
                b"bad\0secret",
                b"bad\x7fsecret",
                "bad\u0085secret".encode("utf-8"),
            ):
                native = FakeCtypes()
                getattr(native, attribute).secret = secret
                store = CommentSecretStore(
                    platform_name=platform_name,
                    ctypes_module=native,
                )

                with self.subTest(platform=platform_name, secret=secret):
                    with self.assertRaises(CommentInsightFailure) as raised:
                        store.read()
                    self.assertEqual(
                        raised.exception.error_code, "comment_ai_not_configured"
                    )
                    if platform_name == "darwin":
                        self.assertEqual(len(native.mac.freed), 1)
                        self.assertEqual(len(native.core.released), 1)
                    else:
                        self.assertEqual(len(native.windows.freed), 1)

    def test_macos_read_releases_before_decode_for_standard_and_control_failures(self):
        secret = "mac-native-read-private-marker"
        for retained, expected, expected_code in (
            (RuntimeError("safe-mac-release-failure"), CommentInsightFailure, None),
            (SystemExit(37), SystemExit, 37),
        ):
            native = FakeCtypes()
            native.mac.secret = secret
            original_release = native.core.CFRelease.callback

            def failing_release(value, *, error=retained):
                original_release(value)
                raise error

            native.core.CFRelease = FakeFunction(failing_release)
            store = CommentSecretStore(
                platform_name="darwin",
                ctypes_module=native,
            )

            with self.subTest(error=type(retained).__name__):
                with self.assertRaises(expected) as raised:
                    store.read()
                if expected is CommentInsightFailure:
                    self.assertEqual(
                        raised.exception.error_code,
                        "comment_ai_not_configured",
                    )
                else:
                    self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(len(native.mac.freed), 1)
                self.assertEqual(len(native.core.released), 1)
                self.assertIsNotNone(retained.__traceback__)
                self.assertNotIn(secret, exception_trace_text(retained))
                self.assertNotIn(secret, exception_trace_text(raised.exception))

    def test_windows_read_frees_before_decode_for_standard_and_control_failures(self):
        secret = "windows-native-read-private-marker"
        for retained, expected, expected_code in (
            (RuntimeError("safe-win-free-failure"), CommentInsightFailure, None),
            (SystemExit(37), SystemExit, 37),
        ):
            native = FakeCtypes()
            native.windows.secret = secret
            original_free = native.windows.CredFree.callback

            def failing_free(pointer, *, error=retained):
                original_free(pointer)
                raise error

            native.windows.CredFree = FakeFunction(failing_free)
            store = CommentSecretStore(
                platform_name="win32",
                ctypes_module=native,
            )

            with self.subTest(error=type(retained).__name__):
                with self.assertRaises(expected) as raised:
                    store.read()
                if expected is CommentInsightFailure:
                    self.assertEqual(
                        raised.exception.error_code,
                        "comment_ai_not_configured",
                    )
                else:
                    self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(len(native.windows.freed), 1)
                self.assertIsNotNone(retained.__traceback__)
                self.assertNotIn(secret, exception_trace_text(retained))
                self.assertNotIn(secret, exception_trace_text(raised.exception))

    def test_macos_read_attempts_all_cleanup_steps_and_keeps_first_control(self):
        secret = "mac-multiple-cleanup-private-marker"
        native = FakeCtypes()
        native.mac.secret = secret
        first = KeyboardInterrupt()
        second = SystemExit(37)
        original_free = native.mac.SecKeychainItemFreeContent.callback
        original_release = native.core.CFRelease.callback

        def failing_free(*args):
            original_free(*args)
            raise first

        def failing_release(value):
            original_release(value)
            raise second

        native.mac.SecKeychainItemFreeContent = FakeFunction(failing_free)
        native.core.CFRelease = FakeFunction(failing_release)
        store = CommentSecretStore(
            platform_name="darwin",
            ctypes_module=native,
        )

        with self.assertRaises(KeyboardInterrupt) as raised:
            store.read()

        self.assertEqual(raised.exception.args, ())
        self.assertEqual(len(native.mac.freed), 1)
        self.assertEqual(len(native.core.released), 1)
        for retained in (first, second):
            self.assertIsNotNone(retained.__traceback__)
            self.assertNotIn(secret, exception_trace_text(retained))

    def test_macos_write_and_delete_do_not_copy_the_old_secret(self):
        for operation in ("write", "delete"):
            native = FakeCtypes()
            native.mac.secret = "old-secret-must-not-be-copied"
            copied_old_secret = []

            def tracking_string_at(pointer, length):
                address = ctypes.cast(pointer, ctypes.c_void_p).value
                returned = {
                    ctypes.addressof(value) for value in native.mac.returned
                }
                if address in returned:
                    copied_old_secret.append(address)
                return ctypes.string_at(pointer, length)

            native.string_at = tracking_string_at
            store = CommentSecretStore(
                platform_name="darwin", ctypes_module=native
            )

            with self.subTest(operation=operation):
                if operation == "write":
                    store.write("new-secret")
                else:
                    store.delete()
                self.assertEqual(copied_old_secret, [])

    def test_macos_write_and_delete_keep_operation_control_over_release_control(self):
        for operation in ("write", "delete"):
            native = FakeCtypes()
            native.mac.secret = "old-secret"
            first = KeyboardInterrupt()
            later = SystemExit(37)
            retained_input = []
            original_release = native.core.CFRelease.callback

            if operation == "write":
                original_operation = (
                    native.mac.SecKeychainItemModifyAttributesAndData.callback
                )

                def interrupted_operation(*args):
                    original_operation(*args)
                    retained_input.append(native.mac.mutable_views[-1])
                    raise first

                native.mac.SecKeychainItemModifyAttributesAndData = FakeFunction(
                    interrupted_operation
                )
            else:
                original_operation = native.mac.SecKeychainItemDelete.callback

                def interrupted_operation(*args):
                    original_operation(*args)
                    raise first

                native.mac.SecKeychainItemDelete = FakeFunction(interrupted_operation)

            def interrupted_release(value):
                original_release(value)
                raise later

            native.core.CFRelease = FakeFunction(interrupted_release)
            store = CommentSecretStore(
                platform_name="darwin",
                ctypes_module=native,
            )

            with self.subTest(operation=operation):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    if operation == "write":
                        store.write("mac-write-private-marker")
                    else:
                        store.delete()

                self.assertEqual(raised.exception.args, ())
                self.assertEqual(len(native.core.released), 1)
                self.assertIsNotNone(first.__traceback__)
                self.assertIsNotNone(later.__traceback__)
                if retained_input:
                    self.assertTrue(
                        all(
                            bytes(view) == b"\0" * len(view)
                            for view in retained_input
                        )
                    )
                for retained in (first, later):
                    self.assertNotIn(
                        "mac-write-private-marker",
                        exception_trace_text(retained),
                    )

    def test_process_control_is_never_converted_to_configuration_failure(self):
        for control_type in (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            store = CommentSecretStore(
                platform_name="darwin",
                ctypes_module=FakeCtypes(mac_fail=control_type()),
            )
            with self.subTest(control=control_type.__name__):
                with self.assertRaises(control_type):
                    store.read()

    def test_secret_store_system_exit_preserves_only_safe_integer_code(self):
        cases = ((37, 37), ("native-system-exit-sensitive", 1))
        for original_code, expected_code in cases:
            original = SystemExit(original_code)
            store = CommentSecretStore(
                platform_name="darwin",
                ctypes_module=FakeCtypes(mac_fail=original),
            )
            with self.subTest(original_code=repr(original_code)):
                caught = None
                try:
                    store.read()
                except SystemExit as error:
                    caught = error
                self.assertIsNotNone(caught)
                self.assertEqual(caught.code, expected_code)
                self.assertEqual(type(caught.code), int)
                self.assertNotIn(
                    "native-system-exit-sensitive",
                    exception_trace_text(caught),
                )
                self.assertEqual(original.args, (original_code,))
                self.assertIsNotNone(original.__traceback__)


class CommentAiProviderTests(unittest.TestCase):
    def provider(self, session, *, base_url="https://ai.example.com/v1", secret="sk-private"):
        return OpenAiCompatibleCommentProvider(
            settings=CommentAiSettings(base_url, "model-x"),
            secret=secret,
            session_factory=lambda: session,
        )

    def test_constructor_never_raises_with_secret_in_its_traceback(self):
        secret = "constructor-secret-private-marker"
        cases = (
            (None, secret, lambda: RecordingSession()),
            (
                CommentAiSettings("https://ai.example.com/v1", "model-x"),
                "bad-\ud800-secret",
                lambda: RecordingSession(),
            ),
            (
                CommentAiSettings("https://ai.example.com/v1", "model-x"),
                "bad-\u0085-secret",
                lambda: RecordingSession(),
            ),
            (
                CommentAiSettings("https://ai.example.com/v1", "model-x"),
                secret,
                None,
            ),
        )

        for settings, candidate_secret, session_factory in cases:
            with self.subTest(secret=repr(candidate_secret), settings=settings):
                constructor_error = None
                provider = None
                try:
                    provider = OpenAiCompatibleCommentProvider(
                        settings=settings,
                        secret=candidate_secret,
                        session_factory=session_factory,
                    )
                except BaseException as error:
                    constructor_error = error
                if constructor_error is not None:
                    self.assertNotIn(
                        candidate_secret,
                        exception_trace_text(constructor_error),
                    )
                    self.fail("constructor raised before sensitive arguments were cleared")
                caught = None
                try:
                    provider.analyze("作品标题", (comment(),))
                except CommentInsightFailure as error:
                    caught = error
                self.assertIsNotNone(caught)
                self.assertEqual(caught.error_code, "comment_ai_not_configured")
                self.assertNotIn(candidate_secret, exception_trace_text(caught))

    def test_constructor_control_is_deferred_and_rebuilt_without_secret(self):
        secret = "constructor-control-secret"
        original = SystemExit(37)
        settings = CommentAiSettings("https://ai.example.com/v1", "model-x")

        with patch(
            "app_core.platform_data_comment_ai._provider_secret_buffer",
            side_effect=original,
            create=True,
        ):
            provider = OpenAiCompatibleCommentProvider(
                settings=settings,
                secret=secret,
                session_factory=lambda: RecordingSession(),
            )

        caught = None
        try:
            provider.analyze("作品标题", (comment(),))
        except SystemExit as error:
            caught = error
        self.assertIsNotNone(caught)
        self.assertEqual(caught.code, 37)
        self.assertNotIn(secret, exception_trace_text(caught))
        self.assertEqual(original.args, (37,))
        self.assertIsNotNone(original.__traceback__)
        self.assertNotIn(secret, exception_trace_text(original))

    def test_constructor_control_after_secret_transfer_zeros_and_detaches_secret(self):
        secret = "constructor-transferred-secret-private-marker"

        for original, expected, expected_code in (
            (
                RuntimeError("safe-constructor-failure"),
                CommentInsightFailure,
                None,
            ),
            (KeyboardInterrupt(), KeyboardInterrupt, None),
            (SystemExit(37), SystemExit, 37),
        ):
            transferred_buffers = []

            class InterruptAfterTransferProvider(OpenAiCompatibleCommentProvider):
                @property
                def model_name(self):
                    return self._model_name

                @model_name.setter
                def model_name(self, value):
                    self._model_name = value
                    if value != "model-x":
                        return
                    count = getattr(self, "_model_assignment_count", 0) + 1
                    self._model_assignment_count = count
                    if count == 2:
                        transferred_buffers.append(self._secret)
                        raise original

            provider = InterruptAfterTransferProvider(
                settings=CommentAiSettings(
                    "https://ai.example.com/v1",
                    "model-x",
                ),
                secret=secret,
                session_factory=lambda: RecordingSession(),
            )
            transferred = transferred_buffers[0]

            with self.subTest(control=type(original).__name__):
                with self.assertRaises(expected) as raised:
                    provider.analyze("作品标题", (comment(),))
                if expected is SystemExit:
                    self.assertEqual(raised.exception.code, expected_code)
                elif expected is CommentInsightFailure:
                    self.assertEqual(
                        raised.exception.error_code,
                        "comment_ai_not_configured",
                    )
                self.assertIsNone(provider._secret)
                self.assertIsInstance(transferred, bytearray)
                self.assertEqual(bytes(transferred), b"\0" * len(transferred))
                self.assertNotIn(secret, exception_trace_text(raised.exception))
                self.assertNotIn(secret, exception_trace_text(original))

    def test_request_contains_only_allowed_comment_data_and_uses_one_owned_send(self):
        session = RecordingSession()
        provider = self.provider(session)

        result = provider.analyze("作品标题", (comment(),))

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.send_calls, 1)
        self.assertEqual(session.post_calls, 0)
        call = session.calls[0]
        self.assertIs(call["request"], session.sent_request)
        self.assertIs(type(session.sent_request), requests.PreparedRequest)
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://ai.example.com/v1/chat/completions")
        self.assertEqual(call["timeout"], (10, 45))
        self.assertIs(call["stream"], True)
        self.assertIs(call["allow_redirects"], False)
        self.assertIs(session.trust_env, False)
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk-private")
        encoded = json.dumps(call["json"], ensure_ascii=False)
        instruction = json.loads(call["json"]["messages"][0]["content"])
        schema = instruction["schema"]
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["required"], ["classifications", "candidates"])
        self.assertEqual(
            schema["properties"]["classifications"]["minItems"], 1
        )
        self.assertEqual(
            schema["properties"]["classifications"]["maxItems"], 1
        )
        self.assertEqual(schema["properties"]["candidates"]["maxItems"], 5)
        self.assertEqual(
            schema["properties"]["candidates"]["items"]["properties"]
            ["evidenceRefs"]["items"]["enum"],
            ["C001"],
        )
        self.assertIn("作品标题", encoded)
        self.assertIn("C001", encoded)
        self.assertIn("为什么会这样？", encoded)
        for label in ("质疑", "认同", "真实经历", "追问", "选题建议", "其他"):
            self.assertIn(label, encoded)
        for forbidden in (
            COMMENT_KEY_A,
            "private-work-id",
            "accountId",
            "contentId",
            "commentKey",
            "likeCount",
            "replyCount",
            "commentedAt",
            OBSERVED,
            "777",
            "888",
            "sk-private",
            "cookie",
            "login",
            "machine",
            "path",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(result.candidates[0].evidence_keys, (COMMENT_KEY_A,))
        self.assertEqual(result.classifications[0].comment_key, COMMENT_KEY_A)
        self.assertTrue(session.closed)
        self.assertTrue(session.outcome.closed)

    def test_local_refs_are_mapped_only_after_complete_valid_response(self):
        contract = insight_contract(
            classifications=[
                {"ref": "C001", "labels": ["追问", "质疑"]},
                {"ref": "C002", "labels": ["真实经历"]},
            ],
            candidates=[
                {
                    "title": "两条评论的共同问题",
                    "reason": "同时引用两条原评论",
                    "evidenceRefs": ["C001", "C002"],
                }
            ],
        )
        session = RecordingSession(FakeResponse(response_bytes(contract)))

        result = self.provider(session).analyze(
            "作品标题",
            (comment(), comment(COMMENT_KEY_B, "我真的遇到过。")),
        )

        self.assertEqual(
            tuple(item.comment_key for item in result.classifications),
            (COMMENT_KEY_A, COMMENT_KEY_B),
        )
        self.assertEqual(
            result.candidates[0].evidence_keys,
            (COMMENT_KEY_A, COMMENT_KEY_B),
        )
        self.assertEqual(result.known_comment_keys, frozenset({COMMENT_KEY_A, COMMENT_KEY_B}))

    def test_response_contract_rejects_unknown_fields_labels_and_malformed_scalars(self):
        invalid_contracts = (
            (insight_contract(extra={"raw": "not-allowed"}), "comment_ai_response_invalid"),
            (
                insight_contract(
                    classifications=[
                        {"ref": "C001", "labels": ["追问"], "raw": "not-allowed"}
                    ]
                ),
                "comment_ai_response_invalid",
            ),
            (
                insight_contract(
                    candidates=[
                        {
                            "title": "题目",
                            "reason": "理由",
                            "evidenceRefs": ["C001"],
                            "raw": "not-allowed",
                        }
                    ]
                ),
                "comment_ai_response_invalid",
            ),
            (
                insight_contract(
                    classifications=[{"ref": "C001", "labels": ["未知分类"]}]
                ),
                "comment_ai_response_invalid",
            ),
            (
                insight_contract(
                    classifications=[{"ref": "C001", "labels": ["追问", "追问"]}]
                ),
                "comment_ai_response_invalid",
            ),
            (
                insight_contract(
                    candidates=[
                        {"title": True, "reason": "理由", "evidenceRefs": ["C001"]}
                    ]
                ),
                "comment_ai_response_invalid",
            ),
        )
        for contract, expected in invalid_contracts:
            with self.subTest(contract=contract):
                provider = self.provider(
                    RecordingSession(FakeResponse(response_bytes(contract)))
                )
                with self.assertRaises(CommentInsightFailure) as raised:
                    provider.analyze("作品标题", (comment(),))
                self.assertEqual(raised.exception.error_code, expected)

    def test_response_contract_requires_exact_classification_coverage(self):
        cases = (
            [],
            [
                {"ref": "C001", "labels": ["追问"]},
                {"ref": "C001", "labels": ["质疑"]},
            ],
            [{"ref": "C999", "labels": ["追问"]}],
        )
        expected_codes = (
            "comment_ai_response_invalid",
            "comment_ai_response_invalid",
            "comment_ai_evidence_invalid",
        )
        for classifications, expected in zip(cases, expected_codes, strict=True):
            with self.subTest(classifications=classifications):
                contract = insight_contract(classifications=classifications)
                provider = self.provider(
                    RecordingSession(FakeResponse(response_bytes(contract)))
                )
                with self.assertRaises(CommentInsightFailure) as raised:
                    provider.analyze("作品标题", (comment(),))
                self.assertEqual(raised.exception.error_code, expected)

    def test_candidate_evidence_rejects_unknown_duplicate_empty_and_sixth_candidate(self):
        candidate = {"title": "题目", "reason": "理由", "evidenceRefs": ["C001"]}
        invalid = (
            (
                [{"title": "题目", "reason": "理由", "evidenceRefs": ["C999"]}],
                "comment_ai_evidence_invalid",
            ),
            (
                [
                    {
                        "title": "题目",
                        "reason": "理由",
                        "evidenceRefs": ["C001", "C001"],
                    }
                ],
                "comment_ai_evidence_invalid",
            ),
            (
                [{"title": "题目", "reason": "理由", "evidenceRefs": []}],
                "comment_ai_evidence_invalid",
            ),
            ([dict(candidate), dict(candidate)], "comment_ai_response_invalid"),
            ([dict(candidate) for _ in range(6)], "comment_ai_response_invalid"),
        )
        for candidates, expected in invalid:
            with self.subTest(candidates=candidates):
                contract = insight_contract(candidates=candidates)
                provider = self.provider(
                    RecordingSession(FakeResponse(response_bytes(contract)))
                )
                with self.assertRaises(CommentInsightFailure) as raised:
                    provider.analyze("作品标题", (comment(),))
                self.assertEqual(raised.exception.error_code, expected)

        reordered = insight_contract(
            classifications=[
                {"ref": "C001", "labels": ["追问"]},
                {"ref": "C002", "labels": ["认同"]},
            ],
            candidates=[
                {
                    "title": "题目",
                    "reason": "理由",
                    "evidenceRefs": ["C001", "C002"],
                },
                {
                    "title": "题目",
                    "reason": "理由",
                    "evidenceRefs": ["C002", "C001"],
                },
            ],
        )
        provider = self.provider(
            RecordingSession(FakeResponse(response_bytes(reordered)))
        )
        with self.assertRaises(CommentInsightFailure) as reordered_raised:
            provider.analyze(
                "作品标题",
                (comment(COMMENT_KEY_A), comment(COMMENT_KEY_B)),
            )
        self.assertEqual(
            reordered_raised.exception.error_code,
            "comment_ai_response_invalid",
        )

    def test_http_timeout_network_and_json_failures_have_only_fixed_codes(self):
        marker = "private-request-url-secret-response"
        cases = (
            (requests.exceptions.Timeout(marker), "comment_ai_timeout"),
            (TimeoutError(marker), "comment_ai_timeout"),
            (requests.exceptions.ConnectionError(marker), "comment_ai_service_unavailable"),
            (RuntimeError(marker), "comment_ai_service_unavailable"),
            (FakeResponse(status_code=504), "comment_ai_timeout"),
            (FakeResponse(status_code=503), "comment_ai_service_unavailable"),
            (FakeResponse(status_code=401), "comment_ai_service_unavailable"),
            (FakeResponse(b"not-json"), "comment_ai_response_invalid"),
            (
                FakeResponse(response_bytes(), content_type="text/plain"),
                "comment_ai_response_invalid",
            ),
        )
        for outcome, expected in cases:
            with self.subTest(outcome=outcome, expected=expected):
                session = RecordingSession(outcome)
                provider = self.provider(session, secret="sk-marker-private")
                with self.assertRaises(CommentInsightFailure) as raised:
                    provider.analyze("作品标题", (comment(),))
                self.assertEqual(raised.exception.error_code, expected)
                self.assertEqual(str(raised.exception), expected)
                self.assertNotIn(marker, repr(raised.exception))
                self.assertNotIn("sk-marker-private", repr(raised.exception))
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(session.closed)

    def test_failure_traceback_and_exception_chain_expose_no_ai_inputs(self):
        secret = "sk-ai-trace-private"
        title = "title-trace-private"
        body = "body-trace-private"
        comment_key = "d" * 64
        raw_marker = "raw-response-trace-private"
        network_error = RuntimeError("network-context-trace-private")
        cases = (
            RecordingSession(network_error),
            RecordingSession(
                FakeResponse(
                    ("{\"broken\":\"" + raw_marker + "\"").encode("utf-8")
                )
            ),
        )

        for session in cases:
            provider = self.provider(session, secret=secret)
            with self.subTest(outcome=type(session.outcome).__name__):
                caught = None
                try:
                    provider.analyze(title, (comment(comment_key, body),))
                except CommentInsightFailure as error:
                    caught = error
                self.assertIsNotNone(caught)
                visible = exception_trace_text(caught)
                for forbidden in (
                    secret,
                    title,
                    body,
                    comment_key,
                    raw_marker,
                    "network-context-trace-private",
                ):
                    self.assertNotIn(forbidden, visible)
                self.assertIsNone(caught.__context__)
                self.assertIsNone(caught.__cause__)
                if isinstance(session.outcome, BaseException):
                    self.assertEqual(
                        session.outcome.args,
                        ("network-context-trace-private",),
                    )
                    self.assertIsNotNone(session.outcome.__traceback__)
                    self.assertIsNone(session.outcome.__context__)
                    self.assertIsNone(session.outcome.__cause__)
                trace = caught.__traceback__
                while trace is not None:
                    if trace.tb_frame.f_code.co_name == "analyze":
                        self.assertIsNone(trace.tb_frame.f_locals.get("self"))
                    trace = trace.tb_next

    def test_process_control_is_rebuilt_without_sensitive_traceback_locals(self):
        for control_type in (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            marker = f"{control_type.__name__}-private-control"
            session = RecordingSession(control_type(marker))
            provider = self.provider(session, secret="sk-control-private")

            with self.subTest(control=control_type.__name__):
                caught = None
                try:
                    provider.analyze(
                        "title-control-private",
                        (comment("e" * 64, "body-control-private"),),
                    )
                except control_type as error:
                    caught = error
                self.assertIsNotNone(caught)
                visible = exception_trace_text(caught)
                for forbidden in (
                    marker,
                    "sk-control-private",
                    "title-control-private",
                    "body-control-private",
                    "e" * 64,
                ):
                    self.assertNotIn(forbidden, visible)
                self.assertIsNone(caught.__context__)
                self.assertIsNone(caught.__cause__)
                self.assertEqual(session.outcome.args, (marker,))
                self.assertIsNotNone(session.outcome.__traceback__)
                self.assertIsNone(session.outcome.__context__)
                self.assertIsNone(session.outcome.__cause__)

    def test_system_exit_preserves_safe_integer_and_redacts_sensitive_code(self):
        cases = ((37, 37), ("system-exit-sensitive-marker", 1))
        for original_code, expected_code in cases:
            original = SystemExit(original_code)
            provider = self.provider(RecordingSession(original))
            with self.subTest(original_code=repr(original_code)):
                caught = None
                try:
                    provider.analyze("作品标题", (comment(),))
                except SystemExit as error:
                    caught = error
                self.assertIsNotNone(caught)
                self.assertEqual(caught.code, expected_code)
                self.assertEqual(type(caught.code), int)
                self.assertNotIn(
                    "system-exit-sensitive-marker",
                    exception_trace_text(caught),
                )
                self.assertEqual(original.args, (original_code,))
                self.assertIsNotNone(original.__traceback__)

    def test_external_request_exception_keeps_its_chain_but_loses_owned_request_data(self):
        secret = "retained-request-secret"
        title = "retained-request-title"
        body = "retained-request-body"
        session = RetainedRequestFailureSession()
        provider = self.provider(session, secret=secret)

        caught = None
        try:
            provider.analyze(title, (comment(body=body),))
        except CommentInsightFailure as error:
            caught = error

        self.assertIsNotNone(caught)
        self.assertEqual(caught.error_code, "comment_ai_service_unavailable")
        self.assertEqual(session.error.args, ("safe-request-failure",))
        self.assertIsNotNone(session.error.__traceback__)
        self.assertIs(session.error.__context__, session.context)
        self.assertIs(session.error.__cause__, session.cause)
        self.assertEqual(session.calls, 1)
        self.assertIs(session.error.request, session.request)
        self.assertIs(type(session.request), requests.PreparedRequest)
        self.assertNotIn("Authorization", session.request.headers)
        self.assertNotIn("Proxy-Authorization", session.request.headers)
        self.assertIsNone(session.request.body)
        self.assertIsNone(session.response.__dict__.get("request"))
        self.assertTrue(session.closed)
        self.assertTrue(session.response.closed_by_provider)
        visible_public = exception_trace_text(caught)
        visible_external = exception_trace_text(session.error)
        for forbidden in (secret, title, body):
            self.assertNotIn(forbidden, visible_public)
            self.assertNotIn(forbidden, visible_external)

    def test_retained_json_decode_error_keeps_chain_without_raw_document(self):
        raw_marker = "retained-json-raw-private-marker"
        retained = json.JSONDecodeError("safe-json-failure", raw_marker, 0)
        safe_cause = ValueError("safe-json-cause")
        safe_context = RuntimeError("safe-json-context")
        retained.__cause__ = safe_cause
        retained.__context__ = safe_context
        original_args = retained.args
        provider = self.provider(RecordingSession(FakeResponse()))

        with patch(
            "app_core.platform_data_comment_ai._loads_json",
            side_effect=retained,
        ):
            with self.assertRaises(CommentInsightFailure) as raised:
                provider.analyze("作品标题", (comment(),))

        self.assertEqual(
            raised.exception.error_code,
            "comment_ai_response_invalid",
        )
        self.assertEqual(retained.args, original_args)
        self.assertIsNotNone(retained.__traceback__)
        self.assertIs(retained.__cause__, safe_cause)
        self.assertIs(retained.__context__, safe_context)
        self.assertNotIn(raw_marker, retained.doc)
        self.assertNotIn(raw_marker, exception_trace_text(raised.exception))

    def test_inner_content_json_decode_error_uses_same_narrow_cleanup_boundary(self):
        raw_marker = "retained-inner-json-private-marker"
        retained = json.JSONDecodeError("safe-inner-json-failure", raw_marker, 0)
        safe_cause = ValueError("safe-inner-json-cause")
        safe_context = RuntimeError("safe-inner-json-context")
        retained.__cause__ = safe_cause
        retained.__context__ = safe_context
        original_args = retained.args
        outer = json.loads(response_bytes())
        provider = self.provider(RecordingSession(FakeResponse()))

        with patch(
            "app_core.platform_data_comment_ai._loads_json",
            side_effect=(outer, retained),
        ):
            with self.assertRaises(CommentInsightFailure) as raised:
                provider.analyze("作品标题", (comment(),))

        self.assertEqual(
            raised.exception.error_code,
            "comment_ai_response_invalid",
        )
        self.assertEqual(retained.args, original_args)
        self.assertIsNotNone(retained.__traceback__)
        self.assertIs(retained.__cause__, safe_cause)
        self.assertIs(retained.__context__, safe_context)
        self.assertNotIn(raw_marker, retained.doc)
        self.assertNotIn(raw_marker, exception_trace_text(raised.exception))

    def test_cleanup_controls_never_skip_owned_request_or_resource_cleanup(self):
        cases = (
            (
                KeyboardInterrupt(),
                SystemExit(91),
                KeyboardInterrupt,
                None,
            ),
            (
                SystemExit(37),
                KeyboardInterrupt(),
                SystemExit,
                37,
            ),
            (
                None,
                KeyboardInterrupt(),
                KeyboardInterrupt,
                None,
            ),
        )
        secret = "cleanup-secret-private-marker"
        title = "cleanup-title-private-marker"
        body = "cleanup-body-private-marker"
        comment_key = "e" * 64

        for (
            response_close_control,
            session_close_control,
            expected,
            expected_code,
        ) in cases:
            response = CleanupControlResponse(
                close_control=response_close_control,
            )
            session = CleanupControlSession(
                response,
                close_control=session_close_control,
            )
            provider = self.provider(session, secret=secret)
            secret_buffer = provider._secret

            with self.subTest(
                response_close=type(response_close_control).__name__,
                session_close=type(session_close_control).__name__,
            ):
                with self.assertRaises(expected) as raised:
                    provider.analyze(
                        title,
                        (comment(key=comment_key, body=body),),
                    )
                if expected is SystemExit:
                    self.assertEqual(raised.exception.code, expected_code)
                prepared = session.request
                self.assertIs(type(prepared), requests.PreparedRequest)
                self.assertIs(session.response_request_before_cleanup, prepared)
                self.assertNotIn("Authorization", prepared.headers)
                self.assertNotIn("Proxy-Authorization", prepared.headers)
                self.assertIsNone(prepared.body)
                self.assertIsNone(response.__dict__.get("request"))
                self.assertIsNone(response.__dict__.get("_content"))
                self.assertIsNone(response.__dict__.get("raw"))
                self.assertEqual(len(session.headers), 0)
                self.assertIsNone(session.auth)
                self.assertEqual(session.proxies, {})
                self.assertEqual(bytes(secret_buffer), b"\0" * len(secret_buffer))
                self.assertTrue(response.closed_by_provider)
                self.assertTrue(session.closed)
                visible = exception_trace_text(raised.exception)
                for forbidden in (secret, title, body, comment_key):
                    self.assertNotIn(forbidden, visible)
                for original in (
                    response_close_control,
                    session_close_control,
                ):
                    if original is None or original.__traceback__ is None:
                        continue
                    original_visible = exception_trace_text(original)
                    for forbidden in (secret, title, body, comment_key):
                        self.assertNotIn(forbidden, original_visible)

    def test_owned_request_ignores_one_shot_and_permanent_response_request_access(self):
        cases = (
            (RuntimeError("safe-one-shot-getter"), False),
            (KeyboardInterrupt(), True),
        )
        for retained, permanent in cases:
            setter = KeyboardInterrupt()
            response = CleanupControlResponse(
                request_control=retained,
                request_control_permanent=permanent,
                request_set_control=setter,
            )
            session = CleanupControlSession(response)
            provider = self.provider(session, secret="owned-secret-private-marker")

            with self.subTest(
                getter=type(retained).__name__,
                permanent=permanent,
            ):
                result = provider.analyze("owned-title", (comment(),))
                self.assertEqual(len(result.classifications), 1)
                self.assertEqual(session.calls, 1)
                self.assertIs(type(session.request), requests.PreparedRequest)
                self.assertIs(
                    session.response_request_before_cleanup,
                    session.request,
                )
                self.assertEqual(response.request_access_count, 0)
                self.assertEqual(response.request_set_count, 0)
                self.assertNotIn("Authorization", session.request.headers)
                self.assertNotIn("Proxy-Authorization", session.request.headers)
                self.assertIsNone(session.request.body)
                self.assertIsNone(response.__dict__.get("request"))
                self.assertEqual(len(session.headers), 0)
                self.assertIsNone(session.auth)
                self.assertEqual(session.proxies, {})
                self.assertTrue(response.closed_by_provider)
                self.assertTrue(session.closed)
                self.assertIsNone(retained.__traceback__)
                self.assertIsNone(setter.__traceback__)

    def test_owned_request_cleanup_never_compares_or_sizes_external_scalar(self):
        scalar = ExplosiveScalar("verification-scalar-private-marker")
        response = CleanupControlResponse()

        def mutate(request):
            request.__dict__["body"] = scalar
            response.__dict__["request"] = scalar

        session = CleanupControlSession(response, request_mutator=mutate)
        provider = self.provider(session)

        result = provider.analyze("作品标题", (comment(),))

        self.assertEqual(len(result.classifications), 1)
        self.assertEqual(scalar.comparisons, 0)
        self.assertEqual(scalar.length_checks, 0)
        self.assertIsNone(session.request.__dict__.get("body"))
        self.assertIsNone(response.__dict__.get("request"))
        self.assertTrue(response.closed_by_provider)
        self.assertTrue(session.closed)

    def test_standard_response_keeps_the_exact_owned_prepared_request_until_cleanup(self):
        response = requests.Response()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json; charset=utf-8"}
        response._content = response_bytes()
        response._content_consumed = True
        response.raw = RawMarker("standard-raw-private-marker")
        session = RecordingSession(response)
        provider = self.provider(session)

        result = provider.analyze("作品标题", (comment(),))

        self.assertEqual(len(result.classifications), 1)
        self.assertIs(type(session.sent_request), requests.PreparedRequest)
        self.assertIs(session.calls[0]["request"], session.sent_request)
        self.assertNotIn("Authorization", session.sent_request.headers)
        self.assertIsNone(session.sent_request.body)
        self.assertIsNone(response.__dict__.get("request"))
        self.assertIsNone(response.__dict__.get("_content"))
        self.assertIsNone(response.__dict__.get("raw"))
        self.assertEqual(session.send_calls, 1)
        self.assertEqual(session.post_calls, 0)

    def test_prepare_control_can_only_reach_the_owned_cleared_prepared_request(self):
        retained = KeyboardInterrupt()
        prepared_objects = []
        session = RecordingSession()
        provider = self.provider(session, secret="prepare-secret-private-marker")

        def interrupted_prepare(
            current,
            *,
            method,
            url,
            headers,
            data,
        ):
            prepared_objects.append(current)
            current.__dict__["method"] = method
            current.__dict__["url"] = url
            current.__dict__["headers"] = headers
            current.__dict__["body"] = data
            raise retained

        with patch.object(
            requests.PreparedRequest,
            "prepare",
            new=interrupted_prepare,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                provider.analyze("prepare-title-private-marker", (comment(),))

        prepared = prepared_objects[0]
        self.assertIs(type(prepared), requests.PreparedRequest)
        self.assertNotIn("Authorization", prepared.headers)
        self.assertIsNone(prepared.body)
        self.assertEqual(session.send_calls, 0)
        self.assertTrue(session.closed)
        self.assertIsNotNone(retained.__traceback__)
        trace = retained.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_name == "interrupted_prepare":
                self.assertIs(trace.tb_frame.f_locals.get("current"), prepared)
                self.assertEqual(trace.tb_frame.f_locals.get("headers"), {})
                self.assertEqual(
                    bytes(trace.tb_frame.f_locals.get("data")),
                    b"\0" * len(trace.tb_frame.f_locals.get("data")),
                )
            trace = trace.tb_next
        for forbidden in (
            "prepare-secret-private-marker",
            "prepare-title-private-marker",
        ):
            self.assertNotIn(forbidden, exception_trace_text(raised.exception))

    def test_streaming_control_after_private_chunk_returns_only_safe_control(self):
        marker = "streaming-private-response-marker"
        for retained, expected, expected_code in (
            (KeyboardInterrupt(), KeyboardInterrupt, None),
            (SystemExit(37), SystemExit, 37),
        ):
            response = StreamingControlResponse(marker, retained)
            session = RecordingSession(response)
            provider = self.provider(session, secret="streaming-secret-marker")
            secret_buffer = provider._secret
            original_args = retained.args

            with self.subTest(control=type(retained).__name__):
                with self.assertRaises(expected) as raised:
                    provider.analyze("streaming-title-marker", (comment(),))
                if expected is SystemExit:
                    self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(retained.args, original_args)
                self.assertIsNotNone(retained.__traceback__)
                self.assertTrue(response.closed)
                self.assertTrue(session.closed)
                self.assertIsNone(response.__dict__.get("_content"))
                self.assertIsNone(response.__dict__.get("request"))
                self.assertIsNone(response.__dict__.get("raw"))
                self.assertNotIn("Authorization", session.sent_request.headers)
                self.assertIsNone(session.sent_request.body)
                self.assertEqual(bytes(secret_buffer), b"\0" * len(secret_buffer))
                for visible in (
                    exception_trace_text(raised.exception),
                    exception_trace_text(retained),
                ):
                    self.assertNotIn(marker, visible)
                    self.assertNotIn("streaming-secret-marker", visible)
                    self.assertNotIn("streaming-title-marker", visible)
                trace = retained.__traceback__
                while trace is not None:
                    if trace.tb_frame.f_code.co_name == "iter_content":
                        retained_response = trace.tb_frame.f_locals.get("self")
                        self.assertIs(retained_response, response)
                        self.assertIsNone(
                            retained_response.__dict__.get("_content")
                        )
                        self.assertIsNone(
                            retained_response.__dict__.get("request")
                        )
                        self.assertIsNone(retained_response.__dict__.get("raw"))
                    if trace.tb_frame.f_code.co_name == "_validated_response_bytes":
                        for name in ("response", "iterator", "chunk", "error"):
                            self.assertIsNone(trace.tb_frame.f_locals.get(name))
                    trace = trace.tb_next

    def test_close_control_traceback_cannot_reach_closed_resource(self):
        marker = "close-bound-response-private-marker"
        retained = KeyboardInterrupt()
        response = CleanupControlResponse(close_control=retained)
        response._content = marker.encode("utf-8")
        session = CleanupControlSession(response)
        provider = self.provider(session, secret="close-bound-secret-marker")
        secret_buffer = provider._secret

        with self.assertRaises(KeyboardInterrupt) as raised:
            provider.analyze("close-bound-title-marker", (comment(),))

        self.assertTrue(response.closed_by_provider)
        self.assertTrue(session.closed)
        self.assertIsNone(response.__dict__.get("_content"))
        self.assertIsNone(response.__dict__.get("request"))
        self.assertIsNone(response.__dict__.get("raw"))
        self.assertNotIn("Authorization", session.request.headers)
        self.assertIsNone(session.request.body)
        self.assertEqual(bytes(secret_buffer), b"\0" * len(secret_buffer))
        self.assertIsNotNone(retained.__traceback__)
        for visible in (
            exception_trace_text(raised.exception),
            exception_trace_text(retained),
        ):
            self.assertNotIn(marker, visible)
            self.assertNotIn("close-bound-secret-marker", visible)
            self.assertNotIn("close-bound-title-marker", visible)
        trace = retained.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_name == "close":
                retained_response = trace.tb_frame.f_locals.get("self")
                self.assertIs(retained_response, response)
                self.assertIsNone(retained_response.__dict__.get("_content"))
                self.assertIsNone(retained_response.__dict__.get("request"))
                self.assertIsNone(retained_response.__dict__.get("raw"))
            if trace.tb_frame.f_code.co_name == "_close_resource_outcome":
                for name in ("resource", "close", "error"):
                    self.assertIsNone(trace.tb_frame.f_locals.get(name))
            trace = trace.tb_next

    def test_ordinary_close_failure_blocks_success_after_all_carriers_are_cleared(self):
        retained = RuntimeError("safe-close-failure")
        response = CleanupControlResponse(close_control=retained)
        session = CleanupControlSession(response)
        provider = self.provider(session)

        with self.assertRaises(CommentInsightFailure) as raised:
            provider.analyze("作品标题", (comment(),))

        self.assertEqual(
            raised.exception.error_code,
            "comment_ai_service_unavailable",
        )
        self.assertIsNone(response.__dict__.get("_content"))
        self.assertIsNone(response.__dict__.get("request"))
        self.assertIsNone(response.__dict__.get("raw"))
        self.assertNotIn("Authorization", session.request.headers)
        self.assertIsNone(session.request.body)
        self.assertTrue(response.closed_by_provider)
        self.assertTrue(session.closed)

    def test_first_operation_control_survives_later_cleanup_control(self):
        secret = "first-control-secret-private-marker"
        first = KeyboardInterrupt()
        later = SystemExit(91)
        session = CleanupControlSession(
            CleanupControlResponse(),
            send_control=first,
            close_control=later,
        )
        provider = self.provider(session, secret=secret)
        secret_buffer = provider._secret

        with self.assertRaises(KeyboardInterrupt) as raised:
            provider.analyze("作品标题", (comment(),))

        self.assertTrue(session.closed)
        self.assertEqual(bytes(secret_buffer), b"\0" * len(secret_buffer))
        self.assertNotIn(secret, exception_trace_text(raised.exception))
        for original in (first, later):
            self.assertIsNotNone(original.__traceback__)
            self.assertNotIn(secret, exception_trace_text(original))
        trace = later.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_name == "close":
                retained_session = trace.tb_frame.f_locals.get("self")
                self.assertIs(retained_session, session)
                self.assertEqual(len(retained_session.headers), 0)
                self.assertIsNone(retained_session.auth)
                self.assertEqual(retained_session.proxies, {})
                self.assertNotIn("Authorization", retained_session.request.headers)
                self.assertIsNone(retained_session.request.body)
            trace = trace.tb_next

    def test_redirects_and_environment_credentials_are_disabled(self):
        response = FakeResponse(status_code=302)
        session = RecordingSession(response)
        provider = self.provider(session)

        with self.assertRaises(CommentInsightFailure) as raised:
            provider.analyze("作品标题", (comment(),))

        self.assertEqual(
            raised.exception.error_code, "comment_ai_service_unavailable"
        )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            session.calls[0]["url"],
            "https://ai.example.com/v1/chat/completions",
        )
        self.assertIs(session.calls[0]["allow_redirects"], False)
        self.assertIs(session.trust_env, False)
        self.assertTrue(response.closed)
        self.assertTrue(session.closed)

    def test_envelope_optional_fields_require_exact_builtin_types(self):
        invalid_envelopes = []
        for path, value in (
            (("id",), 1),
            (("object",), False),
            (("created",), False),
            (("model",), 1),
            (("usage",), []),
            (("system_fingerprint",), False),
            (("service_tier",), False),
            (("choices", 0, "index"), False),
            (("choices", 0, "logprobs"), []),
            (("choices", 0, "message", "refusal"), False),
        ):
            envelope = json.loads(response_bytes())
            current = envelope
            for segment in path[:-1]:
                current = current[segment]
            current[path[-1]] = value
            invalid_envelopes.append((path, envelope))

        for path, envelope in invalid_envelopes:
            raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
            provider = self.provider(RecordingSession(FakeResponse(raw)))
            with self.subTest(path=path):
                with self.assertRaises(CommentInsightFailure) as raised:
                    provider.analyze("作品标题", (comment(),))
                self.assertEqual(
                    raised.exception.error_code, "comment_ai_response_invalid"
                )

    def test_custom_containers_and_surrogate_response_text_are_rejected(self):
        class CustomDict(dict):
            pass

        provider = self.provider(RecordingSession())
        with patch(
            "app_core.platform_data_comment_ai._loads_json",
            return_value=CustomDict(json.loads(response_bytes())),
        ):
            with self.assertRaises(CommentInsightFailure) as custom_raised:
                provider.analyze("作品标题", (comment(),))
        self.assertEqual(
            custom_raised.exception.error_code, "comment_ai_response_invalid"
        )

        contract = insight_contract(
            candidates=[
                {
                    "title": "bad-\ud800",
                    "reason": "理由",
                    "evidenceRefs": ["C001"],
                }
            ]
        )
        envelope = json.loads(response_bytes())
        envelope["choices"][0]["message"]["content"] = json.dumps(
            contract, ensure_ascii=True
        )
        raw = json.dumps(envelope, ensure_ascii=True).encode("utf-8")
        provider = self.provider(RecordingSession(FakeResponse(raw)))
        with self.assertRaises(CommentInsightFailure) as surrogate_raised:
            provider.analyze("作品标题", (comment(),))
        self.assertEqual(
            surrogate_raised.exception.error_code, "comment_ai_response_invalid"
        )

    def test_input_and_response_have_hard_type_size_depth_and_count_limits(self):
        too_deep = "[" * 200 + "0" + "]" * 200
        deep_contract_response = response_bytes()
        envelope = json.loads(deep_contract_response)
        envelope["choices"][0]["message"]["content"] = too_deep
        deep_raw = json.dumps(envelope).encode()
        cases = (
            ("x" * 501, (comment(),), FakeResponse(), "comment_ai_response_invalid"),
            (
                "作品标题",
                (comment(body="x" * 16_385),),
                FakeResponse(),
                "comment_ai_response_invalid",
            ),
            (
                "作品标题",
                tuple(comment(f"{index:064x}", f"评论 {index}") for index in range(101)),
                FakeResponse(),
                "comment_ai_response_invalid",
            ),
            (
                "作品标题",
                (comment(), comment()),
                FakeResponse(),
                "comment_ai_response_invalid",
            ),
            (
                "作品标题",
                (comment(),),
                FakeResponse(b"{" + b"x" * 262_145 + b"}"),
                "comment_ai_response_invalid",
            ),
            (
                "作品标题",
                (comment(),),
                FakeResponse(deep_raw),
                "comment_ai_response_invalid",
            ),
        )
        for title, comments, response, expected in cases:
            with self.subTest(title_len=len(title), comments=len(comments)):
                session = RecordingSession(response)
                provider = self.provider(session)
                with self.assertRaises(CommentInsightFailure) as raised:
                    provider.analyze(title, comments)
                self.assertEqual(raised.exception.error_code, expected)

    def test_oversized_stream_stops_before_reading_the_whole_response(self):
        response = FakeResponse(b"x" * 2_000_000)
        original_size = len(response.content)
        session = RecordingSession(response)

        with self.assertRaises(CommentInsightFailure) as raised:
            self.provider(session).analyze("作品标题", (comment(),))

        self.assertEqual(raised.exception.error_code, "comment_ai_response_invalid")
        self.assertLess(response.chunks_yielded * 16_384, original_size)
        self.assertIsNone(response.content)
        self.assertTrue(response.closed)

    def test_duplicate_json_fields_and_deep_outer_metadata_are_rejected(self):
        content = json.dumps(insight_contract(), ensure_ascii=False)
        message = json.dumps(
            {"role": "assistant", "content": content}, ensure_ascii=False
        )
        choice = (
            '{"index":0,"message":'
            + message
            + ',"finish_reason":"stop"}'
        )
        duplicate = (
            '{"choices":[' + choice + '],"choices":[' + choice + "]}"
        ).encode("utf-8")
        deep = {"value": 0}
        for _ in range(20):
            deep = {"value": deep}
        envelope = json.loads(response_bytes())
        envelope["usage"] = deep
        deep_raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

        for raw in (duplicate, deep_raw):
            with self.subTest(size=len(raw)):
                provider = self.provider(RecordingSession(FakeResponse(raw)))
                with self.assertRaises(CommentInsightFailure) as raised:
                    provider.analyze("作品标题", (comment(),))
                self.assertEqual(
                    raised.exception.error_code, "comment_ai_response_invalid"
                )

    def test_invalid_unicode_title_is_input_failure_without_network(self):
        session = RecordingSession()
        provider = self.provider(session)

        with self.assertRaises(CommentInsightFailure) as raised:
            provider.analyze("bad-surrogate-\ud800", (comment(),))

        self.assertEqual(raised.exception.error_code, "comment_ai_response_invalid")
        self.assertEqual(session.calls, [])

    def test_provider_inputs_reject_c1_controls_before_network(self):
        cases = (
            ("bad-title-\u0085-private", comment()),
            ("作品标题", comment(body="bad-body-\u0085-private")),
        )
        for title, record in cases:
            session = RecordingSession()
            provider = self.provider(session)
            with self.subTest(title=repr(title), body=repr(record.body)):
                with self.assertRaises(CommentInsightFailure) as raised:
                    provider.analyze(title, (record,))
                self.assertEqual(
                    raised.exception.error_code,
                    "comment_ai_response_invalid",
                )
                self.assertEqual(session.calls, [])

    def test_process_control_is_preserved_and_session_still_closes(self):
        for control_type in (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            session = RecordingSession(control_type())
            provider = self.provider(session)
            with self.subTest(control=control_type.__name__):
                with self.assertRaises(control_type):
                    provider.analyze("作品标题", (comment(),))
                self.assertTrue(session.closed)

    def test_internal_model_failure_cannot_escape_as_a_non_ai_error_code(self):
        provider = self.provider(RecordingSession())

        with patch(
            "app_core.platform_data_comment_ai.InsightResult",
            side_effect=CommentInsightFailure("comment_payload_invalid"),
        ):
            with self.assertRaises(CommentInsightFailure) as raised:
                provider.analyze("作品标题", (comment(),))

        self.assertEqual(raised.exception.error_code, "comment_ai_response_invalid")


if __name__ == "__main__":
    unittest.main()
