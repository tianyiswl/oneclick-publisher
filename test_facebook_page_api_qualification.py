from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


_USER_TOKEN = "EAATEST_USER_ACCESS_TOKEN_DO_NOT_LOG"
_PAGE_TOKEN = "EAATEST_PAGE_ACCESS_TOKEN_DO_NOT_LOG"
_TARGET_PAGE_ID = "1200693729803807"
_REQUIRED_SCOPES = (
    "pages_manage_posts",
    "pages_read_engagement",
    "pages_show_list",
)


def _subject():
    try:
        return importlib.import_module(
            "app_core.overseas_facebook_api_qualification"
        )
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "Facebook Page API 资格预检实现尚不存在"
        ) from exc


def _cli_subject():
    try:
        return importlib.import_module(
            "tools.check_facebook_page_api_qualification"
        )
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "Facebook Page API 资格预检 CLI 尚不存在"
        ) from exc


class _Response:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = int(status_code)

    def json(self) -> object:
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("资格预检发出了未预期的网络请求")
        return self.responses.pop(0)


def _permissions(*scopes: str) -> _Response:
    return _Response(
        {
            "data": [
                {"permission": scope, "status": "granted"}
                for scope in scopes
            ]
        }
    )


def _page(
    *,
    page_id: str = _TARGET_PAGE_ID,
    tasks: tuple[str, ...] = ("PROFILE_PLUS_CREATE_CONTENT",),
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "id": page_id,
        "name": "资格预检测试 Page",
        "tasks": list(tasks),
    }
    value.update(extra or {})
    return value


