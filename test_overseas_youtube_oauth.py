# -*- coding: utf-8 -*-
"""Offline contract tests for the desktop YouTube OAuth authorization flow."""

import unittest
from dataclasses import FrozenInstanceError
from urllib.parse import parse_qs, urlparse

from app_core.overseas_youtube_oauth import (
    OAuthAuthorizationError,
    OAuthCallbackVerifier,
    build_authorization_request,
    pkce_s256_challenge,
)


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


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


if __name__ == "__main__":
    unittest.main()
