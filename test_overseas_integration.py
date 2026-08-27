# -*- coding: utf-8 -*-
"""海外平台接线的离线回归：不启动浏览器，不访问平台。"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import conf
from app_core import (
    account_service,
    database as app_database,
    login_service,
    overseas_preflight,
    overseas_youtube_credentials,
    overseas_youtube_login,
    overseas_youtube_profile,
    publish_service,
)
from app_core.overseas_youtube_api import YouTubeChannelIdentity
from myUtils import login as recovered_login
from myUtils import postVideo as recovered_publish
from uploader.youtube_uploader.main import YouTubeVideo


class OverseasAccountEntryTests(unittest.TestCase):
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
            "accountList": ["tiktok.json"],
            "enableTimer": False,
        }

    def test_validation_requires_dry_run_video_and_local_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            (root / "tiktok.json").write_text("{}", encoding="utf-8")
            with patch.object(overseas_preflight, "COOKIE_DIR", root):
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

    def test_recovered_handler_receives_forced_dry_run(self) -> None:
        calls = []

        def handler(*args, **kwargs):
            calls.append((args, kwargs))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            (root / "tiktok.json").write_text("{}", encoding="utf-8")
            with (
                patch.object(overseas_preflight, "COOKIE_DIR", root),
                patch.dict(overseas_preflight.PREFLIGHT_HANDLERS, {6: handler}),
            ):
                payload = self._payload(video)
                payload.update(
                    {
                        "visibility": "private",
                        "collectionName": "测试合集",
                        "aiGenerated": True,
                        "madeForKids": True,
                        "notifySubscribers": False,
                        "shareToFeed": False,
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
        self.assertTrue(calls[0][1]["ai_generated"])
        self.assertTrue(calls[0][1]["made_for_kids"])
        self.assertFalse(calls[0][1]["notify_subscribers"])
        self.assertFalse(calls[0][1]["share_to_feed"])

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

    def test_schedule_is_rejected_until_platform_time_is_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            (root / "tiktok.json").write_text("{}", encoding="utf-8")
            payload = self._payload(video)
            payload["enableTimer"] = True
            payload["scheduleTime"] = "2026-08-08 18:00"
            with patch.object(overseas_preflight, "COOKIE_DIR", root):
                result = overseas_preflight.validate_overseas_preflight_payload(payload)
        self.assertFalse(result["ok"])
        self.assertTrue(any("定时时间" in item for item in result["errors"]))

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