class FacebookPageApiQualificationClientTests(unittest.TestCase):
    def test_page_scope_task_probe_is_read_only_but_not_api_ready(self) -> None:
        subject = _subject()
        session = _Session(
            [
                _permissions(*_REQUIRED_SCOPES),
                _Response({"data": [_page()], "paging": {}}),
            ]
        )
        client = subject.FacebookPageApiQualificationClient(
            session=session,
            graph_version="v25.0",
        )

        result = client.qualify(
            user_access_token=_USER_TOKEN,
            target_page_id=_TARGET_PAGE_ID,
        ).to_public_dict()

        self.assertEqual(
            result,
            {
                "status": "page_scope_task_verified",
                "qualified": False,
                "pagePermissionCheckPassed": True,
                "appAuthorizationVerified": False,
                "apiModeUsable": False,
                "targetPageId": _TARGET_PAGE_ID,
                "pageName": "资格预检测试 Page",
                "grantedScopes": list(_REQUIRED_SCOPES),
                "missingScopes": [],
                "pageTasks": ["PROFILE_PLUS_CREATE_CONTENT"],
                "createContentTaskGranted": True,
                "writeAttempted": False,
            },
        )
        self.assertEqual([call["method"] for call in session.calls], ["GET", "GET"])
        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://graph.facebook.com/v25.0/me/permissions",
                "https://graph.facebook.com/v25.0/me/accounts",
            ],
        )
        for call in session.calls:
            self.assertEqual(
                call["headers"],
                {"Accept": "application/json", "Authorization": f"Bearer {_USER_TOKEN}"},
            )
            self.assertNotIn("access_token", str(call.get("params") or {}))
            self.assertNotIn(_USER_TOKEN, str(call["url"]))
            self.assertIs(call["allow_redirects"], False)
        account_fields = str(session.calls[1]["params"])
        self.assertIn("id,name,tasks", account_fields)
        self.assertNotIn("access_token", account_fields)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(_USER_TOKEN, serialized)
        self.assertNotIn(_PAGE_TOKEN, serialized)

    def test_page_id_uses_the_shared_facebook_identity_normalizer(self) -> None:
        subject = _subject()
        session = _Session(
            [
                _permissions(*_REQUIRED_SCOPES),
                _Response({"data": [_page(page_id="1")], "paging": {}}),
            ]
        )
        client = subject.FacebookPageApiQualificationClient(session=session)

        result = client.qualify(
            user_access_token=_USER_TOKEN,
            target_page_id="1",
        ).to_public_dict()

        self.assertEqual(result["targetPageId"], "1")
        self.assertTrue(result["pagePermissionCheckPassed"])
        self.assertFalse(result["apiModeUsable"])

    def test_missing_scope_stops_before_page_lookup(self) -> None:
        subject = _subject()
        session = _Session(
            [_permissions("pages_read_engagement", "pages_show_list")]
        )
        client = subject.FacebookPageApiQualificationClient(session=session)

        result = client.qualify(
            user_access_token=_USER_TOKEN,
            target_page_id=_TARGET_PAGE_ID,
        ).to_public_dict()

        self.assertEqual(result["status"], "permissions_missing")
        self.assertFalse(result["qualified"])
        self.assertEqual(result["missingScopes"], ["pages_manage_posts"])
        self.assertEqual(result["pageTasks"], [])
        self.assertFalse(result["createContentTaskGranted"])
        self.assertEqual(len(session.calls), 1)

    def test_target_page_without_create_content_task_is_not_qualified(self) -> None:
        subject = _subject()
        session = _Session(
            [
                _permissions(*_REQUIRED_SCOPES),
                _Response(
                    {
                        "data": [
                            _page(tasks=("PROFILE_PLUS_ANALYZE", "PROFILE_PLUS_MODERATE"))
                        ],
                        "paging": {},
                    }
                ),
            ]
        )
        client = subject.FacebookPageApiQualificationClient(session=session)

        result = client.qualify(
            user_access_token=_USER_TOKEN,
            target_page_id=_TARGET_PAGE_ID,
        ).to_public_dict()

        self.assertEqual(result["status"], "create_content_task_missing")
        self.assertFalse(result["qualified"])
        self.assertEqual(
            result["pageTasks"],
            ["PROFILE_PLUS_ANALYZE", "PROFILE_PLUS_MODERATE"],
        )
        self.assertFalse(result["createContentTaskGranted"])

    def test_pagination_reuses_fixed_graph_endpoint_instead_of_next_url(self) -> None:
        subject = _subject()
        session = _Session(
            [
                _permissions(*_REQUIRED_SCOPES),
                _Response(
                    {
                        "data": [_page(page_id="1001")],
                        "paging": {
                            "cursors": {"after": "opaque-cursor-1"},
                            "next": (
                                "https://evil.example/steal?access_token="
                                f"{_USER_TOKEN}"
                            ),
                        },
                    }
                ),
                _Response({"data": [_page()], "paging": {}}),
            ]
        )
        client = subject.FacebookPageApiQualificationClient(session=session)

        result = client.qualify(
            user_access_token=_USER_TOKEN,
            target_page_id=_TARGET_PAGE_ID,
        ).to_public_dict()

        self.assertTrue(result["pagePermissionCheckPassed"])
        self.assertFalse(result["qualified"])
        self.assertFalse(result["apiModeUsable"])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(
            session.calls[2]["url"],
            "https://graph.facebook.com/v25.0/me/accounts",
        )
        self.assertEqual(
            session.calls[2]["params"],
            {"fields": "id,name,tasks", "limit": 100, "after": "opaque-cursor-1"},
        )
        self.assertNotIn("evil.example", str(session.calls))

    def test_graph_token_error_is_stable_and_redacts_response_message(self) -> None:
        subject = _subject()
        session = _Session(
            [
                _Response(
                    {
                        "error": {
                            "code": 190,
                            "message": f"Invalid token {_USER_TOKEN}",
                        }
                    },
                    status_code=400,
                )
            ]
        )
        client = subject.FacebookPageApiQualificationClient(session=session)

        with self.assertRaises(subject.FacebookPageApiQualificationError) as raised:
            client.qualify(
                user_access_token=_USER_TOKEN,
                target_page_id=_TARGET_PAGE_ID,
            )

        self.assertEqual(raised.exception.code, "facebook_api_token_rejected")
        self.assertNotIn(_USER_TOKEN, str(raised.exception))
        self.assertNotIn("Invalid token", str(raised.exception))

    def test_malformed_pagination_cursor_fails_closed(self) -> None:
        subject = _subject()
        session = _Session(
            [
                _permissions(*_REQUIRED_SCOPES),
                _Response(
                    {
                        "data": [_page(page_id="1001")],
                        "paging": {"cursors": {"after": "bad\ncursor"}},
                    }
                ),
            ]
        )
        client = subject.FacebookPageApiQualificationClient(session=session)

        with self.assertRaises(subject.FacebookPageApiQualificationError) as raised:
            client.qualify(
                user_access_token=_USER_TOKEN,
                target_page_id=_TARGET_PAGE_ID,
            )

        self.assertEqual(raised.exception.code, "facebook_api_response_invalid")

    def test_repeated_pagination_cursor_fails_closed(self) -> None:
        subject = _subject()
        session = _Session(
            [
                _permissions(*_REQUIRED_SCOPES),
                _Response(
                    {
                        "data": [_page(page_id="1001")],
                        "paging": {"cursors": {"after": "same-cursor"}},
                    }
                ),
                _Response(
                    {
                        "data": [_page(page_id="1002")],
                        "paging": {"cursors": {"after": "same-cursor"}},
                    }
                ),
            ]
        )
        client = subject.FacebookPageApiQualificationClient(session=session)

        with self.assertRaises(subject.FacebookPageApiQualificationError) as raised:
            client.qualify(
                user_access_token=_USER_TOKEN,
                target_page_id=_TARGET_PAGE_ID,
            )

        self.assertEqual(raised.exception.code, "facebook_api_response_invalid")

    def test_unrequested_page_token_in_response_is_never_projected(self) -> None:
        subject = _subject()
        session = _Session(
            [
                _permissions(*_REQUIRED_SCOPES),
                _Response(
                    {
                        "data": [
                            _page(extra={"access_token": _PAGE_TOKEN})
                        ],
                        "paging": {},
                    }
                ),
            ]
        )
        client = subject.FacebookPageApiQualificationClient(session=session)

        result = client.qualify(
            user_access_token=_USER_TOKEN,
            target_page_id=_TARGET_PAGE_ID,
        ).to_public_dict()

        self.assertTrue(result["pagePermissionCheckPassed"])
        self.assertFalse(result["qualified"])
        self.assertFalse(result["apiModeUsable"])
        self.assertNotIn(_PAGE_TOKEN, json.dumps(result, ensure_ascii=False))


