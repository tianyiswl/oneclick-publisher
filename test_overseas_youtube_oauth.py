# -*- coding: utf-8 -*-
"""Offline contract tests for the desktop YouTube OAuth authorization flow."""

import inspect
import threading
import unittest
from dataclasses import FrozenInstanceError
from typing import Any
from urllib.parse import parse_qs, urlparse

from app_core.overseas_youtube_oauth import (
    OAuthAuthorizationError,
    OAuthCallbackVerifier,
    OAuthCredentialStore,
    OAuthTokenError,
    OAuthTokens,
    YouTubeOAuthTokenClient,
    build_authorization_request,
    pkce_s256_challenge,
)


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class FakeTokenResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class RaisingStatusTokenResponse:
    def json(self) -> dict[str, Any]:
        return {
            "access_token": "access-token-secret",
            "refresh_token": "refresh-token-secret",
            "expires_in": 3600,
            "scope": YOUTUBE_UPLOAD_SCOPE,
        }

    @property
    def status_code(self) -> int:
        raise RuntimeError("status access exposed access-token-secret")


class InvalidStatusTokenResponse:
    status_code = "not-an-http-status"

    def json(self) -> dict[str, Any]:
        return {
            "access_token": "access-token-secret",
            "refresh_token": "refresh-token-secret",
            "expires_in": 3600,
            "scope": YOUTUBE_UPLOAD_SCOPE,
        }


