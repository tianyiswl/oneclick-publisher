# -*- coding: utf-8 -*-
"""Facebook Page-only login and saved Page readback contracts."""

from __future__ import annotations

import asyncio
from io import BytesIO
import sqlite3
import os
import queue
import tempfile
import threading
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from app_core import account_browser_service, account_service, login_service
from app_core.oneclick_authorization import _verify_saved_session_async
from app_core.overseas_meta_errors import FacebookPagePublishError
from app_core.overseas_meta_page_identity import (
    FacebookPageIdentity,
    activate_saved_facebook_page,
    discover_manageable_facebook_pages,
)
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


class _RealComposerElement:
    def __init__(
        self,
        *,
        text: str = "",
        attributes: dict[str, str] | None = None,
        descendants: dict[str, list["_RealComposerElement"]] | None = None,
        visible: bool = True,
    ) -> None:
        self.text = text
        self.attributes = attributes or {}
        self.descendants = descendants or {}
        self.visible = visible

    async def inner_text(self, **_kwargs) -> str:
        return self.text

    async def get_attribute(self, name: str):
        return self.attributes.get(name)

    async def is_visible(self) -> bool:
        return self.visible

    def locator(self, selector: str) -> "_RealComposerLocator":
        return _RealComposerLocator(self.descendants.get(selector, []))


class _RealComposerLocator:
    def __init__(self, elements: list[_RealComposerElement]) -> None:
        self.elements = elements

    @property
    def first(self) -> _RealComposerElement:
        if self.elements:
            return self.elements[0]
        return _RealComposerElement(visible=False)

    async def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> _RealComposerElement:
        return self.elements[index]

    async def inner_text(self, **kwargs) -> str:
        return await self.first.inner_text(**kwargs)


class _RealFacebookComposerPage:
    """Minimal boundary fixture copied from the live Chinese Meta composer."""

    def __init__(self) -> None:
        self.url = (
            "https://business.facebook.com/latest/composer/"
            "?asset_id=123456789012345&nav_ref=biz_unified_f3_login_page_to_mbs"
        )
        self.body_text = (
            "发帖\n发布位置\nFacebook\n测试主页\n影音内容\n"
            "分享照片和视频。\n添加照片/视频\n帖子详情\n文字"
        )

    def locator(self, selector: str) -> _RealComposerLocator:
        if selector == "body":
            return _RealComposerLocator([_RealComposerElement(text=self.body_text)])
        if selector == '[role="combobox"]':
            return _RealComposerLocator(
                [
                    _RealComposerElement(
                        text="发布位置\nFacebook\n测试主页",
                        attributes={"aria-label": "发布位置"},
                    )
                ]
            )
        if selector == 'input[type="file"]':
            return _RealComposerLocator([_RealComposerElement()])
        return _RealComposerLocator([])


class _RealFacebookIconOnlyComposerPage(_RealFacebookComposerPage):
    """The live combobox exposes Facebook as an icon, not inner text."""

    def locator(self, selector: str) -> _RealComposerLocator:
        if selector == '[role="combobox"]':
            facebook_icon_selector = (
                'img[alt*="Facebook" i], [aria-label*="Facebook" i]'
            )
            return _RealComposerLocator(
                [
                    _RealComposerElement(
                        text="测试主页",
                        attributes={"aria-label": "发布位置 测试主页"},
                        descendants={
                            facebook_icon_selector: [
                                _RealComposerElement(
                                    attributes={"alt": "Facebook"}
                                )
                            ]
                        },
                    )
                ]
            )
        return super().locator(selector)


class _DelayedRealFacebookComposerPage(_RealFacebookComposerPage):
    """The post shell can be ready before the Page destination finishes loading."""

    def __init__(self, ready_on_destination_read: int = 3) -> None:
        super().__init__()
        self.ready_on_destination_read = ready_on_destination_read
        self.destination_reads = 0

    def locator(self, selector: str) -> _RealComposerLocator:
        if selector == '[role="combobox"]':
            self.destination_reads += 1
            if self.destination_reads < self.ready_on_destination_read:
                return _RealComposerLocator([])
        return super().locator(selector)


class _LiveRoleButtonFacebookComposerPage(_RealFacebookComposerPage):
    """Exact safe shape captured from the live Meta composer on 2026-08-30."""

    def locator(self, selector: str) -> _RealComposerLocator:
        if selector == '[role="combobox"]':
            facebook_icon_selector = (
                'img[alt*="Facebook" i], [aria-label*="Facebook" i]'
            )
            return _RealComposerLocator(
                [
                    _RealComposerElement(
                        text="测试主页",
                        attributes={"role": "combobox"},
                        descendants={
                            facebook_icon_selector: [
                                _RealComposerElement(
                                    attributes={"alt": "Facebook"}
                                )
                            ]
                        },
                    ),
                    _RealComposerElement(
                        attributes={
                            "role": "combobox",
                            "aria-label": "在对话框中输入内容，即可为帖子添加文字。",
                        }
                    ),
                    _RealComposerElement(
                        text="无按钮",
                        attributes={"role": "combobox"},
                    ),
                ]
            )
        if selector in {
            'input[type="file"]',
            'button:has-text("Add photo/video")',
            'button:has-text("Add photos/videos")',
            'button:has-text("添加照片/视频")',
            '[contenteditable="true"][role="textbox"]',
        }:
            return _RealComposerLocator([])
        if selector == '[role="button"]:has-text("添加照片/视频")':
            return _RealComposerLocator(
                [
                    _RealComposerElement(
                        text="添加照片/视频",
                        attributes={"role": "button"},
                    )
                ]
            )
        return super().locator(selector)


class FacebookPageRealComposerReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_chinese_composer_is_a_completed_facebook_login(self) -> None:
        page = _RealFacebookComposerPage()

        self.assertEqual(
            await recovered_login._browser_login_state(page, 9),
            "ready",
        )

    async def test_live_composer_reads_exact_active_page_identity(self) -> None:
        page = _RealFacebookComposerPage()

        pages = await discover_manageable_facebook_pages(page)

        self.assertEqual(
            pages,
            (
                FacebookPageIdentity(
                    page_id="123456789012345",
                    page_name="测试主页",
                    can_manage_content=True,
                ),
            ),
        )

    async def test_live_composer_accepts_facebook_icon_separate_from_page_name(self) -> None:
        page = _RealFacebookIconOnlyComposerPage()

        pages = await discover_manageable_facebook_pages(page)

        self.assertEqual(
            pages,
            (
                FacebookPageIdentity(
                    page_id="123456789012345",
                    page_name="测试主页",
                    can_manage_content=True,
                ),
            ),
        )

    async def test_login_waits_for_delayed_page_destination_before_rejecting_it(self) -> None:
        page = _DelayedRealFacebookComposerPage(ready_on_destination_read=3)

        with patch.object(
            recovered_login.asyncio,
            "sleep",
            new=AsyncMock(),
        ):
            try:
                selected = await recovered_login.select_facebook_page_for_login(page)
            except FacebookPagePublishError:
                selected = None

        self.assertIsNotNone(selected)
        self.assertEqual(selected.page_id, "123456789012345")
        self.assertEqual(selected.page_name, "测试主页")
        self.assertGreaterEqual(page.destination_reads, 3)

    async def test_live_role_button_media_control_proves_page_content_permission(self) -> None:
        page = _LiveRoleButtonFacebookComposerPage()

        pages = await discover_manageable_facebook_pages(page)

        self.assertEqual(
            pages,
            (
                FacebookPageIdentity(
                    page_id="123456789012345",
                    page_name="测试主页",
                    can_manage_content=True,
                ),
            ),
        )

    async def test_matching_live_composer_is_already_the_requested_page(self) -> None:
        page = _RealFacebookComposerPage()

        selected = await activate_saved_facebook_page(
            page,
            "123456789012345",
        )

        self.assertEqual(selected.page_name, "测试主页")

    async def test_saved_page_activation_waits_for_delayed_real_composer_controls(self) -> None:
        page = _DelayedRealFacebookComposerPage(ready_on_destination_read=3)

        with patch(
            "app_core.overseas_meta_page_identity.asyncio.sleep",
            new=AsyncMock(),
        ):
            selected = await activate_saved_facebook_page(
                page,
                "123456789012345",
                timeout_seconds=0.1,
                poll_interval_seconds=0,
            )

        self.assertEqual(selected.page_id, "123456789012345")
        self.assertEqual(selected.page_name, "测试主页")
        self.assertGreaterEqual(page.destination_reads, 3)


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

    def test_meta_login_options_stay_hidden_even_when_legacy_flag_is_enabled(self) -> None:
        with patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
            clear=True,
        ):
            options = dict(account_service.login_platform_options())

        self.assertEqual(options[6], "TikTok")
        self.assertEqual(options[7], "YouTube")
        self.assertNotIn(8, options)
        self.assertNotIn(9, options)

    def test_direct_page_session_start_rechecks_the_exact_closed_gate(self) -> None:
        session = login_service.RecoveredOverseasLoginSession(9, "Meta 主体")

        with (
            patch.dict(
                os.environ,
                {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "true"},
                clear=True,
            ),
            patch.object(login_service.threading, "Thread") as worker,
            self.assertRaisesRegex(RuntimeError, "Facebook Page V1"),
        ):
            session.start()

        worker.assert_not_called()

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

    def _insert_saved_page_account(self) -> int:
        connection = sqlite3.connect(self.database)
        try:
            account_id = int(
                connection.execute(
                    """
                    INSERT INTO user_info
                        (type, filePath, userName, status, profileName, authMode,
                         accountReference)
                    VALUES (9, 'page.json', '测试 Page', 1, 'Meta 主体',
                            'browser', '1001')
                    """
                ).lastrowid
            )
            connection.commit()
            return account_id
        finally:
            connection.close()

    async def _run_page_login(
        self,
        selection_callback,
        checked_identity=None,
        *,
        wait_result: str = "ready",
        check_error: Exception | None = None,
        cancel_event=None,
        page_records: list[dict[str, object]] | None = None,
        update_account: dict[str, object] | None = None,
        save_error: Exception | None = None,
        chmod_error: Exception | None = None,
    ):
        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        page = _FakePage(
            page_records
            if page_records is not None
            else [
                {"page_id": "1001", "page_name": "同名", "can_manage_content": True},
                {"page_id": "1002", "page_name": "同名", "can_manage_content": True},
            ]
        )
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)

        async def save_state(_context, path, **_kwargs):
            Path(path).write_text("{}", encoding="utf-8")

        status_queue: queue.Queue[object] = queue.Queue()
        saver_patch = (
            patch.object(
                account_service,
                "save_facebook_page_browser_account",
                side_effect=save_error,
            )
            if save_error is not None
            else nullcontext()
        )
        chmod_patch = (
            patch.object(recovered_login.os, "chmod", side_effect=chmod_error)
            if chmod_error is not None
            else nullcontext()
        )
        with (
            patch.object(recovered_login, "BASE_DIR", self.root),
            patch.object(account_service, "connect", self._connect),
            patch.object(recovered_login, "async_playwright", return_value=PlaywrightContext()),
            patch.object(recovered_login, "launch_login_browser", new=AsyncMock(return_value=MagicMock())),
            patch.object(recovered_login, "new_login_context", new=AsyncMock(return_value=context)),
            patch.object(recovered_login, "set_init_script", new=AsyncMock(return_value=context)),
            patch.object(
                recovered_login,
                "_wait_for_browser_login",
                new=AsyncMock(return_value=wait_result),
            ),
            patch.object(recovered_login, "save_context_storage_state", new=AsyncMock(side_effect=save_state)),
            chmod_patch,
            patch.object(
                recovered_login,
                "check_cookie",
                new=AsyncMock(
                    return_value=checked_identity,
                    side_effect=check_error,
                ),
            ),
            patch.object(recovered_login, "close_login_resources", new=AsyncMock()),
            saver_patch,
        ):
            result = await recovered_login._browser_cookie_gen(
                9,
                "Meta 主体",
                status_queue,
                update_mode=update_account is not None,
                record_id=(
                    int(update_account["id"])
                    if update_account is not None
                    else None
                ),
                background_mode=True,
                cancel_event=cancel_event,
                selection_callback=selection_callback,
                expected_account=update_account,
            )
        return result, list(status_queue.queue)

    async def test_meta_business_suite_denied_returns_a_stable_page_error_code(self) -> None:
        result, messages = await self._run_page_login(
            None,
            wait_result="denied",
        )

        self.assertIsNone(result)
        self.assertEqual(
            [str(item) for item in messages if str(item).startswith("ERROR:")],
            ["ERROR:facebook_page_business_access_denied"],
        )
        self.assertNotIn("YouTube", "\n".join(map(str, messages)))

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
            patch.dict(
                os.environ,
                {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                clear=True,
            ),
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

    async def test_saved_page_login_detection_is_rejected_while_feature_is_off(self) -> None:
        account_id = self._insert_saved_page_account()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(account_service, "connect", self._connect),
            patch(
                "app_core.oneclick_authorization.verify_saved_session",
                return_value=True,
            ) as verify,
            self.assertRaises(FacebookPagePublishError) as raised,
        ):
            account_service.validate_accounts([account_id])

        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_feature_disabled",
        )
        verify.assert_not_called()

    async def test_saved_page_login_detection_keeps_existing_behavior_while_feature_is_on(self) -> None:
        account_id = self._insert_saved_page_account()

        with (
            patch.dict(
                os.environ,
                {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                clear=True,
            ),
            patch.object(account_service, "connect", self._connect),
            patch(
                "app_core.oneclick_authorization.verify_saved_session",
                return_value=True,
            ) as verify,
        ):
            result = account_service.validate_accounts([account_id])

        self.assertEqual([row["id"] for row in result["normal"]], [account_id])
        verify.assert_called_once()

    async def test_update_identity_failures_mark_the_original_abnormal_without_overwriting_it(self) -> None:
        connection = sqlite3.connect(self.database)
        account_id = connection.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, authMode,
                 accountReference)
            VALUES (9, 'old.json', '原 Page', 1, 'Meta 主体', 'browser', '1001')
            """
        ).lastrowid
        connection.commit()
        connection.close()
        old_state = self.root / "cookiesFile" / "old.json"
        old_state.write_text('{"saved": true}', encoding="utf-8")
        expected_account = {
            "id": int(account_id),
            "type": 9,
            "status": 1,
            "authMode": "browser",
            "profileName": "Meta 主体",
            "userName": "原 Page",
            "filePath": "old.json",
            "accountReference": "1001",
        }
        allowed_pages = [
            {"page_id": "1001", "page_name": "原 Page", "can_manage_content": True}
        ]
        scenarios = (
            {
                "name": "selection_permission_loss",
                "page_records": [
                    {
                        "page_id": "1001",
                        "page_name": "原 Page",
                        "can_manage_content": False,
                    }
                ],
                "checked_identity": None,
                "check_error": None,
                "save_error": None,
                "error_code": "facebook_page_content_permission_missing",
            },
            {
                "name": "saved_state_identity_mismatch",
                "page_records": allowed_pages,
                "checked_identity": None,
                "check_error": FacebookPagePublishError(
                    "facebook_page_identity_mismatch",
                    "Page mismatch",
                ),
                "save_error": None,
                "error_code": "facebook_page_identity_mismatch",
            },
            {
                "name": "final_save_identity_mismatch",
                "page_records": allowed_pages,
                "checked_identity": _identity("1001", "原 Page"),
                "check_error": None,
                "save_error": FacebookPagePublishError(
                    "facebook_page_identity_mismatch",
                    "Page changed before commit",
                ),
                "error_code": "facebook_page_identity_mismatch",
            },
        )

        for scenario in scenarios:
            with self.subTest(stage=scenario["name"]):
                connection = sqlite3.connect(self.database)
                connection.execute(
                    """
                    UPDATE user_info
                    SET status = 1, filePath = 'old.json', accountReference = '1001'
                    WHERE id = ?
                    """,
                    (int(account_id),),
                )
                connection.commit()
                connection.close()
                result, messages = await self._run_page_login(
                    None,
                    scenario["checked_identity"],
                    check_error=scenario["check_error"],
                    page_records=scenario["page_records"],
                    update_account=expected_account,
                    save_error=scenario["save_error"],
                )

                self.assertIsNone(result)
                self.assertIn(f"ERROR:{scenario['error_code']}", messages)
                connection = sqlite3.connect(self.database)
                saved = connection.execute(
                    "SELECT status, filePath, accountReference FROM user_info WHERE id = ?",
                    (int(account_id),),
                ).fetchone()
                connection.close()
                self.assertEqual(saved, (0, "old.json", "1001"))
                self.assertEqual(
                    old_state.read_text(encoding="utf-8"),
                    '{"saved": true}',
                )

    async def test_unexpected_page_login_failures_delete_only_the_candidate_state(self) -> None:
        connection = sqlite3.connect(self.database)
        account_id = connection.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, authMode,
                 accountReference)
            VALUES (9, 'old.json', '原 Page', 1, 'Meta 主体', 'browser', '1001')
            """
        ).lastrowid
        connection.commit()
        connection.close()
        old_state = self.root / "cookiesFile" / "old.json"
        old_state.write_text('{"saved": true}', encoding="utf-8")
        expected_account = {
            "id": int(account_id),
            "type": 9,
            "status": 1,
            "authMode": "browser",
            "profileName": "Meta 主体",
            "userName": "原 Page",
            "filePath": "old.json",
            "accountReference": "1001",
        }
        pages = [
            {"page_id": "1001", "page_name": "原 Page", "can_manage_content": True}
        ]
        scenarios = (
            {
                "name": "saved_state_navigation_failure",
                "check_error": RuntimeError("navigation failed"),
                "chmod_error": None,
                "save_error": None,
            },
            {
                "name": "candidate_file_permission_failure",
                "check_error": None,
                "chmod_error": OSError("chmod failed"),
                "save_error": None,
            },
            {
                "name": "database_failure",
                "check_error": None,
                "chmod_error": None,
                "save_error": sqlite3.OperationalError("database failed"),
            },
        )

        for scenario in scenarios:
            with self.subTest(stage=scenario["name"]):
                for state_file in (self.root / "cookiesFile").iterdir():
                    if state_file.name != "old.json":
                        state_file.unlink()
                with self.assertRaises(type(
                    scenario["check_error"]
                    or scenario["chmod_error"]
                    or scenario["save_error"]
                )):
                    await self._run_page_login(
                        None,
                        _identity("1001", "原 Page"),
                        check_error=scenario["check_error"],
                        page_records=pages,
                        update_account=expected_account,
                        save_error=scenario["save_error"],
                        chmod_error=scenario["chmod_error"],
                    )

                self.assertEqual(
                    [item.name for item in (self.root / "cookiesFile").iterdir()],
                    ["old.json"],
                )
                self.assertEqual(
                    old_state.read_text(encoding="utf-8"),
                    '{"saved": true}',
                )

    async def test_successful_relogin_reclaims_only_zero_reference_managed_session(self) -> None:
        async def relogin(
            old_file_path: str,
            page_id: str,
            *,
            shared: bool = False,
        ) -> str:
            connection = sqlite3.connect(self.database)
            account_id = connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, authMode,
                     accountReference)
                VALUES (9, ?, '原 Page', 1, 'Meta 主体', 'browser', ?)
                """,
                (old_file_path, page_id),
            ).lastrowid
            if shared:
                connection.execute(
                    """
                    INSERT INTO user_info
                        (type, filePath, userName, status, profileName, authMode,
                         accountReference)
                    VALUES (8, ?, 'Instagram', 1, 'Meta 主体', 'browser', NULL)
                    """,
                    (old_file_path,),
                )
            connection.commit()
            connection.close()
            expected_account = {
                "id": int(account_id),
                "type": 9,
                "status": 1,
                "authMode": "browser",
                "profileName": "Meta 主体",
                "userName": "原 Page",
                "filePath": old_file_path,
                "accountReference": page_id,
            }

            result, messages = await self._run_page_login(
                None,
                _identity(page_id, "原 Page"),
                page_records=[
                    {
                        "page_id": page_id,
                        "page_name": "原 Page",
                        "can_manage_content": True,
                    }
                ],
                update_account=expected_account,
            )
            self.assertIn("200", messages)
            self.assertIsInstance(result, str)
            return str(result)

        unreferenced = self.root / "cookiesFile" / "old-unreferenced.json"
        unreferenced.write_text('{"old": true}', encoding="utf-8")
        await relogin(unreferenced.name, "1001")

        shared = self.root / "cookiesFile" / "old-shared.json"
        shared.write_text('{"shared": true}', encoding="utf-8")
        await relogin(shared.name, "1002", shared=True)

        outside = self.root / "outside-session.json"
        outside.write_text('{"outside": true}', encoding="utf-8")
        symlink = self.root / "cookiesFile" / "outside-link.json"
        symlink.symlink_to(outside)
        await relogin(symlink.name, "1003")

        self.assertEqual(
            (unreferenced.exists(), shared.exists(), symlink.is_symlink(), outside.exists()),
            (False, True, True, True),
        )

    def test_relogin_cleanup_failure_preserves_the_committed_new_session(self) -> None:
        old_state = self.root / "cookiesFile" / "old.json"
        old_state.write_text('{"old": true}', encoding="utf-8")
        connection = sqlite3.connect(self.database)
        account_id = connection.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, authMode,
                 accountReference)
            VALUES (9, 'old.json', '原 Page', 1, 'Meta 主体', 'browser', '2001')
            """
        ).lastrowid
        connection.execute(
            "UPDATE user_info SET filePath = 'new.json' WHERE id = ?",
            (int(account_id),),
        )
        connection.commit()
        connection.close()

        real_unlink = Path.unlink
        old_resolved = old_state.resolve()

        def fail_old_only(path: Path, *args, **kwargs):
            if path.resolve() == old_resolved:
                raise OSError("cleanup failed")
            return real_unlink(path, *args, **kwargs)

        with (
            patch.object(recovered_login, "BASE_DIR", self.root),
            patch.object(Path, "unlink", autospec=True, side_effect=fail_old_only),
        ):
            cleanup_error = None
            try:
                recovered_login._remove_replaced_facebook_page_session(
                    {"filePath": "old.json"},
                    current_cookie="new.json",
                )
            except Exception as exc:
                cleanup_error = exc

        connection = sqlite3.connect(self.database)
        saved_file = connection.execute(
            "SELECT filePath FROM user_info WHERE id = ?",
            (int(account_id),),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(
            (cleanup_error, saved_file, old_state.exists()),
            (None, "new.json", True),
        )

    def test_relogin_cleanup_preserves_managed_file_referenced_by_alias(self) -> None:
        aliases: list[tuple[str, str]] = []
        for index, old_name in enumerate(
            (
                "old-relative.json",
                "old-absolute.json",
                "old-whitespace.json",
                "old-unsafe.json",
            ),
            start=1,
        ):
            old_state = self.root / "cookiesFile" / old_name
            old_state.write_text('{"old": true}', encoding="utf-8")
            if index == 1:
                alias = f"./{old_name}"
            elif index == 2:
                alias = str(old_state.resolve())
            elif index == 3:
                alias = f"  {old_name}\t"
            else:
                alias = f"{old_name}\0"
            aliases.append((old_name, alias))

            connection = sqlite3.connect(self.database)
            connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, authMode,
                     accountReference)
                VALUES (9, ?, 'Page', 1, 'Meta 主体', 'browser', ?)
                """,
                (f"new-{index}.json", str(5000 + index)),
            )
            connection.execute(
                """
                INSERT INTO user_info
                    (type, filePath, userName, status, profileName, authMode,
                     accountReference)
                VALUES (8, ?, 'Instagram', 1, 'Meta 主体', 'browser', NULL)
                """,
                (alias,),
            )
            connection.commit()
            connection.close()

            with patch.object(recovered_login, "BASE_DIR", self.root):
                recovered_login._remove_replaced_facebook_page_session(
                    {"filePath": old_name},
                    current_cookie=f"new-{index}.json",
                )

        self.assertEqual(
            [
                (self.root / "cookiesFile" / old_name).exists()
                for old_name, _alias in aliases
            ],
            [True, True, True, True],
        )

    def test_relogin_cleanup_locks_final_reference_check_through_unlink(self) -> None:
        old_state = self.root / "cookiesFile" / "old-race.json"
        old_state.write_text('{"old": true}', encoding="utf-8")
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, authMode,
                 accountReference)
            VALUES (9, 'new-race.json', 'Page', 1, 'Meta 主体',
                    'browser', '5101')
            """
        )
        connection.commit()
        connection.close()

        competing_write_committed = False
        real_unlink = Path.unlink
        old_resolved = old_state.resolve()

        def race_reference_then_unlink(path: Path, *args, **kwargs):
            nonlocal competing_write_committed
            if path.resolve() == old_resolved:
                competitor = sqlite3.connect(self.database, timeout=0.01)
                try:
                    competitor.execute(
                        """
                        INSERT INTO user_info
                            (type, filePath, userName, status, profileName,
                             authMode, accountReference)
                        VALUES (8, 'old-race.json', 'Instagram', 1,
                                'Meta 主体', 'browser', NULL)
                        """
                    )
                    competitor.commit()
                    competing_write_committed = True
                except sqlite3.OperationalError:
                    competitor.rollback()
                finally:
                    competitor.close()
            return real_unlink(path, *args, **kwargs)

        with (
            patch.object(recovered_login, "BASE_DIR", self.root),
            patch.object(
                Path,
                "unlink",
                autospec=True,
                side_effect=race_reference_then_unlink,
            ),
        ):
            recovered_login._remove_replaced_facebook_page_session(
                {"filePath": "old-race.json"},
                current_cookie="new-race.json",
            )

        connection = sqlite3.connect(self.database)
        remaining_references = connection.execute(
            "SELECT COUNT(*) FROM user_info WHERE filePath = 'old-race.json'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(
            (competing_write_committed, old_state.exists(), remaining_references),
            (False, False, 0),
        )

    def test_feature_flag_off_rejects_direct_page_refresh_before_browser_work(self) -> None:
        connection = sqlite3.connect(self.database)
        account_id = connection.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, authMode,
                 accountReference)
            VALUES (9, 'page.json', 'Page', 1, 'Meta 主体', 'browser', '3001')
            """
        ).lastrowid
        connection.commit()
        connection.close()

        with (
            patch.object(account_service, "connect", self._connect),
            patch.dict(os.environ, {}, clear=True),
            patch.object(account_service, "run_async_capture_account_avatar") as capture,
            self.assertRaises(FacebookPagePublishError) as raised,
        ):
            account_service.refresh_account_avatar(int(account_id))

        self.assertEqual(raised.exception.error_code, "facebook_page_feature_disabled")
        capture.assert_not_called()

    def _insert_refresh_account(
        self,
        *,
        page_id: str | None = "4001",
        user_name: str = "旧 Page 名",
        avatar_path: str = "old-avatar.png",
    ) -> int:
        connection = sqlite3.connect(self.database)
        account_id = connection.execute(
            """
            INSERT INTO user_info
                (type, filePath, userName, status, profileName, avatarPath,
                 authMode, accountReference)
            VALUES (9, 'page.json', ?, 1, 'Meta 主体', ?, 'browser', ?)
            """,
            (user_name, avatar_path, page_id),
        ).lastrowid
        connection.commit()
        connection.close()
        return int(account_id)

    def _saved_refresh_identity(self, account_id: int) -> tuple[str, str]:
        connection = sqlite3.connect(self.database)
        row = connection.execute(
            "SELECT userName, avatarPath FROM user_info WHERE id = ?",
            (int(account_id),),
        ).fetchone()
        connection.close()
        return row

    def test_enabled_refresh_writes_only_the_exact_bound_page_name_and_avatar(self) -> None:
        account_id = self._insert_refresh_account()
        exact_identity = FacebookPageIdentity(
            "4001",
            "精确 Page 名",
            avatar_url="https://scontent.example.fbcdn.net/page.png",
            can_manage_content=True,
        )

        with (
            patch.object(account_service, "connect", self._connect),
            patch.dict(
                os.environ,
                {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                clear=True,
            ),
            patch.object(
                account_service,
                "_read_exact_facebook_page_identity",
                return_value=exact_identity,
                create=True,
            ),
            patch.object(
                account_service,
                "_download_facebook_page_avatar",
                return_value=f"oneclick_facebook_page_{account_id}.png",
                create=True,
            ) as download_avatar,
            patch.object(account_service, "run_async_capture_account_avatar") as generic,
        ):
            refreshed = account_service.refresh_account_avatar(account_id)

        self.assertEqual(
            self._saved_refresh_identity(account_id),
            ("精确 Page 名", f"oneclick_facebook_page_{account_id}.png"),
        )
        self.assertEqual(refreshed["accountReference"], "4001")
        download_avatar.assert_called_once_with(
            exact_identity,
            account_id=account_id,
        )
        generic.assert_not_called()

    def test_page_avatar_download_saves_only_a_bounded_trusted_image(self) -> None:
        image = Image.new("RGB", (2, 2), color=(12, 34, 56))
        encoded = BytesIO()
        image.save(encoded, format="PNG")
        payload = encoded.getvalue()

        class Response:
            status_code = 200
            headers = {
                "Content-Type": "image/png",
                "Content-Length": str(len(payload)),
            }

            @staticmethod
            def iter_content(*, chunk_size: int):
                self_chunk_size = chunk_size
                if self_chunk_size <= 0:
                    raise AssertionError("chunk size must be positive")
                return iter((payload,))

            @staticmethod
            def close() -> None:
                return None

        identity = FacebookPageIdentity(
            "4002",
            "Page",
            avatar_url="https://scontent.example.fbcdn.net/page.png",
            can_manage_content=True,
        )

        with (
            patch.object(account_service, "AVATAR_DIR", self.root / "avatars"),
            patch("requests.get", return_value=Response()) as get_avatar,
        ):
            file_name = account_service._download_facebook_page_avatar(
                identity,
                account_id=7,
            )

        saved = self.root / "avatars" / file_name
        self.assertTrue(saved.is_file())
        with Image.open(saved) as decoded:
            self.assertEqual((decoded.format, decoded.size), ("PNG", (2, 2)))
        get_avatar.assert_called_once_with(
            identity.avatar_url,
            timeout=15,
            stream=True,
            allow_redirects=False,
        )

    def test_page_refresh_mismatch_or_missing_binding_keeps_display_identity_unchanged(self) -> None:
        mismatched_id = self._insert_refresh_account(page_id="4101")
        unbound_id = self._insert_refresh_account(page_id=None)
        original = ("旧 Page 名", "old-avatar.png")
        wrong_identity = FacebookPageIdentity(
            "9999",
            "错误 Page 名",
            avatar_url="https://scontent.example.fbcdn.net/wrong.png",
            can_manage_content=True,
        )

        with (
            patch.object(account_service, "connect", self._connect),
            patch.dict(
                os.environ,
                {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                clear=True,
            ),
            patch.object(
                account_service,
                "_read_exact_facebook_page_identity",
                return_value=wrong_identity,
                create=True,
            ) as read_identity,
            patch.object(
                account_service,
                "_download_facebook_page_avatar",
                create=True,
            ) as download_avatar,
            patch.object(
                account_service,
                "run_async_capture_account_avatar",
                return_value=(None, None),
            ),
        ):
            with self.assertRaises(FacebookPagePublishError) as mismatch:
                account_service.refresh_account_avatar(mismatched_id)
            with self.assertRaises(FacebookPagePublishError) as unbound:
                account_service.refresh_account_avatar(unbound_id)

        self.assertEqual(mismatch.exception.error_code, "facebook_page_identity_mismatch")
        self.assertEqual(unbound.exception.error_code, "facebook_page_identity_mismatch")
        self.assertEqual(self._saved_refresh_identity(mismatched_id), original)
        self.assertEqual(self._saved_refresh_identity(unbound_id), original)
        self.assertEqual(read_identity.call_count, 1)
        download_avatar.assert_not_called()

    def test_page_refresh_identity_reader_passes_the_exact_stored_page_id(self) -> None:
        reader = getattr(account_service, "_read_exact_facebook_page_identity", None)
        self.assertIsNotNone(reader)
        account = {
            "id": 1,
            "type": 9,
            "status": 1,
            "authMode": "browser",
            "filePath": "page.json",
            "accountReference": "4201",
        }
        exact_identity = FacebookPageIdentity(
            "4201",
            "Page",
            avatar_url="https://scontent.example.fbcdn.net/page.png",
            can_manage_content=True,
        )

        with patch(
            "myUtils.auth.check_cookie",
            new=AsyncMock(return_value=exact_identity),
        ) as check:
            refreshed_identity = reader(account)

        self.assertEqual(refreshed_identity, exact_identity)
        check.assert_awaited_once_with(
            9,
            "page.json",
            account_reference="4201",
        )

    def test_backend_identity_failure_is_returned_and_marks_the_saved_row_abnormal(self) -> None:
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
        account = {
            "id": int(account_id),
            "type": 9,
            "status": 1,
            "authMode": "browser",
            "profileName": "Meta 主体",
            "filePath": "page.json",
            "accountReference": "1001",
        }

        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "page.json").write_text("{}", encoding="utf-8")
            for error_code in (
                "facebook_page_identity_mismatch",
                "facebook_page_content_permission_missing",
            ):
                with self.subTest(error_code=error_code):
                    connection = sqlite3.connect(self.database)
                    connection.execute(
                        "UPDATE user_info SET status = 1 WHERE id = ?",
                        (int(account_id),),
                    )
                    connection.commit()
                    connection.close()
                    failure = FacebookPagePublishError(error_code, "Page check failed")
                    with (
                        patch.dict(
                            os.environ,
                            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                            clear=True,
                        ),
                        patch.object(account_service, "connect", self._connect),
                        patch.object(
                            account_browser_service,
                            "COOKIE_DIR",
                            Path(raw),
                        ),
                        patch.object(
                            account_browser_service,
                            "_open_backend",
                            new=AsyncMock(side_effect=failure),
                        ),
                        self.assertRaises(FacebookPagePublishError) as raised,
                    ):
                        account_browser_service.open_account_backend(account)

                    self.assertEqual(raised.exception.error_code, error_code)
                    connection = sqlite3.connect(self.database)
                    status = connection.execute(
                        "SELECT status FROM user_info WHERE id = ?",
                        (int(account_id),),
                    ).fetchone()[0]
                    connection.close()
                    self.assertEqual(status, 0)

    def test_backend_identity_failure_is_returned_when_status_write_also_fails(self) -> None:
        account = {
            "id": 1,
            "type": 9,
            "status": 1,
            "authMode": "browser",
            "profileName": "Meta 主体",
            "filePath": "page.json",
            "accountReference": "1001",
        }
        failure = FacebookPagePublishError(
            "facebook_page_identity_mismatch",
            "Page mismatch",
        )
        startup_results: queue.Queue[object] = queue.Queue()

        with (
            patch.object(
                account_browser_service,
                "_open_backend",
                new=AsyncMock(side_effect=failure),
            ),
            patch.object(
                account_service,
                "update_status",
                side_effect=sqlite3.OperationalError("database unavailable"),
            ),
        ):
            account_browser_service._thread_target(
                account,
                1,
                startup_results,
            )

        returned = startup_results.get_nowait()
        self.assertIs(returned, failure)

    def test_backend_startup_without_a_worker_result_has_a_bounded_stable_failure(self) -> None:
        account = {
            "id": 51,
            "type": 9,
            "status": 1,
            "authMode": "browser",
            "profileName": "Meta 主体",
            "filePath": "page.json",
            "accountReference": "1001",
        }
        worker = MagicMock()
        worker.is_alive.return_value = True
        startup_results: queue.Queue[object] = queue.Queue()
        failures: list[Exception] = []
        real_thread_factory = threading.Thread
        startup_signal = None

        def invoke() -> None:
            try:
                account_browser_service.open_account_backend(account)
            except Exception as exc:
                failures.append(exc)

        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "page.json").write_text("{}", encoding="utf-8")
            try:
                with (
                    patch.dict(
                        os.environ,
                        {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                        clear=True,
                    ),
                    patch.object(account_browser_service, "COOKIE_DIR", Path(raw)),
                    patch.object(
                        account_browser_service,
                        "FACEBOOK_PAGE_BACKEND_STARTUP_TIMEOUT_SECONDS",
                        0.01,
                        create=True,
                    ),
                    patch.object(
                        account_browser_service.queue,
                        "Queue",
                        return_value=startup_results,
                    ),
                    patch.object(
                        account_browser_service.threading,
                        "Thread",
                        return_value=worker,
                    ),
                ):
                    caller = real_thread_factory(target=invoke)
                    caller.start()
                    caller.join(timeout=0.05)
                    if caller.is_alive():
                        startup_results.put(RuntimeError("test-only unblock"))
                        caller.join(timeout=0.5)
                    with account_browser_service._session_lock:
                        startup_signal = account_browser_service._backend_startups.get(51)
            finally:
                with account_browser_service._session_lock:
                    account_browser_service._backend_threads.pop(51, None)
                    account_browser_service._backend_startups.pop(51, None)

        self.assertFalse(caller.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], FacebookPagePublishError)
        self.assertEqual(failures[0].error_code, "facebook_page_identity_mismatch")
        self.assertIn("启动超时", str(failures[0]))
        self.assertIsNotNone(startup_signal)
        self.assertFalse(startup_signal.report(True))
        worker.start.assert_called_once_with()


class FacebookPageSavedSessionTests(unittest.IsolatedAsyncioTestCase):
    def test_malformed_saved_page_status_maps_to_the_stable_identity_error(self) -> None:
        base_account = {
            "id": 5,
            "type": 9,
            "authMode": "browser",
            "profileName": "Meta 主体",
            "filePath": "page.json",
            "accountReference": "1001",
        }

        for status in ("not-a-status", object()):
            with self.subTest(status=repr(status)):
                account = {**base_account, "status": status}
                with self.assertRaises(FacebookPagePublishError) as raised:
                    account_service.validate_saved_facebook_page_account(account)
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_page_identity_mismatch",
                )

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

    async def test_late_page_worker_stops_before_navigation_after_new_page_unblocks(self) -> None:
        account = {
            "id": 5,
            "type": 9,
            "status": 1,
            "authMode": "browser",
            "profileName": "Meta 主体",
            "filePath": "page.json",
            "accountReference": "1001",
        }
        new_page_entered = asyncio.Event()
        release_new_page = asyncio.Event()
        page = MagicMock()
        page.goto = AsyncMock()
        page.bring_to_front = AsyncMock()
        page.wait_for_event = AsyncMock()

        async def blocked_new_page():
            new_page_entered.set()
            await release_new_page.wait()
            return page

        context = MagicMock()
        context.new_page = AsyncMock(side_effect=blocked_new_page)
        context.close = AsyncMock()
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        playwright.stop = AsyncMock()
        startup = account_browser_service._BackendStartupSignal()

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
                worker = asyncio.create_task(
                    account_browser_service._open_backend(account, startup)
                )
                await asyncio.wait_for(new_page_entered.wait(), timeout=0.2)
                timed_out = await asyncio.to_thread(startup.wait, 0.001)
                release_new_page.set()
                await asyncio.wait_for(worker, timeout=0.2)

        self.assertIsInstance(timed_out, FacebookPagePublishError)
        self.assertEqual(timed_out.error_code, "facebook_page_identity_mismatch")
        page.goto.assert_not_awaited()
        activate.assert_not_awaited()
        page.bring_to_front.assert_not_awaited()
        page.wait_for_event.assert_not_awaited()
        context.close.assert_awaited_once_with()
        browser.close.assert_awaited_once_with()
        playwright.stop.assert_awaited_once_with()

    async def test_open_backend_attempts_all_cleanup_when_context_close_fails(self) -> None:
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
        context.close = AsyncMock(side_effect=RuntimeError("context close failed"))
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
                ),
                self.assertRaisesRegex(RuntimeError, "context close failed"),
            ):
                await account_browser_service._open_backend(account)

        context.close.assert_awaited_once_with()
        browser.close.assert_awaited_once_with()
        playwright.stop.assert_awaited_once_with()

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
        with (
            patch.dict(
                os.environ,
                {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                clear=True,
            ),
            patch.object(account_browser_service.threading.Thread, "start") as start,
        ):
            with self.assertRaises(FacebookPagePublishError) as raised:
                account_browser_service.open_account_backend(account)

        self.assertEqual(raised.exception.error_code, "facebook_page_identity_mismatch")
        start.assert_not_called()

    def test_feature_flag_off_rejects_saved_page_backend_before_thread_start(self) -> None:
        account = {
            "id": 5,
            "type": 9,
            "status": 1,
            "authMode": "browser",
            "profileName": "Meta 主体",
            "filePath": "page.json",
            "accountReference": "1001",
        }

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(account_browser_service.threading.Thread, "start") as start,
            self.assertRaisesRegex(RuntimeError, "功能未开启"),
        ):
            account_browser_service.open_account_backend(account)

        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