class _Keyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


class FacebookPageApiCredentialStoreTests(unittest.TestCase):
    def test_user_token_round_trip_uses_page_scoped_keyring_reference(self) -> None:
        subject = _subject()
        keyring = _Keyring()
        store = subject.KeyringFacebookPageApiCredentialStore(backend=keyring)

        store.save_user_access_token(_TARGET_PAGE_ID, _USER_TOKEN)

        self.assertTrue(store.has_user_access_token(_TARGET_PAGE_ID))
        self.assertEqual(
            store.load_user_access_token(_TARGET_PAGE_ID),
            _USER_TOKEN,
        )
        self.assertEqual(
            list(keyring.values),
            [
                (
                    subject.FACEBOOK_PAGE_API_KEYRING_SERVICE,
                    f"facebook-page-api:{_TARGET_PAGE_ID}",
                )
            ],
        )
        self.assertNotIn(_USER_TOKEN, repr(store))

    def test_keyring_read_failure_is_stable_and_secret_free(self) -> None:
        subject = _subject()

        class FailingKeyring(_Keyring):
            def get_password(self, service: str, username: str) -> str | None:
                raise RuntimeError(f"backend leaked {_USER_TOKEN}")

        store = subject.KeyringFacebookPageApiCredentialStore(
            backend=FailingKeyring()
        )

        with self.assertRaises(subject.FacebookPageApiQualificationError) as raised:
            store.load_user_access_token(_TARGET_PAGE_ID)

        self.assertEqual(
            raised.exception.code,
            "facebook_api_credential_unavailable",
        )
        self.assertNotIn(_USER_TOKEN, str(raised.exception))


