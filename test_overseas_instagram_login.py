# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import asyncio
import queue
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app_core import account_browser_service
from app_core.overseas_instagram_account import (
    load_instagram_account_binding,
    save_instagram_browser_account,
)
from app_core.overseas_instagram_browser_identity import (
    INSTAGRAM_COMPOSER_URL,
    extract_instagram_identities,
    instagram_management_url,
    read_confirmed_instagram_identity,
)
from app_core.overseas_instagram_identity import (
    InstagramIdentity,
    InstagramIdentityError,
)
from myUtils import login as recovered_login
from myUtils import auth as recovered_auth


def _identity(*, user_id: str = "17841400000000001") -> InstagramIdentity:
    return InstagramIdentity(
        user_id=user_id,
        username="mobai.studio",
        display_name="墨白工作室",
        avatar_url="https://example.invalid/avatar.jpg",
        account_type="business",
        linked_page_id="100200300400500",
        linked_page_name="墨白 Page",
        can_manage_content=True,
    )


class InstagramGraphIdentityTests(unittest.TestCase):
    def test_extracts_complete_business_identity_from_linked_page_graph(self) -> None:
        payload = {
            "data": {
                "page": {
                    "id": "100200300400500",
                    "name": "墨白 Page",
                    "can_manage_content": True,
                    "instagram_business_account": {
                        "id": "17841400000000001",
                        "username": "mobai.studio",
                        "name": "墨白工作室",
                        "account_type": "BUSINESS",
                        "profile_picture_url": "https://example.invalid/avatar.jpg",
                    },
                }
            }
        }

        self.assertEqual(extract_instagram_identities(payload), (_identity(),))

    def test_extracts_creator_identity_from_explicit_connected_page(self) -> None:
        payload = {
            "payload": {
                "instagram_account": {
                    "instagram_user_id": "17841400000000002",
                    "username": "creator.demo",
                    "full_name": "Creator Demo",
                    "professional_account_type": "CREATOR",
                    "profile_pic_url": "https://example.invalid/creator.jpg",
                    "can_publish": True,
                    "connected_facebook_page": {
                        "id": "900800700600500",
                        "name": "Creator Page",
                    },
                }
            }
        }

        self.assertEqual(
            extract_instagram_identities(payload),
            (
                InstagramIdentity(
                    user_id="17841400000000002",
                    username="creator.demo",
                    display_name="Creator Demo",
                    avatar_url="https://example.invalid/creator.jpg",
                    account_type="creator",
                    linked_page_id="900800700600500",
                    linked_page_name="Creator Page",
                    can_manage_content=True,
                ),
            ),
        )

    def test_does_not_guess_professional_type_or_permission(self) -> None:
        payload = {
            "data": {
                "page": {
                    "id": "100200300400500",
                    "name": "墨白 Page",
                    "instagram_business_account": {
                        "id": "17841400000000001",
                        "username": "mobai.studio",
                        "name": "墨白工作室",
                    },
                }
            }
        }

        self.assertEqual(extract_instagram_identities(payload), ())


class InstagramTwoPageIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_official_pages_must_return_the_same_subject(self) -> None:
        expected = _identity()
        reader = AsyncMock(side_effect=[expected, expected])
        page = object()

        observed = await read_confirmed_instagram_identity(
            page,
            identity_reader=reader,
        )

        self.assertEqual(observed, expected)
        self.assertEqual(reader.await_count, 2)
        self.assertEqual(reader.await_args_list[0].args, (page, INSTAGRAM_COMPOSER_URL))
        self.assertEqual(
            reader.await_args_list[1].args,
            (page, instagram_management_url(expected)),
        )

    async def test_two_page_subject_mismatch_stops_before_save(self) -> None:
        reader = AsyncMock(side_effect=[_identity(), _identity(user_id="17841400000000999")])

        with self.assertRaises(InstagramIdentityError) as raised:
            await read_confirmed_instagram_identity(object(), identity_reader=reader)

        self.assertEqual(raised.exception.error_code, "instagram_identity_mismatch")

    def test_management_url_is_scoped_to_the_linked_page(self) -> None:
        self.assertEqual(
            instagram_management_url(_identity()),
            "https://business.facebook.com/latest/content?asset_id=100200300400500",
        )


class InstagramBrowserAccountPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE user_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type INTEGER NOT NULL,
                filePath TEXT NOT NULL,
                userName TEXT NOT NULL,
                status INTEGER DEFAULT 0,
                profileName TEXT,
                avatarPath TEXT,
                avatarUpdatedAt TEXT,
                lastCheckedAt TEXT,
                lastLoginAt TEXT,
                authMode TEXT NOT NULL DEFAULT 'browser',
                accountReference TEXT
            )
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_atomic_save_and_restart_load_use_stable_instagram_id(self) -> None:
        account_id = save_instagram_browser_account(
            self.conn,
            storage_file_name="instagram-session.json",
            identity=_identity(),
            observed_at="2026-08-31T16:00:00+08:00",
            avatar_file_name="instagram-avatar.png",
        )
        self.conn.commit()

        row = dict(
            self.conn.execute(
                "SELECT type,filePath,userName,profileName,status,avatarPath,"
                "accountReference,authMode FROM user_info WHERE id = ?",
                (account_id,),
            ).fetchone()
        )
        self.assertEqual(
            row,
            {
                "type": 8,
                "filePath": "instagram-session.json",
                "userName": "mobai.studio",
                "profileName": "墨白工作室",
                "status": 1,
                "avatarPath": "instagram-avatar.png",
                "accountReference": "17841400000000001",
                "authMode": "browser",
            },
        )
        restarted = load_instagram_account_binding(self.conn, account_id)
        self.assertEqual(restarted.user_id, "17841400000000001")
        self.assertEqual(restarted.account_type, "business")
        self.assertEqual(restarted.linked_page_id, "100200300400500")

    def test_update_rejects_a_different_instagram_subject(self) -> None:
        account_id = save_instagram_browser_account(
            self.conn,
            storage_file_name="first.json",
            identity=_identity(),
            observed_at="2026-08-31T16:00:00+08:00",
        )
        self.conn.commit()

        with self.assertRaises(InstagramIdentityError) as raised:
            save_instagram_browser_account(
                self.conn,
                storage_file_name="second.json",
                identity=_identity(user_id="17841400000000999"),
                observed_at="2026-08-31T16:05:00+08:00",
                record_id=account_id,
            )
        self.conn.rollback()

        self.assertEqual(raised.exception.error_code, "instagram_identity_mismatch")
        row = self.conn.execute(
            "SELECT filePath,accountReference FROM user_info WHERE id = ?",
            (account_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("first.json", "17841400000000001"))


class InstagramRecoveredLoginFlowTests(unittest.TestCase):
    def test_instagram_recovered_session_has_no_manual_save_bypass(self) -> None:
        session = __import__(
            "app_core.login_service",
            fromlist=["RecoveredOverseasLoginSession"],
        ).RecoveredOverseasLoginSession(8, "Instagram 主体")

        self.assertFalse(session.manual_save_supported)

    def test_login_confirms_stable_identity_before_saving_the_session(self) -> None:
        status_queue: queue.Queue[object] = queue.Queue()
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        page.goto = AsyncMock()
        context.new_page = AsyncMock(return_value=page)

        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        async def save_state(_context, path, **_kwargs):
            Path(path).write_text("{}", encoding="utf-8")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "cookiesFile").mkdir()
            (root / "avatars").mkdir()
            (root / "db").mkdir()
            with (
                patch.object(recovered_login, "BASE_DIR", root),
                patch.object(
                    recovered_login,
                    "async_playwright",
                    return_value=PlaywrightContext(),
                ),
                patch.object(
                    recovered_login,
                    "launch_login_browser",
                    new=AsyncMock(return_value=browser),
                ),
                patch.object(
                    recovered_login,
                    "new_login_context",
                    new=AsyncMock(return_value=context),
                ),
                patch.object(
                    recovered_login,
                    "set_init_script",
                    new=AsyncMock(return_value=context),
                ),
                patch.object(recovered_login, "reveal_page_window", new=AsyncMock()),
                patch.object(
                    recovered_login,
                    "_wait_for_browser_login",
                    new=AsyncMock(return_value="ready"),
                ),
                patch.object(
                    recovered_login,
                    "read_confirmed_instagram_identity",
                    new=AsyncMock(return_value=_identity()),
                ) as identity_read,
                patch.object(
                    recovered_login,
                    "save_context_storage_state",
                    new=AsyncMock(side_effect=save_state),
                ) as storage_save,
                patch.object(
                    recovered_login,
                    "check_cookie",
                    new=AsyncMock(return_value=True),
                ) as legacy_check,
                patch.object(
                    recovered_login,
                    "capture_login_identity",
                    new=AsyncMock(return_value=("avatar.png", "ignored")),
                ),
                patch.object(
                    recovered_login,
                    "save_confirmed_instagram_account",
                    return_value=42,
                ) as account_save,
                patch.object(
                    recovered_login,
                    "close_login_resources",
                    new=AsyncMock(),
                ),
            ):
                result = asyncio.run(
                    recovered_login._browser_cookie_gen(
                        8,
                        "用户输入的临时名称",
                        status_queue,
                    )
                )

        self.assertTrue(str(result).endswith(".json"))
        identity_read.assert_awaited_once_with(page)
        storage_save.assert_awaited_once()
        legacy_check.assert_not_awaited()
        kwargs = account_save.call_args.kwargs
        self.assertEqual(kwargs["identity"], _identity())
        self.assertEqual(kwargs["avatar_path"], "avatar.png")
        self.assertIsNone(kwargs["record_id"])
        self.assertIn("ACCOUNT_ID:42", list(status_queue.queue))
        self.assertEqual(list(status_queue.queue)[-1], "200")

    def test_identity_failure_does_not_write_a_storage_state(self) -> None:
        status_queue: queue.Queue[object] = queue.Queue()
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        page.goto = AsyncMock()
        context.new_page = AsyncMock(return_value=page)

        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "cookiesFile").mkdir()
            (root / "avatars").mkdir()
            (root / "db").mkdir()
            with (
                patch.object(recovered_login, "BASE_DIR", root),
                patch.object(
                    recovered_login,
                    "async_playwright",
                    return_value=PlaywrightContext(),
                ),
                patch.object(
                    recovered_login,
                    "launch_login_browser",
                    new=AsyncMock(return_value=browser),
                ),
                patch.object(
                    recovered_login,
                    "new_login_context",
                    new=AsyncMock(return_value=context),
                ),
                patch.object(
                    recovered_login,
                    "set_init_script",
                    new=AsyncMock(return_value=context),
                ),
                patch.object(recovered_login, "reveal_page_window", new=AsyncMock()),
                patch.object(
                    recovered_login,
                    "_wait_for_browser_login",
                    new=AsyncMock(return_value="ready"),
                ),
                patch.object(
                    recovered_login,
                    "read_confirmed_instagram_identity",
                    new=AsyncMock(
                        side_effect=InstagramIdentityError(
                            "instagram_identity_unavailable",
                            "缺少完整专业账号身份",
                        )
                    ),
                ),
                patch.object(
                    recovered_login,
                    "save_context_storage_state",
                    new=AsyncMock(),
                ) as storage_save,
                patch.object(
                    recovered_login,
                    "close_login_resources",
                    new=AsyncMock(),
                ),
            ):
                result = asyncio.run(
                    recovered_login._browser_cookie_gen(
                        8,
                        "用户输入的临时名称",
                        status_queue,
                    )
                )

        self.assertIsNone(result)
        storage_save.assert_not_awaited()
        self.assertEqual(
            list(status_queue.queue)[-2:],
            ["ERROR:instagram_identity_unavailable", "500"],
        )


