# -*- coding: utf-8 -*-
"""TikTok 稳定账号身份合同的离线测试，不访问真实平台。"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app_core import account_service
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
    def __init__(
        self,
        links: list[_FakeProfileLink],
        *,
        url: str = "https://www.tiktok.com/foryou",
    ) -> None:
        self.links = _FakeProfileLinks(links)
        self.url = url

    def locator(self, selector: str) -> _FakeProfileLinks:
        self.selector = selector
        return self.links


class _DelayedProfileLinkPage:
    def __init__(
        self,
        samples: list[list[_FakeProfileLink]],
        *,
        url: str = "https://www.tiktok.com/foryou",
    ) -> None:
        self.samples = list(samples)
        self.locator_calls = 0
        self.url = url

    def locator(self, selector: str) -> _FakeProfileLinks:
        self.selector = selector
        index = min(self.locator_calls, len(self.samples) - 1)
        self.locator_calls += 1
        return _FakeProfileLinks(self.samples[index])


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

    def test_read_identity_waits_for_a_profile_link_then_requires_a_stable_repeat(self):
        visible = _FakeProfileLink("/@expected.user", "Expected", visible=True)
        page = _DelayedProfileLinkPage([[], [visible], [visible]])

        identity = asyncio.run(
            read_tiktok_identity(page, poll_seconds=0.0, max_attempts=3)
        )

        self.assertEqual(identity.handle, "expected.user")
        self.assertEqual(page.locator_calls, 3)

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

    def test_read_identity_accepts_a_stable_hidden_profile_anchor_only_in_studio(self):
        page = _FakeTikTokPage(
            [_FakeProfileLink("/@Expected.User", "private display", visible=False)],
            url="https://www.tiktok.com/tiktokstudio/upload?lang=en",
        )

        identity = asyncio.run(
            read_tiktok_identity(page, poll_seconds=0.0, max_attempts=2)
        )

        self.assertEqual(identity.handle, "expected.user")
        self.assertEqual(identity.display_name, "")
        self.assertEqual(identity.profile_url, "https://www.tiktok.com/@expected.user")

    def test_read_identity_rejects_a_hidden_profile_anchor_outside_studio(self):
        page = _FakeTikTokPage(
            [_FakeProfileLink("/@expected.user", "private display", visible=False)],
            url="https://www.tiktok.com/foryou?private=value",
        )

        with self.assertRaises(TikTokIdentityError) as raised:
            asyncio.run(read_tiktok_identity(page, poll_seconds=0.0, max_attempts=2))

        self.assertEqual(raised.exception.error_code, "tiktok_account_invalid")
        self.assertNotIn("private", raised.exception.public_message)
        self.assertNotIn("value", raised.exception.public_message)

    def test_read_identity_rejects_a_hidden_profile_anchor_on_login_route(self):
        page = _FakeTikTokPage(
            [_FakeProfileLink("/@expected.user", "private display", visible=False)],
            url="https://www.tiktok.com/login?private=value",
        )

        with self.assertRaises(TikTokIdentityError) as raised:
            asyncio.run(read_tiktok_identity(page, poll_seconds=0.0, max_attempts=2))

        self.assertEqual(raised.exception.error_code, "tiktok_account_invalid")
        self.assertNotIn("private", raised.exception.public_message)
        self.assertNotIn("value", raised.exception.public_message)

    def test_read_identity_rejects_hidden_conflicting_handles_on_every_route(self):
        for url in (
            "https://www.tiktok.com/tiktokstudio/upload",
            "https://www.tiktok.com/login",
        ):
            with self.subTest(url=url):
                page = _FakeTikTokPage(
                    [
                        _FakeProfileLink("/@expected.user", visible=False),
                        _FakeProfileLink("/@other.user", visible=False),
                    ],
                    url=url,
                )

                with self.assertRaises(TikTokIdentityError) as raised:
                    asyncio.run(
                        read_tiktok_identity(page, poll_seconds=0.0, max_attempts=2)
                    )

                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_account_identity_ambiguous",
                )


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

    def _save_account(
        self,
        *,
        reference: str = "expected.user",
        file_name: str = "tiktok.json",
        status: int = 1,
    ) -> int:
        connection = sqlite3.connect(self.database)
        account_id = connection.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, accountReference, authMode)
            VALUES (6, ?, 'Expected', ?, ?, 'browser')
            """,
            (file_name, status, reference),
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
        account_id = self._save_account(reference="", status=0)
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
        self.assertEqual(self._stored_account(account_id)["status"], 1)

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

    def test_concurrent_initial_bind_allows_only_one_verified_handle(self) -> None:
        account_id = self._save_account(reference="")
        barrier = threading.Barrier(2)
        write_lock = threading.Lock()

        class RacingCursor:
            def __init__(self, cursor, *, wait_after_fetch: bool) -> None:
                self._cursor = cursor
                self._wait_after_fetch = wait_after_fetch

            @property
            def rowcount(self):
                return self._cursor.rowcount

            def fetchone(self):
                row = self._cursor.fetchone()
                if self._wait_after_fetch:
                    barrier.wait(timeout=3)
                return row

        class RacingConnection:
            def __init__(self, connection) -> None:
                self._connection = connection
                self._identity_reads = 0

            def execute(self, sql, params=()):
                is_identity_read = sql.lstrip().upper().startswith("SELECT ID, TYPE")
                if is_identity_read:
                    self._identity_reads += 1
                    return RacingCursor(
                        self._connection.execute(sql, params),
                        wait_after_fetch=self._identity_reads == 1,
                    )
                with write_lock:
                    return RacingCursor(
                        self._connection.execute(sql, params),
                        wait_after_fetch=False,
                    )

        @contextmanager
        def racing_connect():
            connection = sqlite3.connect(
                self.database,
                isolation_level=None,
                check_same_thread=False,
                timeout=3,
            )
            connection.row_factory = sqlite3.Row
            try:
                yield RacingConnection(connection)
            finally:
                connection.close()

        outcomes: list[tuple[str, str]] = []
        outcome_lock = threading.Lock()

        def bind(handle: str) -> None:
            try:
                identity_service.persist_tiktok_identity(
                    account_id,
                    TikTokIdentity(
                        handle,
                        handle,
                        f"https://www.tiktok.com/@{handle}",
                    ),
                    allow_initial_bind=True,
                )
            except TikTokIdentityError as exc:
                outcome = ("error", exc.error_code)
            else:
                outcome = ("success", handle)
            with outcome_lock:
                outcomes.append(outcome)

        with patch.object(identity_service, "connect", racing_connect):
            workers = [
                threading.Thread(target=bind, args=(handle,))
                for handle in ("first.user", "second.user")
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sum(kind == "success" for kind, _ in outcomes), 1)
        self.assertEqual(
            [value for kind, value in outcomes if kind == "error"],
            ["tiktok_account_identity_mismatch"],
        )
        self.assertIn(
            self._stored_account(account_id)["accountReference"],
            {"first.user", "second.user"},
        )

    def test_persist_rejects_row_deleted_or_changed_type_after_read(self) -> None:
        for mutation in ("delete", "change_type"):
            with self.subTest(mutation=mutation):
                account_id = self._save_account(reference="")
                mutated = False

                class MutationCursor:
                    def __init__(self, cursor, *, mutate_after_fetch: bool) -> None:
                        self._cursor = cursor
                        self._mutate_after_fetch = mutate_after_fetch

                    @property
                    def rowcount(self):
                        return self._cursor.rowcount

                    def fetchone(inner_self):
                        nonlocal mutated
                        row = inner_self._cursor.fetchone()
                        if inner_self._mutate_after_fetch and not mutated:
                            mutated = True
                            other = sqlite3.connect(self.database, isolation_level=None)
                            if mutation == "delete":
                                other.execute("DELETE FROM user_info WHERE id = ?", (account_id,))
                            else:
                                other.execute(
                                    "UPDATE user_info SET type = 7 WHERE id = ?",
                                    (account_id,),
                                )
                            other.close()
                        return row

                class MutationConnection:
                    def __init__(self, connection) -> None:
                        self._connection = connection
                        self._reads = 0

                    def execute(inner_self, sql, params=()):
                        is_identity_read = sql.lstrip().upper().startswith("SELECT ID, TYPE")
                        if is_identity_read:
                            inner_self._reads += 1
                        return MutationCursor(
                            inner_self._connection.execute(sql, params),
                            mutate_after_fetch=is_identity_read and inner_self._reads == 1,
                        )

                @contextmanager
                def mutation_connect():
                    connection = sqlite3.connect(self.database, isolation_level=None)
                    connection.row_factory = sqlite3.Row
                    try:
                        yield MutationConnection(connection)
                    finally:
                        connection.close()

                with patch.object(identity_service, "connect", mutation_connect):
                    with self.assertRaises(TikTokIdentityError) as raised:
                        identity_service.persist_tiktok_identity(
                            account_id,
                            TikTokIdentity(
                                "expected.user",
                                "Expected",
                                "https://www.tiktok.com/@expected.user",
                            ),
                            allow_initial_bind=True,
                        )

                self.assertEqual(raised.exception.error_code, "tiktok_account_invalid")

    def test_empty_binding_rejects_persist_when_initial_bind_is_disabled(self) -> None:
        account_id = self._save_account(reference="")

        with self.assertRaises(TikTokIdentityError) as raised:
            identity_service.persist_tiktok_identity(
                account_id,
                TikTokIdentity(
                    "expected.user",
                    "Expected",
                    "https://www.tiktok.com/@expected.user",
                ),
                allow_initial_bind=False,
            )

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_account_identity_mismatch",
        )
        self.assertEqual(self._stored_account(account_id)["accountReference"], "")

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

    def test_saved_validation_persists_the_verified_identity_once(self) -> None:
        account_id = self._save_account(reference="")
        (self.root / "tiktok.json").write_text("{}", encoding="utf-8")
        page = SimpleNamespace(
            goto=AsyncMock(),
            close=AsyncMock(),
            url="https://www.tiktok.com/tiktokstudio/upload",
        )
        factory, _runtime, _browser, _context = self._playwright_runtime(page)
        identity = TikTokIdentity(
            "expected.user",
            "Expected",
            "https://www.tiktok.com/@expected.user",
        )
        persist = MagicMock()

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
            patch.object(identity_service, "persist_tiktok_identity", persist),
        ):
            checked = identity_service.validate_saved_tiktok_account(
                dict(self._stored_account(account_id))
            )

        self.assertEqual(checked, identity)
        persist.assert_called_once_with(
            account_id,
            identity,
            allow_initial_bind=True,
        )


class TikTokAccountPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "accounts.db"
        connection = sqlite3.connect(self.database)
        connection.execute(
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
                remark TEXT,
                lastCheckedAt TEXT,
                lastLoginAt TEXT,
                authMode TEXT NOT NULL DEFAULT 'browser',
                accountReference TEXT,
                oauthScopeVersion INTEGER NOT NULL DEFAULT 1
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
                with connection:
                    yield connection
            finally:
                connection.close()

        self.connect_patch = patch.object(
            account_service,
            "connect",
            connect_test_database,
        )
        self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)

    def _stored_account(self, account_id: int) -> dict:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM user_info WHERE id = ?",
            (int(account_id),),
        ).fetchone()
        connection.close()
        return dict(row)

    def test_account_saver_binds_verified_handle_and_rejects_duplicate_new_binding(self):
        identity = TikTokIdentity(
            "expected.user",
            "Expected",
            "https://www.tiktok.com/@expected.user",
        )

        account_id = account_service.save_tiktok_browser_account(
            profile_name="TikTok 测试",
            storage_file_name="first.json",
            identity=identity,
        )

        self.assertGreater(account_id, 0)
        stored = self._stored_account(account_id)
        self.assertEqual(stored["type"], 6)
        self.assertEqual(stored["authMode"], "browser")
        self.assertEqual(stored["accountReference"], "expected.user")
        with self.assertRaises(TikTokIdentityError) as raised:
            account_service.save_tiktok_browser_account(
                profile_name="重复",
                storage_file_name="second.json",
                identity=identity,
            )
        self.assertEqual(raised.exception.error_code, "tiktok_account_invalid")

    def test_update_repeats_identity_binding_check_inside_transaction(self):
        account_id = account_service.save_tiktok_browser_account(
            profile_name="TikTok 测试",
            storage_file_name="first.json",
            identity=TikTokIdentity(
                "expected.user", "Expected", "https://www.tiktok.com/@expected.user"
            ),
        )
        expected = self._stored_account(account_id)

        with self.assertRaises(TikTokIdentityError) as raised:
            account_service.save_tiktok_browser_account(
                profile_name="TikTok 测试",
                storage_file_name="second.json",
                identity=TikTokIdentity(
                    "other.user", "Other", "https://www.tiktok.com/@other.user"
                ),
                record_id=account_id,
                expected_account=expected,
            )

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_account_identity_mismatch",
        )
        self.assertEqual(self._stored_account(account_id)["filePath"], "first.json")

    def test_update_rejects_concurrent_row_change_without_overwriting_it(self):
        account_id = account_service.save_tiktok_browser_account(
            profile_name="TikTok 测试",
            storage_file_name="first.json",
            identity=TikTokIdentity(
                "expected.user", "Expected", "https://www.tiktok.com/@expected.user"
            ),
        )
        expected = self._stored_account(account_id)
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE user_info SET profileName = 'concurrent' WHERE id = ?",
            (account_id,),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(TikTokIdentityError) as raised:
            account_service.save_tiktok_browser_account(
                profile_name="TikTok 新名称",
                storage_file_name="second.json",
                identity=TikTokIdentity(
                    "expected.user", "Expected", "https://www.tiktok.com/@expected.user"
                ),
                record_id=account_id,
                expected_account=expected,
            )

        self.assertEqual(raised.exception.error_code, "tiktok_account_invalid")
        current = self._stored_account(account_id)
        self.assertEqual(current["profileName"], "concurrent")
        self.assertEqual(current["filePath"], "first.json")


if __name__ == "__main__":
    unittest.main()
