# -*- coding: utf-8 -*-
"""API Key 的 macOS Keychain / Windows Credential Manager 适配器。"""

from __future__ import annotations

import asyncio
import ctypes as _ctypes
from ctypes import wintypes as _native_wintypes
from dataclasses import dataclass
import sys
import unicodedata

from .platform_data_comment_models import CommentInsightFailure


SERVICE_NAME = "com.oneclickpublisher.comment-insight"
ACCOUNT_NAME = "default"

_MAX_SECRET_BYTES = 8192
_MAC_NOT_FOUND = -25300
_WINDOWS_NOT_FOUND = 1168
_WINDOWS_CREDENTIAL_TYPE_GENERIC = 1
_WINDOWS_CREDENTIAL_PERSIST_LOCAL_MACHINE = 2
_PROCESS_CONTROL = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
_OUTCOME_OK = "ok"
_OUTCOME_FAILURE = "failure"
_OUTCOME_CONTROL = "control"
_COMMIT_COMMITTED = "committed"
_COMMIT_NOT_COMMITTED = "not_committed"
_COMMIT_UNKNOWN = "unknown"


def _not_configured() -> CommentInsightFailure:
    return CommentInsightFailure("comment_ai_not_configured")


def _control_outcome_value(error: BaseException):
    if isinstance(error, asyncio.CancelledError):
        return ("cancelled", None)
    if isinstance(error, KeyboardInterrupt):
        return ("keyboard_interrupt", None)
    code = error.code if isinstance(error, SystemExit) else None
    if type(code) is not int or not -2_147_483_648 <= code <= 2_147_483_647:
        code = 1
    return ("system_exit", code)


@dataclass(frozen=True, slots=True)
class SecretMutationReceipt:
    """Non-sensitive result of a native credential mutation.

    ``commit_state`` distinguishes a proven commit, a proven non-commit, and
    an indeterminate native return boundary. ``committed`` stays compatible
    with the older boolean API while using ``None`` for that indeterminate
    state. All remaining values are fixed tokens or a sanitized integer.
    """

    committed: bool | None
    status: str
    control: str = ""
    exit_code: int | None = None
    commit_state: str = ""

    def __post_init__(self) -> None:
        commit_state = self.commit_state
        if commit_state == "":
            if type(self.committed) is bool:
                commit_state = (
                    _COMMIT_COMMITTED
                    if self.committed
                    else _COMMIT_NOT_COMMITTED
                )
                object.__setattr__(self, "commit_state", commit_state)
            else:
                raise _not_configured()
        expected_committed = {
            _COMMIT_COMMITTED: True,
            _COMMIT_NOT_COMMITTED: False,
            _COMMIT_UNKNOWN: None,
        }.get(commit_state, object())
        if self.committed is not expected_committed or self.status not in {
            "success",
            "failed",
            "control",
        }:
            raise _not_configured()
        if self.status in {"success", "failed"}:
            if self.control != "" or self.exit_code is not None:
                raise _not_configured()
            return
        if self.control not in {
            "cancelled",
            "keyboard_interrupt",
            "system_exit",
        }:
            raise _not_configured()
        if self.control == "system_exit":
            if (
                type(self.exit_code) is not int
                or not -2_147_483_648 <= self.exit_code <= 2_147_483_647
            ):
                raise _not_configured()
        elif self.exit_code is not None:
            raise _not_configured()


def _mutation_receipt(commit_state: str, outcome) -> SecretMutationReceipt:
    committed = {
        _COMMIT_COMMITTED: True,
        _COMMIT_NOT_COMMITTED: False,
        _COMMIT_UNKNOWN: None,
    }.get(commit_state, object())
    if (
        committed is not True
        and committed is not False
        and committed is not None
    ):
        raise _not_configured()
    if outcome is None:
        return SecretMutationReceipt(
            committed,
            "success",
            commit_state=commit_state,
        )
    if outcome[0] == _OUTCOME_CONTROL:
        control, code = outcome[1]
        return SecretMutationReceipt(
            committed,
            "control",
            control,
            code,
            commit_state,
        )
    return SecretMutationReceipt(
        committed,
        "failed",
        commit_state=commit_state,
    )


