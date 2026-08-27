"""System-browser YouTube OAuth login and restart identity validation."""

from __future__ import annotations

import queue
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable, Mapping
from typing import Protocol

import requests

from .overseas_youtube_api import (
    YouTubeChannelIdentity,
    YouTubeChannelIdentityClient,
    YouTubeChannelLookupError,
)
from .overseas_youtube_credentials import (
    KeyringOAuthCredentialStore,
    OAuthCredentialError,
)
from .overseas_youtube_oauth import (
    OAuthAuthorizationError,
    OAuthLoopbackAuthorizationSession,
    OAuthTokens,
    OAuthTokenError,
    YOUTUBE_UPLOAD_SCOPE,
    YouTubeOAuthTokenClient,
    start_authorization_session,
)


DEFAULT_CALLBACK_TIMEOUT_SECONDS = 180.0
YOUTUBE_OAUTH_AUTH_MODE = "youtube_oauth"


class YouTubeOAuthLoginError(Exception):
    """A desktop login stopped with a stable, secret-free reason code."""


class _AccountSaver(Protocol):
    def __call__(
        self,
        *,
        profile_name: str,
        credential_reference: str,
        channel_id: str,
        display_name: str | None,
        record_id: int | None,
    ) -> int: ...


def _new_credential_reference() -> str:
    return f"youtube-oauth:{uuid.uuid4().hex}"


class YouTubeOAuthLoginSession:
    """Queue-compatible login session used by the existing Qt dialog."""

    manual_save_supported = False

    def __init__(
        self,
        *,
        client_id: str,
        profile_name: str,
        update_mode: bool,
        record_id: int | None,
        existing_account: Mapping[str, object] | None,
        account_saver: _AccountSaver,
        credential_store=None,
        browser_opener: Callable[[str], object] = webbrowser.open,
        authorization_session_factory: Callable[
            [str], OAuthLoopbackAuthorizationSession
        ] = start_authorization_session,
        token_client=None,
        channel_client=None,
        credential_reference_factory: Callable[[], str] = _new_credential_reference,
        callback_timeout_seconds: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    ) -> None:
        self.client_id = str(client_id or "").strip()
        self.profile_name = str(profile_name or "").strip()
        self.update_mode = bool(update_mode)
        self.record_id = int(record_id) if record_id is not None else None
        self.existing_account = dict(existing_account) if existing_account else None
        self.queue: queue.Queue[str] = queue.Queue()
        self._account_saver = account_saver
        self._credential_store = credential_store or KeyringOAuthCredentialStore()
        self._browser_opener = browser_opener
        self._authorization_session_factory = authorization_session_factory
        transport = requests.Session()
        self._token_client = token_client or YouTubeOAuthTokenClient(
            transport,
            clock=time.time,
        )
        self._channel_client = channel_client or YouTubeChannelIdentityClient(
            transport
        )
        self._credential_reference_factory = credential_reference_factory
        self._callback_timeout_seconds = float(callback_timeout_seconds)
        self._cancel_requested = threading.Event()
        self._authorization_session: OAuthLoopbackAuthorizationSession | None = None

    def __repr__(self) -> str:
        return "YouTubeOAuthLoginSession(<redacted>)"

    def start(self) -> None:
        threading.Thread(
            target=self._thread_main,
            daemon=True,
            name="oneclick-youtube-oauth-login",
        ).start()

    def save(self) -> None:
        self.queue.put("YouTube 官方授权完成后会自动保存，不能手动跳过频道校验。")

    def cancel(self) -> None:
        self._cancel_requested.set()
        session = self._authorization_session
        if session is not None:
            session.cancel()

    def _thread_main(self) -> None:
        try:
            self.run()
        except YouTubeOAuthLoginError as exc:
            reason = str(exc)
            if reason == "authorization_cancelled":
                self.queue.put("CANCELLED")
            else:
                self.queue.put(f"ERROR:{reason}")
        except Exception:
            self.queue.put("ERROR:youtube_oauth_login_failed")

    def run(self) -> int:
        if not self.client_id:
            raise YouTubeOAuthLoginError("youtube_oauth_client_not_configured")
        if not self.profile_name:
            raise YouTubeOAuthLoginError("profile_name_required")
        if self._cancel_requested.is_set():
            raise YouTubeOAuthLoginError("authorization_cancelled")

        try:
            authorization = self._authorization_session_factory(self.client_id)
        except Exception:
            raise YouTubeOAuthLoginError("authorization_start_failed") from None
        self._authorization_session = authorization
        try:
            opened = self._browser_opener(authorization.request.authorization_url)
            if opened is False:
                raise YouTubeOAuthLoginError("system_browser_open_failed")
            self.queue.put("BROWSER_OPENED")
            callback = authorization.receive_callback(
                timeout_seconds=self._callback_timeout_seconds
            )
            if self._cancel_requested.is_set():
                raise YouTubeOAuthLoginError("authorization_cancelled")
            tokens = self._token_client.exchange_authorization_code(
                client_id=self.client_id,
                request=authorization.request,
                callback=callback,
            )
            identity = self._channel_client.lookup_authenticated_channel(
                tokens.access_token
            )
        except YouTubeOAuthLoginError:
            raise
        except OAuthAuthorizationError as exc:
            if self._cancel_requested.is_set():
                raise YouTubeOAuthLoginError("authorization_cancelled") from None
            reason = "authorization_denied" if str(exc) == "authorization denied" else "authorization_invalid"
            raise YouTubeOAuthLoginError(reason) from None
        except OAuthTokenError as exc:
            reason = str(exc)
            if reason not in {
                "authorization_invalid",
                "oauth_transport_unavailable",
                "oauth_token_rejected",
                "oauth_token_response_invalid",
            }:
                reason = "oauth_token_rejected"
            raise YouTubeOAuthLoginError(reason) from None
        except YouTubeChannelLookupError:
            raise YouTubeOAuthLoginError("channel_identity_unavailable") from None
        except Exception:
            raise YouTubeOAuthLoginError("youtube_oauth_login_failed") from None
        finally:
            self._authorization_session = None

        reference, previous_token = self._credential_target(identity)
        try:
            self._credential_store.save_refresh_token(
                reference,
                tokens.refresh_token,
            )
        except OAuthCredentialError:
            raise YouTubeOAuthLoginError("credential_unavailable") from None
        try:
            account_id = self._account_saver(
                profile_name=self.profile_name,
                credential_reference=reference,
                channel_id=identity.channel_id,
                display_name=identity.display_name,
                record_id=self.record_id if self.update_mode else None,
            )
        except Exception:
            self._rollback_credential(reference, previous_token)
            raise YouTubeOAuthLoginError("account_persistence_failed") from None
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            self._rollback_credential(reference, previous_token)
            raise YouTubeOAuthLoginError("account_persistence_failed") from None
        if account_id <= 0:
            self._rollback_credential(reference, previous_token)
            raise YouTubeOAuthLoginError("account_persistence_failed")
        self.queue.put(f"ACCOUNT_SAVED:{account_id}")
        return account_id

    def _credential_target(
        self,
        identity: YouTubeChannelIdentity,
    ) -> tuple[str, str | None]:
        if not self.update_mode:
            reference = str(self._credential_reference_factory() or "").strip()
            return reference, None
        account = self.existing_account or {}
        if (
            int(account.get("type") or 0) != 7
            or str(account.get("authMode") or "") != YOUTUBE_OAUTH_AUTH_MODE
            or self.record_id is None
            or int(account.get("id") or 0) != self.record_id
        ):
            raise YouTubeOAuthLoginError("authorization_invalid")
        expected_channel = str(account.get("accountReference") or "").strip()
        if not expected_channel or expected_channel != identity.channel_id:
            raise YouTubeOAuthLoginError("channel_identity_mismatch")
        reference = str(account.get("filePath") or "").strip()
        try:
            previous_token = self._credential_store.load_refresh_token(reference)
        except OAuthCredentialError:
            raise YouTubeOAuthLoginError("credential_unavailable") from None
        return reference, previous_token

    def _rollback_credential(
        self,
        reference: str,
        previous_token: str | None,
    ) -> None:
        try:
            if previous_token:
                self._credential_store.save_refresh_token(reference, previous_token)
            else:
                self._credential_store.delete_refresh_token(reference)
        except Exception:
            pass


