# -*- coding: utf-8 -*-
"""Facebook Page-only login and saved Page readback contracts."""

from __future__ import annotations

import asyncio
import sqlite3
import os
import queue
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app_core import account_browser_service, account_service, login_service
from app_core.oneclick_authorization import _verify_saved_session_async
from app_core.overseas_meta_errors import FacebookPagePublishError
from app_core.overseas_meta_page_identity import FacebookPageIdentity
from myUtils import login as recovered_login


class _FakePageRow:
    def __init__(self, page: "_FakePage", record: dict[str, object]) -> None:
        self.page = page
        self.record = record

    async def get_attribute(self, name: str):
        values = {
            "data-page-id": self.record["page_id"],
            "data-page-name": self.record["page_name"],
            "data-avatar-url": self.record.get("avatar_url", ""),
            "data-can-manage-content": (
                "true" if self.record.get("can_manage_content") else "false"
            ),
        }
        return values.get(name)

    async def inner_text(self) -> str:
        return str(self.record["page_name"])

    async def click(self) -> None:
        self.page.active_page_id = str(self.record["page_id"])


class _FakeLocator:
    def __init__(self, rows: list[_FakePageRow]) -> None:
        self.rows = rows

    async def count(self) -> int:
        return len(self.rows)

    def nth(self, index: int) -> _FakePageRow:
        return self.rows[index]


class _FakePage:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records
        self.active_page_id = ""
        self.url = "https://business.facebook.com/latest/composer/"

    async def goto(self, *_args, **_kwargs) -> None:
        return None

    def locator(self, selector: str) -> _FakeLocator:
        records = self.records
        if 'data-page-active="true"' in selector:
            records = [
                item
                for item in records
                if item["page_id"] == self.active_page_id
            ]
        elif 'data-page-id="' in selector:
            expected = selector.split('data-page-id="', 1)[1].split('"', 1)[0]
            records = [item for item in records if item["page_id"] == expected]
        return _FakeLocator([_FakePageRow(self, item) for item in records])


def _identity(
    page_id: str,
    name: str = "测试 Page",
    *,
    allowed: bool = True,
) -> FacebookPageIdentity:
    return FacebookPageIdentity(
        page_id=page_id,
        page_name=name,
        can_manage_content=allowed,
    )


class FacebookPageLoginSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_pages_pause_until_an_exact_page_id_is_selected(self) -> None:
        session = login_service.RecoveredOverseasLoginSession(9, "Meta 主体")
        pages = (_identity("1001", "同名"), _identity("1002", "同名"))

        waiting = asyncio.create_task(
            session._request_facebook_page_selection(pages)
        )
        request = await session.next_selection_request()

        self.assertIsInstance(
            request,
            login_service.FacebookPageSelectionRequest,
        )
        self.assertEqual([item.page_id for item in request.pages], ["1001", "1002"])
        self.assertFalse(waiting.done())
        session.select_facebook_page("1002")
        self.assertEqual((await waiting).page_id, "1002")

    async def test_unknown_page_selection_is_rejected_without_resuming(self) -> None:
        session = login_service.RecoveredOverseasLoginSession(9, "Meta 主体")
        waiting = asyncio.create_task(
            session._request_facebook_page_selection((_identity("1001"), _identity("1002")))
        )
        await session.next_selection_request()

        with self.assertRaises(FacebookPagePublishError) as raised:
            session.select_facebook_page("9999")

        self.assertEqual(raised.exception.error_code, "facebook_page_identity_mismatch")
        self.assertFalse(waiting.done())
        session.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting

    async def test_zero_one_and_permission_missing_use_task_one_identity_rules(self) -> None:
        with self.assertRaises(FacebookPagePublishError) as zero:
            await recovered_login.select_facebook_page_for_login(_FakePage([]))
        self.assertEqual(zero.exception.error_code, "facebook_page_not_found")

        page = _FakePage(
            [{"page_id": "1001", "page_name": "唯一", "can_manage_content": True}]
        )
        selected = await recovered_login.select_facebook_page_for_login(page)
        self.assertEqual(selected.page_id, "1001")
        self.assertEqual(page.active_page_id, "1001")

        denied = _FakePage(
            [{"page_id": "1001", "page_name": "无权限", "can_manage_content": False}]
        )
        with self.assertRaises(FacebookPagePublishError) as permission:
            await recovered_login.select_facebook_page_for_login(denied)
        self.assertEqual(
            permission.exception.error_code,
            "facebook_page_content_permission_missing",
        )

    async def test_update_mode_keeps_the_original_saved_page_without_prompting(self) -> None:
        page = _FakePage(
            [
                {"page_id": "1001", "page_name": "原 Page", "can_manage_content": True},
                {"page_id": "1002", "page_name": "其它 Page", "can_manage_content": True},
            ]
        )
        selector = AsyncMock(return_value="1002")

        selected = await recovered_login.select_facebook_page_for_login(
            page,
            selection_callback=selector,
            expected_page_id="1001",
        )

        self.assertEqual(selected.page_id, "1001")
        self.assertEqual(page.active_page_id, "1001")
        selector.assert_not_awaited()