class _NativeCommitMarker:
    """Fixed-state out marker for injected native mutation boundaries."""

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state = _COMMIT_UNKNOWN

    @property
    def state(self) -> str:
        return self._state

    def mark_committed(self) -> None:
        self._state = _COMMIT_COMMITTED

    def mark_not_committed(self) -> None:
        self._state = _COMMIT_NOT_COMMITTED


def _invoke_native_mutation(function, *arguments):
    """Invoke one mutation and retain only fixed commit/control evidence."""

    marker = _NativeCommitMarker()
    result = None
    outcome = None
    invoker = None
    error = None
    try:
        invoker = getattr(
            function,
            "_oneclick_invoke_with_commit_marker",
            None,
        )
        if invoker is None:
            result = function(*arguments)
        elif callable(invoker):
            result = invoker(marker, *arguments)
        else:
            raise _not_configured()
    except BaseException as error:
        outcome = _error_outcome(error)
    commit_state = marker.state
    marker = None
    function = None
    arguments = None
    invoker = None
    error = None
    return result, commit_state, outcome


def _receipt_outcome(receipt: SecretMutationReceipt):
    if receipt.status == "success":
        return None
    if receipt.status == "control":
        return (_OUTCOME_CONTROL, (receipt.control, receipt.exit_code))
    return (_OUTCOME_FAILURE, None)


def _error_outcome(error: BaseException):
    if isinstance(error, _PROCESS_CONTROL):
        return (_OUTCOME_CONTROL, _control_outcome_value(error))
    return (_OUTCOME_FAILURE, None)


def _first_outcome(current, candidate):
    if current is None:
        return candidate
    if current[0] != _OUTCOME_CONTROL and (
        candidate is not None and candidate[0] == _OUTCOME_CONTROL
    ):
        return candidate
    return current


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
        error = _not_configured()
    error.__traceback__ = None
    error.__context__ = None
    error.__cause__ = None
    raise error from None


def _valid_secret_text(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and bool(value.strip())
        and not any(
            unicodedata.category(character) == "Cc" for character in value
        )
    )


def _secret_buffer(value: object) -> bytearray:
    encoded = None
    try:
        if not _valid_secret_text(value):
            raise _not_configured()
        try:
            encoded = bytearray(value, "utf-8")
        except _PROCESS_CONTROL:
            raise
        except BaseException:
            raise _not_configured() from None
        if not encoded or len(encoded) > _MAX_SECRET_BYTES:
            _zero(encoded)
            encoded = None
            raise _not_configured()
        result = encoded
        encoded = None
        return result
    finally:
        value = None
        if type(encoded) is bytearray:
            _zero(encoded)


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