class _CredentialStore:
    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.loaded_page_ids: list[str] = []
        self.saved: list[tuple[str, str]] = []

    def load_user_access_token(self, page_id: object) -> str | None:
        self.loaded_page_ids.append(str(page_id))
        return self.token

    def save_user_access_token(self, page_id: object, token: object) -> None:
        self.saved.append((str(page_id), str(token)))


def _saved_account() -> dict[str, object]:
    return {
        "id": 14,
        "type": 9,
        "status": 1,
        "authMode": "browser",
        "filePath": "facebook-page-session.json",
        "accountReference": _TARGET_PAGE_ID,
        "userName": "资格预检测试 Page",
    }


class FacebookPageSavedAccountQualificationTests(unittest.TestCase):
    def test_saved_account_loader_opens_sqlite_in_read_only_mode(self) -> None:
        subject = _subject()
        with tempfile.TemporaryDirectory() as tmpdir:
            database_path = Path(tmpdir) / "account.db"
            with sqlite3.connect(database_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE user_info (
                        id INTEGER PRIMARY KEY,
                        type INTEGER,
                        filePath TEXT,
                        userName TEXT,
                        status INTEGER,
                        profileName TEXT,
                        avatarPath TEXT,
                        avatarUpdatedAt TEXT,
                        remark TEXT,
                        lastCheckedAt TEXT,
                        lastLoginAt TEXT,
                        authMode TEXT,
                        accountReference TEXT,
                        oauthScopeVersion INTEGER
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO user_info
                        (id, type, filePath, userName, status, profileName,
                         authMode, accountReference, oauthScopeVersion)
                    VALUES (14, 9, 'facebook-page-session.json',
                            '资格预检测试 Page', 1, '墨白', 'browser', ?, 1)
                    """,
                    (_TARGET_PAGE_ID,),
                )
            before = database_path.stat().st_mtime_ns

            loader = getattr(
                subject, "load_saved_facebook_page_account_read_only", None
            )
            self.assertTrue(callable(loader), "缺少只读账号加载器")
            account = loader(14, database_path=database_path)

            self.assertEqual(account["accountReference"], _TARGET_PAGE_ID)
            self.assertEqual(account["authMode"], "browser")
            self.assertEqual(database_path.stat().st_mtime_ns, before)
            self.assertFalse(database_path.with_name("account.db-wal").exists())
            self.assertFalse(database_path.with_name("account.db-journal").exists())

    def test_browser_account_without_api_token_stops_at_authorization_gate(self) -> None:
        subject = _subject()
        store = _CredentialStore()

        class ClientThatMustNotRun:
            def qualify(self, **_kwargs: object) -> object:
                raise AssertionError("缺少 API 授权时不能尝试 Graph 请求")

        qualify_saved = getattr(
            subject, "qualify_saved_facebook_page_account", None
        )
        self.assertTrue(callable(qualify_saved), "缺少已保存账号资格预检入口")
        result = qualify_saved(
            _saved_account(),
            credential_store=store,
            client=ClientThatMustNotRun(),
        )

        self.assertEqual(
            result,
            {
                "status": "authorization_required",
                "qualified": False,
                "pagePermissionCheckPassed": False,
                "appAuthorizationVerified": False,
                "apiModeUsable": False,
                "targetPageId": _TARGET_PAGE_ID,
                "pageName": "资格预检测试 Page",
                "grantedScopes": [],
                "missingScopes": list(_REQUIRED_SCOPES),
                "pageTasks": [],
                "createContentTaskGranted": False,
                "writeAttempted": False,
            },
        )
        self.assertEqual(store.loaded_page_ids, [_TARGET_PAGE_ID])
        self.assertEqual(store.saved, [])

    def test_supplied_token_is_checked_in_memory_and_never_persisted(self) -> None:
        subject = _subject()
        store = _CredentialStore()
        client = subject.FacebookPageApiQualificationClient(
            session=_Session(
                [
                    _permissions(*_REQUIRED_SCOPES),
                    _Response({"data": [_page()], "paging": {}}),
                ]
            )
        )

        check_token = getattr(
            subject, "check_supplied_user_access_token", None
        )
        self.assertTrue(callable(check_token), "缺少仅内存令牌核对入口")
        result = check_token(
            _saved_account(),
            _USER_TOKEN,
            client=client,
        )

        self.assertEqual(result["status"], "page_scope_task_verified")
        self.assertTrue(result["pagePermissionCheckPassed"])
        self.assertFalse(result["appAuthorizationVerified"])
        self.assertFalse(result["apiModeUsable"])
        self.assertFalse(result["qualified"])
        self.assertFalse(result["credentialStored"])
        self.assertEqual(store.saved, [])
        self.assertNotIn(_USER_TOKEN, json.dumps(result, ensure_ascii=False))

    def test_under_scoped_token_is_not_persisted(self) -> None:
        subject = _subject()
        store = _CredentialStore()
        client = subject.FacebookPageApiQualificationClient(
            session=_Session(
                [_permissions("pages_read_engagement", "pages_show_list")]
            )
        )

        check_token = getattr(
            subject, "check_supplied_user_access_token", None
        )
        self.assertTrue(callable(check_token), "缺少仅内存令牌核对入口")
        result = check_token(
            _saved_account(),
            _USER_TOKEN,
            client=client,
        )

        self.assertEqual(result["status"], "permissions_missing")
        self.assertFalse(result["credentialStored"])
        self.assertEqual(store.saved, [])


class FacebookPageApiQualificationCliTests(unittest.TestCase):
    def test_check_reports_authorization_required_without_opening_browser(self) -> None:
        cli = _cli_subject()
        stdout = io.StringIO()
        store = _CredentialStore()

        class ClientThatMustNotRun:
            def qualify(self, **_kwargs: object) -> object:
                raise AssertionError("缺少授权时不能发起 Graph 请求")

        exit_code = cli.run(
            ["check", "--account-id", "14"],
            stdout=stdout,
            account_loader=lambda account_id: (
                _saved_account() if account_id == 14 else None
            ),
            credential_store=store,
            client=ClientThatMustNotRun(),
            secret_reader=lambda _prompt: (_ for _ in ()).throw(
                AssertionError("check 不得要求输入令牌")
            ),
        )

        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["status"], "authorization_required")
        self.assertFalse(result["qualified"])
        self.assertFalse(result["writeAttempted"])

    def test_check_token_reads_hidden_value_but_never_stores_or_prints_it(self) -> None:
        subject = _subject()
        cli = _cli_subject()
        stdout = io.StringIO()
        store = _CredentialStore()
        client = subject.FacebookPageApiQualificationClient(
            session=_Session(
                [
                    _permissions(*_REQUIRED_SCOPES),
                    _Response({"data": [_page()], "paging": {}}),
                ]
            )
        )

        exit_code = cli.run(
            ["check-token", "--account-id", "14"],
            stdout=stdout,
            account_loader=lambda _account_id: _saved_account(),
            credential_store=store,
            client=client,
            secret_reader=lambda _prompt: _USER_TOKEN,
        )

        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["status"], "page_scope_task_verified")
        self.assertTrue(result["pagePermissionCheckPassed"])
        self.assertFalse(result["appAuthorizationVerified"])
        self.assertFalse(result["apiModeUsable"])
        self.assertFalse(result["qualified"])
        self.assertFalse(result["credentialStored"])
        self.assertEqual(store.saved, [])
        self.assertNotIn(_USER_TOKEN, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
