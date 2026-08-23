# -*- coding: utf-8 -*-
"""API Key 的 macOS Keychain / Windows Credential Manager 适配器。"""

from __future__ import annotations

import asyncio
import ctypes as _ctypes
from ctypes import wintypes as _native_wintypes
import sys

from .platform_data_comment_models import CommentInsightFailure


SERVICE_NAME = "com.oneclickpublisher.comment-insight"
ACCOUNT_NAME = "default"

_MAX_SECRET_BYTES = 8192
_MAC_NOT_FOUND = -25300
_WINDOWS_NOT_FOUND = 1168
_WINDOWS_CREDENTIAL_TYPE_GENERIC = 1
_WINDOWS_CREDENTIAL_PERSIST_LOCAL_MACHINE = 2
_PROCESS_CONTROL = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


def _not_configured() -> CommentInsightFailure:
    return CommentInsightFailure("comment_ai_not_configured")


def _secret_buffer(value: object) -> bytearray:
    if type(value) is not str or not value or not value.strip():
        raise _not_configured()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _not_configured()
    try:
        encoded = bytearray(value, "utf-8")
    except BaseException as exc:
        if isinstance(exc, _PROCESS_CONTROL):
            raise
        raise _not_configured() from None
    if not encoded or len(encoded) > _MAX_SECRET_BYTES:
        _zero(encoded)
        raise _not_configured()
    return encoded


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


class CommentSecretStore:
    """只通过操作系统原生 API 读写评论洞察密钥。"""

    def __init__(self, *, platform_name=None, ctypes_module=None) -> None:
        self._platform = sys.platform if platform_name is None else platform_name
        self._ctypes = _ctypes if ctypes_module is None else ctypes_module

    def read(self) -> str | None:
        try:
            if self._platform == "darwin":
                return self._mac_read()
            if self._platform == "win32":
                return self._windows_read()
            raise _not_configured()
        except _PROCESS_CONTROL:
            raise
        except CommentInsightFailure:
            raise _not_configured() from None
        except BaseException:
            raise _not_configured() from None

    def write(self, secret: str) -> None:
        mutable = _secret_buffer(secret)
        try:
            if self._platform == "darwin":
                self._mac_write(mutable)
                return
            if self._platform == "win32":
                self._windows_write(mutable)
                return
            raise _not_configured()
        except _PROCESS_CONTROL:
            raise
        except CommentInsightFailure:
            raise _not_configured() from None
        except BaseException:
            raise _not_configured() from None
        finally:
            _zero(mutable)

    def delete(self) -> None:
        try:
            if self._platform == "darwin":
                self._mac_delete()
                return
            if self._platform == "win32":
                self._windows_delete()
                return
            raise _not_configured()
        except _PROCESS_CONTROL:
            raise
        except CommentInsightFailure:
            raise _not_configured() from None
        except BaseException:
            raise _not_configured() from None

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

    def _mac_find(self, security, core):
        ctypes = self._ctypes
        service, account = self._mac_names()
        password_length = ctypes.c_uint32(0)
        password_data = ctypes.c_void_p()
        item_ref = ctypes.c_void_p()
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
        if status == _MAC_NOT_FOUND:
            return None, None
        if status != 0:
            raise _not_configured()
        try:
            if (
                not password_data.value
                or password_length.value <= 0
                or password_length.value > _MAX_SECRET_BYTES
            ):
                raise _not_configured()
            native = ctypes.string_at(password_data, password_length.value)
            try:
                value = native.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                raise _not_configured() from None
            if not value:
                raise _not_configured()
        except BaseException:
            try:
                if password_data.value:
                    security.SecKeychainItemFreeContent(None, password_data)
            finally:
                self._mac_release_item(core, item_ref)
            raise
        if security.SecKeychainItemFreeContent(None, password_data) != 0:
            self._mac_release_item(core, item_ref)
            raise _not_configured()
        return value, item_ref

    def _mac_release_item(self, core, item_ref) -> None:
        if item_ref is not None and getattr(item_ref, "value", None):
            core.CFRelease(item_ref)

    def _mac_read(self) -> str | None:
        security, core = self._mac_libraries()
        value, item_ref = self._mac_find(security, core)
        try:
            return value
        finally:
            self._mac_release_item(core, item_ref)

    def _mac_write(self, mutable: bytearray) -> None:
        ctypes = self._ctypes
        security, core = self._mac_libraries()
        _existing, item_ref = self._mac_find(security, core)
        secret_array = (ctypes.c_ubyte * len(mutable)).from_buffer(mutable)
        secret_pointer = ctypes.cast(secret_array, ctypes.c_void_p)
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
        finally:
            self._mac_release_item(core, item_ref)

    def _mac_delete(self) -> None:
        security, core = self._mac_libraries()
        _existing, item_ref = self._mac_find(security, core)
        if item_ref is None:
            return
        try:
            if security.SecKeychainItemDelete(item_ref) != 0:
                raise _not_configured()
        finally:
            self._mac_release_item(core, item_ref)

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
        advapi.CredWriteW.restype = ctypes.c_bool
        advapi.CredWriteW.argtypes = [
            ctypes.POINTER(credential_type),
            wintypes.DWORD,
        ]
        advapi.CredReadW.restype = ctypes.c_bool
        advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(credential_pointer_type),
        ]
        advapi.CredDeleteW.restype = ctypes.c_bool
        advapi.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        advapi.CredFree.restype = None
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        return advapi

    def _windows_read(self) -> str | None:
        ctypes = self._ctypes
        credential_type, credential_pointer_type, wintypes = self._windows_types()
        advapi = self._windows_library(
            credential_type, credential_pointer_type, wintypes
        )
        credential_pointer = credential_pointer_type()
        succeeded = advapi.CredReadW(
            SERVICE_NAME,
            _WINDOWS_CREDENTIAL_TYPE_GENERIC,
            0,
            ctypes.byref(credential_pointer),
        )
        if not succeeded:
            if ctypes.get_last_error() == _WINDOWS_NOT_FOUND:
                return None
            raise _not_configured()
        try:
            credential = credential_pointer.contents
            length = int(credential.CredentialBlobSize)
            if (
                length <= 0
                or length > _MAX_SECRET_BYTES
                or not credential.CredentialBlob
            ):
                raise _not_configured()
            native = ctypes.string_at(credential.CredentialBlob, length)
            try:
                value = native.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                raise _not_configured() from None
            if not value:
                raise _not_configured()
            return value
        finally:
            advapi.CredFree(credential_pointer)

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