class CommentSecretStore:
    """只通过操作系统原生 API 读写评论洞察密钥。"""

    def __init__(self, *, platform_name=None, ctypes_module=None) -> None:
        self._platform = sys.platform if platform_name is None else platform_name
        self._ctypes = _ctypes if ctypes_module is None else ctypes_module

    def read(self) -> str | None:
        outcome = self._read_outcome()
        if outcome[0] == _OUTCOME_OK:
            return outcome[1]
        self = None
        _raise_clean_outcome(outcome)

    def is_configured(self) -> bool:
        """Return credential existence without reading or copying its value."""

        outcome = self._configured_outcome()
        if outcome[0] == _OUTCOME_OK:
            return outcome[1]
        self = None
        _raise_clean_outcome(outcome)

    def _configured_outcome(self):
        value = None
        try:
            if self._platform == "darwin":
                value = self._mac_is_configured()
            elif self._platform == "win32":
                value = self._windows_is_configured()
            else:
                raise _not_configured()
            return (_OUTCOME_OK, value)
        except _PROCESS_CONTROL as error:
            return (_OUTCOME_CONTROL, _control_outcome_value(error))
        except BaseException:
            return (_OUTCOME_FAILURE, None)
        finally:
            self = None
            value = None

    def _read_outcome(self):
        try:
            if self._platform == "darwin":
                value = self._mac_read()
            elif self._platform == "win32":
                value = self._windows_read()
            else:
                raise _not_configured()
            return (_OUTCOME_OK, value)
        except _PROCESS_CONTROL as error:
            return (_OUTCOME_CONTROL, _control_outcome_value(error))
        except BaseException:
            return (_OUTCOME_FAILURE, None)
        finally:
            self = None
            value = None

    def write(self, secret: str) -> None:
        receipt = self.write_with_receipt(secret)
        secret = None
        outcome = _receipt_outcome(receipt)
        receipt = None
        if outcome is None:
            return
        self = None
        _raise_clean_outcome(outcome)

    def write_with_receipt(self, secret: str) -> SecretMutationReceipt:
        """Write a credential and report commit separately from cleanup."""

        try:
            return self._write_receipt(secret)
        finally:
            secret = None
            self = None

    def _write_receipt(self, secret) -> SecretMutationReceipt:
        mutable = None
        commit_state = _COMMIT_NOT_COMMITTED
        outcome = None
        error = None
        try:
            mutable = _secret_buffer(secret)
            if self._platform == "darwin":
                commit_state, outcome = self._mac_write(mutable)
            elif self._platform == "win32":
                commit_state, outcome = self._windows_write(mutable)
            else:
                raise _not_configured()
        except BaseException as error:
            outcome = _first_outcome(outcome, _error_outcome(error))
        finally:
            secret = None
            if type(mutable) is bytearray:
                _zero(mutable)
            mutable = None
            self = None
            error = None
        return _mutation_receipt(commit_state, outcome)

    def delete(self) -> None:
        receipt = self.delete_with_receipt()
        outcome = _receipt_outcome(receipt)
        receipt = None
        if outcome is None:
            return
        self = None
        _raise_clean_outcome(outcome)

    def delete_with_receipt(self) -> SecretMutationReceipt:
        """Delete a credential and report commit separately from cleanup."""

        return self._delete_receipt()

    def _delete_receipt(self) -> SecretMutationReceipt:
        commit_state = _COMMIT_NOT_COMMITTED
        outcome = None
        error = None
        try:
            if self._platform == "darwin":
                commit_state, outcome = self._mac_delete()
            elif self._platform == "win32":
                commit_state, outcome = self._windows_delete()
            else:
                raise _not_configured()
        except BaseException as error:
            outcome = _first_outcome(outcome, _error_outcome(error))
        finally:
            self = None
            error = None
        return _mutation_receipt(commit_state, outcome)

    def _mac_libraries(self):
        ctypes = self._ctypes
        security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        core = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        security.SecKeychainFindGenericPassword.restype = ctypes.c_int
        security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainAddGenericPassword.restype = ctypes.c_int
        security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int
        security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        security.SecKeychainItemDelete.restype = ctypes.c_int
        security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        security.SecKeychainItemFreeContent.restype = ctypes.c_int
        security.SecKeychainItemFreeContent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        core.CFRelease.restype = None
        core.CFRelease.argtypes = [ctypes.c_void_p]
        return security, core

    def _mac_names(self):
        ctypes = self._ctypes
        service = ctypes.create_string_buffer(SERVICE_NAME.encode("utf-8"))
        account = ctypes.create_string_buffer(ACCOUNT_NAME.encode("utf-8"))
        return service, account

    def _mac_free_content(self, security, password_data) -> None:
        if getattr(password_data, "value", None):
            if security.SecKeychainItemFreeContent(None, password_data) != 0:
                raise _not_configured()

    def _mac_cleanup_outcome(
        self,
        security,
        core,
        password_data,
        item_ref,
        *,
        release_item,
    ):
        outcome = None
        try:
            self._mac_free_content(security, password_data)
        except BaseException as error:
            outcome = _first_outcome(outcome, _error_outcome(error))
        if release_item:
            try:
                self._mac_release_item(core, item_ref)
            except BaseException as error:
                outcome = _first_outcome(outcome, _error_outcome(error))
        self = None
        security = None
        core = None
        password_data = None
        item_ref = None
        return outcome

    def _mac_release_outcome(self, core, item_ref):
        try:
            self._mac_release_item(core, item_ref)
            return None
        except BaseException as error:
            return _error_outcome(error)
        finally:
            self = None
            core = None
            item_ref = None

    def _mac_find(self, security, core, *, read_secret):
        ctypes = self._ctypes
        service, account = self._mac_names()
        password_length = ctypes.c_uint32(0)
        password_data = ctypes.c_void_p()
        item_ref = ctypes.c_void_p()
        try:
            status = security.SecKeychainFindGenericPassword(
                None,
                len(SERVICE_NAME.encode("utf-8")),
                ctypes.cast(service, ctypes.c_void_p),
                len(ACCOUNT_NAME.encode("utf-8")),
                ctypes.cast(account, ctypes.c_void_p),
                ctypes.byref(password_length),
                ctypes.byref(password_data),
                ctypes.byref(item_ref),
            )
        except BaseException as error:
            outcome = _error_outcome(error)
            cleanup_outcome = self._mac_cleanup_outcome(
                security,
                core,
                password_data,
                item_ref,
                release_item=True,
            )
            outcome = _first_outcome(outcome, cleanup_outcome)
            _raise_clean_outcome(outcome)
        if status == _MAC_NOT_FOUND:
            cleanup_outcome = self._mac_cleanup_outcome(
                security,
                core,
                password_data,
                item_ref,
                release_item=True,
            )
            if cleanup_outcome is not None:
                _raise_clean_outcome(cleanup_outcome)
            return None, None
        if status != 0:
            cleanup_outcome = self._mac_cleanup_outcome(
                security,
                core,
                password_data,
                item_ref,
                release_item=True,
            )
            if cleanup_outcome is not None:
                _raise_clean_outcome(cleanup_outcome)
            raise _not_configured()
        native = None
        value = None
        if read_secret:
            if (
                not password_data.value
                or password_length.value <= 0
                or password_length.value > _MAX_SECRET_BYTES
            ):
                cleanup_outcome = self._mac_cleanup_outcome(
                    security,
                    core,
                    password_data,
                    item_ref,
                    release_item=True,
                )
                if cleanup_outcome is not None:
                    _raise_clean_outcome(cleanup_outcome)
                raise _not_configured()
            try:
                native = bytearray(
                    ctypes.string_at(password_data, password_length.value)
                )
            except _PROCESS_CONTROL as error:
                outcome = _error_outcome(error)
            except BaseException:
                outcome = (_OUTCOME_FAILURE, None)
            else:
                outcome = None
            cleanup_outcome = self._mac_cleanup_outcome(
                security,
                core,
                password_data,
                item_ref,
                release_item=True,
            )
            outcome = _first_outcome(outcome, cleanup_outcome)
            if outcome is not None:
                if type(native) is bytearray:
                    _zero(native)
                native = None
                _raise_clean_outcome(outcome)
            try:
                value = native.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                raise _not_configured() from None
            finally:
                _zero(native)
                native = None
            if not _valid_secret_text(value):
                value = None
                raise _not_configured()
            result = value
            value = None
            return result, None

        cleanup_outcome = self._mac_cleanup_outcome(
            security,
            core,
            password_data,
            item_ref,
            release_item=False,
        )
        if cleanup_outcome is not None:
            release_outcome = self._mac_release_outcome(core, item_ref)
            cleanup_outcome = _first_outcome(
                cleanup_outcome,
                release_outcome,
            )
            _raise_clean_outcome(cleanup_outcome)
        return None, item_ref

    def _mac_release_item(self, core, item_ref) -> None:
        if item_ref is not None and getattr(item_ref, "value", None):
            core.CFRelease(item_ref)

    def _mac_read(self) -> str | None:
        security, core = self._mac_libraries()
        value, _item_ref = self._mac_find(
            security,
            core,
            read_secret=True,
        )
        return value

    def _mac_is_configured(self) -> bool:
        ctypes = self._ctypes
        security, core = self._mac_libraries()
        service, account = self._mac_names()
        item_ref = ctypes.c_void_p()
        status = None
        outcome = None
        error = None
        try:
            status = security.SecKeychainFindGenericPassword(
                None,
                len(SERVICE_NAME.encode("utf-8")),
                ctypes.cast(service, ctypes.c_void_p),
                len(ACCOUNT_NAME.encode("utf-8")),
                ctypes.cast(account, ctypes.c_void_p),
                None,
                None,
                ctypes.byref(item_ref),
            )
        except BaseException as error:
            outcome = _first_outcome(outcome, _error_outcome(error))
        release_outcome = self._mac_release_outcome(core, item_ref)
        outcome = _first_outcome(outcome, release_outcome)
        result = status == 0 and bool(item_ref.value)
        missing = status == _MAC_NOT_FOUND and not item_ref.value
        ctypes = None
        security = None
        core = None
        service = None
        account = None
        item_ref = None
        status = None
        error = None
        release_outcome = None
        self = None
        if outcome is not None:
            _raise_clean_outcome(outcome)
        if missing:
            return False
        if not result:
            raise _not_configured()
        return True

    def _mac_write(self, mutable: bytearray):
        ctypes = self._ctypes
        security, core = self._mac_libraries()
        _existing, item_ref = self._mac_find(
            security, core, read_secret=False
        )
        secret_array = (ctypes.c_ubyte * len(mutable)).from_buffer(mutable)
        secret_pointer = ctypes.cast(secret_array, ctypes.c_void_p)
        service = None
        account = None
        status = None
        commit_state = _COMMIT_NOT_COMMITTED
        outcome = None
        if item_ref is not None:
            status, commit_state, call_outcome = _invoke_native_mutation(
                security.SecKeychainItemModifyAttributesAndData,
                item_ref,
                None,
                len(mutable),
                secret_pointer,
            )
        else:
            service, account = self._mac_names()
            status, commit_state, call_outcome = _invoke_native_mutation(
                security.SecKeychainAddGenericPassword,
                None,
                len(SERVICE_NAME.encode("utf-8")),
                ctypes.cast(service, ctypes.c_void_p),
                len(ACCOUNT_NAME.encode("utf-8")),
                ctypes.cast(account, ctypes.c_void_p),
                len(mutable),
                secret_pointer,
                None,
            )
        outcome = _first_outcome(outcome, call_outcome)
        if call_outcome is None:
            if status == 0:
                commit_state = _COMMIT_COMMITTED
            else:
                if commit_state != _COMMIT_COMMITTED:
                    commit_state = _COMMIT_NOT_COMMITTED
                outcome = _first_outcome(
                    outcome,
                    (_OUTCOME_FAILURE, None),
                )
        release_outcome = self._mac_release_outcome(core, item_ref)
        outcome = _first_outcome(outcome, release_outcome)
        ctypes = None
        security = None
        core = None
        _existing = None
        item_ref = None
        mutable = None
        secret_array = None
        secret_pointer = None
        service = None
        account = None
        status = None
        call_outcome = None
        release_outcome = None
        self = None
        return commit_state, outcome

    def _mac_delete(self):
        security, core = self._mac_libraries()
        _existing, item_ref = self._mac_find(
            security, core, read_secret=False
        )
        if item_ref is None:
            return _COMMIT_NOT_COMMITTED, None
        status, commit_state, outcome = _invoke_native_mutation(
            security.SecKeychainItemDelete,
            item_ref,
        )
        if outcome is None:
            if status == 0:
                commit_state = _COMMIT_COMMITTED
            else:
                if commit_state != _COMMIT_COMMITTED:
                    commit_state = _COMMIT_NOT_COMMITTED
                outcome = (_OUTCOME_FAILURE, None)
        release_outcome = self._mac_release_outcome(core, item_ref)
        outcome = _first_outcome(outcome, release_outcome)
        security = None
        core = None
        _existing = None
        item_ref = None
        status = None
        release_outcome = None
        self = None
        return commit_state, outcome

    def _windows_types(self):
        ctypes = self._ctypes
        wintypes = getattr(ctypes, "wintypes", None)
        if wintypes is None and ctypes is _ctypes:
            wintypes = _native_wintypes
        if wintypes is None:
            raise _not_configured()

        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        return Credential, ctypes.POINTER(Credential), wintypes

    def _windows_library(self, credential_type, credential_pointer_type, wintypes):
        ctypes = self._ctypes
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi.CredWriteW.restype = wintypes.BOOL
        advapi.CredWriteW.argtypes = [
            ctypes.POINTER(credential_type),
            wintypes.DWORD,
        ]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(credential_pointer_type),
        ]
        advapi.CredDeleteW.restype = wintypes.BOOL
        advapi.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        advapi.CredFree.restype = None
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        return advapi

    def _windows_free_outcome(self, advapi, credential_pointer):
        try:
            if bool(credential_pointer):
                advapi.CredFree(credential_pointer)
            return None
        except BaseException as error:
            return _error_outcome(error)
        finally:
            self = None
            advapi = None
            credential_pointer = None

    def _windows_read(self) -> str | None:
        ctypes = self._ctypes
        credential_type, credential_pointer_type, wintypes = self._windows_types()
        advapi = self._windows_library(
            credential_type, credential_pointer_type, wintypes
        )
        credential_pointer = credential_pointer_type()
        try:
            succeeded = advapi.CredReadW(
                SERVICE_NAME,
                _WINDOWS_CREDENTIAL_TYPE_GENERIC,
                0,
                ctypes.byref(credential_pointer),
            )
        except BaseException as error:
            outcome = _error_outcome(error)
            cleanup_outcome = self._windows_free_outcome(
                advapi,
                credential_pointer,
            )
            outcome = _first_outcome(outcome, cleanup_outcome)
            _raise_clean_outcome(outcome)
        if not succeeded:
            cleanup_outcome = self._windows_free_outcome(
                advapi,
                credential_pointer,
            )
            if cleanup_outcome is not None:
                _raise_clean_outcome(cleanup_outcome)
            if ctypes.get_last_error() == _WINDOWS_NOT_FOUND:
                return None
            raise _not_configured()
        native = None
        value = None
        try:
            credential = credential_pointer.contents
            length = int(credential.CredentialBlobSize)
            if (
                length <= 0
                or length > _MAX_SECRET_BYTES
                or not credential.CredentialBlob
            ):
                raise _not_configured()
            native = bytearray(
                ctypes.string_at(credential.CredentialBlob, length)
            )
            outcome = None
        except _PROCESS_CONTROL as error:
            outcome = _error_outcome(error)
        except BaseException:
            outcome = (_OUTCOME_FAILURE, None)
        cleanup_outcome = self._windows_free_outcome(
            advapi,
            credential_pointer,
        )
        outcome = _first_outcome(outcome, cleanup_outcome)
        if outcome is not None:
            if type(native) is bytearray:
                _zero(native)
            native = None
            _raise_clean_outcome(outcome)
        try:
            value = native.decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            raise _not_configured() from None
        finally:
            _zero(native)
            native = None
        if not _valid_secret_text(value):
            value = None
            raise _not_configured()
        result = value
        value = None
        return result

    def _windows_is_configured(self) -> bool:
        ctypes = self._ctypes
        credential_type, credential_pointer_type, wintypes = self._windows_types()
        advapi = self._windows_library(
            credential_type, credential_pointer_type, wintypes
        )
        credential_pointer = credential_pointer_type()
        succeeded = None
        last_error = None
        outcome = None
        error = None
        try:
            succeeded = advapi.CredReadW(
                SERVICE_NAME,
                _WINDOWS_CREDENTIAL_TYPE_GENERIC,
                0,
                ctypes.byref(credential_pointer),
            )
            if not succeeded:
                last_error = ctypes.get_last_error()
        except BaseException as error:
            outcome = _first_outcome(outcome, _error_outcome(error))
        cleanup_outcome = self._windows_free_outcome(
            advapi,
            credential_pointer,
        )
        outcome = _first_outcome(outcome, cleanup_outcome)
        result = bool(succeeded) and bool(credential_pointer)
        missing = (
            not succeeded
            and last_error == _WINDOWS_NOT_FOUND
            and not bool(credential_pointer)
        )
        ctypes = None
        credential_type = None
        credential_pointer_type = None
        wintypes = None
        advapi = None
        credential_pointer = None
        succeeded = None
        last_error = None
        error = None
        cleanup_outcome = None
        self = None
        if outcome is not None:
            _raise_clean_outcome(outcome)
        if missing:
            return False
        if not result:
            raise _not_configured()
        return True

    def _windows_write(self, mutable: bytearray):
        ctypes = self._ctypes
        credential_type, credential_pointer_type, wintypes = self._windows_types()
        advapi = self._windows_library(
            credential_type, credential_pointer_type, wintypes
        )
        secret_array = (ctypes.c_ubyte * len(mutable)).from_buffer(mutable)
        credential = credential_type()
        credential.Flags = 0
        credential.Type = _WINDOWS_CREDENTIAL_TYPE_GENERIC
        credential.TargetName = SERVICE_NAME
        credential.Comment = None
        credential.CredentialBlobSize = len(mutable)
        credential.CredentialBlob = ctypes.cast(
            secret_array, ctypes.POINTER(ctypes.c_ubyte)
        )
        credential.Persist = _WINDOWS_CREDENTIAL_PERSIST_LOCAL_MACHINE
        credential.AttributeCount = 0
        credential.Attributes = None
        credential.TargetAlias = None
        credential.UserName = ACCOUNT_NAME
        succeeded, commit_state, outcome = _invoke_native_mutation(
            advapi.CredWriteW,
            ctypes.byref(credential),
            0,
        )
        if outcome is None:
            if succeeded:
                commit_state = _COMMIT_COMMITTED
            else:
                if commit_state != _COMMIT_COMMITTED:
                    commit_state = _COMMIT_NOT_COMMITTED
                outcome = (_OUTCOME_FAILURE, None)
        return commit_state, outcome

    def _windows_delete(self):
        credential_type, credential_pointer_type, wintypes = self._windows_types()
        advapi = self._windows_library(
            credential_type, credential_pointer_type, wintypes
        )
        succeeded, commit_state, outcome = _invoke_native_mutation(
            advapi.CredDeleteW,
            SERVICE_NAME,
            _WINDOWS_CREDENTIAL_TYPE_GENERIC,
            0,
        )
        if outcome is not None:
            return commit_state, outcome
        if succeeded:
            return _COMMIT_COMMITTED, None
        if self._ctypes.get_last_error() == _WINDOWS_NOT_FOUND:
            if commit_state != _COMMIT_COMMITTED:
                commit_state = _COMMIT_NOT_COMMITTED
            return commit_state, None
        if commit_state != _COMMIT_COMMITTED:
            commit_state = _COMMIT_NOT_COMMITTED
        return commit_state, (_OUTCOME_FAILURE, None)
