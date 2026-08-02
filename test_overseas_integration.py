# -*- coding: utf-8 -*-
"""海外平台接线的离线回归：不启动浏览器，不访问平台。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app_core import account_service, login_service, overseas_preflight, publish_service
from myUtils import login as recovered_login


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
                result = overseas_preflight.run_overseas_preflight_sync(
                    self._payload(video)
                )

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1]["dry_run"])
        self.assertFalse(calls[0][1]["dry_run_hold_browser"])
        self.assertIsNone(calls[0][1]["schedule_time"])

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

    def test_formal_publish_remains_closed_for_unverified_browser_channels(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            with self.assertRaisesRegex(ValueError, "只开放小红书和公众号"):
                publish_service._validate_payloads(
                    [
                        {
                            **self._payload(video),
                            "runtimeMode": "publish",
                            "debugDryRun": False,
                        }
                    ]
                )


if __name__ == "__main__":
    unittest.main()
