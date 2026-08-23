"""Secret-safe OAuth authorization contract for desktop YouTube uploads."""

from __future__ import annotations

import base64
import hashlib
import math
import secrets
import socket
import threading
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, runtime_checkable
from urllib.parse import ParseResult, parse_qs, urlencode, urlparse, urlunparse


GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
TOKEN_REQUEST_TIMEOUT_SECONDS = 10.0


class OAuthAuthorizationError(Exception):
    """An authorization callback could not be accepted without exposing secrets."""


class OAuthTokenError(Exception):
    """A token lifecycle operation failed with a stable, secret-safe reason code."""


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationRequest:
    """One desktop OAuth authorization attempt; all fields are secret-bearing."""

    authorization_url: str = field(repr=False)
    callback_url: str = field(repr=False)
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationCallback:
    """A verified callback whose code is intentionally hidden by representation."""

    authorized: bool
    authorization_code: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """Short-lived access and renewable refresh tokens with a public expiry only."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    scope: str
    token_type: str = "Bearer"


@runtime_checkable
class OAuthCredentialStore(Protocol):
    """Boundary for a platform credential store; implementations belong to integration."""

    def load_refresh_token(self, credential_reference: str) -> str | None:
        """Return a refresh token from the operating system credential store."""

    def save_refresh_token(self, credential_reference: str, refresh_token: str) -> None:
        """Save a refresh token to the operating system credential store."""


class _OAuthTokenResponse(Protocol):
    status_code: int

    def json(self) -> object:
        """Return the decoded OAuth response body."""


class OAuthTokenTransport(Protocol):
    """Injected HTTP boundary for OAuth token endpoint requests."""

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        timeout: float,
    ) -> _OAuthTokenResponse:
        """POST form data to the token endpoint."""


class YouTubeOAuthTokenClient:
    """Exchange and refresh Google desktop OAuth tokens without owning persistence."""

    def __init__(
        self,
        transport: OAuthTokenTransport,
        *,
        clock: Callable[[], float],
        timeout_seconds: float = TOKEN_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    def exchange_authorization_code(
        self,
        *,
        client_id: str,
        request: OAuthAuthorizationRequest,
        callback: OAuthAuthorizationCallback,
    ) -> OAuthTokens:
        """Exchange one verified authorization callback for renewable OAuth tokens."""
        if not client_id or not callback.authorized:
            raise OAuthTokenError("authorization_invalid")
        return self._request_tokens(
            {
                "client_id": client_id,
                "code": callback.authorization_code,
                "code_verifier": request.code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": request.callback_url,
            },
            fallback_refresh_token=None,
        )

    def refresh_access_token(
        self,
        *,
        client_id: str,
        existing_tokens: OAuthTokens,
    ) -> OAuthTokens:
        """Refresh an access token while retaining an omitted refresh token."""
        if not client_id or not existing_tokens.refresh_token:
            raise OAuthTokenError("credential_unavailable")
        return self._request_tokens(
            {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": existing_tokens.refresh_token,
            },
            fallback_refresh_token=existing_tokens.refresh_token,
        )

    def _request_tokens(
        self,
        data: Mapping[str, str],
        *,
        fallback_refresh_token: str | None,
    ) -> OAuthTokens:
        try:
            response = self._transport.post(
                GOOGLE_TOKEN_ENDPOINT,
                data=data,
                timeout=self._timeout_seconds,
            )
        except Exception:
            raise OAuthTokenError("oauth_transport_unavailable") from None

        payload = _response_payload(response)
        if not 200 <= response.status_code < 300:
            if payload.get("error") == "invalid_grant":
                raise OAuthTokenError("authorization_invalid")
            raise OAuthTokenError("oauth_token_rejected")
        return _tokens_from_payload(
            payload,
            expires_at=self._clock(),
            fallback_refresh_token=fallback_refresh_token,
        )


def pkce_s256_challenge(code_verifier: str) -> str:
    """Return the RFC 7636 S256 code challenge for a PKCE verifier."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_request(client_id: str) -> OAuthAuthorizationRequest:
    """Create one loopback-only Google desktop authorization request."""
    if not client_id:
        raise ValueError("OAuth client ID is required")

    callback_url = _random_loopback_callback_url()
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": callback_url,
            "response_type": "code",
            "scope": YOUTUBE_UPLOAD_SCOPE,
            "state": state,
            "code_challenge": pkce_s256_challenge(code_verifier),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return OAuthAuthorizationRequest(
        authorization_url=f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}",
        callback_url=callback_url,
        state=state,
        code_verifier=code_verifier,
    )


class OAuthCallbackVerifier:
    """Accept exactly one callback for one OAuth authorization request."""

    def __init__(self, request: OAuthAuthorizationRequest) -> None:
        self._request = request
        self._consumed = False
        self._consume_lock = threading.Lock()

    def consume(self, callback_url: str) -> OAuthAuthorizationCallback:
        """Verify one loopback callback, rejecting every later callback safely."""
        with self._consume_lock:
            if self._consumed:
                raise OAuthAuthorizationError("authorization response already consumed")
            self._consumed = True

        try:
            parsed = urlparse(callback_url)
            expected = urlparse(self._request.callback_url)
        except ValueError:
            raise OAuthAuthorizationError("authorization response invalid") from None
        if not _matches_callback_endpoint(parsed, expected):
            raise OAuthAuthorizationError("authorization response invalid")

        values = parse_qs(parsed.query, keep_blank_values=True)
        if values.get("state") != [self._request.state]:
            raise OAuthAuthorizationError("authorization response invalid")
        if "error" in values:
            raise OAuthAuthorizationError("authorization denied")

        codes = values.get("code")
        if codes is None or len(codes) != 1 or not codes[0]:
            raise OAuthAuthorizationError("authorization response invalid")
        return OAuthAuthorizationCallback(authorized=True, authorization_code=codes[0])


def _random_loopback_callback_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    path = f"/oauth/callback/{secrets.token_urlsafe(24)}"
    return urlunparse(("http", f"127.0.0.1:{port}", path, "", "", ""))


def _matches_callback_endpoint(callback: ParseResult, expected: ParseResult) -> bool:
    try:
        return bool(
            callback.scheme == expected.scheme == "http"
            and callback.hostname == expected.hostname == "127.0.0.1"
            and callback.port == expected.port
            and callback.path == expected.path
            and callback.params == expected.params == ""
            and callback.fragment == expected.fragment == ""
            and callback.username is None
            and callback.password is None
        )
    except ValueError:
        return False


def _response_payload(response: _OAuthTokenResponse) -> Mapping[str, object]:
    try:
        payload = response.json()
    except Exception:
        raise OAuthTokenError("oauth_token_response_invalid") from None
    if not isinstance(payload, Mapping):
        raise OAuthTokenError("oauth_token_response_invalid")
    return payload


def _tokens_from_payload(
    payload: Mapping[str, object],
    *,
    expires_at: float,
    fallback_refresh_token: str | None,
) -> OAuthTokens:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token", fallback_refresh_token)
    scope = payload.get("scope")
    token_type = payload.get("token_type", "Bearer")
    try:
        expires_in = float(payload["expires_in"])
    except (KeyError, TypeError, ValueError):
        raise OAuthTokenError("oauth_token_response_invalid") from None
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(refresh_token, str)
        or not refresh_token
        or not isinstance(scope, str)
        or not scope
        or not isinstance(token_type, str)
        or not token_type
        or not math.isfinite(expires_in)
        or expires_in <= 0
    ):
        raise OAuthTokenError("oauth_token_response_invalid")
    return OAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at + expires_in,
        scope=scope,
        token_type=token_type,
    )