class FakeTokenTransport:
    def __init__(
        self,
        response: FakeTokenResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def post(self, url: str, *, data: dict[str, str], timeout: float) -> FakeTokenResponse:
        self.calls.append((url, data, timeout))
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


class InMemoryCredentialStore:
    """Test-only credential boundary; the production module provides no fallback."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def load_refresh_token(self, credential_reference: str) -> str | None:
        return self._values.get(credential_reference)

    def save_refresh_token(self, credential_reference: str, refresh_token: str) -> None:
        self._values[credential_reference] = refresh_token


class YouTubeOAuthAuthorizationContractTests(unittest.TestCase):
    def test_authorization_request_uses_loopback_pkce_and_minimum_google_parameters(self) -> None:
        request = build_authorization_request("desktop-client-id")
        parsed = urlparse(request.authorization_url)
        query = parse_qs(parsed.query)
        callback = urlparse(request.callback_url)

        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "https://accounts.google.com/o/oauth2/v2/auth",
        )
        self.assertEqual(query["client_id"], ["desktop-client-id"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["scope"], [YOUTUBE_UPLOAD_SCOPE])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["state"], [request.state])
        self.assertEqual(query["redirect_uri"], [request.callback_url])
        self.assertEqual(callback.scheme, "http")
        self.assertEqual(callback.hostname, "127.0.0.1")
        self.assertIsNotNone(callback.port)
        self.assertGreater(callback.port, 0)
        self.assertNotEqual(callback.path, "/")
        self.assertEqual(
            query["code_challenge"],
            [pkce_s256_challenge(request.code_verifier)],
        )

    def test_authorization_requests_have_distinct_callback_and_secret_values(self) -> None:
        first = build_authorization_request("desktop-client-id")
        second = build_authorization_request("desktop-client-id")

        self.assertNotEqual(first.callback_url, second.callback_url)
        self.assertNotEqual(first.state, second.state)
        self.assertNotEqual(first.code_verifier, second.code_verifier)

    def test_pkce_s256_challenge_matches_rfc7636_example(self) -> None:
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

        self.assertEqual(
            pkce_s256_challenge(verifier),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        )

    def test_callback_success_returns_code_only_through_redacted_value_object(self) -> None:
        request = build_authorization_request("desktop-client-id")
        verifier = OAuthCallbackVerifier(request)
        callback_url = (
            f"{request.callback_url}?code=authorization-code-secret&state={request.state}"
        )

        result = verifier.consume(callback_url)

        self.assertTrue(result.authorized)
        self.assertEqual(result.authorization_code, "authorization-code-secret")
        self.assertNotIn("authorization-code-secret", repr(result))
        self.assertNotIn(request.state, repr(result))

    def test_callback_denial_stops_without_exposing_callback_secret_values(self) -> None:
        request = build_authorization_request("desktop-client-id")
        verifier = OAuthCallbackVerifier(request)
        callback_url = (
            f"{request.callback_url}?error=access_denied&state={request.state}"
        )

        with self.assertRaisesRegex(OAuthAuthorizationError, "authorization denied") as raised:
            verifier.consume(callback_url)

        self.assertNotIn(request.state, str(raised.exception))
        self.assertNotIn(callback_url, str(raised.exception))
        self.assertNotIn(request.code_verifier, str(raised.exception))

    def test_callback_without_code_is_invalid_and_redacted(self) -> None:
        request = build_authorization_request("desktop-client-id")
        verifier = OAuthCallbackVerifier(request)
        callback_url = f"{request.callback_url}?state={request.state}"

        with self.assertRaisesRegex(OAuthAuthorizationError, "authorization response invalid") as raised:
            verifier.consume(callback_url)

        self.assertNotIn(request.state, str(raised.exception))
        self.assertNotIn(callback_url, str(raised.exception))

    def test_callback_with_wrong_state_is_invalid_and_redacted(self) -> None:
        request = build_authorization_request("desktop-client-id")
        verifier = OAuthCallbackVerifier(request)
        callback_url = f"{request.callback_url}?code=code-secret&state=wrong-state-secret"

        with self.assertRaisesRegex(OAuthAuthorizationError, "authorization response invalid") as raised:
            verifier.consume(callback_url)

        self.assertNotIn("code-secret", str(raised.exception))
        self.assertNotIn("wrong-state-secret", str(raised.exception))
        self.assertNotIn(request.state, str(raised.exception))

    def test_callback_with_a_different_path_is_invalid(self) -> None:
        request = build_authorization_request("desktop-client-id")
        verifier = OAuthCallbackVerifier(request)
        callback_url = (
            f"{request.callback_url};not-the-registered-path"
            f"?code=code-secret&state={request.state}"
        )

        with self.assertRaisesRegex(OAuthAuthorizationError, "authorization response invalid"):
            verifier.consume(callback_url)

    def test_malformed_callback_is_normalized_without_echoing_its_value(self) -> None:
        request = build_authorization_request("desktop-client-id")
        verifier = OAuthCallbackVerifier(request)
        malformed_callback = "http://[::1"

        with self.assertRaisesRegex(OAuthAuthorizationError, "authorization response invalid") as raised:
            verifier.consume(malformed_callback)

        self.assertNotIn(malformed_callback, str(raised.exception))
        self.assertNotIn(request.state, str(raised.exception))

    def test_simultaneous_callbacks_allow_exactly_one_consumer(self) -> None:
        request = build_authorization_request("desktop-client-id")
        verifier = OAuthCallbackVerifier(request)
        callback_url = f"{request.callback_url}?code=code-secret&state={request.state}"
        source_lines, source_start = inspect.getsourcelines(OAuthCallbackVerifier.consume)
        consumed_write_line = next(
            source_start + index
            for index, line in enumerate(source_lines)
            if line.strip() == "self._consumed = True"
        )
        synchronized_check = threading.Barrier(2)
        start = threading.Barrier(3)
        successes: list[object] = []
        errors: list[Exception] = []

        def synchronize_unsynchronized_write(frame, event, argument):
            del argument
            if (
                event == "line"
                and frame.f_code is OAuthCallbackVerifier.consume.__code__
                and frame.f_lineno == consumed_write_line
            ):
                try:
                    synchronized_check.wait(timeout=0.2)
                except threading.BrokenBarrierError:
                    pass
            return synchronize_unsynchronized_write

        def consume_callback() -> None:
            start.wait()
            try:
                successes.append(verifier.consume(callback_url))
            except Exception as error:
                errors.append(error)

        previous_trace = threading.gettrace()
        threading.settrace(synchronize_unsynchronized_write)
        try:
            threads = [threading.Thread(target=consume_callback) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join()
        finally:
            threading.settrace(previous_trace)

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OAuthAuthorizationError)
        self.assertEqual(str(errors[0]), "authorization response already consumed")

    def test_callback_can_only_be_consumed_once(self) -> None:
        request = build_authorization_request("desktop-client-id")
        verifier = OAuthCallbackVerifier(request)
        callback_url = f"{request.callback_url}?code=code-secret&state={request.state}"

        verifier.consume(callback_url)
        with self.assertRaisesRegex(OAuthAuthorizationError, "authorization response already consumed"):
            verifier.consume(callback_url)

    def test_value_objects_are_immutable_and_secret_safe_in_representations(self) -> None:
        request = build_authorization_request("desktop-client-id")
        result = OAuthCallbackVerifier(request).consume(
            f"{request.callback_url}?code=code-secret&state={request.state}"
        )

        with self.assertRaises(FrozenInstanceError):
            request.state = "replacement"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            result.authorization_code = "replacement"  # type: ignore[misc]
        for value in (repr(request), str(request), repr(result), str(result)):
            self.assertNotIn(request.state, value)
            self.assertNotIn(request.code_verifier, value)
            self.assertNotIn(request.callback_url, value)
            self.assertNotIn("code-secret", value)


class YouTubeOAuthTokenLifecycleTests(unittest.TestCase):
    def _authorization_values(self):
        request = build_authorization_request("desktop-client-id")
        callback = OAuthCallbackVerifier(request).consume(
            f"{request.callback_url}?code=authorization-code-secret&state={request.state}"
        )
        return request, callback

    def test_code_exchange_uses_pkce_and_returns_redacted_token_expiry(self) -> None:
        request, callback = self._authorization_values()
        transport = FakeTokenTransport(
            FakeTokenResponse(
                200,
                {
                    "access_token": "access-token-secret",
                    "refresh_token": "refresh-token-secret",
                    "expires_in": 3600,
                    "scope": YOUTUBE_UPLOAD_SCOPE,
                    "token_type": "Bearer",
                },
            )
        )
        client = YouTubeOAuthTokenClient(transport, clock=lambda: 1000.0)

        tokens = client.exchange_authorization_code(
            client_id="desktop-client-id",
            request=request,
            callback=callback,
        )

        self.assertEqual(tokens.access_token, "access-token-secret")
        self.assertEqual(tokens.refresh_token, "refresh-token-secret")
        self.assertEqual(tokens.expires_at, 4600.0)
        self.assertEqual(tokens.scope, YOUTUBE_UPLOAD_SCOPE)
        self.assertNotIn("access-token-secret", repr(tokens))
        self.assertNotIn("refresh-token-secret", str(tokens))
        self.assertEqual(len(transport.calls), 1)
        endpoint, data, timeout = transport.calls[0]
        self.assertEqual(endpoint, "https://oauth2.googleapis.com/token")
        self.assertEqual(timeout, 10.0)
        self.assertEqual(
            data,
            {
                "client_id": "desktop-client-id",
                "code": "authorization-code-secret",
                "code_verifier": request.code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": request.callback_url,
            },
        )

    def test_refresh_preserves_existing_refresh_token_when_response_omits_it(self) -> None:
        transport = FakeTokenTransport(
            FakeTokenResponse(
                200,
                {
                    "access_token": "replacement-access-token-secret",
                    "expires_in": "120",
                    "scope": YOUTUBE_UPLOAD_SCOPE,
                    "token_type": "Bearer",
                },
            )
        )
        client = YouTubeOAuthTokenClient(transport, clock=lambda: 25.0)
        existing = OAuthTokens(
            access_token="old-access-token-secret",
            refresh_token="existing-refresh-token-secret",
            expires_at=30.0,
            scope=YOUTUBE_UPLOAD_SCOPE,
        )

        refreshed = client.refresh_access_token(
            client_id="desktop-client-id",
            existing_tokens=existing,
        )

        self.assertEqual(refreshed.access_token, "replacement-access-token-secret")
        self.assertEqual(refreshed.refresh_token, "existing-refresh-token-secret")
        self.assertEqual(refreshed.expires_at, 145.0)
        self.assertEqual(
            transport.calls[0][1],
            {
                "client_id": "desktop-client-id",
                "grant_type": "refresh_token",
                "refresh_token": "existing-refresh-token-secret",
            },
        )

    def test_invalid_grant_is_normalized_without_tokens_or_response_body(self) -> None:
        transport = FakeTokenTransport(
            FakeTokenResponse(
                400,
                {
                    "error": "invalid_grant",
                    "error_description": "refresh-token-secret was revoked",
                },
            )
        )
        client = YouTubeOAuthTokenClient(transport, clock=lambda: 0.0)
        existing = OAuthTokens(
            access_token="access-token-secret",
            refresh_token="refresh-token-secret",
            expires_at=1.0,
            scope=YOUTUBE_UPLOAD_SCOPE,
        )

        with self.assertRaisesRegex(OAuthTokenError, "^authorization_invalid$") as raised:
            client.refresh_access_token(
                client_id="desktop-client-id",
                existing_tokens=existing,
            )

        self.assertNotIn("refresh-token-secret", str(raised.exception))
        self.assertNotIn("access-token-secret", str(raised.exception))
        self.assertNotIn("was revoked", str(raised.exception))

    def test_timeout_is_normalized_without_request_values(self) -> None:
        request, callback = self._authorization_values()
        transport = FakeTokenTransport(error=TimeoutError("timeout access-token-secret"))
        client = YouTubeOAuthTokenClient(transport, clock=lambda: 0.0)

        with self.assertRaisesRegex(OAuthTokenError, "^oauth_transport_unavailable$") as raised:
            client.exchange_authorization_code(
                client_id="desktop-client-id",
                request=request,
                callback=callback,
            )

        self.assertNotIn("access-token-secret", str(raised.exception))
        self.assertNotIn(callback.authorization_code, str(raised.exception))
        self.assertNotIn(request.code_verifier, str(raised.exception))
        self.assertNotIn(request.callback_url, str(raised.exception))

    def test_token_endpoint_rejection_hides_raw_response_body(self) -> None:
        request, callback = self._authorization_values()
        transport = FakeTokenTransport(
            FakeTokenResponse(
                500,
                {
                    "error": "server_error",
                    "error_description": "authorization-code-secret and access-token-secret",
                },
            )
        )
        client = YouTubeOAuthTokenClient(transport, clock=lambda: 0.0)

        with self.assertRaisesRegex(OAuthTokenError, "^oauth_token_rejected$") as raised:
            client.exchange_authorization_code(
                client_id="desktop-client-id",
                request=request,
                callback=callback,
            )

        self.assertNotIn("authorization-code-secret", str(raised.exception))
        self.assertNotIn("access-token-secret", str(raised.exception))
        self.assertNotIn("server_error", str(raised.exception))

    def test_raising_response_status_is_normalized_without_secret_values(self) -> None:
        request, callback = self._authorization_values()
        transport = FakeTokenTransport(response=RaisingStatusTokenResponse())
        client = YouTubeOAuthTokenClient(transport, clock=lambda: 0.0)

        with self.assertRaisesRegex(OAuthTokenError, "^oauth_token_response_invalid$") as raised:
            client.exchange_authorization_code(
                client_id="desktop-client-id",
                request=request,
                callback=callback,
            )

        self.assertNotIn("access-token-secret", str(raised.exception))
        self.assertNotIn(callback.authorization_code, str(raised.exception))
        self.assertNotIn(request.code_verifier, str(raised.exception))

    def test_non_integer_response_status_is_normalized_without_secret_values(self) -> None:
        request, callback = self._authorization_values()
        transport = FakeTokenTransport(response=InvalidStatusTokenResponse())
        client = YouTubeOAuthTokenClient(transport, clock=lambda: 0.0)

        with self.assertRaisesRegex(OAuthTokenError, "^oauth_token_response_invalid$") as raised:
            client.exchange_authorization_code(
                client_id="desktop-client-id",
                request=request,
                callback=callback,
            )

        self.assertNotIn("access-token-secret", str(raised.exception))
        self.assertNotIn(callback.authorization_code, str(raised.exception))
        self.assertNotIn(request.callback_url, str(raised.exception))

    def test_credential_store_is_an_injected_protocol_without_cleartext_fallback(self) -> None:
        store = InMemoryCredentialStore()

        self.assertIsInstance(store, OAuthCredentialStore)
        self.assertIsNone(store.load_refresh_token("youtube:channel-1"))
        store.save_refresh_token("youtube:channel-1", "refresh-token-secret")
        self.assertEqual(
            store.load_refresh_token("youtube:channel-1"),
            "refresh-token-secret",
        )


if __name__ == "__main__":
    unittest.main()
