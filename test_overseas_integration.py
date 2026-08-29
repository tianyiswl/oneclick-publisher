# -*- coding: utf-8 -*-
"""海外平台接线的离线回归：不启动浏览器，不访问平台。"""

from __future__ import annotations

import asyncio
import queue
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import conf
from app_core import (
    account_service,
    database as app_database,
    login_service,
    overseas_preflight,
    overseas_tiktok_publish,
    overseas_tiktok_system_login,
    overseas_youtube_credentials,
    overseas_youtube_login,
    overseas_youtube_profile,
    publish_service,
)
from app_core import overseas_tiktok_identity as tiktok_identity_service
from app_core.overseas_youtube_api import YouTubeChannelIdentity
from app_core.overseas_tiktok_identity import TikTokIdentity, TikTokIdentityError
from myUtils import login as recovered_login
from myUtils import postVideo as recovered_publish
from uploader.youtube_uploader.main import YouTubeVideo


class OverseasAccountEntryTests(unittest.TestCase):
    def _create_tiktok_login_database(self, root: Path) -> Path:
        (root / "db").mkdir(exist_ok=True)
        (root / "cookiesFile").mkdir(exist_ok=True)
        (root / "avatars").mkdir(exist_ok=True)
        database = root / "db" / "database.db"
        connection = sqlite3.connect(database)
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
                lastCheckedAt TEXT,
                lastLoginAt TEXT,
                accountReference TEXT
            )
            """
        )
        connection.commit()
        connection.close()
        return database

    def _login_connection_factory(self, database: Path):
        @contextmanager
        def open_test_connection(_path, *, row_factory=False):
            connection = sqlite3.connect(database)
            if row_factory:
                connection.row_factory = sqlite3.Row
            try:
                yield connection
            finally:
                connection.close()

        return open_test_connection

    def _identity_connection_factory(self, database: Path):
        @contextmanager
        def connect_identity_database():
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

        return connect_identity_database

    def _run_tiktok_login_attempt(
        self,
        root: Path,
        identity: TikTokIdentity,
        *,
        update_mode: bool = False,
        record_id: int | None = None,
        save_state=None,
    ):
        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        page.goto = AsyncMock()

        async def default_save_state(_context, path, **_kwargs):
            Path(path).write_text("{}", encoding="utf-8")

        async def capture_identity(_page, _platform_type, _avatar_key):
            avatar = root / "avatars" / "new.png"
            avatar.write_bytes(b"new-avatar")
            return "new.png", "New Display"

        status_queue = queue.Queue()
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
                "save_context_storage_state",
                new=AsyncMock(side_effect=save_state or default_save_state),
            ),
            patch.object(
                recovered_login,
                "check_cookie",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                recovered_login,
                "read_tiktok_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch.object(
                recovered_login,
                "capture_login_identity",
                new=AsyncMock(side_effect=capture_identity),
            ),
            patch.object(
                recovered_login,
                "close_login_resources",
                new=AsyncMock(),
            ),
        ):
            result = asyncio.run(
                recovered_login._browser_cookie_gen(
                    6,
                    "New Profile",
                    status_queue,
                    update_mode=update_mode,
                    record_id=record_id,
                )
            )
        return result, list(status_queue.queue)

    def test_login_options_expose_three_entries_for_four_targets(self) -> None:
        entries = dict(account_service.LOGIN_PLATFORM_OPTIONS)
        self.assertEqual(entries[6], "TikTok")
        self.assertEqual(entries[7], "YouTube")
        self.assertIn("Meta", entries[8])
        self.assertNotIn(9, entries)
        self.assertEqual(account_service.login_platform_type(9), 8)

    def test_login_service_routes_meta_to_recovered_shared_session(self) -> None:
        with patch.object(
            login_service.RecoveredOverseasLoginSession,
            "start",
        ) as start:
            session = login_service.start_login(9, "海外主体")
        self.assertIsInstance(
            session,
            login_service.RecoveredOverseasLoginSession,
        )
        self.assertEqual(session.platform_type, 8)
        start.assert_called_once_with()

    def test_login_service_routes_tiktok_to_system_browser_session(self) -> None:
        created = MagicMock()
        created.start = MagicMock()
        with patch.object(
            overseas_tiktok_system_login,
            "TikTokSystemBrowserLoginSession",
            return_value=created,
        ) as session_type:
            session = login_service.start_login(6, "TikTok 测试")

        self.assertIs(session, created)
        created.start.assert_called_once_with()
        kwargs = session_type.call_args.kwargs
        self.assertEqual(kwargs["profile_name"], "TikTok 测试")
        self.assertFalse(kwargs["update_mode"])
        self.assertIsNone(kwargs["record_id"])
        self.assertIsNone(kwargs["existing_account"])

    def test_login_service_routes_youtube_to_official_oauth_session(self) -> None:
        created = MagicMock()
        created.start = MagicMock()
        with (
            patch.object(
                conf,
                "YOUTUBE_OAUTH_CLIENT_ID",
                "desktop-client.apps.googleusercontent.com",
                create=True,
            ),
            patch.object(
                overseas_youtube_login,
                "YouTubeOAuthLoginSession",
                return_value=created,
            ) as session_type,
            patch.object(
                account_service,
                "get_managed_account",
                return_value=None,
                create=True,
            ),
        ):
            session = login_service.start_login(7, "海外主体")

        self.assertIs(session, created)
        created.start.assert_called_once_with()
        kwargs = session_type.call_args.kwargs
        self.assertEqual(
            kwargs["client_id"],
            "desktop-client.apps.googleusercontent.com",
        )
        self.assertEqual(kwargs["profile_name"], "海外主体")
        self.assertIs(kwargs["account_saver"], account_service.save_youtube_oauth_account)

    def test_tiktok_login_persists_stable_handle_after_account_row_exists(self) -> None:
        identity = TikTokIdentity(
            "expected.user",
            "Expected",
            "https://www.tiktok.com/@expected.user",
        )

        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        page.goto = AsyncMock()

        async def save_state(_context, path, **_kwargs):
            Path(path).write_text("{}", encoding="utf-8")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "cookiesFile").mkdir()
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
                patch.object(
                    recovered_login,
                    "reveal_page_window",
                    new=AsyncMock(),
                ),
                patch.object(
                    recovered_login,
                    "_wait_for_browser_login",
                    new=AsyncMock(return_value="ready"),
                ),
                patch.object(
                    recovered_login,
                    "save_context_storage_state",
                    new=AsyncMock(side_effect=save_state),
                ),
                patch.object(
                    recovered_login,
                    "check_cookie",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(
                    recovered_login,
                    "capture_login_identity",
                    new=AsyncMock(return_value=(None, "Expected")),
                ),
                patch.object(
                    recovered_login,
                    "read_tiktok_identity",
                    new=AsyncMock(return_value=identity),
                    create=True,
                ),
                patch.object(recovered_login, "save_login_account", return_value=61),
                patch.object(
                    recovered_login,
                    "persist_tiktok_identity",
                    create=True,
                ) as persist,
                patch.object(
                    recovered_login,
                    "close_login_resources",
                    new=AsyncMock(),
                ),
            ):
                result = asyncio.run(
                    recovered_login._browser_cookie_gen(
                        6,
                        "TikTok 主体",
                        queue.Queue(),
                    )
                )

        self.assertIsNotNone(result)
        persist.assert_called_once_with(61, identity, allow_initial_bind=True)

    def test_tiktok_identity_read_failure_leaves_no_account_row_or_session(self) -> None:
        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        page.goto = AsyncMock()

        async def save_state(_context, path, **_kwargs):
            Path(path).write_text("{}", encoding="utf-8")

        save_account = MagicMock(return_value=61)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cookie_dir = root / "cookiesFile"
            cookie_dir.mkdir()
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
                patch.object(
                    recovered_login,
                    "reveal_page_window",
                    new=AsyncMock(),
                ),
                patch.object(
                    recovered_login,
                    "_wait_for_browser_login",
                    new=AsyncMock(return_value="ready"),
                ),
                patch.object(
                    recovered_login,
                    "save_context_storage_state",
                    new=AsyncMock(side_effect=save_state),
                ),
                patch.object(
                    recovered_login,
                    "check_cookie",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(
                    recovered_login,
                    "read_tiktok_identity",
                    new=AsyncMock(
                        side_effect=TikTokIdentityError(
                            "tiktok_account_invalid",
                            "TikTok 页面没有返回稳定账号标识",
                        )
                    ),
                ),
                patch.object(recovered_login, "save_login_account", save_account),
                patch.object(
                    recovered_login,
                    "close_login_resources",
                    new=AsyncMock(),
                ),
            ):
                result = asyncio.run(
                    recovered_login._browser_cookie_gen(
                        6,
                        "TikTok 主体",
                        queue.Queue(),
                    )
                )
            remaining_sessions = list(cookie_dir.iterdir())

        self.assertIsNone(result)
        save_account.assert_not_called()
        self.assertEqual(remaining_sessions, [])

    def test_failed_tiktok_binding_cleans_new_row_and_preserves_updated_reference(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "db").mkdir()
            (root / "cookiesFile").mkdir()
            database = root / "db" / "database.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE user_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type INTEGER NOT NULL,
                    filePath TEXT NOT NULL,
                    status INTEGER,
                    avatarPath TEXT,
                    accountReference TEXT
                )
                """
            )
            new_id = connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, status, accountReference)
                VALUES (6, 'new.json', 0, '')
                """
            ).lastrowid
            update_id = connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, status, accountReference)
                VALUES (6, 'old.json', 1, 'expected.user')
                """
            ).lastrowid
            connection.commit()
            connection.close()

            @contextmanager
            def open_test_connection(_path):
                connection = sqlite3.connect(database)
                try:
                    yield connection
                finally:
                    connection.close()

            new_session = root / "cookiesFile" / "new.json"
            update_session = root / "cookiesFile" / "update.json"
            new_session.write_text("{}", encoding="utf-8")
            update_session.write_text("{}", encoding="utf-8")
            with (
                patch.object(recovered_login, "BASE_DIR", root),
                patch.object(
                    recovered_login,
                    "open_connection",
                    open_test_connection,
                ),
            ):
                recovered_login._discard_failed_tiktok_login(
                    int(new_id),
                    update_mode=False,
                    cookie_path=new_session,
                )
                recovered_login._discard_failed_tiktok_login(
                    int(update_id),
                    update_mode=True,
                    cookie_path=update_session,
                )

            connection = sqlite3.connect(database)
            new_row = connection.execute(
                "SELECT status, accountReference FROM user_info WHERE id = ?",
                (new_id,),
            ).fetchone()
            updated_row = connection.execute(
                "SELECT status, accountReference FROM user_info WHERE id = ?",
                (update_id,),
            ).fetchone()
            connection.close()
            sessions_removed = not new_session.exists() and not update_session.exists()

        self.assertIsNone(new_row)
        self.assertEqual(updated_row, (1, "expected.user"))
        self.assertTrue(sessions_removed)

    def test_tiktok_update_mismatch_preserves_complete_old_account_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "db").mkdir()
            (root / "cookiesFile").mkdir()
            (root / "avatars").mkdir()
            database = root / "db" / "database.db"
            connection = sqlite3.connect(database)
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
                    lastCheckedAt TEXT,
                    lastLoginAt TEXT,
                    accountReference TEXT
                )
                """
            )
            account_id = connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, avatarPath,
                     avatarUpdatedAt, lastCheckedAt, lastLoginAt, accountReference)
                VALUES (6, 'old.json', 'Old Display', 1, 'Old Profile', 'old.png',
                        '2026-08-01 01:00:00', '2026-08-01 02:00:00',
                        '2026-08-01 03:00:00', 'expected.user')
                """
            ).lastrowid
            connection.commit()
            old_row = connection.execute(
                "SELECT * FROM user_info WHERE id = ?",
                (account_id,),
            ).fetchone()
            connection.close()
            old_session = root / "cookiesFile" / "old.json"
            old_avatar = root / "avatars" / "old.png"
            old_session.write_text("{}", encoding="utf-8")
            old_avatar.write_bytes(b"old-avatar")

            @contextmanager
            def open_test_connection(_path, *, row_factory=False):
                connection = sqlite3.connect(database)
                if row_factory:
                    connection.row_factory = sqlite3.Row
                try:
                    yield connection
                finally:
                    connection.close()

            @contextmanager
            def connect_identity_database():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                try:
                    yield connection
                    connection.commit()
                finally:
                    connection.close()

            with (
                patch.object(
                    recovered_login,
                    "open_connection",
                    open_test_connection,
                ),
                patch.object(
                    tiktok_identity_service,
                    "connect",
                    connect_identity_database,
                ),
            ):
                result, messages = self._run_tiktok_login_attempt(
                    root,
                    TikTokIdentity(
                        "other.user",
                        "Other",
                        "https://www.tiktok.com/@other.user",
                    ),
                    update_mode=True,
                    record_id=int(account_id),
                )

            connection = sqlite3.connect(database)
            current_row = connection.execute(
                "SELECT * FROM user_info WHERE id = ?",
                (account_id,),
            ).fetchone()
            connection.close()
            candidate_sessions = list((root / "cookiesFile").glob("*.json"))
            candidate_avatars = list((root / "avatars").glob("*.png"))

        self.assertIsNone(result)
        self.assertIn("500", messages)
        self.assertEqual(current_row, old_row)
        self.assertEqual([path.name for path in candidate_sessions], ["old.json"])
        self.assertEqual([path.name for path in candidate_avatars], ["old.png"])

    def test_tiktok_save_exception_removes_partial_new_row_and_candidate_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = self._create_tiktok_login_database(root)

            def partially_save(
                platform_type,
                cookie_file,
                profile_name,
                update_mode=False,
                record_id=None,
                avatar_path=None,
                display_name=None,
            ):
                connection = sqlite3.connect(database)
                connection.execute(
                    """
                    INSERT INTO user_info
                        (type, filePath, userName, status, profileName,
                         avatarPath, accountReference)
                    VALUES (?, ?, ?, 0, ?, ?, '')
                    """,
                    (
                        platform_type,
                        cookie_file,
                        display_name or profile_name,
                        profile_name,
                        avatar_path,
                    ),
                )
                connection.commit()
                connection.close()
                raise RuntimeError("save-internal-detail")

            with (
                patch.object(
                    recovered_login,
                    "open_connection",
                    self._login_connection_factory(database),
                ),
                patch.object(recovered_login, "save_login_account", partially_save),
            ):
                try:
                    result, messages = self._run_tiktok_login_attempt(
                        root,
                        TikTokIdentity(
                            "expected.user",
                            "Expected",
                            "https://www.tiktok.com/@expected.user",
                        ),
                    )
                except RuntimeError as exc:
                    result, messages = exc, []

            connection = sqlite3.connect(database)
            row_count = connection.execute(
                "SELECT COUNT(*) FROM user_info"
            ).fetchone()[0]
            connection.close()
            remaining_sessions = list((root / "cookiesFile").iterdir())
            remaining_avatars = list((root / "avatars").iterdir())

        self.assertIsNone(result)
        self.assertEqual(row_count, 0)
        self.assertEqual(remaining_sessions, [])
        self.assertEqual(remaining_avatars, [])
        self.assertNotIn("save-internal-detail", " ".join(messages))

    def test_tiktok_atomic_update_exception_preserves_complete_old_account(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = self._create_tiktok_login_database(root)
            connection = sqlite3.connect(database)
            account_id = connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, avatarPath,
                     avatarUpdatedAt, lastCheckedAt, lastLoginAt, accountReference)
                VALUES (6, 'old.json', 'Old Display', 1, 'Old Profile', 'old.png',
                        '2026-08-01 01:00:00', '2026-08-01 02:00:00',
                        '2026-08-01 03:00:00', 'expected.user')
                """
            ).lastrowid
            connection.commit()
            old_row = connection.execute(
                "SELECT * FROM user_info WHERE id = ?",
                (account_id,),
            ).fetchone()
            connection.close()
            (root / "cookiesFile" / "old.json").write_text("{}", encoding="utf-8")
            (root / "avatars" / "old.png").write_bytes(b"old-avatar")

            with (
                patch.object(
                    recovered_login,
                    "open_connection",
                    self._login_connection_factory(database),
                ),
                patch.object(
                    recovered_login,
                    "_save_tiktok_update_if_unchanged",
                    side_effect=RuntimeError("atomic-update-internal-detail"),
                ),
            ):
                try:
                    result, messages = self._run_tiktok_login_attempt(
                        root,
                        TikTokIdentity(
                            "expected.user",
                            "Expected",
                            "https://www.tiktok.com/@expected.user",
                        ),
                        update_mode=True,
                        record_id=int(account_id),
                    )
                except RuntimeError as exc:
                    result, messages = exc, []

            connection = sqlite3.connect(database)
            current_row = connection.execute(
                "SELECT * FROM user_info WHERE id = ?",
                (account_id,),
            ).fetchone()
            connection.close()
            remaining_sessions = sorted(
                path.name for path in (root / "cookiesFile").iterdir()
            )
            remaining_avatars = sorted(
                path.name for path in (root / "avatars").iterdir()
            )

        self.assertIsNone(result)
        self.assertEqual(current_row, old_row)
        self.assertEqual(remaining_sessions, ["old.json"])
        self.assertEqual(remaining_avatars, ["old.png"])
        self.assertNotIn("atomic-update-internal-detail", " ".join(messages))

    def test_tiktok_permission_exception_removes_partially_written_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = self._create_tiktok_login_database(root)
            with (
                patch.object(
                    recovered_login,
                    "open_connection",
                    self._login_connection_factory(database),
                ),
                patch.object(
                    recovered_login.os,
                    "chmod",
                    side_effect=OSError("permission-internal-detail"),
                ),
            ):
                try:
                    result, messages = self._run_tiktok_login_attempt(
                        root,
                        TikTokIdentity(
                            "expected.user",
                            "Expected",
                            "https://www.tiktok.com/@expected.user",
                        ),
                    )
                except OSError as exc:
                    result, messages = exc, []

            connection = sqlite3.connect(database)
            row_count = connection.execute(
                "SELECT COUNT(*) FROM user_info"
            ).fetchone()[0]
            connection.close()
            remaining_sessions = list((root / "cookiesFile").iterdir())

        self.assertIsNone(result)
        self.assertEqual(row_count, 0)
        self.assertEqual(remaining_sessions, [])
        self.assertNotIn("permission-internal-detail", " ".join(messages))

    def test_failed_tiktok_update_cleanup_preserves_concurrent_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = self._create_tiktok_login_database(root)
            connection = sqlite3.connect(database)
            account_id = connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, avatarPath,
                     avatarUpdatedAt, lastCheckedAt, lastLoginAt, accountReference)
                VALUES (6, 'old.json', 'Old Display', 1, 'Old Profile', 'old.png',
                        '2026-08-01 01:00:00', '2026-08-01 02:00:00',
                        '2026-08-01 03:00:00', 'expected.user')
                """
            ).lastrowid
            connection.commit()
            connection.row_factory = sqlite3.Row
            previous_account = dict(
                connection.execute(
                    "SELECT * FROM user_info WHERE id = ?",
                    (account_id,),
                ).fetchone()
            )
            connection.close()

            old_session = root / "cookiesFile" / "old.json"
            failed_session = root / "cookiesFile" / "failed.json"
            successful_session = root / "cookiesFile" / "successful.json"
            old_session.write_text("old", encoding="utf-8")
            failed_session.write_text("failed", encoding="utf-8")
            successful_session.write_text("successful", encoding="utf-8")

            successful_update_finished = threading.Event()
            outcomes: list[tuple[str, str]] = []
            outcome_lock = threading.Lock()

            def finish_successful_update() -> None:
                try:
                    recovered_login._save_tiktok_update_if_unchanged(
                        previous_account,
                        cookie_file="successful.json",
                        profile_name="Successful Profile",
                        avatar_path=None,
                        display_name="Successful Display",
                        account_reference="expected.user",
                    )
                except Exception as exc:
                    outcome = ("successful_error", type(exc).__name__)
                else:
                    outcome = ("success", "successful.json")
                finally:
                    successful_update_finished.set()
                with outcome_lock:
                    outcomes.append(outcome)

            def clean_failed_update() -> None:
                if not successful_update_finished.wait(timeout=3):
                    with outcome_lock:
                        outcomes.append(("failed_error", "timeout"))
                    return
                try:
                    recovered_login._save_tiktok_update_if_unchanged(
                        previous_account,
                        cookie_file="failed.json",
                        profile_name="Failed Profile",
                        avatar_path=None,
                        display_name="Failed Display",
                        account_reference="expected.user",
                    )
                except TikTokIdentityError as exc:
                    outcome = ("rejected", exc.error_code)
                except Exception as exc:
                    outcome = ("failed_error", type(exc).__name__)
                else:
                    outcome = ("unexpected_success", "failed.json")
                recovered_login._discard_failed_tiktok_login(
                    int(account_id),
                    update_mode=True,
                    cookie_path=failed_session,
                    previous_account=previous_account,
                )
                with outcome_lock:
                    outcomes.append(outcome)

            with (
                patch.object(recovered_login, "BASE_DIR", root),
                patch.object(
                    recovered_login,
                    "open_connection",
                    self._login_connection_factory(database),
                ),
            ):
                successful_worker = threading.Thread(target=finish_successful_update)
                failed_worker = threading.Thread(target=clean_failed_update)
                failed_worker.start()
                successful_worker.start()
                successful_worker.join(timeout=5)
                failed_worker.join(timeout=5)

            connection = sqlite3.connect(database)
            current = connection.execute(
                """
                SELECT filePath, userName, profileName, status, accountReference
                FROM user_info WHERE id = ?
                """,
                (account_id,),
            ).fetchone()
            connection.close()
            successful_session_exists = successful_session.exists()
            failed_session_exists = failed_session.exists()

        self.assertFalse(successful_worker.is_alive())
        self.assertFalse(failed_worker.is_alive())
        self.assertCountEqual(
            outcomes,
            [
                ("success", "successful.json"),
                ("rejected", "tiktok_account_invalid"),
            ],
        )
        self.assertEqual(
            current,
            (
                "successful.json",
                "Successful Display",
                "Successful Profile",
                1,
                "expected.user",
            ),
        )
        self.assertTrue(successful_session_exists)
        self.assertFalse(failed_session_exists)

    def test_generic_tiktok_save_rejects_unconditional_update(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = self._create_tiktok_login_database(root)
            connection = sqlite3.connect(database)
            account_id = connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName,
                     accountReference)
                VALUES (6, 'old.json', 'Old Display', 1, 'Old Profile',
                        'expected.user')
                """
            ).lastrowid
            connection.commit()
            old_row = connection.execute(
                "SELECT * FROM user_info WHERE id = ?",
                (account_id,),
            ).fetchone()
            connection.close()

            with (
                patch.object(recovered_login, "BASE_DIR", root),
                patch.object(
                    recovered_login,
                    "open_connection",
                    self._login_connection_factory(database),
                ),
            ):
                with self.assertRaises(TikTokIdentityError) as raised:
                    recovered_login.save_login_account(
                        6,
                        "candidate.json",
                        "Candidate Profile",
                        update_mode=True,
                        record_id=int(account_id),
                        display_name="Candidate Display",
                    )

            connection = sqlite3.connect(database)
            current_row = connection.execute(
                "SELECT * FROM user_info WHERE id = ?",
                (account_id,),
            ).fetchone()
            connection.close()

        self.assertEqual(raised.exception.error_code, "tiktok_account_invalid")
        self.assertEqual(current_row, old_row)

    def test_tiktok_cleanup_database_failure_keeps_pending_session_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = self._create_tiktok_login_database(root)
            connection_attempts = 0

            @contextmanager
            def fail_cleanup_connection(_path, *, row_factory=False):
                nonlocal connection_attempts
                connection_attempts += 1
                if connection_attempts > 1:
                    raise sqlite3.OperationalError("cleanup-database-unavailable")
                connection = sqlite3.connect(database)
                if row_factory:
                    connection.row_factory = sqlite3.Row
                try:
                    yield connection
                    connection.commit()
                finally:
                    connection.close()

            with (
                patch.object(
                    recovered_login,
                    "open_connection",
                    fail_cleanup_connection,
                ),
                patch.object(
                    recovered_login,
                    "persist_tiktok_identity",
                    side_effect=RuntimeError("persist-internal-detail"),
                ),
            ):
                result, messages = self._run_tiktok_login_attempt(
                    root,
                    TikTokIdentity(
                        "expected.user",
                        "Expected",
                        "https://www.tiktok.com/@expected.user",
                    ),
                )

            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT status, filePath, avatarPath FROM user_info"
            ).fetchone()
            connection.close()
            saved_session = root / "cookiesFile" / row[1]
            saved_avatar = root / "avatars" / row[2]
            saved_session_exists = saved_session.exists()
            saved_avatar_exists = saved_avatar.exists()

        self.assertIsNone(result)
        self.assertEqual(row[0], 0)
        self.assertTrue(saved_session_exists)
        self.assertTrue(saved_avatar_exists)
        self.assertNotIn("cleanup-database-unavailable", " ".join(messages))
        self.assertNotIn("persist-internal-detail", " ".join(messages))

    def test_failed_new_login_cleanup_preserves_concurrently_activated_row(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = self._create_tiktok_login_database(root)
            connection = sqlite3.connect(database)
            account_id = connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName,
                     accountReference)
                VALUES (6, 'candidate.json', 'Pending', 0, 'Pending', '')
                """
            ).lastrowid
            connection.commit()
            connection.close()
            candidate_session = root / "cookiesFile" / "candidate.json"
            candidate_session.write_text("candidate", encoding="utf-8")

            activated = threading.Event()

            def activate_from_verified_session() -> None:
                verifier = sqlite3.connect(database)
                verifier.execute(
                    """
                    UPDATE user_info
                    SET status = 1, accountReference = 'expected.user'
                    WHERE id = ? AND status = 0
                      AND (accountReference IS NULL OR TRIM(accountReference) = '')
                    """,
                    (account_id,),
                )
                verifier.commit()
                verifier.close()
                activated.set()

            def finish_failed_login_cleanup() -> None:
                if not activated.wait(timeout=3):
                    return
                recovered_login._discard_failed_tiktok_login(
                    int(account_id),
                    update_mode=False,
                    cookie_path=candidate_session,
                )

            with (
                patch.object(recovered_login, "BASE_DIR", root),
                patch.object(
                    recovered_login,
                    "open_connection",
                    self._login_connection_factory(database),
                ),
            ):
                cleanup_worker = threading.Thread(target=finish_failed_login_cleanup)
                verifier_worker = threading.Thread(target=activate_from_verified_session)
                cleanup_worker.start()
                verifier_worker.start()
                verifier_worker.join(timeout=5)
                cleanup_worker.join(timeout=5)

            connection = sqlite3.connect(database)
            current = connection.execute(
                """
                SELECT status, accountReference, filePath
                FROM user_info WHERE id = ?
                """,
                (account_id,),
            ).fetchone()
            connection.close()
            candidate_session_exists = candidate_session.exists()

        self.assertFalse(verifier_worker.is_alive())
        self.assertFalse(cleanup_worker.is_alive())
        self.assertEqual(current, (1, "expected.user", "candidate.json"))
        self.assertTrue(candidate_session_exists)

    def test_tiktok_replaced_artifact_unlink_errors_do_not_change_login_success(self) -> None:
        real_unlink = Path.unlink
        for failing_name in ("old.json", "old.png"):
            with self.subTest(failing_name=failing_name):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    database = self._create_tiktok_login_database(root)
                    connection = sqlite3.connect(database)
                    account_id = connection.execute(
                        """
                        INSERT INTO user_info
                            (type, filePath, userName, status, profileName,
                             avatarPath, avatarUpdatedAt, lastCheckedAt,
                             lastLoginAt, accountReference)
                        VALUES (6, 'old.json', 'Old Display', 1, 'Old Profile',
                                'old.png', '2026-08-01 01:00:00',
                                '2026-08-01 02:00:00', '2026-08-01 03:00:00',
                                'expected.user')
                        """
                    ).lastrowid
                    connection.commit()
                    connection.close()
                    old_session = root / "cookiesFile" / "old.json"
                    old_avatar = root / "avatars" / "old.png"
                    old_session.write_text("old", encoding="utf-8")
                    old_avatar.write_bytes(b"old-avatar")

                    def fail_selected_unlink(path, *args, **kwargs):
                        if path.name == failing_name:
                            raise PermissionError("unlink-internal-detail")
                        return real_unlink(path, *args, **kwargs)

                    with (
                        patch.object(
                            recovered_login,
                            "open_connection",
                            self._login_connection_factory(database),
                        ),
                        patch.object(Path, "unlink", fail_selected_unlink),
                    ):
                        result, messages = self._run_tiktok_login_attempt(
                            root,
                            TikTokIdentity(
                                "expected.user",
                                "Expected",
                                "https://www.tiktok.com/@expected.user",
                            ),
                            update_mode=True,
                            record_id=int(account_id),
                        )

                    connection = sqlite3.connect(database)
                    current = connection.execute(
                        """
                        SELECT status, accountReference, filePath, avatarPath
                        FROM user_info WHERE id = ?
                        """,
                        (account_id,),
                    ).fetchone()
                    connection.close()
                    old_session_exists = old_session.exists()
                    old_avatar_exists = old_avatar.exists()

                self.assertIsNotNone(result)
                self.assertIn("200", messages)
                self.assertNotIn("500", messages)
                self.assertNotIn("unlink-internal-detail", " ".join(messages))
                self.assertEqual(current[0:2], (1, "expected.user"))
                self.assertNotEqual(current[2], "old.json")
                self.assertEqual(current[3], "new.png")
                self.assertEqual(old_session_exists, failing_name == "old.json")
                self.assertEqual(old_avatar_exists, failing_name == "old.png")


class YouTubeOAuthAccountPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "database.db"
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
                yield connection
                connection.commit()
            finally:
                connection.close()

        self.connect_patch = patch.object(
            account_service,
            "connect",
            connect_test_database,
        )
        self.demo_patch = patch.object(account_service, "_ensure_demo_accounts")
        self.promote_patch = patch.object(
            account_service,
            "_promote_confirmed_oneclick_sessions",
        )
        self.connect_patch.start()
        self.demo_patch.start()
        self.promote_patch.start()

    def tearDown(self) -> None:
        self.promote_patch.stop()
        self.demo_patch.stop()
        self.connect_patch.stop()
        self.temp.cleanup()

    def _save_oauth_account(self) -> int:
        return account_service.save_youtube_oauth_account(
            profile_name="海外主体",
            credential_reference="youtube-oauth:opaque-reference",
            channel_id="UC123",
            display_name="测试频道",
        )

    def _save_tiktok_account(self, *, reference: str = "expected.user") -> int:
        connection = sqlite3.connect(self.database)
        account_id = connection.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, authMode,
                 accountReference)
            VALUES (6, 'tiktok.json', 'Expected', 1, 'TikTok 主体',
                    'browser', ?)
            """,
            (reference,),
        ).lastrowid
        connection.commit()
        connection.close()
        return int(account_id)

    def test_tiktok_identity_issues_mark_saved_account_invalid(self) -> None:
        for error_code in (
            "tiktok_account_identity_mismatch",
            "tiktok_session_missing",
            "tiktok_session_expired",
        ):
            with self.subTest(error_code=error_code):
                account_id = self._save_tiktok_account()
                with (
                    patch.object(
                        account_service,
                        "validate_saved_tiktok_account",
                        side_effect=TikTokIdentityError(error_code, "TikTok 账号异常"),
                        create=True,
                    ),
                    patch(
                        "app_core.oneclick_authorization.verify_saved_session",
                        return_value=True,
                    ),
                ):
                    result = account_service.validate_accounts([account_id])

                self.assertEqual(result["accounts"][0]["status"], 0)
                self.assertEqual(result["authIssues"][account_id], error_code)
                self.assertEqual(
                    result["accounts"][0]["accountReference"],
                    "expected.user",
                )

    def test_abnormal_or_unbound_tiktok_account_is_not_publishable(self) -> None:
        normal_id = self._save_tiktok_account(reference="expected.user")
        abnormal_id = self._save_tiktok_account(reference="expected.user")
        unbound_id = self._save_tiktok_account(reference="")
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE user_info SET status = 0 WHERE id = ?",
            (abnormal_id,),
        )
        connection.commit()
        connection.close()

        publishable = account_service.list_publishable_accounts()

        self.assertEqual([row["id"] for row in publishable], [normal_id])
        self.assertNotIn(abnormal_id, [row["id"] for row in publishable])
        self.assertNotIn(unbound_id, [row["id"] for row in publishable])

    def test_legacy_oauth_row_migrates_to_scope_version_one(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            legacy_database = Path(raw) / "legacy.db"
            connection = sqlite3.connect(legacy_database)
            connection.execute(
                """
                CREATE TABLE user_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type INTEGER NOT NULL,
                    filePath TEXT NOT NULL,
                    userName TEXT NOT NULL,
                    status INTEGER DEFAULT 0,
                    authMode TEXT NOT NULL DEFAULT 'browser',
                    accountReference TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, authMode, accountReference)
                VALUES (7, 'youtube-oauth:legacy', '旧频道', 1,
                        'youtube_oauth', 'UC-legacy')
                """
            )
            connection.commit()
            connection.close()

            with patch.object(app_database, "DB_PATH", legacy_database):
                app_database.ensure_schema()

            connection = sqlite3.connect(legacy_database)
            value = connection.execute(
                "SELECT oauthScopeVersion FROM user_info WHERE id = 1"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(value, 1)

    def test_oauth_account_is_managed_but_excluded_from_publish_facing_accounts(self) -> None:
        account_id = self._save_oauth_account()

        self.assertEqual(account_service.list_accounts(), [])
        managed = account_service.list_managed_accounts()
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0]["id"], account_id)
        self.assertEqual(managed[0]["authMode"], "youtube_oauth")
        self.assertEqual(managed[0]["filePath"], "youtube-oauth:opaque-reference")
        self.assertEqual(managed[0]["accountReference"], "UC123")
        self.assertEqual(managed[0]["userName"], "测试频道")
        self.assertEqual(
            [row["id"] for row in account_service.list_publishable_accounts()],
            [account_id],
        )

        connection = sqlite3.connect(self.database)
        stored = repr(connection.execute("SELECT * FROM user_info").fetchone())
        connection.close()
        self.assertNotIn("refresh", stored.lower())
        self.assertNotIn("access-token", stored)

    def test_legacy_oauth_scope_is_shown_as_publish_permission_upgrade(self) -> None:
        account_id = self._save_oauth_account()

        managed = account_service.list_managed_accounts()

        self.assertEqual([row["id"] for row in managed], [account_id])
        self.assertTrue(managed[0]["needsPublishScopeUpgrade"])
        self.assertEqual(managed[0]["statusText"], "需要升级发布权限")
        self.assertFalse(managed[0]["isHealthy"])

    def test_restart_validation_uses_official_channel_check_for_oauth_account(self) -> None:
        account_id = self._save_oauth_account()
        identity = YouTubeChannelIdentity(
            channel_id="UC123",
            display_name="重启后频道",
        )
        with (
            patch.object(
                conf,
                "YOUTUBE_OAUTH_CLIENT_ID",
                "desktop-client.apps.googleusercontent.com",
                create=True,
            ),
            patch.object(
                overseas_youtube_login,
                "validate_saved_youtube_oauth_account",
                return_value=identity,
            ) as validate,
        ):
            result = account_service.validate_accounts([account_id])

        self.assertEqual([row["id"] for row in result["normal"]], [account_id])
        validate.assert_called_once()
        checked_account = validate.call_args.args[0]
        self.assertEqual(checked_account["accountReference"], "UC123")
        self.assertEqual(
            validate.call_args.kwargs["client_id"],
            "desktop-client.apps.googleusercontent.com",
        )

    def test_successful_oauth_validation_returns_the_fresh_database_status(self) -> None:
        account_id = self._save_oauth_account()
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE user_info SET status = 0 WHERE id = ?",
            (account_id,),
        )
        connection.commit()
        connection.close()
        identity = YouTubeChannelIdentity(
            channel_id="UC123",
            display_name="重启后频道",
        )
        with (
            patch.object(
                conf,
                "YOUTUBE_OAUTH_CLIENT_ID",
                "desktop-client.apps.googleusercontent.com",
                create=True,
            ),
            patch.object(
                overseas_youtube_login,
                "validate_saved_youtube_oauth_account",
                return_value=identity,
            ),
        ):
            result = account_service.validate_accounts([account_id])

        self.assertEqual([row["id"] for row in result["normal"]], [account_id])
        self.assertEqual(result["checked"][0]["status"], 1)

    def test_oauth_channel_mismatch_is_returned_as_a_distinct_safe_issue(self) -> None:
        account_id = self._save_oauth_account()
        with (
            patch.object(
                conf,
                "YOUTUBE_OAUTH_CLIENT_ID",
                "desktop-client.apps.googleusercontent.com",
                create=True,
            ),
            patch.object(
                overseas_youtube_login,
                "validate_saved_youtube_oauth_account",
                side_effect=overseas_youtube_login.YouTubeOAuthLoginError(
                    "channel_identity_mismatch"
                ),
            ),
        ):
            result = account_service.validate_accounts([account_id])

        self.assertEqual(
            result["checked"][0]["authIssueCode"],
            "youtube_channel_identity_mismatch",
        )
        self.assertIn("频道身份不一致", result["failures"][0])
        self.assertEqual([row["id"] for row in result["abnormal"]], [account_id])

    def test_oauth_profile_refresh_updates_only_public_name_and_local_avatar(self) -> None:
        account_id = self._save_oauth_account()
        with (
            patch.object(
                overseas_youtube_profile,
                "refresh_youtube_oauth_profile",
                return_value={
                    "displayName": "刷新后频道",
                    "avatarFileName": "oneclick_account_1.png",
                },
            ) as refresh,
            patch.object(account_service, "run_async_capture_account_avatar") as browser,
        ):
            account = account_service.refresh_account_avatar(account_id)

        refresh.assert_called_once()
        browser.assert_not_called()
        self.assertEqual(account["userName"], "刷新后频道")
        self.assertEqual(account["avatarPath"], "oneclick_account_1.png")
        connection = sqlite3.connect(self.database)
        stored = repr(connection.execute("SELECT * FROM user_info").fetchone())
        connection.close()
        self.assertNotIn("yt3.ggpht.com", stored)
        self.assertNotIn("access-token", stored)

    def test_deleting_oauth_account_removes_keyring_entry_not_cookie_file(self) -> None:
        account_id = self._save_oauth_account()
        credential_store = MagicMock()
        with patch.object(
            overseas_youtube_credentials,
            "KeyringOAuthCredentialStore",
            return_value=credential_store,
        ):
            account_service.delete_account(account_id)

        credential_store.delete_refresh_token.assert_called_once_with(
            "youtube-oauth:opaque-reference"
        )
        connection = sqlite3.connect(self.database)
        count = connection.execute("SELECT COUNT(*) FROM user_info").fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    def test_meta_relogin_updates_shared_instagram_and_facebook_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "database.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE user_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type INTEGER NOT NULL,
                    filePath TEXT,
                    userName TEXT,
                    status INTEGER,
                    profileName TEXT,
                    avatarPath TEXT,
                    avatarUpdatedAt TEXT,
                    lastCheckedAt TEXT,
                    lastLoginAt TEXT
                )
                """
            )
            facebook_id = connection.execute(
                """
                INSERT INTO user_info (
                    type, filePath, userName, status, profileName
                ) VALUES (9, 'old.json', '旧账号', 0, '旧主体')
                """
            ).lastrowid
            connection.execute(
                """
                INSERT INTO user_info (
                    type, filePath, userName, status, profileName
                ) VALUES (8, 'old.json', '旧账号', 0, '旧主体')
                """
            )
            connection.commit()
            connection.close()

            @contextmanager
            def open_test_connection(_path, *, row_factory=False):
                conn = sqlite3.connect(database)
                if row_factory:
                    conn.row_factory = sqlite3.Row
                try:
                    yield conn
                finally:
                    conn.close()

            with patch.object(
                recovered_login,
                "open_connection",
                open_test_connection,
            ):
                saved_ids = recovered_login.save_meta_login_accounts(
                    "new.json",
                    "新主体",
                    update_mode=True,
                    record_id=facebook_id,
                    display_name="Meta 新账号",
                )

            connection = sqlite3.connect(database)
            rows = connection.execute(
                """
                SELECT id, type, filePath, userName, status, profileName
                FROM user_info ORDER BY type
                """
            ).fetchall()
            connection.close()
            self.assertEqual(set(saved_ids), {int(row[0]) for row in rows})
            self.assertEqual([row[1] for row in rows], [8, 9])
            self.assertTrue(all(row[2] == "new.json" for row in rows))
            self.assertTrue(all(row[3] == "Meta 新账号" for row in rows))
            self.assertTrue(all(row[4] == 1 for row in rows))
            self.assertTrue(all(row[5] == "新主体" for row in rows))