def validate_saved_youtube_oauth_account(
    account: Mapping[str, object],
    *,
    client_id: str,
    credential_store=None,
    token_client=None,
    channel_client=None,
) -> YouTubeChannelIdentity:
    """Refresh one saved credential and require the same stable channel ID."""
    normalized_client_id = str(client_id or "").strip()
    if not normalized_client_id:
        raise YouTubeOAuthLoginError("youtube_oauth_client_not_configured")
    if (
        not isinstance(account, Mapping)
        or int(account.get("type") or 0) != 7
        or str(account.get("authMode") or "") != YOUTUBE_OAUTH_AUTH_MODE
    ):
        raise YouTubeOAuthLoginError("authorization_invalid")
    reference = str(account.get("filePath") or "").strip()
    expected_channel = str(account.get("accountReference") or "").strip()
    if not reference or not expected_channel:
        raise YouTubeOAuthLoginError("credential_unavailable")

    store = credential_store or KeyringOAuthCredentialStore()
    transport = requests.Session()
    token_service = token_client or YouTubeOAuthTokenClient(
        transport,
        clock=time.time,
    )
    channel_service = channel_client or YouTubeChannelIdentityClient(transport)
    try:
        refresh_token = store.load_refresh_token(reference)
        if not refresh_token:
            raise YouTubeOAuthLoginError("credential_unavailable")
        refreshed = token_service.refresh_access_token(
            client_id=normalized_client_id,
            existing_tokens=OAuthTokens(
                access_token="refresh_pending",
                refresh_token=refresh_token,
                expires_at=0.0,
                scope=YOUTUBE_UPLOAD_SCOPE,
            ),
        )
        identity = channel_service.lookup_authenticated_channel(
            refreshed.access_token
        )
        if identity.channel_id != expected_channel:
            raise YouTubeOAuthLoginError("channel_identity_mismatch")
        if refreshed.refresh_token != refresh_token:
            store.save_refresh_token(reference, refreshed.refresh_token)
        return identity
    except YouTubeOAuthLoginError:
        raise
    except OAuthCredentialError:
        raise YouTubeOAuthLoginError("credential_unavailable") from None
    except OAuthTokenError as exc:
        reason = "authorization_invalid" if str(exc) == "authorization_invalid" else "credential_unavailable"
        raise YouTubeOAuthLoginError(reason) from None
    except YouTubeChannelLookupError:
        raise YouTubeOAuthLoginError("channel_identity_unavailable") from None
    except Exception:
        raise YouTubeOAuthLoginError("channel_identity_unavailable") from None
