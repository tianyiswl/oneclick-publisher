# -*- coding: utf-8 -*-
"""TikTok 稳定账号身份合同的离线测试，不访问真实平台。"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app_core import overseas_tiktok_identity as identity_service
from app_core.overseas_tiktok_identity import (
    TikTokIdentity,
    TikTokIdentityError,
    normalize_tiktok_handle,
    read_tiktok_identity,
    validate_identity_binding,
)


class _FakeProfileLink:
    def __init__(self, href: str, text: str = "", *, visible: bool = True) -> None:
        self.href = href
        self.text = text
        self.visible = visible

    async def is_visible(self, **_kwargs) -> bool:
        return self.visible

    async def get_attribute(self, name: str) -> str | None:
        return self.href if name == "href" else None

    async def inner_text(self, **_kwargs) -> str:
        return self.text


class _FakeProfileLinks:
    def __init__(self, links: list[_FakeProfileLink]) -> None:
        self.links = links

    async def count(self) -> int:
        return len(self.links)

    def nth(self, index: int) -> _FakeProfileLink:
        return self.links[index]


class _FakeTikTokPage:
    def __init__(self, links: list[_FakeProfileLink]) -> None:
        self.links = _FakeProfileLinks(links)

    def locator(self, selector: str) -> _FakeProfileLinks:
        self.selector = selector
        return self.links


class TikTokIdentityTests(unittest.TestCase):
    def test_normalize_handle_accepts_profile_url_and_at_prefix(self):
        self.assertEqual(normalize_tiktok_handle("https://www.tiktok.com/@Test.User"), "test.user")
        self.assertEqual(normalize_tiktok_handle("@Test.User"), "test.user")

    def test_existing_binding_rejects_another_logged_in_handle(self):
        account = {"accountReference": "expected.user"}
        identity = TikTokIdentity("other.user", "Other", "https://www.tiktok.com/@other.user")
        with self.assertRaises(TikTokIdentityError) as raised:
            validate_identity_binding(account, identity, allow_initial_bind=False)
        self.assertEqual(raised.exception.error_code, "tiktok_account_identity_mismatch")

    def test_empty_legacy_binding_can_be_initialized_once(self):
        identity = TikTokIdentity("expected.user", "Expected", "https://www.tiktok.com/@expected.user")
        self.assertEqual(
            validate_identity_binding({"accountReference": ""}, identity, allow_initial_bind=True),
            "expected.user",
        )

    def test_nonempty_invalid_legacy_binding_cannot_be_initialized(self):
        identity = TikTokIdentity("expected.user", "Expected", "https://www.tiktok.com/@expected.user")
        with self.assertRaises(TikTokIdentityError) as raised:
            validate_identity_binding(
                {"accountReference": "not a valid handle!"},
                identity,
                allow_initial_bind=True,
            )
        self.assertEqual(raised.exception.error_code, "tiktok_account_identity_mismatch")

    def test_existing_binding_accepts_the_same_normalized_handle(self):
        identity = TikTokIdentity(
            "@Expected.User", "Expected", "https://www.tiktok.com/@expected.user"
        )
        self.assertEqual(
            validate_identity_binding(
                {"accountReference": "https://www.tiktok.com/@EXPECTED.USER"},
                identity,
                allow_initial_bind=False,
            ),
            "expected.user",
        )

    def test_read_identity_uses_the_single_visible_stable_profile_link(self):
        page = _FakeTikTokPage(
            [_FakeProfileLink("/@expected.user", "Expected", visible=True)]
        )
        identity = asyncio.run(read_tiktok_identity(page))
        self.assertEqual(identity.handle, "expected.user")
        self.assertEqual(identity.display_name, "Expected")
        self.assertEqual(identity.profile_url, "https://www.tiktok.com/@expected.user")

    def test_read_identity_rejects_when_no_visible_profile_handle_exists(self):
        page = _FakeTikTokPage(
            [_FakeProfileLink("/@expected.user", "Expected", visible=False)]
        )
        with self.assertRaises(TikTokIdentityError) as raised:
            asyncio.run(read_tiktok_identity(page))
        self.assertEqual(raised.exception.error_code, "tiktok_account_invalid")

    def test_read_identity_rejects_conflicting_visible_profile_handles(self):
        page = _FakeTikTokPage(
            [
                _FakeProfileLink("/@expected.user", "Expected"),
                _FakeProfileLink("/@other.user", "Other"),
            ]
        )
        with self.assertRaises(TikTokIdentityError) as raised:
            asyncio.run(read_tiktok_identity(page))
        self.assertEqual(raised.exception.error_code, "tiktok_account_identity_ambiguous")


class TikTokSavedIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "database.db"
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            CREATE TABLE user_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type INTEGER NOT NULL,
                filePath TEXT NOT NULL,
                userName TEXT NOT NULL,
                status INTEGER DEFAULT 0,
                accountReference TEXT,
                authMode TEXT NOT NULL DEFAULT 'browser'
            )
            """
        )
        connection.commit()
        connection.close()

        @contextmanager
        def connect_test_database():
            connection = sqlite3.connect(self.database)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

        self.connect_patch = patch.object(
            identity_service,
            "connect",
            connect_test_database,
            create=True,
        )
        self.cookie_patch = patch.object(
            identity_service,
            "COOKIE_DIR",
            self.root,
            create=True,
        )
        self.connect_patch.start()
        self.cookie_patch.start()

    def tearDown(self) -> None:
        self.cookie_patch.stop()
        self.connect_patch.stop()
        self.temp.cleanup()

    def _save_account(self, *, reference: str = "expected.user", file_name: str = "tiktok.json") -> int:
        connection = sqlite3.connect(self.database)
        account_id = connection.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, accountReference, authMode)
            VALUES (6, ?, 'Expected', 1, ?, 'browser')
            """,
            (file_name, reference),
        ).lastrowid
        connection.commit()
        connection.close()
        return int(account_id)

    def _stored_account(self, account_id: int) -> sqlite3.Row:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM user_info WHERE id = ?",
            (account_id,),
        ).fetchone()
        connection.close()
        return row

    def _playwright_runtime(self, page):
        context = SimpleNamespace(
            new_page=AsyncMock(return_value=page),
            close=AsyncMock(),
        )
        browser = SimpleNamespace(
            new_context=AsyncMock(return_value=context),
            close=AsyncMock(),
        )
        runtime = SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
            stop=AsyncMock(),
        )
        factory = MagicMock()
        factory.start = AsyncMock(return_value=runtime)
        return factory, runtime, browser, context

    def test_initial_persist_binds_only_the_verified_handle(self) -> None:
        account_id = self._save_account(reference="")
        identity = TikTokIdentity(
            "Expected.User",
            "Expected",
            "https://www.tiktok.com/@expected.user",
        )

        identity_service.persist_tiktok_identity(
            account_id,
            identity,
            allow_initial_bind=True,
        )

        self.assertEqual(
            self._stored_account(account_id)["accountReference"],
            "expected.user",
        )

    def test_persist_mismatch_preserves_the_existing_reference(self) -> None:
        account_id = self._save_account(reference="expected.user")
        identity = TikTokIdentity(
            "other.user",
            "Other",
            "https://www.tiktok.com/@other.user",
        )

        with self.assertRaises(TikTokIdentityError) as raised:
            identity_service.persist_tiktok_identity(
                account_id,
                identity,
                allow_initial_bind=True,
            )

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_account_identity_mismatch",
        )
        self.assertEqual(
            self._stored_account(account_id)["accountReference"],
            "expected.user",
        )

    def test_saved_validation_reports_missing_session_without_starting_browser(self) -> None:
        account_id = self._save_account(file_name="missing.json")
        factory = MagicMock()
        factory.start = AsyncMock()
        with patch.object(
            identity_service,
            "async_playwright",
            return_value=factory,
            create=True,
        ):
            with self.assertRaises(TikTokIdentityError) as raised:
                identity_service.validate_saved_tiktok_account(
                    dict(self._stored_account(account_id))
                )

        self.assertEqual(raised.exception.error_code, "tiktok_session_missing")
        factory.start.assert_not_awaited()

    def test_saved_validation_marks_missing_unique_handle_as_expired_and_closes_resources(self) -> None:
        account_id = self._save_account()
        (self.root / "tiktok.json").write_text("{}", encoding="utf-8")
        page = SimpleNamespace(
            goto=AsyncMock(),
            close=AsyncMock(),
            url="https://www.tiktok.com/login",
        )
        factory, runtime, browser, context = self._playwright_runtime(page)
        with (
            patch.object(
                identity_service,
                "async_playwright",
                return_value=factory,
                create=True,
            ),
            patch.object(
                identity_service,
                "read_tiktok_identity",
                new=AsyncMock(
                    side_effect=TikTokIdentityError(
                        "tiktok_account_invalid",
                        "TikTok 页面没有返回稳定账号标识",
                    )
                ),
            ),
        ):
            with self.assertRaises(TikTokIdentityError) as raised:
                identity_service.validate_saved_tiktok_account(
                    dict(self._stored_account(account_id))
                )

        self.assertEqual(raised.exception.error_code, "tiktok_session_expired")
        page.close.assert_awaited_once()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        runtime.stop.assert_awaited_once()

    def test_saved_validation_silently_initializes_one_legacy_binding(self) -> None:
        account_id = self._save_account(reference="")
        (self.root / "tiktok.json").write_text("{}", encoding="utf-8")
        page = SimpleNamespace(
            goto=AsyncMock(),
            close=AsyncMock(),
            url="https://www.tiktok.com/tiktokstudio/upload",
        )
        factory, runtime, _browser, _context = self._playwright_runtime(page)
        identity = TikTokIdentity(
            "expected.user",
            "Expected",
            "https://www.tiktok.com/@expected.user",
        )
        with (
            patch.object(
                identity_service,
                "async_playwright",
                return_value=factory,
                create=True,
            ),
            patch.object(
                identity_service,
                "read_tiktok_identity",
                new=AsyncMock(return_value=identity),
            ),
        ):
            checked = identity_service.validate_saved_tiktok_account(
                dict(self._stored_account(account_id))
            )

        self.assertEqual(checked, identity)
        self.assertEqual(
            self._stored_account(account_id)["accountReference"],
            "expected.user",
        )
        runtime.chromium.launch.assert_awaited_once_with(headless=True)


if __name__ == "__main__":
    unittest.main()
