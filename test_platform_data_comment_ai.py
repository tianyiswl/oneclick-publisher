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


class RecordingSession:
    def __init__(self, outcome=None) -> None:
        self.outcome = FakeResponse() if outcome is None else outcome
        self.calls = []
        self.closed = False
        self.trust_env = True

    def post(
        self,
        url,
        *,
        headers,
        json,
        timeout,
        stream,
        allow_redirects=None,
    ):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "json": deepcopy(json),
                "timeout": timeout,
                "stream": stream,
                "allow_redirects": allow_redirects,
            }
        )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def close(self):
        self.closed = True


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
        value = self.secret if type(self.secret) is bytes else self.secret.encode("utf-8")
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
        invalid = ("", " ", " model", "model ", "model\n", "x" * 257, 1, True, None)
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
        self.assertIsNone(native_error.__traceback__)
        self.assertIsNone(native_error.__context__)
        self.assertIsNone(native_error.__cause__)
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
            for secret in (b"bad\0secret", b"bad\x7fsecret"):
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

    def test_process_control_is_never_converted_to_configuration_failure(self):
        for control_type in (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            store = CommentSecretStore(
                platform_name="darwin",
                ctypes_module=FakeCtypes(mac_fail=control_type()),
            )
            with self.subTest(control=control_type.__name__):
                with self.assertRaises(control_type):
                    store.read()


class CommentAiProviderTests(unittest.TestCase):
    def provider(self, session, *, base_url="https://ai.example.com/v1", secret="sk-private"):
        return OpenAiCompatibleCommentProvider(
            settings=CommentAiSettings(base_url, "model-x"),
            secret=secret,
            session_factory=lambda: session,
        )

    def test_request_contains_only_allowed_comment_data_and_uses_one_post(self):
        session = RecordingSession()
        provider = self.provider(session)

        result = provider.analyze("作品标题", (comment(),))

        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
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
                    self.assertIsNone(session.outcome.__traceback__)
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
                self.assertIsNone(session.outcome.__traceback__)
                self.assertIsNone(session.outcome.__context__)
                self.assertIsNone(session.outcome.__cause__)

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
        session = RecordingSession(response)

        with self.assertRaises(CommentInsightFailure) as raised:
            self.provider(session).analyze("作品标题", (comment(),))

        self.assertEqual(raised.exception.error_code, "comment_ai_response_invalid")
        self.assertLess(response.chunks_yielded * 16_384, len(response.content))
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
