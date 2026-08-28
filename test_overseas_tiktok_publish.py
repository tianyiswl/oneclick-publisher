# -*- coding: utf-8 -*-
"""TikTok 单视频本地合同测试；不启动浏览器或访问平台。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app_core import account_service, overseas_tiktok_publish, task_service
from app_core.overseas_tiktok_identity import (
    TikTokIdentity,
    validate_identity_binding,
)


_DEFAULT_ACCOUNT = object()


class TikTokPublishContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"offline-tiktok-video")
        self.session = self.root / "tiktok.json"
        self.session.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "sessionid",
                            "value": "must-not-leak",
                            "domain": ".tiktok.com",
                            "path": "/",
                        }
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def account(self, **changes) -> dict:
        result = {
            "id": 61,
            "type": 6,
            "status": 1,
            "authMode": "browser",
            "filePath": self.session.name,
            "accountReference": "Expected.User",
        }
        result.update(changes)
        return result

    def payload(self, **changes) -> dict:
        result = {
            "type": 6,
            "contentType": "video",
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "title": "TikTok 标题",
            "description": "本地预检正文。",
            "tags": ["OneClick", "AI工具"],
            "fileList": [str(self.video)],
            "accountIds": [61],
            "accountList": [self.session.name],
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
        result.update(changes)
        return result

    def validate(
        self,
        payload: dict | None = None,
        *,
        mode: str = "preflight",
        account=_DEFAULT_ACCOUNT,
    ):
        row = self.account() if account is _DEFAULT_ACCOUNT else account
        with (
            patch.object(overseas_tiktok_publish, "COOKIE_DIR", self.root),
            patch.object(
                overseas_tiktok_publish,
                "_read_account_record",
                return_value=row,
                create=True,
            ),
        ):
            return overseas_tiktok_publish.validate_tiktok_payload(
                payload or self.payload(),
                mode=mode,
            )

    def assert_error_code(
        self,
        error_code: str,
        payload: dict,
        *,
        account=_DEFAULT_ACCOUNT,
        mode: str = "preflight",
    ) -> None:
        with self.assertRaises(overseas_tiktok_publish.TikTokPublishError) as raised:
            self.validate(payload, mode=mode, account=account)
        self.assertEqual(raised.exception.error_code, error_code)

    def test_accepts_real_controlled_preflight_payload_and_prepares_one_subject(self) -> None:
        prepared = self.validate()

        self.assertEqual(prepared["accountId"], 61)
        self.assertEqual(prepared["expectedAccountReference"], "expected.user")
        self.assertEqual(prepared["accountFile"], str(self.session.resolve()))
        self.assertEqual(prepared["videoPath"], str(self.video.resolve()))
        self.assertEqual(prepared["topics"], ["OneClick", "AI工具"])
        self.assertEqual(prepared["plainCaption"], "TikTok 标题\n\n本地预检正文。")

    def test_modes_are_exact_and_runtime_mode_must_match(self) -> None:
        cases = (
            ("preflight", "preflight"),
            ("platform_form_check", "platform_form_check"),
            ("formal", "publish"),
        )
        for mode, runtime_mode in cases:
            with self.subTest(mode=mode):
                prepared = self.validate(
                    self.payload(runtimeMode=runtime_mode, debugDryRun=mode == "preflight"),
                    mode=mode,
                )
                self.assertEqual(prepared["mode"], mode)

        for mode in ("publish", "dry_run", "", "PREflight"):
            with self.subTest(rejected_mode=mode):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    self.payload(),
                    mode=mode,
                )

        self.assert_error_code(
            "tiktok_unsupported_publish_setting",
            self.payload(runtimeMode="publish"),
        )

    def test_flat_and_nested_unsupported_settings_cannot_bypass_contract(self) -> None:
        cases = (
            {"enableTimer": True},
            {"scheduleTime": "2026-08-29 10:00"},
            {"schedule": {"localTime": "2026-08-29 10:00"}},
            {"visibility": "private"},
            {"settings": {"visibility": "private"}},
            {"aiGenerated": True},
            {"aiDisclosure": {"containsAiGeneratedContent": True}},
            {"settings": {"aiGenerated": True}},
            {"coverPath": str(self.root / "cover.png")},
            {"coverPaths": {"3:4": str(self.root / "cover.png")}},
            {"settings": {"coverPath": str(self.root / "cover.png")}},
            {"collectionName": "测试合集"},
            {"settings": {"collection": "测试合集"}},
            {"mentions": ["someone"]},
            {"content": {"mentions": ["someone"]}},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    self.payload(**changes),
                )

    def test_immediate_schedule_defaults_are_required_and_type_strict(self) -> None:
        without_enable_timer = self.payload()
        without_enable_timer.pop("enableTimer")
        without_daily_times = self.payload()
        without_daily_times.pop("dailyTimes")
        cases = (
            without_enable_timer,
            without_daily_times,
            self.payload(enableTimer=1),
            self.payload(scheduleTime="2026-08-29 10:00"),
            self.payload(dailyTimes=["10:00"]),
            self.payload(dailyTimes=()),
            self.payload(videosPerDay=True),
            self.payload(videosPerDay=2),
            self.payload(startDays=False),
            self.payload(startDays=1),
            self.payload(timeJitterMinutes=False),
            self.payload(timeJitterMinutes=5),
            self.payload(schedule={"enabled": 0}),
            self.payload(schedule={"enabled": True}),
            self.payload(
                schedule={"enabled": False, "localTime": "2026-08-29 10:00"}
            ),
            self.payload(publishAt=None),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    payload,
                )

        prepared = self.validate(
            self.payload(
                schedule={"enabled": False},
                scheduleTimezone="UTC",
            )
        )
        self.assertEqual(prepared["visibility"], "public")

    def test_schedule_fields_are_root_only_and_root_schedule_has_exact_keys(self) -> None:
        nested_cases = (
            {"settings": {"enableTimer": False}},
            {"content": {"dailyTimes": []}},
            {"target": {"platform": "TikTok", "videosPerDay": 1}},
            {
                "targets": [
                    {
                        "platform": "TikTok",
                        "accountId": 61,
                        "startDays": 0,
                    }
                ]
            },
            {
                "platformOverrides": {
                    "TikTok": {"timeJitterMinutes": 0},
                }
            },
            {"content": {"schedule": None}},
            {"target": {"platform": "TikTok", "scheduledAt": None}},
        )
        root_schedule_cases = (
            {"schedule": None},
            {"schedule": {}},
            {"schedule": {"enabled": 0}},
            {"schedule": {"timezone": "UTC"}},
            {"schedule": {"enabled": False, "at": None}},
            {"schedule": {"enabled": False, "publishAt": None}},
            {"schedule": {"enabled": False, "time": ""}},
            {"schedule": {"enabled": False, "localTime": None}},
        )
        for changes in (*nested_cases, *root_schedule_cases):
            with self.subTest(changes=changes):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    self.payload(**changes),
                )

        prepared = self.validate(
            self.payload(schedule={"enabled": False, "timezone": "UTC"})
        )
        self.assertEqual(prepared["visibility"], "public")

    def test_schedule_timezone_aliases_are_rejected_outside_root_schedule(self) -> None:
        cases = (
            {"settings": {"scheduleTimezone": "UTC"}},
            {"settings": {"timezone": "UTC"}},
            {"content": {"scheduleTimezone": "UTC"}},
            {"content": {"timezone": "UTC"}},
            {
                "target": {
                    "platform": "TikTok",
                    "accountId": 61,
                    "scheduleTimezone": "UTC",
                }
            },
            {
                "target": {
                    "platform": "TikTok",
                    "accountId": 61,
                    "timezone": "UTC",
                }
            },
            {
                "targets": [
                    {
                        "platform": "TikTok",
                        "accountId": 61,
                        "scheduleTimezone": "UTC",
                    }
                ]
            },
            {
                "targets": [
                    {
                        "platform": "TikTok",
                        "accountId": 61,
                        "timezone": "UTC",
                    }
                ]
            },
            {
                "platformOverrides": {
                    "TikTok": {"scheduleTimezone": "UTC"},
                }
            },
            {
                "platformOverrides": {
                    "TikTok": {"timezone": "UTC"},
                }
            },
            {"timezone": "UTC"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    self.payload(**changes),
                )

        prepared = self.validate(
            self.payload(schedule={"enabled": False, "timezone": "UTC"})
        )
        self.assertEqual(prepared["visibility"], "public")

    def test_account_ids_and_session_lists_must_each_resolve_to_same_single_account(self) -> None:
        second = self.root / "second.json"
        second.write_text("{}", encoding="utf-8")
        for changes in (
            {"accountIds": [61, 62]},
            {"accountList": [self.session.name, second.name]},
            {"accountId": 62},
            {"accountFile": second.name},
            {"accountIds": []},
            {"accountList": []},
        ):
            with self.subTest(changes=changes):
                self.assert_error_code(
                    "tiktok_account_invalid",
                    self.payload(**changes),
                )

    def test_nested_controlled_targets_cannot_hide_extra_or_different_account(self) -> None:
        second = self.root / "second.json"
        second.write_text("{}", encoding="utf-8")
        for targets in (
            [
                {"platform": "TikTok", "accountId": 61},
                {"platform": "TikTok", "accountId": 62},
            ],
            [{"platform": "TikTok", "accountId": 62}],
            [
                {
                    "platform": "TikTok",
                    "accountId": 61,
                    "accountFile": second.name,
                }
            ],
            [
                {
                    "platform": "TikTok",
                    "accountId": 61,
                    "status": 0,
                }
            ],
        ):
            with self.subTest(targets=targets):
                self.assert_error_code(
                    "tiktok_account_invalid",
                    self.payload(targets=targets),
                )

    def test_account_must_be_normal_browser_tiktok_with_stable_reference(self) -> None:
        for changes in (
            {"status": 0},
            {"type": 7},
            {"authMode": "youtube_oauth"},
            {"accountReference": ""},
            {"accountReference": "https://example.com/@wrong"},
        ):
            with self.subTest(changes=changes):
                self.assert_error_code(
                    "tiktok_account_invalid",
                    self.payload(),
                    account=self.account(**changes),
                )

        self.assert_error_code(
            "tiktok_account_invalid",
            self.payload(),
            account=None,
        )

    def test_account_lookup_reads_database_without_running_account_migrations(self) -> None:
        database = self.root / "database.db"
        connection = sqlite3.connect(database)
        connection.execute(
            """
            CREATE TABLE user_info (
                id INTEGER PRIMARY KEY,
                type INTEGER NOT NULL,
                status INTEGER NOT NULL,
                authMode TEXT,
                filePath TEXT NOT NULL,
                accountReference TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO user_info
                (id, type, status, authMode, filePath, accountReference)
            VALUES (61, 6, 1, 'browser', 'tiktok.json', 'expected.user')
            """
        )
        connection.commit()
        connection.close()
        original_database = database.read_bytes()

        with (
            patch.object(overseas_tiktok_publish, "COOKIE_DIR", self.root),
            patch.object(overseas_tiktok_publish, "DB_PATH", database, create=True),
            patch.object(
                account_service,
                "get_managed_account",
                side_effect=AssertionError("preflight must not run account migrations"),
            ),
        ):
            prepared = overseas_tiktok_publish.validate_tiktok_payload(
                self.payload(),
                mode="preflight",
            )

        self.assertEqual(prepared["accountId"], 61)
        self.assertEqual(database.read_bytes(), original_database)

    def test_account_session_rejects_missing_malformed_and_unsafe_paths(self) -> None:
        missing = self.account(filePath="missing.json")
        self.assert_error_code(
            "tiktok_session_missing",
            self.payload(accountList=["missing.json"]),
            account=missing,
        )

        malformed = self.root / "malformed.json"
        malformed.write_text("not-json", encoding="utf-8")
        self.assert_error_code(
            "tiktok_account_invalid",
            self.payload(accountList=[malformed.name]),
            account=self.account(filePath=malformed.name),
        )

        for unsafe in ("../tiktok.json", str(self.session.resolve())):
            with self.subTest(unsafe=unsafe):
                self.assert_error_code(
                    "tiktok_account_invalid",
                    self.payload(accountList=[unsafe]),
                    account=self.account(filePath=unsafe),
                )

        outside = self.root.parent / f"{self.root.name}-outside.json"
        outside.write_text("{}", encoding="utf-8")
        link = self.root / "linked.json"
        try:
            link.symlink_to(outside)
            self.assert_error_code(
                "tiktok_account_invalid",
                self.payload(accountList=[link.name]),
                account=self.account(filePath=link.name),
            )
        finally:
            outside.unlink(missing_ok=True)

    def test_session_requires_basename_and_structured_storage_state(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        nested_session = nested / "session.json"
        nested_session.write_text(
            '{"cookies": [], "origins": []}',
            encoding="utf-8",
        )
        cases = (
            ("nested/session.json", '{"cookies": [], "origins": []}'),
            ("empty.json", "{}"),
            ("cookies-object.json", '{"cookies": {}, "origins": []}'),
            ("origins-object.json", '{"cookies": [], "origins": {}}'),
            ("cookie-item.json", '{"cookies": ["secret"], "origins": []}'),
            ("origin-item.json", '{"cookies": [], "origins": ["secret"]}'),
        )
        for name, content in cases:
            path = self.root / name
            if "/" not in name:
                path.write_text(content, encoding="utf-8")
            with self.subTest(name=name):
                self.assert_error_code(
                    "tiktok_account_invalid",
                    self.payload(accountList=[name]),
                    account=self.account(filePath=name),
                )

    def test_storage_state_validates_cookie_origin_and_local_storage_fields(self) -> None:
        valid_cookie = {
            "name": "sessionid",
            "value": "opaque",
            "domain": ".tiktok.com",
            "path": "/",
        }
        cases = (
            {"cookies": [{}], "origins": []},
            {
                "cookies": [{**valid_cookie, "value": 7}],
                "origins": [],
            },
            {
                "cookies": [{**valid_cookie, "expires": True}],
                "origins": [],
            },
            {
                "cookies": [{**valid_cookie, "expires": float("nan")}],
                "origins": [],
            },
            {
                "cookies": [{**valid_cookie, "httpOnly": 1}],
                "origins": [],
            },
            {
                "cookies": [{**valid_cookie, "secure": "false"}],
                "origins": [],
            },
            {
                "cookies": [{**valid_cookie, "sameSite": "unsafe"}],
                "origins": [],
            },
            {
                "cookies": [{**valid_cookie, "sameSite": ["Lax"]}],
                "origins": [],
            },
            {
                "cookies": [{**valid_cookie, "partitionKey": ["secret"]}],
                "origins": [],
            },
            {"cookies": [], "origins": [{}]},
            {
                "cookies": [],
                "origins": [{"origin": "file:///tmp/session", "localStorage": []}],
            },
            {
                "cookies": [],
                "origins": [
                    {"origin": "https://www.tiktok.com:bad", "localStorage": []}
                ],
            },
            {
                "cookies": [],
                "origins": [{"origin": "https://www.tiktok.com"}],
            },
            {
                "cookies": [],
                "origins": [
                    {"origin": "https://www.tiktok.com", "localStorage": {}}
                ],
            },
            {
                "cookies": [],
                "origins": [
                    {"origin": "https://www.tiktok.com", "localStorage": [{}]}
                ],
            },
            {
                "cookies": [],
                "origins": [
                    {
                        "origin": "https://www.tiktok.com",
                        "localStorage": [{"name": 1, "value": "opaque"}],
                    }
                ],
            },
        )
        for index, storage_state in enumerate(cases):
            name = f"malformed-storage-{index}.json"
            (self.root / name).write_text(
                json.dumps(storage_state),
                encoding="utf-8",
            )
            with self.subTest(index=index):
                self.assert_error_code(
                    "tiktok_account_invalid",
                    self.payload(accountList=[name]),
                    account=self.account(filePath=name),
                )

        valid_name = "valid-storage.json"
        (self.root / valid_name).write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            **valid_cookie,
                            "expires": 1790000000.5,
                            "httpOnly": True,
                            "secure": True,
                            "sameSite": "Lax",
                            "partitionKey": "https://www.tiktok.com",
                        }
                    ],
                    "origins": [
                        {
                            "origin": "https://www.tiktok.com",
                            "localStorage": [
                                {"name": "theme", "value": "dark"},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        prepared = self.validate(
            self.payload(accountList=[valid_name]),
            account=self.account(filePath=valid_name),
        )
        self.assertEqual(prepared["accountFile"], str((self.root / valid_name).resolve()))

    def test_video_must_be_one_supported_local_regular_file(self) -> None:
        second = self.root / "second.mov"
        second.write_bytes(b"second")
        missing = self.root / "missing.mp4"
        unsupported = self.root / "video.txt"
        unsupported.write_bytes(b"not-video")
        folder = self.root / "folder.mp4"
        folder.mkdir()
        for files in (
            [],
            [str(self.video), str(second)],
            [str(missing)],
            [str(unsupported)],
            [str(folder)],
        ):
            with self.subTest(files=files):
                self.assert_error_code(
                    "tiktok_video_file_invalid",
                    self.payload(fileList=files),
                )

        video_link = self.root / "linked.mp4"
        video_link.symlink_to(self.video)
        self.assert_error_code(
            "tiktok_video_file_invalid",
            self.payload(fileList=[str(video_link)]),
        )

    def test_body_raw_mention_and_conflicting_content_aliases_are_rejected(self) -> None:
        for changes in (
            {"description": "正文包含 @someone 提及"},
            {"body": "不同的正文"},
            {"topics": ["不同话题"]},
            {"assets": [str(self.video), str(self.root / "other.mp4")]},
        ):
            with self.subTest(changes=changes):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    self.payload(**changes),
                )

    def test_raw_mentions_are_rejected_in_title_and_body_at_all_content_locations(self) -> None:
        cases = (
            {"title": "@someone title"},
            {"description": "@someone body"},
            {"title": "@someone title", "content": {"title": "@someone title"}},
            {
                "description": "@someone body",
                "content": {"body": "@someone body"},
            },
            {
                "title": "@someone title",
                "platformOverrides": {"TikTok": {"title": "@someone title"}},
            },
            {
                "description": "@someone body",
                "platformOverrides": {"TikTok": {"body": "@someone body"}},
            },
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    self.payload(**changes),
                )

    def test_tiktok_platform_override_cannot_hide_mention_or_conflicting_topics(self) -> None:
        for override in (
            {"body": "平台正文包含 @someone"},
            {"tags": ["另一个话题"]},
        ):
            with self.subTest(override=override):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    self.payload(platformOverrides={"TikTok": override}),
                )

    def test_sensitive_aliases_in_unapproved_nested_paths_are_rejected(self) -> None:
        second = self.root / "second.mp4"
        second.write_bytes(b"second")
        cases = (
            {"settings": {"accountId": 62}},
            {"content": {"accountFile": "other.json"}},
            {
                "targets": [
                    {
                        "platform": "TikTok",
                        "accountId": 61,
                        "videoPath": str(second),
                    }
                ]
            },
            {
                "targets": [
                    {
                        "platform": "TikTok",
                        "accountId": 61,
                        "body": "hidden body",
                    }
                ]
            },
            {
                "targets": [
                    {
                        "platform": "TikTok",
                        "accountId": 61,
                        "tags": ["hidden-topic"],
                    }
                ]
            },
            {
                "platformOverrides": {
                    "TikTok": {"assets": [str(second)]},
                }
            },
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    self.payload(**changes),
                )

    def test_allowed_content_video_path_aliases_must_match_the_primary_video(self) -> None:
        second = self.root / "second.mp4"
        second.write_bytes(b"second")
        for changes in (
            {"content": {"videoPath": str(second)}},
            {
                "platformOverrides": {
                    "TikTok": {"videoPath": str(second)},
                }
            },
        ):
            with self.subTest(changes=changes):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    self.payload(**changes),
                )

        prepared = self.validate(
            self.payload(
                content={"videoPath": str(self.video)},
                platformOverrides={
                    "TikTok": {"videoPath": str(self.video)},
                },
            )
        )
        self.assertEqual(prepared["videoPath"], str(self.video.resolve()))

    def test_other_platform_override_fields_do_not_affect_flattened_tiktok_payload(self) -> None:
        prepared = self.validate(
            self.payload(
                platformOverrides={
                    "YouTube": {
                        "visibility": "private",
                        "assets": ["youtube-only.mov"],
                        "body": "YouTube-only body",
                    }
                }
            )
        )

        self.assertEqual(prepared["accountId"], 61)
        self.assertEqual(prepared["visibility"], "public")

    def test_normalized_duplicate_topics_are_rejected(self) -> None:
        for topics in (
            ["AI", "#ai"],
            ["ＡＩ工具", "ai工具"],
            ["个人 项目", "个人项目"],
        ):
            with self.subTest(topics=topics):
                self.assert_error_code(
                    "tiktok_unsupported_publish_setting",
                    self.payload(tags=topics),
                )

    def test_combined_content_over_2200_is_rejected_without_truncation_or_echo(self) -> None:
        marker = "SENSITIVE-BODY-DO-NOT-ECHO"
        payload = self.payload(description=marker + "文" * 2200, tags=[])
        with self.assertRaises(overseas_tiktok_publish.TikTokPublishError) as raised:
            self.validate(payload)

        self.assertEqual(raised.exception.error_code, "tiktok_content_too_long")
        self.assertNotIn(marker, str(raised.exception))
        self.assertNotIn(str(self.video), str(raised.exception))
        self.assertNotIn(str(self.session), str(raised.exception))

    def test_caption_composer_is_canonical_for_format_hash_and_2200_boundary(self) -> None:
        exact_caption = "T\n\n" + "b" * 2194 + " #x"
        self.assertEqual(len(exact_caption), 2200)
        self.assertEqual(
            overseas_tiktok_publish.compose_tiktok_caption(
                "Title",
                "Body",
                ["One", "Two"],
            ),
            "Title\n\nBody #One #Two",
        )

        prepared = self.validate(
            self.payload(title="T", description="b" * 2194, tags=["x"])
        )
        self.assertEqual(prepared["plainCaption"], "T\n\n" + "b" * 2194)
        self.assertEqual(
            prepared["textSha256"],
            hashlib.sha256(exact_caption.encode("utf-8")).hexdigest(),
        )

        self.assert_error_code(
            "tiktok_content_too_long",
            self.payload(title="T", description="b" * 2195, tags=["x"]),
        )

    def test_local_preflight_returns_hash_only_snapshot_and_does_not_touch_session(self) -> None:
        original_session = self.session.read_bytes()
        with (
            patch.object(overseas_tiktok_publish, "COOKIE_DIR", self.root),
            patch.object(
                overseas_tiktok_publish,
                "_read_account_record",
                return_value=self.account(),
            ),
            patch(
                "app_core.overseas_tiktok_identity.async_playwright"
            ) as playwright,
        ):
            result = overseas_tiktok_publish.run_tiktok_local_preflight(self.payload())

        playwright.assert_not_called()
        self.assertEqual(result["phase"], "local_preflight_passed")
        self.assertFalse(result["receipt"]["platformWriteOccurred"])
        self.assertFalse(result["receipt"]["finalActionTriggered"])
        self.assertEqual(
            set(result["snapshot"]),
            {
                "accountId",
                "videoSha256",
                "textSha256",
                "topics",
                "visibility",
                "mode",
            },
        )
        self.assertEqual(
            result["snapshot"]["videoSha256"],
            hashlib.sha256(self.video.read_bytes()).hexdigest(),
        )
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(self.session), serialized)
        self.assertNotIn(str(self.video), serialized)
        self.assertNotIn("本地预检正文", serialized)
        self.assertNotIn("must-not-leak", serialized)
        self.assertEqual(self.session.read_bytes(), original_session)

    def test_error_receipt_is_whitelisted_before_exposure(self) -> None:
        error = overseas_tiktok_publish.TikTokPublishError(
            "tiktok_account_invalid",
            "账号不可用",
            receipt={
                "accountId": 61,
                "visibility": "public",
                "accountFile": str(self.session),
                "body": "完整正文",
                "cookie": "must-not-leak",
            },
        )

        self.assertEqual(error.receipt, {"accountId": 61, "visibility": "public"})
        self.assertNotIn(str(self.session), repr(error.receipt))
        self.assertNotIn("完整正文", repr(error.receipt))
        self.assertNotIn("must-not-leak", repr(error.receipt))

    def test_video_read_error_is_fixed_and_never_echoes_path(self) -> None:
        original_open = Path.open

        def fail_video_open(path, *args, **kwargs):
            if path == self.video.resolve():
                raise OSError(f"cannot read {path} COOKIE=must-not-leak")
            return original_open(path, *args, **kwargs)

        with (
            patch.object(Path, "open", autospec=True, side_effect=fail_video_open),
            self.assertRaises(overseas_tiktok_publish.TikTokPublishError) as raised,
        ):
            self.validate()

        self.assertEqual(raised.exception.error_code, "tiktok_video_file_invalid")
        public_error = str(raised.exception)
        self.assertNotIn(str(self.video), public_error)
        self.assertNotIn("COOKIE", public_error)
        self.assertNotIn("must-not-leak", public_error)

    def test_error_receipt_validates_values_inside_public_keys(self) -> None:
        marker = "FULL-BODY-COOKIE-/tmp/session.json"

        class LeakyString(str):
            def __repr__(self) -> str:
                return marker

        unsafe = overseas_tiktok_publish.TikTokPublishError(
            "tiktok_account_invalid",
            "账号不可用",
            receipt={
                "accountId": [61, marker],
                "visibility": marker,
                "mode": marker,
                "phase": marker,
                "platformWriteOccurred": marker,
                "finalActionTriggered": 1,
                "contentId": marker,
                "contentUrl": f"https://www.tiktok.com/{marker}",
                "publishedAt": marker,
            },
        )
        self.assertEqual(unsafe.receipt, {})
        self.assertNotIn(marker, repr(unsafe.receipt))

        subclass_value = overseas_tiktok_publish.TikTokPublishError(
            "tiktok_account_invalid",
            "账号不可用",
            receipt={"mode": LeakyString("formal")},
        )
        self.assertEqual(subclass_value.receipt, {})
        self.assertNotIn(marker, repr(subclass_value.receipt))

        safe = overseas_tiktok_publish.TikTokPublishError(
            "tiktok_publish_outcome_unknown",
            "发布结果待确认",
            receipt={
                "accountId": 61,
                "visibility": "public",
                "mode": "formal",
                "phase": "published_readback_confirmed",
                "platformWriteOccurred": True,
                "finalActionTriggered": True,
                "contentId": "7512345678901234567",
                "contentUrl": (
                    "https://www.tiktok.com/@expected.user/video/"
                    "7512345678901234567"
                ),
                "publishedAt": "2026-08-28T12:30:00+08:00",
            },
        )
        self.assertEqual(safe.receipt["accountId"], 61)
        self.assertEqual(safe.receipt["visibility"], "public")
        self.assertEqual(safe.receipt["mode"], "formal")
        self.assertEqual(safe.receipt["phase"], "published_readback_confirmed")
        self.assertTrue(safe.receipt["platformWriteOccurred"])
        self.assertTrue(safe.receipt["finalActionTriggered"])
        self.assertEqual(safe.receipt["contentId"], "7512345678901234567")
        self.assertEqual(
            safe.receipt["contentUrl"],
            "https://www.tiktok.com/@expected.user/video/7512345678901234567",
        )
        self.assertEqual(safe.receipt["publishedAt"], "2026-08-28T12:30:00+08:00")


class TikTokPlatformSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"offline-tiktok-video")
        self.session = self.root / "tiktok.json"
        self.session.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        self.identity = TikTokIdentity(
            "expected.user",
            "Expected User",
            "https://www.tiktok.com/@expected.user",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepared(self, *, mode: str) -> dict:
        caption = "TikTok 标题\n\n受控发布正文 #OneClick #AI工具"
        return {
            "accountId": 61,
            "accountFile": str(self.session),
            "expectedAccountReference": "expected.user",
            "videoPath": str(self.video),
            "videoSha256": hashlib.sha256(self.video.read_bytes()).hexdigest(),
            "title": "TikTok 标题",
            "body": "受控发布正文",
            "topics": ["OneClick", "AI工具"],
            "plainCaption": "TikTok 标题\n\n受控发布正文",
            "textSha256": hashlib.sha256(caption.encode("utf-8")).hexdigest(),
            "visibility": "public",
            "mode": mode,
        }

    def form_receipt(self, **changes) -> dict:
        result = {
            "plainCaption": "TikTok 标题\n\n受控发布正文",
            "topicEntities": ["OneClick", "AI工具"],
            "visibility": "public",
            "finalCaption": "TikTok 标题\n\n受控发布正文 #OneClick #AI工具",
            "finalActionReady": True,
        }
        result.update(changes)
        return result

    def fake_uploader(self, *, submit_result: dict | None = None):
        uploader = SimpleNamespace()
        uploader.prepare_form = AsyncMock(return_value=self.form_receipt())
        uploader.submit_once = AsyncMock(
            return_value=submit_result
            or {
                "status": "published",
                "evidence": "platform_feedback:video posted successfully",
                "formSnapshot": self.form_receipt(),
            }
        )
        uploader._base = AsyncMock(return_value=object())
        uploader.publish_confirmed = False
        return uploader

    def run_sync(
        self,
        *,
        mode: str,
        uploader=None,
        identity_effect=None,
        account_effect=None,
        verification_effect=None,
        event_sink: list[str] | None = None,
    ):
        uploader = uploader or self.fake_uploader()
        page = SimpleNamespace(
            url="about:blank",
            goto=AsyncMock(return_value=None),
            wait_for_timeout=AsyncMock(return_value=None),
            close=AsyncMock(return_value=None),
        )
        context = SimpleNamespace(
            new_page=AsyncMock(return_value=page),
            close=AsyncMock(return_value=None),
        )
        browser = SimpleNamespace(close=AsyncMock(return_value=None))
        manager = SimpleNamespace(
            start=AsyncMock(return_value=object()),
            stop=AsyncMock(return_value=None),
        )
        events: list[str] = []
        event_sink = event_sink if event_sink is not None else []
        account = {
            "id": 61,
            "type": 6,
            "status": 1,
            "authMode": "browser",
            "filePath": self.session.name,
            "accountReference": "expected.user",
        }
        identity_reader = AsyncMock(
            side_effect=identity_effect
            if identity_effect is not None
            else [self.identity, self.identity]
        )
        account_reader = (
            MagicMock(return_value=account)
            if account_effect is None
            else MagicMock(side_effect=account_effect)
        )
        save_session = AsyncMock(return_value=None)
        uploader._test_save_session = save_session

        def record_event(_task_id, event_type, _message, *, level="info"):
            del level
            events.append(event_type)
            event_sink.append(event_type)

        with (
            patch.object(
                overseas_tiktok_publish,
                "validate_tiktok_payload",
                return_value=self.prepared(mode=mode),
            ),
            patch.object(overseas_tiktok_publish, "_read_account_record", account_reader),
            patch.object(
                overseas_tiktok_publish,
                "read_tiktok_identity",
                new=identity_reader,
                create=True,
            ),
            patch.object(
                overseas_tiktok_publish,
                "validate_identity_binding",
                new=validate_identity_binding,
                create=True,
            ),
            patch.object(
                overseas_tiktok_publish,
                "TiktokVideo",
                return_value=uploader,
                create=True,
            ) as uploader_type,
            patch.object(
                overseas_tiktok_publish,
                "async_playwright",
                return_value=manager,
                create=True,
            ),
            patch.object(
                overseas_tiktok_publish,
                "launch_publish_browser",
                new=AsyncMock(return_value=browser),
                create=True,
            ),
            patch.object(
                overseas_tiktok_publish,
                "new_publish_context",
                new=AsyncMock(return_value=context),
                create=True,
            ),
            patch.object(
                overseas_tiktok_publish,
                "set_init_script",
                new=AsyncMock(return_value=context),
                create=True,
            ),
            patch.object(
                overseas_tiktok_publish,
                "save_context_storage_state",
                new=save_session,
                create=True,
            ),
            patch.object(
                overseas_tiktok_publish,
                "_tiktok_verification_reason",
                new=AsyncMock(
                    side_effect=verification_effect
                    if verification_effect is not None
                    else None,
                    return_value=None,
                ),
                create=True,
            ),
            patch.object(task_service, "record_task_event", side_effect=record_event),
        ):
            result = overseas_tiktok_publish.run_tiktok_platform_sync(
                {"type": 6},
                mode=mode,
                task_id=71,
            )
        return SimpleNamespace(
            result=result,
            uploader=uploader,
            uploader_type=uploader_type,
            page=page,
            context=context,
            browser=browser,
            manager=manager,
            events=events,
            identity_reader=identity_reader,
            account_reader=account_reader,
            save_session=save_session,
        )

    def test_platform_form_check_prepares_once_and_never_submits(self) -> None:
        run = self.run_sync(mode="platform_form_check")

        run.uploader.prepare_form.assert_awaited_once()
        run.uploader.submit_once.assert_not_awaited()
        prepare_page, prepare_base = run.uploader.prepare_form.await_args.args
        self.assertIs(prepare_page, run.page)
        self.assertIs(prepare_base, run.uploader._base.return_value)
        self.assertEqual(run.result["phase"], "platform_form_verified")
        self.assertTrue(run.result["receipt"]["platformWriteOccurred"])
        self.assertFalse(run.result["receipt"]["finalActionTriggered"])
        self.assertIn("tiktok_platform_form_started", run.events)
        self.assertIn("tiktok_platform_form_verified", run.events)
        self.assertNotIn("tiktok_final_action_triggered", run.events)

    def test_formal_uses_same_prepared_page_then_submits_once(self) -> None:
        run = self.run_sync(mode="formal")

        run.uploader.prepare_form.assert_awaited_once()
        run.uploader.submit_once.assert_awaited_once()
        self.assertEqual(
            run.uploader.prepare_form.await_args.args,
            run.uploader.submit_once.await_args.args,
        )
        self.assertIs(run.uploader.prepare_form.await_args.args[0], run.page)
        self.assertTrue(run.uploader.publish_confirmed)
        self.assertEqual(run.result["phase"], "platform_accepted")
        self.assertEqual(run.events.count("tiktok_final_action_triggered"), 1)
        self.assertIn("tiktok_platform_accepted", run.events)

    def test_final_action_event_is_persisted_immediately_before_button_click(self) -> None:
        order: list[str] = []
        uploader = self.fake_uploader()
        button = SimpleNamespace(
            click=AsyncMock(side_effect=lambda: order.append("click"))
        )
        uploader._post_button = AsyncMock(return_value=button)

        async def submit_once(_page, base):
            current = await uploader._post_button(base)
            await current.click()
            return {
                "status": "published",
                "evidence": "platform_feedback:video posted successfully",
            }

        uploader.submit_once = AsyncMock(side_effect=submit_once)
        self.run_sync(mode="formal", uploader=uploader, event_sink=order)

        click_index = order.index("click")
        self.assertEqual(order[click_index - 1], "tiktok_final_action_triggered")
        self.assertEqual(order.count("tiktok_final_action_triggered"), 1)

    def test_identity_mismatch_stops_before_upload_and_session_refresh(self) -> None:
        uploader = self.fake_uploader()
        other = TikTokIdentity(
            "other.user",
            "Other User",
            "https://www.tiktok.com/@other.user",
        )
        with self.assertRaises(overseas_tiktok_publish.TikTokPublishError) as raised:
            self.run_sync(
                mode="platform_form_check",
                uploader=uploader,
                identity_effect=[other],
            )

        self.assertEqual(raised.exception.error_code, "tiktok_account_identity_mismatch")
        uploader.prepare_form.assert_not_awaited()
        uploader.submit_once.assert_not_awaited()
        uploader._test_save_session.assert_not_awaited()

    def test_form_snapshot_mismatch_stops_before_final_action(self) -> None:
        uploader = self.fake_uploader()
        uploader.prepare_form.return_value = self.form_receipt(
            finalCaption="不同的页面正文",
        )
        with self.assertRaises(overseas_tiktok_publish.TikTokPublishError) as raised:
            self.run_sync(mode="formal", uploader=uploader)

        self.assertEqual(raised.exception.error_code, "tiktok_form_snapshot_mismatch")
        uploader.submit_once.assert_not_awaited()

    def test_explicit_platform_rejection_is_not_marked_ambiguous(self) -> None:
        uploader = self.fake_uploader()
        uploader.submit_once.side_effect = RuntimeError("TikTok 页面提示最终发布失败")
        with self.assertRaises(overseas_tiktok_publish.TikTokPublishError) as raised:
            self.run_sync(mode="formal", uploader=uploader)

        self.assertEqual(raised.exception.error_code, "tiktok_publish_rejected")
        self.assertFalse(raised.exception.outcome_ambiguous)
        self.assertTrue(raised.exception.receipt["finalActionTriggered"])
        self.assertEqual(raised.exception.receipt["phase"], "final_action_triggered")

    def test_post_click_exception_becomes_ambiguous_and_blocks_success(self) -> None:
        uploader = self.fake_uploader()
        uploader.submit_once.side_effect = RuntimeError("page detached after click")
        with self.assertRaises(overseas_tiktok_publish.TikTokPublishError) as raised:
            self.run_sync(mode="formal", uploader=uploader)

        self.assertEqual(raised.exception.error_code, "tiktok_publish_outcome_unknown")
        self.assertTrue(raised.exception.outcome_ambiguous)
        self.assertTrue(raised.exception.receipt["finalActionTriggered"])

    def test_exact_content_readback_is_distinct_from_platform_acceptance(self) -> None:
        uploader = self.fake_uploader(
            submit_result={
                "status": "published",
                "evidence": "platform_feedback:video posted successfully",
                "contentId": "7512345678901234567",
                "contentUrl": (
                    "https://www.tiktok.com/@expected.user/video/"
                    "7512345678901234567"
                ),
                "publishedAt": "2026-08-28T12:30:00+08:00",
            }
        )
        run = self.run_sync(mode="formal", uploader=uploader)

        self.assertEqual(run.result["phase"], "published_readback_confirmed")
        self.assertEqual(
            run.result["receipt"]["contentId"],
            "7512345678901234567",
        )
        self.assertIn("tiktok_published_readback_confirmed", run.events)

    def test_unverified_submit_result_is_always_ambiguous(self) -> None:
        uploader = self.fake_uploader(
            submit_result={"status": "published", "evidence": ""}
        )
        with self.assertRaises(overseas_tiktok_publish.TikTokPublishError) as raised:
            self.run_sync(mode="formal", uploader=uploader)

        self.assertEqual(raised.exception.error_code, "tiktok_publish_outcome_unknown")
        self.assertTrue(raised.exception.outcome_ambiguous)

    def test_matching_identity_allows_refresh_of_existing_session_only(self) -> None:
        run = self.run_sync(mode="platform_form_check")

        run.save_session.assert_awaited_once_with(run.context, str(self.session))

    def test_manual_verification_emits_waiting_and_resolved_events(self) -> None:
        uploader = self.fake_uploader()
        uploader._wait_for_manual_intervention = AsyncMock(return_value=None)

        async def prepare(page, _base):
            await uploader._wait_for_manual_intervention(page)
            return self.form_receipt()

        uploader.prepare_form = AsyncMock(side_effect=prepare)
        run = self.run_sync(
            mode="platform_form_check",
            uploader=uploader,
            verification_effect=["TikTok 要求安全验证", None],
        )

        self.assertIn("tiktok_waiting_user_verification", run.events)
        self.assertIn("tiktok_user_verification_resolved", run.events)


if __name__ == "__main__":
    unittest.main()
