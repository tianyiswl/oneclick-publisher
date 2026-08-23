"""Secret-safe OAuth authorization contract for desktop YouTube uploads."""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
from dataclasses import dataclass, field
from urllib.parse import ParseResult, parse_qs, urlencode, urlparse, urlunparse


GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class OAuthAuthorizationError(Exception):
    """An authorization callback could not be accepted without exposing secrets."""


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
