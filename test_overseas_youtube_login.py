# -*- coding: utf-8 -*-
"""YouTube 官方桌面 OAuth 登录与系统凭据库的离线契约。"""

from __future__ import annotations

import queue
import unittest

from app_core.overseas_youtube_api import YouTubeChannelIdentity
from app_core.overseas_youtube_credentials import (
    KeyringOAuthClientSecretStore,
    KeyringOAuthCredentialStore,
    OAuthCredentialError,
)
from app_core.overseas_youtube_login import (
    YouTubeAuthorizedSession,
    YouTubeOAuthLoginError,
    YouTubeOAuthLoginSession,
    authorize_saved_youtube_account,
    validate_saved_youtube_oauth_account,
)
from app_core.overseas_youtube_oauth import (
    OAuthAuthorizationCallback,
    OAuthAuthorizationRequest,
    OAuthTokens,
    YOUTUBE_OAUTH_SCOPE,
)


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str | None]] = []
        self.failure: Exception | None = None

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.failure:
            raise self.failure
        self.calls.append(("save", service, username))
        self.values[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        if self.failure:
            raise self.failure
        self.calls.append(("load", service, username))
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if self.failure:
            raise self.failure
        self.calls.append(("delete", service, username))
        self.values.pop((service, username), None)


class YouTubeCredentialStoreTests(unittest.TestCase):
    def test_keyring_client_secret_store_round_trips_without_plaintext_fallback(self) -> None:
        backend = FakeKeyring()
        store = KeyringOAuthClientSecretStore(
            backend=backend,
            service_name="com.hellomobai.yijianfa.youtube.client.test",
        )
        client_id = "desktop-client.apps.googleusercontent.com"
        client_secret = "desktop-client-secret-must-stay-private"

        store.save_client_secret(client_id, client_secret)
        self.assertEqual(store.load_client_secret(client_id), client_secret)

        self.assertNotIn(client_secret, repr(store))
        self.assertFalse(any(client_secret in repr(call) for call in backend.calls))

    def test_keyring_store_round_trips_and_deletes_without_plaintext_fallback(self) -> None:
        backend = FakeKeyring()
        store = KeyringOAuthCredentialStore(
            backend=backend,
            service_name="com.hellomobai.yijianfa.youtube.test",
        )
        reference = "youtube-oauth:reference-123"
        refresh_token = "refresh-token-must-stay-secret"

        store.save_refresh_token(reference, refresh_token)
        self.assertEqual(store.load_refresh_token(reference), refresh_token)
        store.delete_refresh_token(reference)
        self.assertIsNone(store.load_refresh_token(reference))

        self.assertNotIn(refresh_token, repr(store))
        self.assertFalse(any(refresh_token in repr(call) for call in backend.calls))
        self.assertEqual(
            backend.calls,
            [
                ("save", "com.hellomobai.yijianfa.youtube.test", reference),
                ("load", "com.hellomobai.yijianfa.youtube.test", reference),
                ("load", "com.hellomobai.yijianfa.youtube.test", reference),
                ("delete", "com.hellomobai.yijianfa.youtube.test", reference),
                ("load", "com.hellomobai.yijianfa.youtube.test", reference),
            ],
        )

    def test_keyring_store_rejects_invalid_values_and_redacts_backend_failures(self) -> None:
        backend = FakeKeyring()
        store = KeyringOAuthCredentialStore(backend=backend)
        secret = "provider-secret-in-error"

        for reference, token in (
            ("", "token"),
            ("../not-a-reference", "token"),
            ("youtube-oauth:valid", ""),
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(OAuthCredentialError) as caught:
                    store.save_refresh_token(reference, token)
                self.assertEqual(str(caught.exception), "credential_unavailable")

        backend.failure = RuntimeError(secret)
        with self.assertRaises(OAuthCredentialError) as caught:
            store.save_refresh_token("youtube-oauth:valid", secret)
        self.assertEqual(str(caught.exception), "credential_unavailable")
        self.assertNotIn(secret, repr(caught.exception))


class FakeAuthorizationSession:
    def __init__(self) -> None:
        self.request = OAuthAuthorizationRequest(
            authorization_url="https://accounts.google.com/o/oauth2/v2/auth?redacted=1",
            callback_url="http://127.0.0.1:45678/oauth/callback/redacted",
            state="secret-state",
            code_verifier="secret-verifier",
        )
        self.cancelled = False
        self.receive_calls: list[float] = []

    def receive_callback(self, *, timeout_seconds: float) -> OAuthAuthorizationCallback:
        self.receive_calls.append(timeout_seconds)
        return OAuthAuthorizationCallback(
            authorized=True,
            authorization_code="secret-code",
        )

    def cancel(self) -> None:
        self.cancelled = True


class FakeTokenClient:
    def __init__(self) -> None:
        self.exchange_calls: list[dict] = []
        self.refresh_calls: list[dict] = []

    def exchange_authorization_code(self, **kwargs) -> OAuthTokens:
        self.exchange_calls.append(kwargs)
        return OAuthTokens(
            access_token="access-token-secret",
            refresh_token="refresh-token-secret",
            expires_at=3600.0,
            scope=YOUTUBE_OAUTH_SCOPE,
        )

    def refresh_access_token(self, **kwargs) -> OAuthTokens:
        self.refresh_calls.append(kwargs)
        return OAuthTokens(
            access_token="refreshed-access-secret",
            refresh_token=kwargs["existing_tokens"].refresh_token,
            expires_at=7200.0,
            scope=YOUTUBE_OAUTH_SCOPE,
        )


class FakeChannelClient:
    def __init__(self, identity: YouTubeChannelIdentity) -> None:
        self.identity = identity
        self.access_tokens: list[str] = []

    def lookup_authenticated_channel(self, access_token: str) -> YouTubeChannelIdentity:
        self.access_tokens.append(access_token)
        return self.identity


class InMemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deleted: list[str] = []

    def load_refresh_token(self, credential_reference: str) -> str | None:
        return self.values.get(credential_reference)

    def save_refresh_token(self, credential_reference: str, refresh_token: str) -> None:
        self.values[credential_reference] = refresh_token

    def delete_refresh_token(self, credential_reference: str) -> None:
        self.deleted.append(credential_reference)
        self.values.pop(credential_reference, None)


class InMemoryClientSecretStore:
    def __init__(self, secret: str | None = "desktop-client-secret") -> None:
        self.secret = secret
        self.loaded_client_ids: list[str] = []

    def load_client_secret(self, client_id: str) -> str | None:
        self.loaded_client_ids.append(client_id)
        return self.secret


class YouTubeOAuthLoginSessionTests(unittest.TestCase):
    def _session(self, **overrides) -> tuple[YouTubeOAuthLoginSession, dict]:
        authorization_session = FakeAuthorizationSession()
        token_client = FakeTokenClient()
        channel_client = FakeChannelClient(
            YouTubeChannelIdentity(channel_id="UC-current", display_name="当前频道")
        )
        credential_store = InMemoryCredentialStore()
        client_secret_store = InMemoryClientSecretStore()
        saved: list[dict] = []
        opened: list[str] = []

        def account_saver(**kwargs) -> int:
            saved.append(kwargs)
            return 42

        values = {
            "client_id": "desktop-client.apps.googleusercontent.com",
            "profile_name": "海外主体",
            "update_mode": False,
            "record_id": None,
            "existing_account": None,
            "account_saver": account_saver,
            "credential_store": credential_store,
            "client_secret_store": client_secret_store,
            "browser_opener": lambda url: opened.append(url) or True,
            "authorization_session_factory": lambda _client_id: authorization_session,
            "token_client": token_client,
            "channel_client": channel_client,
            "credential_reference_factory": lambda: "youtube-oauth:new-reference",
            "callback_timeout_seconds": 30.0,
        }
        values.update(overrides)
        session = YouTubeOAuthLoginSession(**values)
        return session, {
            "authorization_session": authorization_session,
            "token_client": token_client,
            "channel_client": channel_client,
            "credential_store": credential_store,
            "client_secret_store": client_secret_store,
            "saved": saved,
            "opened": opened,
        }

    def test_system_browser_login_saves_only_refresh_token_and_public_channel_identity(self) -> None:
        session, evidence = self._session()

        session.run()

        messages = []
        while True:
            try:
                messages.append(session.queue.get_nowait())
            except queue.Empty:
                break
        self.assertEqual(messages, ["BROWSER_OPENED", "ACCOUNT_SAVED:42"])
        self.assertEqual(
            evidence["opened"],
            ["https://accounts.google.com/o/oauth2/v2/auth?redacted=1"],
        )
        self.assertEqual(evidence["authorization_session"].receive_calls, [30.0])
        self.assertEqual(
            evidence["token_client"].exchange_calls[0]["client_secret"],
            "desktop-client-secret",
        )
        self.assertEqual(
            evidence["credential_store"].values,
            {"youtube-oauth:new-reference": "refresh-token-secret"},
        )
        self.assertEqual(
            evidence["saved"],
            [
                {
                    "profile_name": "海外主体",
                    "credential_reference": "youtube-oauth:new-reference",
                    "channel_id": "UC-current",
                    "display_name": "当前频道",
                    "record_id": None,
                    "oauth_scope_version": 2,
                }
            ],
        )
        public_text = repr(messages) + repr(evidence["saved"])
        for secret in (
            "secret-code",
            "secret-state",
            "secret-verifier",
            "access-token-secret",
            "refresh-token-secret",
        ):
            self.assertNotIn(secret, public_text)

    def test_relogin_refuses_a_different_channel_without_overwriting_saved_credential(self) -> None:
        store = InMemoryCredentialStore()
        store.values["youtube-oauth:existing"] = "old-refresh-token"
        session, evidence = self._session(
            update_mode=True,
            record_id=9,
            existing_account={
                "id": 9,
                "type": 7,
                "authMode": "youtube_oauth",
                "filePath": "youtube-oauth:existing",
                "accountReference": "UC-expected",
            },
            credential_store=store,
        )

        with self.assertRaises(YouTubeOAuthLoginError) as caught:
            session.run()

        self.assertEqual(str(caught.exception), "channel_identity_mismatch")
        self.assertEqual(store.values["youtube-oauth:existing"], "old-refresh-token")
        self.assertEqual(evidence["saved"], [])

    def test_publish_authorization_rejects_legacy_scope_before_loading_secret(self) -> None:
        store = InMemoryCredentialStore()
        store.values["youtube-oauth:legacy-ref"] = "legacy-refresh-secret"
        token_client = FakeTokenClient()
        channel_client = FakeChannelClient(
            YouTubeChannelIdentity(channel_id="UC-legacy", display_name="旧频道")
        )
        account = {
            "id": 12,
            "type": 7,
            "authMode": "youtube_oauth",
            "filePath": "youtube-oauth:legacy-ref",
            "accountReference": "UC-legacy",
            "oauthScopeVersion": 1,
        }

        with self.assertRaises(YouTubeOAuthLoginError) as caught:
            authorize_saved_youtube_account(
                account,
                client_id="desktop-client.apps.googleusercontent.com",
                credential_store=store,
                client_secret_store=InMemoryClientSecretStore(),
                token_client=token_client,
                channel_client=channel_client,
                require_publish_scope=True,
            )

        self.assertEqual(
            str(caught.exception),
            "youtube_oauth_scope_upgrade_required",
        )
        self.assertEqual(token_client.refresh_calls, [])
        self.assertEqual(channel_client.access_tokens, [])

    def test_publish_authorization_returns_redacted_session_for_scope_v2(self) -> None:
        store = InMemoryCredentialStore()
        store.values["youtube-oauth:v2-ref"] = "saved-refresh-secret"
        token_client = FakeTokenClient()
        channel_client = FakeChannelClient(
            YouTubeChannelIdentity(channel_id="UC-v2", display_name="发布频道")
        )
        account = {
            "id": 13,
            "type": 7,
            "authMode": "youtube_oauth",
            "filePath": "youtube-oauth:v2-ref",
            "accountReference": "UC-v2",
            "oauthScopeVersion": 2,
        }

        authorized = authorize_saved_youtube_account(
            account,
            client_id="desktop-client.apps.googleusercontent.com",
            credential_store=store,
            client_secret_store=InMemoryClientSecretStore(),
            token_client=token_client,
            channel_client=channel_client,
            require_publish_scope=True,
        )

        self.assertIsInstance(authorized, YouTubeAuthorizedSession)
        self.assertEqual(authorized.identity.channel_id, "UC-v2")
        self.assertEqual(authorized.access_token, "refreshed-access-secret")
        self.assertNotIn("refreshed-access-secret", repr(authorized))
        self.assertNotIn("saved-refresh-secret", repr(authorized))

    def test_new_login_rolls_back_keyring_when_account_persistence_fails(self) -> None:
        store = InMemoryCredentialStore()

        def fail_save(**_kwargs) -> int:
            raise RuntimeError("database body must not escape")

        session, _evidence = self._session(
            credential_store=store,
            account_saver=fail_save,
        )

        with self.assertRaises(YouTubeOAuthLoginError) as caught:
            session.run()

        self.assertEqual(str(caught.exception), "account_persistence_failed")
        self.assertEqual(store.values, {})
        self.assertEqual(store.deleted, ["youtube-oauth:new-reference"])

    def test_missing_client_id_stops_before_browser_or_callback_listener(self) -> None:
        factory_calls: list[str] = []
        session, evidence = self._session(
            client_id="",
            authorization_session_factory=lambda client_id: factory_calls.append(client_id),
        )

        with self.assertRaises(YouTubeOAuthLoginError) as caught:
            session.run()

        self.assertEqual(str(caught.exception), "youtube_oauth_client_not_configured")
        self.assertEqual(factory_calls, [])
        self.assertEqual(evidence["opened"], [])

    def test_missing_client_secret_stops_before_browser_or_callback_listener(self) -> None:
        factory_calls: list[str] = []
        session, evidence = self._session(
            client_secret_store=InMemoryClientSecretStore(None),
            authorization_session_factory=lambda client_id: factory_calls.append(client_id),
        )

        with self.assertRaises(YouTubeOAuthLoginError) as caught:
            session.run()

        self.assertEqual(
            str(caught.exception),
            "youtube_oauth_client_secret_not_configured",
        )
        self.assertEqual(factory_calls, [])
        self.assertEqual(evidence["opened"], [])

    def test_restart_validation_refreshes_token_and_requires_the_same_channel(self) -> None:
        store = InMemoryCredentialStore()
        store.values["youtube-oauth:restart-ref"] = "saved-refresh-secret"
        token_client = FakeTokenClient()
        channel_client = FakeChannelClient(
            YouTubeChannelIdentity(channel_id="UC-restart", display_name="重启频道")
        )
        account = {
            "id": 11,
            "type": 7,
            "authMode": "youtube_oauth",
            "filePath": "youtube-oauth:restart-ref",
            "accountReference": "UC-restart",
        }

        identity = validate_saved_youtube_oauth_account(
            account,
            client_id="desktop-client.apps.googleusercontent.com",
            credential_store=store,
            client_secret_store=InMemoryClientSecretStore(),
            token_client=token_client,
            channel_client=channel_client,
        )

        self.assertEqual(identity.channel_id, "UC-restart")
        self.assertEqual(channel_client.access_tokens, ["refreshed-access-secret"])
        self.assertEqual(len(token_client.refresh_calls), 1)
        self.assertEqual(
            token_client.refresh_calls[0]["client_secret"],
            "desktop-client-secret",
        )
        existing = token_client.refresh_calls[0]["existing_tokens"]
        self.assertEqual(existing.refresh_token, "saved-refresh-secret")
        self.assertNotIn("saved-refresh-secret", repr(identity))

        channel_client.identity = YouTubeChannelIdentity(
            channel_id="UC-other",
            display_name="其他频道",
        )
        with self.assertRaises(YouTubeOAuthLoginError) as caught:
            validate_saved_youtube_oauth_account(
                account,
                client_id="desktop-client.apps.googleusercontent.com",
                credential_store=store,
                client_secret_store=InMemoryClientSecretStore(),
                token_client=token_client,
                channel_client=channel_client,
            )
        self.assertEqual(str(caught.exception), "channel_identity_mismatch")


if __name__ == "__main__":
    unittest.main()
