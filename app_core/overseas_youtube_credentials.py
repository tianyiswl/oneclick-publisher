"""Operating-system credential storage for YouTube OAuth refresh tokens."""

from __future__ import annotations

import re
from typing import Protocol


YOUTUBE_OAUTH_KEYRING_SERVICE = "com.hellomobai.yijianfa.youtube.oauth"
YOUTUBE_OAUTH_CLIENT_KEYRING_SERVICE = (
    "com.hellomobai.yijianfa.youtube.oauth.client"
)
_CREDENTIAL_REFERENCE = re.compile(r"^youtube-oauth:[A-Za-z0-9_-]{4,96}$")
_CLIENT_ID = re.compile(
    r"^[A-Za-z0-9._-]{8,192}\.apps\.googleusercontent\.com$"
)


class OAuthCredentialError(Exception):
    """The operating-system credential store was unavailable or rejected data."""


class _KeyringBackend(Protocol):
    def set_password(self, service: str, username: str, password: str) -> None: ...

    def get_password(self, service: str, username: str) -> str | None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _default_keyring_backend() -> _KeyringBackend:
    try:
        import keyring
    except Exception:
        raise OAuthCredentialError("credential_unavailable") from None
    return keyring


def _valid_reference(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized != value or not _CREDENTIAL_REFERENCE.fullmatch(normalized):
        return None
    return normalized


def _valid_client_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized != value or not _CLIENT_ID.fullmatch(normalized):
        return None
    return normalized


class KeyringOAuthClientSecretStore:
    """Store the Google desktop client secret only in the system credential store."""

    def __init__(
        self,
        *,
        backend: _KeyringBackend | None = None,
        service_name: str = YOUTUBE_OAUTH_CLIENT_KEYRING_SERVICE,
    ) -> None:
        if not isinstance(service_name, str) or not service_name.strip():
            raise OAuthCredentialError("credential_unavailable")
        self._backend = backend
        self._service_name = service_name.strip()

    def __repr__(self) -> str:
        return "KeyringOAuthClientSecretStore(<system credential store>)"

    def _keyring(self) -> _KeyringBackend:
        if self._backend is None:
            self._backend = _default_keyring_backend()
        return self._backend

    def load_client_secret(self, client_id: str) -> str | None:
        normalized_client_id = _valid_client_id(client_id)
        if normalized_client_id is None:
            raise OAuthCredentialError("credential_unavailable")
        try:
            secret = self._keyring().get_password(
                self._service_name,
                normalized_client_id,
            )
        except Exception:
            raise OAuthCredentialError("credential_unavailable") from None
        if secret is None:
            return None
        if not isinstance(secret, str) or not secret:
            raise OAuthCredentialError("credential_unavailable")
        return secret

    def save_client_secret(self, client_id: str, client_secret: str) -> None:
        normalized_client_id = _valid_client_id(client_id)
        if (
            normalized_client_id is None
            or not isinstance(client_secret, str)
            or not client_secret
        ):
            raise OAuthCredentialError("credential_unavailable")
        try:
            self._keyring().set_password(
                self._service_name,
                normalized_client_id,
                client_secret,
            )
        except Exception:
            raise OAuthCredentialError("credential_unavailable") from None


class KeyringOAuthCredentialStore:
    """Store only refresh tokens in Keychain or Windows Credential Locker."""

    def __init__(
        self,
        *,
        backend: _KeyringBackend | None = None,
        service_name: str = YOUTUBE_OAUTH_KEYRING_SERVICE,
    ) -> None:
        if not isinstance(service_name, str) or not service_name.strip():
            raise OAuthCredentialError("credential_unavailable")
        self._backend = backend
        self._service_name = service_name.strip()

    def __repr__(self) -> str:
        return "KeyringOAuthCredentialStore(<system credential store>)"

    def _keyring(self) -> _KeyringBackend:
        if self._backend is None:
            self._backend = _default_keyring_backend()
        return self._backend

    def load_refresh_token(self, credential_reference: str) -> str | None:
        reference = _valid_reference(credential_reference)
        if reference is None:
            raise OAuthCredentialError("credential_unavailable")
        try:
            token = self._keyring().get_password(self._service_name, reference)
        except Exception:
            raise OAuthCredentialError("credential_unavailable") from None
        if token is None:
            return None
        if not isinstance(token, str) or not token:
            raise OAuthCredentialError("credential_unavailable")
        return token

    def save_refresh_token(self, credential_reference: str, refresh_token: str) -> None:
        reference = _valid_reference(credential_reference)
        if reference is None or not isinstance(refresh_token, str) or not refresh_token:
            raise OAuthCredentialError("credential_unavailable")
        try:
            self._keyring().set_password(
                self._service_name,
                reference,
                refresh_token,
            )
        except Exception:
            raise OAuthCredentialError("credential_unavailable") from None

    def delete_refresh_token(self, credential_reference: str) -> None:
        reference = _valid_reference(credential_reference)
        if reference is None:
            raise OAuthCredentialError("credential_unavailable")
        try:
            backend = self._keyring()
            if backend.get_password(self._service_name, reference) is None:
                return
            backend.delete_password(self._service_name, reference)
        except Exception:
            raise OAuthCredentialError("credential_unavailable") from None