class FacebookPageLoginRoutingTests(unittest.TestCase):
    def test_default_off_hides_facebook_page_and_refuses_startup(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotIn(9, dict(account_service.login_platform_options()))
            with self.assertRaisesRegex(RuntimeError, "Facebook Page V1"):
                login_service.start_login(9, "Meta 主体")

    def test_test_only_opt_in_exposes_a_distinct_type_9_login(self) -> None:
        with (
            patch.dict(os.environ, {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"}, clear=True),
            patch.object(login_service.RecoveredOverseasLoginSession, "start") as start,
        ):
            options = dict(account_service.login_platform_options())
            session = login_service.start_login(9, "Meta 主体")

        self.assertEqual(options[8], "Instagram Reels")
        self.assertEqual(options[9], "Facebook Page")
        self.assertEqual(account_service.login_platform_type(9), 9)
        self.assertEqual(session.platform_type, 9)
        start.assert_called_once_with()

    def test_page_only_session_routes_to_page_generator_not_shared_meta_save(self) -> None:
        session = login_service.RecoveredOverseasLoginSession(9, "Meta 主体")
        with (
            patch.object(recovered_login, "facebook_page_cookie_gen", new=AsyncMock()) as page_login,
            patch.object(recovered_login, "meta_cookie_gen", new=AsyncMock()) as instagram_login,
        ):
            asyncio.run(session._run())

        page_login.assert_awaited_once()
        instagram_login.assert_not_awaited()


class FacebookPagePersistenceTimingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "cookiesFile").mkdir()
        (self.root / "avatars").mkdir()
        (self.root / "db").mkdir()
        self.database = self.root / "db" / "database.db"
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

    def tearDown(self) -> None:
        self.temp.cleanup()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _row_count(self) -> int:
        connection = sqlite3.connect(self.database)
        try:
            return int(connection.execute("SELECT COUNT(*) FROM user_info").fetchone()[0])
        finally:
            connection.close()

    async def _run_page_login(
        self,
        selection_callback,
        checked_identity=None,
        *,
        check_error: Exception | None = None,
        cancel_event=None,
    ):
        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        page = _FakePage(
            [
                {"page_id": "1001", "page_name": "同名", "can_manage_content": True},
                {"page_id": "1002", "page_name": "同名", "can_manage_content": True},
            ]
        )
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)

        async def save_state(_context, path, **_kwargs):
            Path(path).write_text("{}", encoding="utf-8")

        status_queue: queue.Queue[object] = queue.Queue()
        with (
            patch.object(recovered_login, "BASE_DIR", self.root),
            patch.object(account_service, "connect", self._connect),
            patch.object(recovered_login, "async_playwright", return_value=PlaywrightContext()),
            patch.object(recovered_login, "launch_login_browser", new=AsyncMock(return_value=MagicMock())),
            patch.object(recovered_login, "new_login_context", new=AsyncMock(return_value=context)),
            patch.object(recovered_login, "set_init_script", new=AsyncMock(return_value=context)),
            patch.object(recovered_login, "_wait_for_browser_login", new=AsyncMock(return_value="ready")),
            patch.object(recovered_login, "save_context_storage_state", new=AsyncMock(side_effect=save_state)),
            patch.object(
                recovered_login,
                "check_cookie",
                new=AsyncMock(
                    return_value=checked_identity,
                    side_effect=check_error,
                ),
            ),
            patch.object(recovered_login, "close_login_resources", new=AsyncMock()),
        ):
            result = await recovered_login._browser_cookie_gen(
                9,
                "Meta 主体",
                status_queue,
                background_mode=True,
                cancel_event=cancel_event,
                selection_callback=selection_callback,
            )
        return result, list(status_queue.queue)

    async def test_multiple_page_login_writes_nothing_until_selection_then_saves_only_type_9(self) -> None:
        request_seen = asyncio.Event()
        choice_ready = asyncio.Event()

        async def choose(pages):
            self.assertEqual([item.page_id for item in pages], ["1001", "1002"])
            request_seen.set()
            await choice_ready.wait()
            return "1002"

        running = asyncio.create_task(self._run_page_login(choose, _identity("1002", "同名")))
        await request_seen.wait()
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(list((self.root / "cookiesFile").iterdir()), [])

        choice_ready.set()
        result, messages = await running

        self.assertIsNotNone(result)
        self.assertIn("200", messages)
        connection = sqlite3.connect(self.database)
        rows = connection.execute(
            "SELECT type, accountReference FROM user_info"
        ).fetchall()
        connection.close()
        self.assertEqual(rows, [(9, "1002")])

    async def test_cancelled_page_selection_saves_no_row_or_session_file(self) -> None:
        async def cancel(_pages):
            raise asyncio.CancelledError

        result, messages = await self._run_page_login(cancel, _identity("1001"))

        self.assertIsNone(result)
        self.assertIn("CANCELLED", messages)
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(list((self.root / "cookiesFile").iterdir()), [])

    async def test_unknown_page_selection_saves_nothing(self) -> None:
        async def choose_unknown(_pages):
            return "9999"

        result, messages = await self._run_page_login(
            choose_unknown,
            _identity("1001"),
        )

        self.assertIsNone(result)
        self.assertIn("ERROR:facebook_page_identity_mismatch", messages)
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(list((self.root / "cookiesFile").iterdir()), [])

    async def test_exact_readback_permission_loss_removes_the_candidate_session(self) -> None:
        async def choose(_pages):
            return "1001"

        failure = FacebookPagePublishError(
            "facebook_page_content_permission_missing",
            "Page permission missing",
        )
        result, messages = await self._run_page_login(
            choose,
            check_error=failure,
        )

        self.assertIsNone(result)
        self.assertIn("ERROR:facebook_page_content_permission_missing", messages)
        self.assertIn("500", messages)
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(list((self.root / "cookiesFile").iterdir()), [])

    async def test_exact_readback_denied_identity_is_not_saved_as_success(self) -> None:
        async def choose(_pages):
            return "1001"

        result, messages = await self._run_page_login(
            choose,
            _identity("1001", "同名", allowed=False),
        )

        self.assertIsNone(result)
        self.assertIn("ERROR:facebook_page_content_permission_missing", messages)
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(list((self.root / "cookiesFile").iterdir()), [])

    async def test_cancel_after_selection_but_before_save_still_writes_nothing(self) -> None:
        cancelled = threading.Event()

        async def choose(_pages):
            cancelled.set()
            return "1001"

        result, messages = await self._run_page_login(
            choose,
            _identity("1001", "同名"),
            cancel_event=cancelled,
        )

        self.assertIsNone(result)
        self.assertIn("CANCELLED", messages)
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(list((self.root / "cookiesFile").iterdir()), [])

    async def test_restart_mismatch_marks_the_row_abnormal_and_keeps_the_page_id(self) -> None:
        connection = sqlite3.connect(self.database)
        account_id = connection.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, authMode,
                 accountReference)
            VALUES (9, 'page.json', '测试 Page', 1, 'Meta 主体', 'browser', '1001')
            """
        ).lastrowid
        connection.commit()
        connection.close()
        failure = FacebookPagePublishError(
            "facebook_page_identity_mismatch",
            "Page mismatch",
        )

        with (
            patch.object(account_service, "connect", self._connect),
            patch(
                "app_core.oneclick_authorization.verify_saved_session",
                side_effect=failure,
            ),
        ):
            result = account_service.validate_accounts(
                [int(account_id)],
                invalid_status=2,
            )

        self.assertEqual(result["accounts"][0]["status"], 0)
        self.assertEqual(
            result["authIssues"][int(account_id)],
            "facebook_page_identity_mismatch",
        )
        self.assertEqual(result["accounts"][0]["accountReference"], "1001")


class FacebookPageSavedSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_session_check_returns_the_exact_page_identity(self) -> None:
        identity = _identity("1001")
        with tempfile.TemporaryDirectory() as raw:
            state_file = Path(raw) / "page.json"
            state_file.write_text("{}", encoding="utf-8")
            account = {
                "id": 1,
                "type": 9,
                "status": 1,
                "authMode": "browser",
                "profileName": "Meta 主体",
                "filePath": state_file.name,
                "accountReference": "1001",
            }
            with (
                patch("app_core.oneclick_authorization.COOKIE_DIR", Path(raw)),
                patch("myUtils.auth.check_cookie", new=AsyncMock(return_value=identity)) as check,
            ):
                result = await _verify_saved_session_async(account)

        self.assertIs(result, identity)
        check.assert_awaited_once_with(
            9,
            "page.json",
            preview=False,
            account_reference="1001",
        )

    async def test_saved_session_mismatch_and_permission_loss_are_not_boolean_success(self) -> None:
        for error_code in (
            "facebook_page_identity_mismatch",
            "facebook_page_content_permission_missing",
        ):
            with self.subTest(error_code=error_code), tempfile.TemporaryDirectory() as raw:
                state_file = Path(raw) / "page.json"
                state_file.write_text("{}", encoding="utf-8")
                account = {
                    "type": 9,
                    "status": 1,
                    "authMode": "browser",
                    "profileName": "Meta 主体",
                    "filePath": state_file.name,
                    "accountReference": "1001",
                }
                failure = FacebookPagePublishError(error_code, "Page check failed")
                with (
                    patch("app_core.oneclick_authorization.COOKIE_DIR", Path(raw)),
                    patch("myUtils.auth.check_cookie", new=AsyncMock(side_effect=failure)),
                    self.assertRaises(FacebookPagePublishError) as raised,
                ):
                    await _verify_saved_session_async(account)
                self.assertEqual(raised.exception.error_code, error_code)

    async def test_open_backend_activates_and_reads_back_the_bound_page_before_showing_it(self) -> None:
        account = {
            "id": 5,
            "type": 9,
            "status": 1,
            "authMode": "browser",
            "profileName": "Meta 主体",
            "filePath": "page.json",
            "accountReference": "1001",
        }
        page = MagicMock()
        page.goto = AsyncMock()
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
            (Path(raw) / "page.json").write_text("{}", encoding="utf-8")
            with (
                patch("app_core.account_browser_service.COOKIE_DIR", Path(raw)),
                patch(
                    "playwright.async_api.async_playwright",
                    return_value=MagicMock(start=AsyncMock(return_value=playwright)),
                ),
                patch(
                    "app_core.account_browser_service.activate_saved_facebook_page",
                    new=AsyncMock(return_value=_identity("1001")),
                    create=True,
                ) as activate,
            ):
                await account_browser_service._open_backend(account)

        activate.assert_awaited_once_with(page, "1001")
        page.bring_to_front.assert_awaited_once_with()

    def test_unbound_legacy_row_refuses_backend_open_before_thread_start(self) -> None:
        account = {
            "id": 5,
            "type": 9,
            "status": 0,
            "authMode": "browser",
            "profileName": "Meta 主体",
            "filePath": "legacy.json",
            "accountReference": "",
        }
        with patch.object(account_browser_service.threading.Thread, "start") as start:
            with self.assertRaises(FacebookPagePublishError) as raised:
                account_browser_service.open_account_backend(account)

        self.assertEqual(raised.exception.error_code, "facebook_page_identity_mismatch")
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