class InstagramBackendOpenTests(unittest.IsolatedAsyncioTestCase):
    def test_open_backend_rejects_unbound_instagram_before_starting_a_worker(self) -> None:
        account = {
            "id": 42,
            "type": 8,
            "status": 1,
            "authMode": "browser",
            "profileName": "墨白工作室",
            "filePath": "instagram.json",
            "accountReference": "17841400000000001",
        }
        failure = InstagramIdentityError(
            "instagram_account_invalid",
            "Instagram 本地账号缺少稳定身份绑定。",
        )
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "instagram.json").write_text("{}", encoding="utf-8")
            with (
                patch.object(account_browser_service, "COOKIE_DIR", Path(raw)),
                patch.object(
                    account_browser_service,
                    "_saved_instagram_backend_identity",
                    side_effect=failure,
                ) as validate,
                patch.object(account_browser_service.threading.Thread, "start") as start,
                self.assertRaises(InstagramIdentityError) as raised,
            ):
                account_browser_service.open_account_backend(account)

        self.assertEqual(raised.exception.error_code, "instagram_account_invalid")
        validate.assert_called_once_with(account)
        start.assert_not_called()

    async def test_open_backend_scopes_to_linked_page_and_rechecks_instagram_subject(self) -> None:
        account = {
            "id": 42,
            "type": 8,
            "status": 1,
            "authMode": "browser",
            "profileName": "墨白工作室",
            "filePath": "instagram.json",
            "accountReference": "17841400000000001",
        }
        page = MagicMock()
        page.bring_to_front = AsyncMock()
        page.wait_for_event = AsyncMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        playwright.stop = AsyncMock()

        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "instagram.json").write_text("{}", encoding="utf-8")
            with (
                patch.object(account_browser_service, "COOKIE_DIR", Path(raw)),
                patch(
                    "playwright.async_api.async_playwright",
                    return_value=MagicMock(start=AsyncMock(return_value=playwright)),
                ),
                patch.object(
                    account_browser_service,
                    "_load_saved_instagram_identity",
                    return_value=_identity(),
                ) as load_identity,
                patch.object(
                    account_browser_service,
                    "navigate_and_read_instagram_identity",
                    new=AsyncMock(return_value=_identity()),
                ) as read_identity,
            ):
                await account_browser_service._open_backend(account)

        load_identity.assert_called_once_with(42)
        read_identity.assert_awaited_once_with(
            page,
            "https://business.facebook.com/latest/content?asset_id=100200300400500",
        )
        page.bring_to_front.assert_awaited_once_with()

    async def test_open_backend_refuses_a_different_live_instagram_subject(self) -> None:
        account = {
            "id": 42,
            "type": 8,
            "status": 1,
            "authMode": "browser",
            "profileName": "墨白工作室",
            "filePath": "instagram.json",
            "accountReference": "17841400000000001",
        }
        page = MagicMock()
        page.bring_to_front = AsyncMock()
        page.wait_for_event = AsyncMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        playwright.stop = AsyncMock()

        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "instagram.json").write_text("{}", encoding="utf-8")
            with (
                patch.object(account_browser_service, "COOKIE_DIR", Path(raw)),
                patch(
                    "playwright.async_api.async_playwright",
                    return_value=MagicMock(start=AsyncMock(return_value=playwright)),
                ),
                patch.object(
                    account_browser_service,
                    "_load_saved_instagram_identity",
                    return_value=_identity(),
                ),
                patch.object(
                    account_browser_service,
                    "navigate_and_read_instagram_identity",
                    new=AsyncMock(return_value=_identity(user_id="17841400000000999")),
                ),
                self.assertRaises(InstagramIdentityError) as raised,
            ):
                await account_browser_service._open_backend(account)

        self.assertEqual(raised.exception.error_code, "instagram_identity_mismatch")
        page.bring_to_front.assert_not_awaited()


class InstagramRestartSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_session_reopens_scoped_backend_and_requires_same_subject(self) -> None:
        expected = _identity()
        state_file = Path("/tmp/instagram-session.json")
        page = MagicMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()

        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        with (
            patch.object(
                recovered_auth,
                "async_playwright",
                return_value=PlaywrightContext(),
            ),
            patch.object(
                recovered_auth,
                "launch_chromium_with_codecs",
                new=AsyncMock(return_value=browser),
            ),
            patch.object(
                recovered_auth,
                "set_init_script",
                new=AsyncMock(return_value=context),
            ),
            patch.object(
                recovered_auth,
                "navigate_and_read_instagram_identity",
                new=AsyncMock(return_value=expected),
            ) as read_identity,
        ):
            observed = await recovered_auth.cookie_auth_instagram(
                state_file,
                expected,
                preview=False,
            )

        self.assertEqual(observed, expected)
        browser.new_context.assert_awaited_once_with(storage_state=str(state_file))
        read_identity.assert_awaited_once_with(
            page,
            "https://business.facebook.com/latest/content?asset_id=100200300400500",
        )
        context.close.assert_awaited_once_with()
        browser.close.assert_awaited_once_with()

    async def test_saved_session_subject_mismatch_is_not_boolean_success(self) -> None:
        expected = _identity()
        page = MagicMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()

        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        with (
            patch.object(
                recovered_auth,
                "async_playwright",
                return_value=PlaywrightContext(),
            ),
            patch.object(
                recovered_auth,
                "launch_chromium_with_codecs",
                new=AsyncMock(return_value=browser),
            ),
            patch.object(
                recovered_auth,
                "set_init_script",
                new=AsyncMock(return_value=context),
            ),
            patch.object(
                recovered_auth,
                "navigate_and_read_instagram_identity",
                new=AsyncMock(return_value=_identity(user_id="17841400000000999")),
            ),
            self.assertRaises(InstagramIdentityError) as raised,
        ):
            await recovered_auth.cookie_auth_instagram(
                Path("/tmp/instagram-session.json"),
                expected,
            )

        self.assertEqual(raised.exception.error_code, "instagram_identity_mismatch")


if __name__ == "__main__":
    unittest.main()