class OverseasPreflightTests(unittest.TestCase):
    def _payload(self, video: Path) -> dict:
        return {
            "type": 6,
            "contentType": "video",
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "backgroundMode": True,
            "debugDryRunHoldBrowser": False,
            "title": "海外预检",
            "description": "只检查字段，不发布。",
            "tags": ["oneclick"],
            "fileList": [str(video)],
            "accountIds": [61],
            "accountList": ["tiktok.json"],
            "visibility": "public",
            "enableTimer": False,
            "scheduleTime": None,
            "scheduleTimezone": "Asia/Shanghai",
            "videosPerDay": 1,
            "dailyTimes": [],
            "startDays": 0,
            "timeJitterMinutes": 0,
            "coverPath": "",
            "coverPaths": {},
            "aiGenerated": False,
            "collectionName": "",
            "mentions": [],
        }

    def _account(self, file_name: str = "tiktok.json") -> dict:
        return {
            "id": 61,
            "type": 6,
            "status": 1,
            "authMode": "browser",
            "filePath": file_name,
            "accountReference": "expected.user",
        }

    @contextmanager
    def _local_tiktok(self, root: Path):
        with (
            patch.object(overseas_preflight, "COOKIE_DIR", root),
            patch.object(overseas_tiktok_publish, "COOKIE_DIR", root),
            patch.object(
                overseas_tiktok_publish,
                "_read_account_record",
                return_value=self._account(),
            ),
        ):
            yield

    def test_validation_requires_dry_run_video_and_local_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            (root / "tiktok.json").write_text(
                '{"cookies": [], "origins": []}',
                encoding="utf-8",
            )
            with self._local_tiktok(root):
                result = overseas_preflight.validate_overseas_preflight_payload(
                    self._payload(video)
                )
                self.assertTrue(result["ok"])

                not_dry_run = self._payload(video)
                not_dry_run["debugDryRun"] = False
                result = overseas_preflight.validate_overseas_preflight_payload(
                    not_dry_run
                )
                self.assertFalse(result["ok"])
                self.assertTrue(any("debugDryRun=true" in item for item in result["errors"]))

                article = self._payload(video)
                article["contentType"] = "article"
                result = overseas_preflight.validate_overseas_preflight_payload(article)
                self.assertFalse(result["ok"])
                self.assertTrue(any("视频通道" in item for item in result["errors"]))

    def test_meta_recovered_handler_receives_forced_dry_run(self) -> None:
        calls = []

        def handler(*args, **kwargs):
            calls.append((args, kwargs))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            (root / "instagram.json").write_text("{}", encoding="utf-8")
            with (
                patch.object(overseas_preflight, "COOKIE_DIR", root),
                patch.dict(overseas_preflight.PREFLIGHT_HANDLERS, {8: handler}),
            ):
                payload = self._payload(video)
                payload.update(
                    {
                        "type": 8,
                        "accountList": ["instagram.json"],
                        "visibility": "private",
                        "collectionName": "测试合集",
                        "aiGenerated": False,
                        "madeForKids": True,
                        "notifySubscribers": False,
                        "shareToFeed": True,
                    }
                )
                result = overseas_preflight.run_overseas_preflight_sync(payload)

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1]["dry_run"])
        self.assertFalse(calls[0][1]["dry_run_hold_browser"])
        self.assertIsNone(calls[0][1]["schedule_time"])
        self.assertEqual(calls[0][1]["visibility"], "private")
        self.assertEqual(calls[0][1]["collection_name"], "测试合集")
        self.assertFalse(calls[0][1]["ai_generated"])
        self.assertTrue(calls[0][1]["made_for_kids"])
        self.assertFalse(calls[0][1]["notify_subscribers"])
        self.assertTrue(calls[0][1]["share_to_feed"])

    def test_tiktok_default_preflight_never_calls_browser_handler_or_session_writer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            session = root / "tiktok.json"
            session.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
            original_session = session.read_bytes()
            handler = MagicMock(side_effect=AssertionError("TikTok handler must stay local"))
            with (
                self._local_tiktok(root),
                patch.dict(overseas_preflight.PREFLIGHT_HANDLERS, {6: handler}),
                patch.object(
                    overseas_tiktok_publish,
                    "_load_async_playwright_factory",
                ) as playwright_factory,
                patch.object(
                    overseas_tiktok_publish,
                    "_load_tiktok_uploader_class",
                ) as uploader_factory,
                patch.object(
                    overseas_tiktok_publish,
                    "load_sanitized_tiktok_storage_state_file",
                ) as load_session,
                patch.object(
                    overseas_tiktok_publish,
                    "replace_tiktok_storage_state_file",
                ) as replace_session,
                patch.object(recovered_publish, "reveal_page_window") as reveal,
            ):
                result = overseas_preflight.run_overseas_preflight_sync(
                    self._payload(video)
                )

            handler.assert_not_called()
            playwright_factory.assert_not_called()
            uploader_factory.assert_not_called()
            load_session.assert_not_called()
            replace_session.assert_not_called()
            reveal.assert_not_called()
            self.assertEqual(result["phase"], "local_preflight_passed")
            self.assertFalse(result["receipt"]["platformWriteOccurred"])
            self.assertFalse(result["receipt"]["finalActionTriggered"])
            self.assertEqual(session.read_bytes(), original_session)

    def test_any_tiktok_platform_signal_blocks_all_legacy_handlers_before_rejection(self) -> None:
        cases = (
            {"type": 7, "platformType": 6},
            {"type": 8, "platform": "TikTok"},
            {
                "type": 9,
                "target": {
                    "platform": "TikTok",
                    "accountId": 61,
                    "schedule": None,
                },
            },
            {
                "type": 7,
                "targets": [
                    {
                        "platform": "TikTok",
                        "accountId": 61,
                        "schedule": None,
                    }
                ],
            },
            {
                "type": 8,
                "targets": {
                    "platform": "TikTok",
                    "accountId": 61,
                },
            },
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            (root / "tiktok.json").write_text(
                '{"cookies": [], "origins": []}',
                encoding="utf-8",
            )
            handlers = {
                platform_type: MagicMock(
                    side_effect=AssertionError("legacy handler must not run")
                )
                for platform_type in (6, 7, 8, 9)
            }
            with (
                self._local_tiktok(root),
                patch.dict(overseas_preflight.PREFLIGHT_HANDLERS, handlers),
                patch.object(tiktok_identity_service, "async_playwright") as playwright,
                patch.object(recovered_publish, "reveal_page_window") as reveal,
                patch(
                    "uploader.tk_uploader.main.load_sanitized_tiktok_storage_state_file"
                ) as load_session,
                patch(
                    "uploader.tk_uploader.main.replace_tiktok_storage_state_file"
                ) as replace_session,
            ):
                for signals in cases:
                    with self.subTest(signals=signals):
                        with self.assertRaises(
                            overseas_tiktok_publish.TikTokPublishError
                        ):
                            overseas_preflight.run_overseas_preflight_sync(
                                self._payload(video) | signals
                            )

            for handler in handlers.values():
                handler.assert_not_called()
            playwright.assert_not_called()
            reveal.assert_not_called()
            load_session.assert_not_called()
            replace_session.assert_not_called()

    def test_youtube_preflight_requires_verified_fields_and_reports_private_upload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            (root / "youtube.json").write_text("{}", encoding="utf-8")
            payload = self._payload(video)
            payload.update(
                {
                    "type": 7,
                    "accountList": ["youtube.json"],
                    "visibility": "private",
                }
            )
            with (
                patch.object(overseas_preflight, "COOKIE_DIR", root),
                patch.dict(
                    overseas_preflight.PREFLIGHT_HANDLERS,
                    {7: lambda *_args, **_kwargs: [None]},
                ),
            ):
                with self.assertRaisesRegex(
                    overseas_preflight.OverseasPreflightError,
                    "逐字段回读",
                ):
                    overseas_preflight.run_overseas_preflight_sync(payload)

            receipt = {
                "status": "preflight_ready",
                "evidence": "youtube_preflight_fields_verified",
                "verifiedFields": [
                    "video",
                    "title",
                    "description",
                    "tags",
                    "audience",
                    "visibility",
                ],
                "visibility": "private",
                "platformMutation": "private_upload",
            }
            with (
                patch.object(overseas_preflight, "COOKIE_DIR", root),
                patch.dict(
                    overseas_preflight.PREFLIGHT_HANDLERS,
                    {7: lambda *_args, **_kwargs: [receipt]},
                ),
            ):
                result = overseas_preflight.run_overseas_preflight_sync(payload)

        self.assertTrue(result["ok"])
        self.assertEqual(result["platformMutation"], "private_upload")
        self.assertEqual(result["visibility"], "private")
        self.assertEqual(
            set(result["verifiedFields"]),
            {"video", "title", "description", "tags", "audience", "visibility"},
        )

    def test_youtube_preflight_is_limited_to_one_account_and_one_video(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            (root / "first.json").write_text("{}", encoding="utf-8")
            (root / "second.json").write_text("{}", encoding="utf-8")
            payload = self._payload(first)
            payload.update(
                {
                    "type": 7,
                    "fileList": [str(first), str(second)],
                    "accountList": ["first.json", "second.json"],
                }
            )
            with patch.object(overseas_preflight, "COOKIE_DIR", root):
                checked = overseas_preflight.validate_overseas_preflight_payload(
                    payload
                )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("一条视频" in item for item in checked["errors"]))
        self.assertTrue(any("一个账号" in item for item in checked["errors"]))

    def test_browser_validation_blocks_silent_field_loss(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            (root / "tiktok.json").write_text("{}", encoding="utf-8")
            youtube = self._payload(video)
            youtube.update({"type": 7, "notifySubscribers": False})
            instagram = self._payload(video)
            instagram.update({"type": 8, "shareToFeed": False})
            facebook = self._payload(video)
            facebook.update({"type": 9, "aiGenerated": True})
            with patch.object(overseas_preflight, "COOKIE_DIR", root):
                youtube_result = overseas_preflight.validate_overseas_preflight_payload(
                    youtube
                )
                instagram_result = overseas_preflight.validate_overseas_preflight_payload(
                    instagram
                )
                facebook_result = overseas_preflight.validate_overseas_preflight_payload(
                    facebook
                )
        self.assertTrue(any("不通知订阅者" in item for item in youtube_result["errors"]))
        self.assertTrue(any("仅 Reels" in item for item in instagram_result["errors"]))
        self.assertTrue(any("AI 声明" in item for item in facebook_result["errors"]))

    def test_recovered_app_receives_platform_specific_options(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            app = recovered_publish._make_platform_app(
                {
                    "type": 7,
                    "title": "透传测试",
                    "description": "离线",
                    "tags": [],
                    "visibility": "unlisted",
                    "madeForKids": True,
                    "notifySubscribers": False,
                    "aiGenerated": True,
                },
                str(video),
                0,
                Path(raw) / "youtube.json",
                dry_run=True,
            )
        self.assertEqual(app.visibility, "unlisted")
        self.assertTrue(app.made_for_kids)
        self.assertFalse(app.notify_subscribers)
        self.assertTrue(app.ai_generated)

    def test_recovered_tiktok_immediate_zero_never_reactivates_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            app = recovered_publish._make_platform_app(
                {
                    "type": 6,
                    "title": "TikTok immediate",
                    "description": "offline",
                    "tags": [],
                    "visibility": "public",
                },
                str(video),
                0,
                Path(raw) / "tiktok.json",
                dry_run=True,
            )
        self.assertIsNone(app._requested_schedule_at)
        self.assertIsNone(app._schedule_target)

    def test_schedule_is_rejected_until_platform_time_is_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            (root / "tiktok.json").write_text("{}", encoding="utf-8")
            payload = self._payload(video)
            payload["enableTimer"] = True
            payload["scheduleTime"] = "2026-08-08 18:00"
            payload["dailyTimes"] = ["18:00"]
            with self._local_tiktok(root):
                result = overseas_preflight.validate_overseas_preflight_payload(payload)
        self.assertFalse(result["ok"])
        self.assertTrue(any("排期" in item for item in result["errors"]))

    def test_formal_publish_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            with self.assertRaisesRegex(ValueError, "正式发布确认"):
                publish_service._validate_payloads(
                    [
                        {
                            **self._payload(video),
                            "runtimeMode": "publish",
                            "debugDryRun": False,
                        }
                    ]
                )


class YouTubeBrowserFieldTests(unittest.TestCase):
    def test_audience_selection_uses_target_control_and_readback(self) -> None:
        class Locator:
            def __init__(self, matched: bool):
                self.matched = matched
                self.first = self
                self.clicked = False

            async def count(self):
                return 1 if self.matched else 0

            async def is_visible(self):
                return self.matched

            async def click(self):
                self.clicked = True

            async def evaluate(self, _script):
                return self.clicked

        class Page:
            def __init__(self):
                self.locators = {}

            def locator(self, selector):
                matched = "VIDEO_MADE_FOR_KIDS_MFK" in selector
                result = Locator(matched)
                self.locators[selector] = result
                return result

            async def wait_for_timeout(self, _milliseconds):
                return None

        app = YouTubeVideo(
            "受众测试",
            "/not/used.mp4",
            [],
            "/not/used.json",
        )
        app.made_for_kids = True
        page = Page()
        asyncio.run(app.set_audience(page))
        target = next(
            locator
            for selector, locator in page.locators.items()
            if "VIDEO_MADE_FOR_KIDS_MFK" in selector
        )
        self.assertTrue(target.clicked)

    def test_audience_selection_stops_when_readback_is_missing(self) -> None:
        class Locator:
            first = None

            def __init__(self):
                self.first = self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def click(self):
                return None

            async def evaluate(self, _script):
                return False

        class Page:
            def locator(self, _selector):
                return Locator()

            async def wait_for_timeout(self, _milliseconds):
                return None

        app = YouTubeVideo(
            "受众测试",
            "/not/used.mp4",
            [],
            "/not/used.json",
        )
        with self.assertRaisesRegex(RuntimeError, "无法回读确认"):
            asyncio.run(app.set_audience(Page()))


if __name__ == "__main__":
    unittest.main()
