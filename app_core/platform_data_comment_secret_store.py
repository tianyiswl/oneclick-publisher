# -*- coding: utf-8 -*-
"""API Key 的 macOS Keychain / Windows Credential Manager 适配器。"""

from __future__ import annotations

import asyncio
import ctypes as _ctypes
from ctypes import wintypes as _native_wintypes
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
        outcome = self._write_outcome(secret)
        secret = None
        if outcome[0] == _OUTCOME_OK:
            return
        self = None
        _raise_clean_outcome(outcome)

    def _write_outcome(self, secret):
        mutable = None
        try:
            mutable = _secret_buffer(secret)
            if self._platform == "darwin":
                self._mac_write(mutable)
            elif self._platform == "win32":
                self._windows_write(mutable)
            else:
                raise _not_configured()
            return (_OUTCOME_OK, None)
        except _PROCESS_CONTROL as error:
            return (_OUTCOME_CONTROL, _control_outcome_value(error))
        except BaseException:
            return (_OUTCOME_FAILURE, None)
        finally:
            secret = None
            if type(mutable) is bytearray:
                _zero(mutable)
            self = None

    def delete(self) -> None:
        outcome = self._delete_outcome()
        if outcome[0] == _OUTCOME_OK:
            return
        self = None
        _raise_clean_outcome(outcome)

    def _delete_outcome(self):
        try:
            if self._platform == "darwin":
                self._mac_delete()
            elif self._platform == "win32":
                self._windows_delete()
            else:
                raise _not_configured()
            return (_OUTCOME_OK, None)
        except _PROCESS_CONTROL as error:
            return (_OUTCOME_CONTROL, _control_outcome_value(error))
        except BaseException:
            return (_OUTCOME_FAILURE, None)
        finally:
            self = None

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

    def _mac_write(self, mutable: bytearray) -> None:
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
        outcome = None
        error = None
        try:
            if item_ref is not None:
                status = security.SecKeychainItemModifyAttributesAndData(
                    item_ref, None, len(mutable), secret_pointer
                )
            else:
                service, account = self._mac_names()
                status = security.SecKeychainAddGenericPassword(
                    None,
                    len(SERVICE_NAME.encode("utf-8")),
                    ctypes.cast(service, ctypes.c_void_p),
                    len(ACCOUNT_NAME.encode("utf-8")),
                    ctypes.cast(account, ctypes.c_void_p),
                    len(mutable),
                    secret_pointer,
                    None,
                )
            if status != 0:
                raise _not_configured()
        except BaseException as error:
            outcome = _first_outcome(outcome, _error_outcome(error))
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
        error = None
        release_outcome = None
        self = None
        if outcome is not None:
            _raise_clean_outcome(outcome)

    def _mac_delete(self) -> None:
        security, core = self._mac_libraries()
        _existing, item_ref = self._mac_find(
            security, core, read_secret=False
        )
        if item_ref is None:
            return
        status = None
        outcome = None
        error = None
        try:
            status = security.SecKeychainItemDelete(item_ref)
            if status != 0:
                raise _not_configured()
        except BaseException as error:
            outcome = _first_outcome(outcome, _error_outcome(error))
        release_outcome = self._mac_release_outcome(core, item_ref)
        outcome = _first_outcome(outcome, release_outcome)
        security = None
        core = None
        _existing = None
        item_ref = None
        status = None
        error = None
        release_outcome = None
        self = None
        if outcome is not None:
            _raise_clean_outcome(outcome)

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

    def _windows_write(self, mutable: bytearray) -> None:
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
        if not advapi.CredWriteW(ctypes.byref(credential), 0):
            raise _not_configured()

    def _windows_delete(self) -> None:
        credential_type, credential_pointer_type, wintypes = self._windows_types()
        advapi = self._windows_library(
            credential_type, credential_pointer_type, wintypes
        )
        if advapi.CredDeleteW(SERVICE_NAME, _WINDOWS_CREDENTIAL_TYPE_GENERIC, 0):
            return
        if self._ctypes.get_last_error() == _WINDOWS_NOT_FOUND:
            return
        raise _not_configured()
