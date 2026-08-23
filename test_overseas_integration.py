# -*- coding: utf-8 -*-
"""海外平台接线的离线回归：不启动浏览器，不访问平台。"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app_core import account_service, login_service, overseas_preflight, publish_service
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
