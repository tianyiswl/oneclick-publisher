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
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_REQUIRED_SCOPES = frozenset(
    {YOUTUBE_READONLY_SCOPE, YOUTUBE_UPLOAD_SCOPE}
)
YOUTUBE_OAUTH_SCOPE = f"{YOUTUBE_READONLY_SCOPE} {YOUTUBE_UPLOAD_SCOPE}"
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
        client_secret: str | None = None,
        request: OAuthAuthorizationRequest,
        callback: OAuthAuthorizationCallback,
    ) -> OAuthTokens:
        """Exchange one verified authorization callback for renewable OAuth tokens."""
        if not client_id or not callback.authorized:
            raise OAuthTokenError("authorization_invalid")
        data = {
            "client_id": client_id,
            "code": callback.authorization_code,
            "code_verifier": request.code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": request.callback_url,
        }
        if client_secret:
            data["client_secret"] = client_secret
        return self._request_tokens(
            data,
            fallback_refresh_token=None,
            fallback_scope=None,
        )

    def refresh_access_token(
        self,
        *,
        client_id: str,
        client_secret: str | None = None,
        existing_tokens: OAuthTokens,
    ) -> OAuthTokens:
        """Refresh an access token while retaining an omitted refresh token."""
        if not client_id or not existing_tokens.refresh_token:
            raise OAuthTokenError("credential_unavailable")
        _validate_token_authority(
            scope=existing_tokens.scope,
            token_type=existing_tokens.token_type,
        )
        data = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": existing_tokens.refresh_token,
        }
        if client_secret:
            data["client_secret"] = client_secret
        return self._request_tokens(
            data,
            fallback_refresh_token=existing_tokens.refresh_token,
            fallback_scope=existing_tokens.scope,
        )

    def _request_tokens(
        self,
        data: Mapping[str, str],
        *,
        fallback_refresh_token: str | None,
        fallback_scope: str | None,
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
        if not _response_is_success(response):
            if payload.get("error") == "invalid_grant":
                raise OAuthTokenError("authorization_invalid")
            raise OAuthTokenError("oauth_token_rejected")
        return _tokens_from_payload(
            payload,
            expires_at=self._clock(),
            fallback_refresh_token=fallback_refresh_token,
            fallback_scope=fallback_scope,
        )


def pkce_s256_challenge(code_verifier: str) -> str:
    """Return the RFC 7636 S256 code challenge for a PKCE verifier."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthLoopbackAuthorizationSession:
    """Own one live loopback listener and one secret-safe OAuth request."""

    def __init__(
        self,
        request: OAuthAuthorizationRequest,
        listener: socket.socket,
    ) -> None:
        self._request = request
        self._listener: socket.socket | None = listener
        self._verifier = OAuthCallbackVerifier(request)
        self._state_lock = threading.Lock()
        self._receive_lock = threading.Lock()
        self._closed = False

    @property
    def request(self) -> OAuthAuthorizationRequest:
        """Return the immutable request while retaining listener ownership."""
        return self._request

    def __repr__(self) -> str:
        return "OAuthLoopbackAuthorizationSession(<redacted>)"

    __str__ = __repr__

    def consume_callback_url(self, callback_url: str) -> OAuthAuthorizationCallback:
        """Consume one externally received callback and always release the port."""
        self._require_active_listener()
        try:
            return self._verifier.consume(callback_url)
        finally:
            self.close()

    def receive_callback(self, *, timeout_seconds: float) -> OAuthAuthorizationCallback:
        """Receive one local HTTP callback, respond safely, and close the listener."""
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            self.close()
            raise OAuthAuthorizationError("authorization response timeout") from None
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            self.close()
            raise OAuthAuthorizationError("authorization response timeout")

        with self._receive_lock:
            listener = self._require_active_listener()
            try:
                listener.settimeout(timeout)
                connection, _address = listener.accept()
            except socket.timeout:
                self.close()
                raise OAuthAuthorizationError("authorization response timeout") from None
            except OSError:
                self.close()
                raise OAuthAuthorizationError("authorization response invalid") from None

            with connection:
                try:
                    connection.settimeout(timeout)
                    target = _read_http_callback_target(connection)
                    callback_url = _callback_url_from_target(
                        target,
                        expected=self._request.callback_url,
                    )
                    result = self._verifier.consume(callback_url)
                except OAuthAuthorizationError:
                    _send_loopback_response(connection, accepted=False)
                    raise
                except Exception:
                    _send_loopback_response(connection, accepted=False)
                    raise OAuthAuthorizationError(
                        "authorization response invalid"
                    ) from None
                else:
                    _send_loopback_response(connection, accepted=True)
                    return result
                finally:
                    self.close()

    def cancel(self) -> None:
        """Cancel the authorization attempt and release its callback port."""
        self.close()

    def close(self) -> None:
        """Idempotently release the listener, including a blocked receiver."""
        listener: socket.socket | None
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            listener = self._listener
            self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def _require_active_listener(self) -> socket.socket:
        with self._state_lock:
            listener = self._listener
            if self._closed or listener is None:
                raise OAuthAuthorizationError("authorization session closed")
            return listener

    def __enter__(self) -> OAuthLoopbackAuthorizationSession:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def start_authorization_session(client_id: str) -> OAuthLoopbackAuthorizationSession:
    """Bind and listen before exposing one desktop OAuth authorization URL."""
    if not client_id:
        raise ValueError("OAuth client ID is required")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        callback_url = urlunparse(
            (
                "http",
                f"127.0.0.1:{port}",
                f"/oauth/callback/{secrets.token_urlsafe(24)}",
                "",
                "",
                "",
            )
        )
        request = _authorization_request(client_id, callback_url)
        return OAuthLoopbackAuthorizationSession(request, listener)
    except Exception:
        listener.close()
        raise


def _authorization_request(
    client_id: str,
    callback_url: str,
) -> OAuthAuthorizationRequest:
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": callback_url,
            "response_type": "code",
            "scope": YOUTUBE_OAUTH_SCOPE,
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


def _read_http_callback_target(connection: socket.socket) -> str:
    request_bytes = bytearray()
    while b"\r\n\r\n" not in request_bytes:
        chunk = connection.recv(4096)
        if not chunk:
            break
        request_bytes.extend(chunk)
        if len(request_bytes) > 16384:
            raise OAuthAuthorizationError("authorization response invalid")
    try:
        request_line = bytes(request_bytes).split(b"\r\n", 1)[0].decode("ascii")
        method, target, version = request_line.split(" ", 2)
    except (UnicodeError, ValueError):
        raise OAuthAuthorizationError("authorization response invalid") from None
    if method != "GET" or version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise OAuthAuthorizationError("authorization response invalid")
    return target


def _callback_url_from_target(target: str, *, expected: str) -> str:
    try:
        parsed_target = urlparse(target)
        parsed_expected = urlparse(expected)
    except ValueError:
        raise OAuthAuthorizationError("authorization response invalid") from None
    if (
        parsed_target.scheme
        or parsed_target.netloc
        or not parsed_target.path.startswith("/")
    ):
        raise OAuthAuthorizationError("authorization response invalid")
    return urlunparse(
        (
            parsed_expected.scheme,
            parsed_expected.netloc,
            parsed_target.path,
            parsed_target.params,
            parsed_target.query,
            parsed_target.fragment,
        )
    )


def _send_loopback_response(connection: socket.socket, *, accepted: bool) -> None:
    body = (
        b"Authorization response received. You may close this window."
        if accepted
        else b"Authorization response rejected. You may close this window."
    )
    status = b"200 OK" if accepted else b"400 Bad Request"
    response = (
        b"HTTP/1.1 "
        + status
        + b"\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n"
        + body
    )
    try:
        connection.sendall(response)
    except OSError:
        pass


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


def _response_is_success(response: _OAuthTokenResponse) -> bool:
    try:
        status_code = response.status_code
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise TypeError
        return 200 <= status_code < 300
    except OAuthTokenError:
        raise
    except Exception:
        raise OAuthTokenError("oauth_token_response_invalid") from None


def _tokens_from_payload(
    payload: Mapping[str, object],
    *,
    expires_at: float,
    fallback_refresh_token: str | None,
    fallback_scope: str | None,
) -> OAuthTokens:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token", fallback_refresh_token)
    scope = payload.get("scope", fallback_scope)
    token_type = payload.get("token_type", "Bearer")
    raw_expires_in = payload.get("expires_in")
    if isinstance(raw_expires_in, bool):
        raise OAuthTokenError("oauth_token_response_invalid")
    try:
        expires_in = float(raw_expires_in)
    except (OverflowError, TypeError, ValueError):
        raise OAuthTokenError("oauth_token_response_invalid") from None
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(refresh_token, str)
        or not refresh_token
        or not math.isfinite(expires_in)
        or expires_in <= 0
    ):
        raise OAuthTokenError("oauth_token_response_invalid")
    _validate_token_authority(scope=scope, token_type=token_type)
    return OAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at + expires_in,
        scope=scope,
        token_type="Bearer",
    )


def _validate_token_authority(*, scope: object, token_type: object) -> None:
    if (
        not isinstance(scope, str)
        or set(scope.split()) != YOUTUBE_REQUIRED_SCOPES
        or not isinstance(token_type, str)
        or token_type.casefold() != "bearer"
    ):
        raise OAuthTokenError("oauth_token_response_invalid")
