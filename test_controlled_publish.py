# -*- coding: utf-8 -*-

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app_core.controlled_publish import (
    ControlledPublishError,
    build_controlled_payloads,
    consume_authorization,
    consume_direct_authorization,
    create_authorization,
    create_authorization_schema,
    create_direct_authorization,
    create_direct_authorization_schema,
    _find_blocking_formal_scope_task,
    authorize_completed_check,
    authorize_direct_request,
    _find_successful_silicon_formal_task,
    _find_successful_formal_scope_task,
    project_task,
    scope_fingerprint,
    submit_request,
)
from app_core import database, task_service
from app_core.douyin_graphic_matrix_service import prepare_matrix


class ControlledPublishTests(unittest.TestCase):
    def _bundle(self, root: Path) -> Path:
        (root / "video.mp4").write_bytes(b"video")
        (root / "cover.png").write_bytes(b"png")
        (root / "正文.md").write_text(
            "# 硅基探索 008\n\n## 抖音\n预览\n\n## 小红书\n预览",
            encoding="utf-8",
        )
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": "oneclick-content/v1",
                    "contentType": "video",
                    "title": "预览标题",
                    "bodyFile": "正文.md",
                    "tags": ["预览标签"],
                    "assets": ["video.mp4"],
                    "covers": {"3:4": "cover.png"},
                    "preferredPlatforms": ["抖音", "小红书"],
                    "platformOverrides": {
                        "抖音": {"title": "抖音标题", "body": "抖音正文", "tags": ["抖音话题"]},
                        "小红书": {"title": "小红书标题", "body": "小红书正文", "tags": ["小红书话题"]},
                    },
                    "debugDryRun": True,
                    "publishAllowed": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return manifest

    def _youtube_bundle(self, root: Path) -> Path:
        (root / "youtube.mp4").write_bytes(b"youtube-video")
        (root / "youtube-cover.png").write_bytes(b"youtube-cover")
        (root / "正文.md").write_text("YouTube 正文", encoding="utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": "oneclick-content/v1",
                    "contentType": "video",
                    "title": "YouTube 标题",
                    "bodyFile": "正文.md",
                    "tags": ["oneclick"],
                    "assets": ["youtube.mp4"],
                    "covers": {"4:3": "youtube-cover.png"},
                    "preferredPlatforms": ["YouTube"],
                    "platformOverrides": {
                        "YouTube": {
                            "title": "YouTube 独立标题",
                            "body": "YouTube 独立正文",
                            "tags": ["youtube"],
                        }
                    },
                    "debugDryRun": True,
                    "publishAllowed": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return manifest

    def _tiktok_bundle(self, root: Path, *, body: str = "TikTok body") -> Path:
        (root / "tiktok.mp4").write_bytes(b"tiktok-video")
        (root / "tiktok-cover.png").write_bytes(b"tiktok-cover")
        (root / "body.md").write_text(body, encoding="utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": "oneclick-content/v1",
                    "contentType": "video",
                    "title": "TikTok title",
                    "bodyFile": "body.md",
                    "tags": ["oneclick"],
                    "assets": ["tiktok.mp4"],
                    "covers": {"3:4": "tiktok-cover.png"},
                    "preferredPlatforms": ["TikTok"],
                    "platformOverrides": {
                        "TikTok": {
                            "title": "TikTok title",
                            "body": body,
                            "tags": ["oneclick"],
                        }
                    },
                    "debugDryRun": True,
                    "publishAllowed": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return manifest

    @staticmethod
    def _tiktok_account(**changes) -> dict:
        return {
            "id": 61,
            "type": 6,
            "filePath": "tiktok-session.json",
            "profileName": "TikTok saved account",
            "userName": "TikTok saved account",
            "authMode": "browser",
            "accountReference": "expected.user",
            "status": 1,
            **changes,
        }

    @staticmethod
    def _tiktok_request(manifest: Path, *, mode: str = "preflight", **changes) -> dict:
        request = {
            "projectId": "tiktok-offline-test",
            "manifestPath": str(manifest),
            "mode": mode,
            "targets": [
                {
                    "platform": "TikTok",
                    "accountId": 61,
                    "schedule": None,
                    "settings": {"visibility": "public"},
                }
            ],
        }
        request.update(changes)
        return request

    @staticmethod
    def _accounts() -> list[dict]:
        return [
            {"id": 31, "type": 3, "filePath": "douyin.json", "profileName": "抖音主体", "userName": "抖音主体"},
            {"id": 11, "type": 1, "filePath": "xhs.json", "profileName": "小红书主体", "userName": "小红书主体"},
        ]

    def test_builds_clean_mixed_schedule_preflight_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary))
            payloads = build_controlled_payloads(
                {
                    "manifestPath": str(manifest),
                    "mode": "preflight",
                    "targets": [
                        {"platform": "抖音", "accountId": 31, "schedule": None},
                        {"platform": "小红书", "accountId": 11, "schedule": {"localTime": "2026-08-24 20:30", "timezone": "Asia/Shanghai"}},
                    ],
                },
                accounts=self._accounts(),
            )

        self.assertEqual([item["type"] for item in payloads], [3, 1])
        self.assertEqual(payloads[0]["title"], "抖音标题")
        self.assertEqual(payloads[0]["description"], "抖音正文")
        self.assertNotIn("## 抖音", payloads[0]["description"])
        self.assertFalse(payloads[0]["enableTimer"])
        self.assertTrue(payloads[1]["enableTimer"])
        self.assertEqual(payloads[1]["scheduleTime"], "2026-08-24 20:30")
        self.assertTrue(all(item["debugDryRun"] is True for item in payloads))

    def test_controlled_payload_carries_valid_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary))
            payload = build_controlled_payloads(
                {
                    "projectId": "silicon-exploration",
                    "manifestPath": str(manifest),
                    "mode": "preflight",
                    "targets": [
                        {"platform": "抖音", "accountId": 31, "schedule": None}
                    ],
                },
                accounts=self._accounts(),
            )[0]

        self.assertEqual(payload["contentProjectId"], "silicon-exploration")

    def test_controlled_payload_rejects_invalid_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary))
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    {
                        "projectId": "../other-project",
                        "manifestPath": str(manifest),
                        "mode": "preflight",
                        "targets": [
                            {"platform": "抖音", "accountId": 31, "schedule": None}
                        ],
                    },
                    accounts=self._accounts(),
                )

        self.assertEqual(raised.exception.error_code, "content_project_id_invalid")

    def test_formal_requires_preflight_and_authorization_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary))
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    {
                        "manifestPath": str(manifest),
                        "mode": "formal",
                        "targets": [{"platform": "抖音", "accountId": 31, "schedule": None}],
                    },
                    accounts=self._accounts(),
                )
        self.assertEqual(raised.exception.error_code, "controlled_authorization_required")

    def test_direct_publish_is_hidden_and_does_not_require_preflight_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary))
            payloads = build_controlled_payloads(
                {
                    "manifestPath": str(manifest),
                    "mode": "direct",
                    "directAuthorizationId": "direct-grant",
                    "targets": [
                        {"platform": "抖音", "accountId": 31, "schedule": None}
                    ],
                },
                accounts=self._accounts(),
            )

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["runtimeMode"], "publish")
        self.assertFalse(payloads[0]["debugDryRun"])
        self.assertTrue(payloads[0]["backgroundMode"])

    def test_direct_publish_requires_one_time_authorization_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary))
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    {
                        "manifestPath": str(manifest),
                        "mode": "direct",
                        "targets": [
                            {"platform": "抖音", "accountId": 31, "schedule": None}
                        ],
                    },
                    accounts=self._accounts(),
                )

        self.assertEqual(
            raised.exception.error_code,
            "controlled_direct_authorization_required",
        )

    def test_tiktok_preflight_builds_local_only_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._tiktok_bundle(Path(temporary))
            payload = build_controlled_payloads(
                self._tiktok_request(manifest),
                accounts=[self._tiktok_account()],
            )[0]

        self.assertEqual(payload["runtimeMode"], "preflight")
        self.assertTrue(payload["debugDryRun"])
        self.assertFalse(payload["backgroundMode"])
        self.assertFalse(payload["overseasVideoPublishConfirmed"])
        self.assertEqual(payload["tiktokExpectedAccountReference"], "expected.user")
        self.assertEqual(payload["tiktokExecutionIntent"], "formal_public")

    def test_tiktok_platform_form_check_requires_literal_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._tiktok_bundle(Path(temporary))
            for confirmation in (None, 1, "true", [], {}):
                request = self._tiktok_request(
                    manifest,
                    mode="platform_form_check",
                    platformFormCheckConfirmed=confirmation,
                )
                with self.subTest(confirmation=confirmation), self.assertRaises(
                    ControlledPublishError
                ) as raised:
                    build_controlled_payloads(
                        request,
                        accounts=[self._tiktok_account()],
                    )
                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_platform_form_check_confirmation_required",
                )

    def test_tiktok_platform_form_check_never_creates_or_consumes_formal_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._tiktok_bundle(Path(temporary))
            request = self._tiktok_request(
                manifest,
                mode="platform_form_check",
                platformFormCheckConfirmed=True,
            )

            def start(payloads):
                rows = [dict(item) for item in payloads]
                return {
                    "id": 62,
                    "taskNo": "T62",
                    "mode": "oneclick_platform_form_check",
                    "status": "pending",
                    "payloadJson": json.dumps(rows, ensure_ascii=False),
                    "items": [],
                    "events": [],
                }

            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_desktop_publish",
                side_effect=start,
            ), patch(
                "app_core.task_service.get_task",
                return_value=None,
            ), patch(
                "app_core.controlled_publish.create_authorization"
            ) as create_formal, patch(
                "app_core.controlled_publish.consume_authorization"
            ) as consume_formal, patch(
                "app_core.controlled_publish.consume_direct_authorization"
            ) as consume_direct:
                result = submit_request(request)

        self.assertEqual(result["phase"], "platform_form_check")
        create_formal.assert_not_called()
        consume_formal.assert_not_called()
        consume_direct.assert_not_called()

    def test_tiktok_modes_and_local_contract_fail_closed_before_task_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._tiktok_bundle(root)
            cases = [
                (
                    "direct",
                    self._tiktok_request(
                        manifest,
                        mode="direct",
                        directAuthorizationId="grant",
                    ),
                    [self._tiktok_account()],
                    "tiktok_direct_mode_unsupported",
                ),
                (
                    "status",
                    self._tiktok_request(manifest),
                    [self._tiktok_account(status=0)],
                    "tiktok_account_invalid",
                ),
                (
                    "identity",
                    self._tiktok_request(manifest),
                    [self._tiktok_account(accountReference="")],
                    "tiktok_account_invalid",
                ),
                (
                    "auth",
                    self._tiktok_request(manifest),
                    [self._tiktok_account(authMode="oauth")],
                    "tiktok_account_invalid",
                ),
                (
                    "schedule",
                    self._tiktok_request(
                        manifest,
                        targets=[
                            {
                                "platform": "TikTok",
                                "accountId": 61,
                                "schedule": {
                                    "localTime": "2026-08-28 10:00",
                                    "timezone": "Asia/Shanghai",
                                },
                                "settings": {"visibility": "public"},
                            }
                        ],
                    ),
                    [self._tiktok_account()],
                    "tiktok_unsupported_publish_setting",
                ),
                (
                    "empty_schedule_object",
                    self._tiktok_request(
                        manifest,
                        targets=[
                            {
                                "platform": "TikTok",
                                "accountId": 61,
                                "schedule": {},
                                "settings": {"visibility": "public"},
                            }
                        ],
                    ),
                    [self._tiktok_account()],
                    "tiktok_unsupported_publish_setting",
                ),
                (
                    "visibility",
                    self._tiktok_request(
                        manifest,
                        targets=[
                            {
                                "platform": "TikTok",
                                "accountId": 61,
                                "schedule": None,
                                "settings": {"visibility": "private"},
                            }
                        ],
                    ),
                    [self._tiktok_account()],
                    "tiktok_unsupported_publish_setting",
                ),
                (
                    "multi_target",
                    self._tiktok_request(
                        manifest,
                        targets=[
                            {
                                "platform": "TikTok",
                                "accountId": 61,
                                "schedule": None,
                                "settings": {"visibility": "public"},
                            },
                            {
                                "platform": "TikTok",
                                "accountId": 62,
                                "schedule": None,
                                "settings": {"visibility": "public"},
                            },
                        ],
                    ),
                    [self._tiktok_account(), self._tiktok_account(id=62)],
                    "tiktok_target_invalid",
                ),
            ]
            for label, request, accounts, error_code in cases:
                with self.subTest(label=label), self.assertRaises(
                    ControlledPublishError
                ) as raised:
                    build_controlled_payloads(request, accounts=accounts)
                self.assertEqual(raised.exception.error_code, error_code)

            raw_mention = self._tiktok_bundle(root, body="body @raw.user")
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    self._tiktok_request(raw_mention),
                    accounts=[self._tiktok_account()],
                )
            self.assertEqual(
                raised.exception.error_code,
                "tiktok_unsupported_publish_setting",
            )

    def test_tiktok_fingerprint_binds_content_video_identity_and_execution_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._tiktok_bundle(root)
            base = build_controlled_payloads(
                self._tiktok_request(manifest),
                accounts=[self._tiktok_account()],
            )
            form_check = build_controlled_payloads(
                self._tiktok_request(
                    manifest,
                    mode="platform_form_check",
                    platformFormCheckConfirmed=True,
                ),
                accounts=[self._tiktok_account()],
            )
            mutations = []
            for key, value in (
                ("description", "changed body"),
                ("tags", ["changed-topic"]),
                ("fileList", [str(root / "changed.mp4")]),
                ("tiktokExpectedAccountReference", "other.user"),
            ):
                mutations.append([{**base[0], key: value}])

        self.assertNotEqual(scope_fingerprint(base), scope_fingerprint(form_check))
        for changed in mutations:
            self.assertNotEqual(scope_fingerprint(base), scope_fingerprint(changed))

    def test_tiktok_authorization_requires_a_successful_local_preflight_event(self) -> None:
        payload = {
            "type": 6,
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "tiktokExpectedAccountReference": "expected.user",
            "tiktokExecutionIntent": "formal_public",
        }
        fake_task = {
            "id": 61,
            "mode": "oneclick_preflight",
            "status": "success",
            "payloadJson": json.dumps([payload]),
            "items": [{"platformType": 6, "status": "success"}],
            "events": [{"eventType": "platform_preflight"}],
        }
        with patch.object(task_service, "get_task", return_value=fake_task):
            with self.assertRaises(ControlledPublishError) as raised:
                authorize_completed_check(61)

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_local_preflight_required",
        )

    def test_tiktok_project_task_maps_safe_lifecycle_without_sensitive_fields(self) -> None:
        base = {
            "id": 61,
            "taskNo": "T61",
            "mode": "oneclick_publish",
            "status": "running",
            "payloadJson": json.dumps(
                [{"type": 6, "accountIds": [61]}], ensure_ascii=False
            ),
            "items": [
                {
                    "platformType": 6,
                    "status": "running",
                    "accountLabel": "TikTok saved account",
                }
            ],
        }
        waiting = project_task(
            {
                **base,
                "events": [
                    {
                        "eventType": "tiktok_waiting_user_verification",
                        "message": "private/session/path @expected.user full post",
                    }
                ],
            }
        )
        ambiguous = project_task(
            {
                **base,
                "status": "failed",
                "items": [
                    {
                        "platformType": 6,
                        "status": "failed",
                        "errorCode": "tiktok_publish_outcome_unknown",
                    }
                ],
                "events": [{"eventType": "tiktok_publish_outcome_ambiguous"}],
            }
        )

        self.assertEqual(waiting["stage"], "waiting_verification")
        self.assertEqual(waiting["userAction"]["type"], "tiktok_verification")
        self.assertNotIn("expected.user", waiting["userAction"]["message"])
        self.assertNotIn("session", waiting["userAction"]["message"])
        self.assertEqual(ambiguous["stage"], "ambiguous")

    def test_tiktok_ambiguous_receipt_sets_public_stage_without_event_message(self) -> None:
        projected = project_task(
            {
                "id": 63,
                "taskNo": "T63",
                "mode": "oneclick_publish",
                "status": "failed",
                "payloadJson": json.dumps(
                    [{"type": 6, "accountIds": [61]}], ensure_ascii=False
                ),
                "items": [
                    {
                        "platformType": 6,
                        "status": "failed",
                        "errorCode": "tiktok_publish_outcome_unknown",
                        "receiptJson": json.dumps(
                            {
                                "finalActionTriggered": True,
                                "phase": "ambiguous",
                            }
                        ),
                    }
                ],
                "events": [],
            }
        )

        self.assertEqual(projected["stage"], "ambiguous")

    def test_douyin_body_raw_mention_is_not_treated_as_platform_mention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._bundle(Path(temporary))
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["platformOverrides"]["抖音"]["body"] = "正文\n@抖音科技"
            manifest.write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    {
                        "manifestPath": str(manifest),
                        "mode": "preflight",
                        "targets": [
                            {"platform": "抖音", "accountId": 31, "schedule": None}
                        ],
                    },
                    accounts=self._accounts(),
                )

        self.assertEqual(
            raised.exception.error_code,
            "controlled_mentions_unsupported",
        )

    def test_youtube_target_defaults_private_and_keeps_explicit_audience(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._youtube_bundle(Path(temporary))
            payload = build_controlled_payloads(
                {
                    "manifestPath": str(manifest),
                    "mode": "preflight",
                    "targets": [
                        {
                            "platform": "YouTube",
                            "accountId": 71,
                            "schedule": None,
                            "settings": {
                                "visibility": "private",
                                "madeForKids": False,
                            },
                        }
                    ],
                },
                accounts=[
                    {
                        "id": 71,
                        "type": 7,
                        "filePath": "youtube-oauth:test",
                        "profileName": "YouTube 测试",
                        "authMode": "youtube_oauth",
                        "oauthScopeVersion": 2,
                        "accountReference": "UC-test",
                        "status": 1,
                    }
                ],
            )[0]

        self.assertEqual(payload["visibility"], "private")
        self.assertIs(payload["madeForKids"], False)
        self.assertTrue(payload["notifySubscribers"])
        self.assertTrue(payload["youtubeOfficialApi"])
        self.assertTrue(payload["backgroundMode"])

    def test_youtube_schedule_requires_scheduled_public(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._youtube_bundle(Path(temporary))
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    {
                        "manifestPath": str(manifest),
                        "mode": "preflight",
                        "targets": [
                            {
                                "platform": "YouTube",
                                "accountId": 71,
                                "schedule": {
                                    "localTime": "2026-08-28 09:00",
                                    "timezone": "Asia/Shanghai",
                                },
                                "settings": {
                                    "visibility": "private",
                                    "madeForKids": False,
                                },
                            }
                        ],
                    },
                    accounts=[
                        {
                            "id": 71,
                            "type": 7,
                            "filePath": "youtube-oauth:test",
                            "authMode": "youtube_oauth",
                            "oauthScopeVersion": 2,
                            "accountReference": "UC-test",
                            "status": 1,
                        }
                    ],
                )

        self.assertEqual(raised.exception.error_code, "youtube_schedule_invalid")

    def test_youtube_formal_requires_upgraded_oauth_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._youtube_bundle(Path(temporary))
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    {
                        "manifestPath": str(manifest),
                        "mode": "formal",
                        "confirmedPreflightTaskId": 4,
                        "authorizationId": "grant",
                        "targets": [
                            {
                                "platform": "YouTube",
                                "accountId": 71,
                                "schedule": None,
                                "settings": {
                                    "visibility": "private",
                                    "madeForKids": False,
                                },
                            }
                        ],
                    },
                    accounts=[
                        {
                            "id": 71,
                            "type": 7,
                            "filePath": "youtube-oauth:test",
                            "authMode": "youtube_oauth",
                            "oauthScopeVersion": 1,
                            "accountReference": "UC-test",
                            "status": 1,
                        }
                    ],
                )

        self.assertEqual(
            raised.exception.error_code,
            "youtube_oauth_scope_upgrade_required",
        )

    def test_youtube_settings_change_authorization_fingerprint(self) -> None:
        base = [
            {
                "type": 7,
                "accountIds": [71],
                "visibility": "private",
                "madeForKids": False,
                "notifySubscribers": True,
            }
        ]
        visibility_changed = [{**base[0], "visibility": "unlisted"}]
        audience_changed = [{**base[0], "madeForKids": True}]

        self.assertNotEqual(
            scope_fingerprint(base), scope_fingerprint(visibility_changed)
        )
        self.assertNotEqual(
            scope_fingerprint(base), scope_fingerprint(audience_changed)
        )

    def test_authorization_is_bound_short_lived_and_single_use(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        create_authorization_schema(conn)
        payloads = [{"type": 3, "accountIds": [31], "title": "标题", "description": "正文", "scheduleTime": None}]
        now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        grant = create_authorization(conn, 77, payloads, now=now, ttl_seconds=600)

        consume_authorization(conn, grant["authorizationId"], 77, payloads, now=now + timedelta(seconds=1))
        with self.assertRaises(ControlledPublishError) as replayed:
            consume_authorization(conn, grant["authorizationId"], 77, payloads, now=now + timedelta(seconds=2))
        self.assertEqual(replayed.exception.error_code, "controlled_authorization_consumed")

        second = create_authorization(conn, 78, payloads, now=now, ttl_seconds=1)
        with self.assertRaises(ControlledPublishError) as expired:
            consume_authorization(conn, second["authorizationId"], 78, payloads, now=now + timedelta(seconds=2))
        self.assertEqual(expired.exception.error_code, "controlled_authorization_expired")

        third = create_authorization(conn, 79, payloads, now=now, ttl_seconds=600)
        changed = [{**payloads[0], "title": "被修改"}]
        self.assertNotEqual(scope_fingerprint(payloads), scope_fingerprint(changed))
        with self.assertRaises(ControlledPublishError) as changed_error:
            consume_authorization(conn, third["authorizationId"], 79, changed, now=now + timedelta(seconds=1))
        self.assertEqual(changed_error.exception.error_code, "controlled_authorization_scope_mismatch")

        declaration_changed = [{**payloads[0], "aiGenerated": True}]
        self.assertNotEqual(
            scope_fingerprint(payloads), scope_fingerprint(declaration_changed)
        )

    def test_direct_authorization_is_bound_short_lived_and_single_use(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        create_direct_authorization_schema(conn)
        payloads = [
            {
                "type": 10,
                "accountIds": [6],
                "title": "标题",
                "description": "正文",
                "scheduleTime": "2026-08-28 09:00",
                "scheduleTimezone": "Asia/Shanghai",
            }
        ]
        now = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
        grant = create_direct_authorization(
            conn,
            payloads,
            source="content-project-gateway",
            now=now,
            ttl_seconds=60,
        )

        consume_direct_authorization(
            conn,
            grant["authorizationId"],
            payloads,
            now=now + timedelta(seconds=1),
        )
        with self.assertRaises(ControlledPublishError) as replayed:
            consume_direct_authorization(
                conn,
                grant["authorizationId"],
                payloads,
                now=now + timedelta(seconds=2),
            )
        self.assertEqual(
            replayed.exception.error_code,
            "controlled_direct_authorization_consumed",
        )

        changed = [{**payloads[0], "accountIds": [7]}]
        second = create_direct_authorization(
            conn,
            payloads,
            source="content-project-gateway",
            now=now,
            ttl_seconds=60,
        )
        with self.assertRaises(ControlledPublishError) as mismatch:
            consume_direct_authorization(
                conn,
                second["authorizationId"],
                changed,
                now=now + timedelta(seconds=1),
            )
        self.assertEqual(
            mismatch.exception.error_code,
            "controlled_direct_authorization_scope_mismatch",
        )

    def test_direct_request_authorizes_and_consumes_before_starting_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._bundle(root)
            db_patch = patch.object(database, "DB_PATH", root / "database.db")
            request = {
                "manifestPath": str(manifest),
                "mode": "direct",
                "targets": [
                    {"platform": "抖音", "accountId": 31, "schedule": None}
                ],
            }
            captured: list[list[dict]] = []

            def start(payloads):
                rows = [dict(item) for item in payloads]
                captured.append(rows)
                return {
                    "id": 91,
                    "taskNo": "T91",
                    "mode": "oneclick_publish",
                    "status": "pending",
                    "payloadJson": json.dumps(rows, ensure_ascii=False),
                    "items": [],
                    "events": [],
                }

            with db_patch, patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=self._accounts(),
            ), patch(
                "app_core.task_service.list_tasks", return_value=[]
            ), patch(
                "app_core.task_service.get_task", return_value=None
            ), patch(
                "app_core.publish_service.start_desktop_publish",
                side_effect=start,
            ):
                database.ensure_schema()
                grant = authorize_direct_request(request)
                authorized = {
                    **request,
                    "directAuthorizationId": grant["authorizationId"],
                }
                result = submit_request(authorized)
                with self.assertRaises(ControlledPublishError) as replayed:
                    submit_request(authorized)

        self.assertEqual(result["taskId"], 91)
        self.assertEqual(result["stage"], "preparing")
        self.assertTrue(captured[0][0]["backgroundMode"])
        self.assertEqual(
            replayed.exception.error_code,
            "controlled_direct_authorization_consumed",
        )

    def test_task_projection_keeps_preflight_distinct_from_publish_receipt(self) -> None:
        projected = project_task(
            {
                "id": 42,
                "taskNo": "T42",
                "mode": "oneclick_preflight",
                "status": "partial_failed",
                "payloadJson": json.dumps(
                    [
                        {"type": 3, "accountIds": [31], "scheduleTime": None},
                        {"type": 1, "accountIds": [11], "scheduleTime": "2026-08-24 20:30"},
                    ],
                    ensure_ascii=False,
                ),
                "items": [
                    {
                        "platformType": 3,
                        "status": "success",
                        "message": "预发布检查通过",
                        "accountLabel": "抖音主体",
                    },
                    {
                        "platformType": 1,
                        "status": "failed",
                        "message": "PreflightError：封面入口不可用（错误码 xhs_cover_trigger_missing）",
                        "accountLabel": "小红书主体",
                        "scheduleSummary": "2026-08-24 20:30",
                    },
                ],
            }
        )
        self.assertEqual(projected["taskId"], 42)
        self.assertEqual(projected["phase"], "preflight")
        self.assertEqual(projected["platforms"][0]["receipt"], None)
        self.assertEqual(projected["platforms"][0]["account"], "抖音主体")
        self.assertEqual(projected["platforms"][0]["accountId"], 31)
        self.assertEqual(projected["platforms"][1]["errorCode"], "xhs_cover_trigger_missing")
        self.assertEqual(projected["platforms"][1]["scheduledAt"], "2026-08-24 20:30")

    def test_task_projection_exposes_stored_youtube_receipt_in_every_phase(self) -> None:
        projected = project_task(
            {
                "id": 43,
                "taskNo": "T43",
                "mode": "oneclick_preflight",
                "status": "failed",
                "payloadJson": json.dumps(
                    [{"type": 7, "accountIds": [71]}], ensure_ascii=False
                ),
                "items": [
                    {
                        "platformType": 7,
                        "status": "failed",
                        "message": "YouTube 检查失败",
                        "errorCode": "youtube_channel_mismatch",
                        "receiptJson": json.dumps(
                            {
                                "visibility": "private",
                                "platformMutation": "none",
                            }
                        ),
                    }
                ],
            }
        )

        platform = projected["platforms"][0]
        self.assertEqual(platform["accountId"], 71)
        self.assertEqual(platform["errorCode"], "youtube_channel_mismatch")
        self.assertEqual(
            platform["receipt"],
            {"visibility": "private", "platformMutation": "none"},
        )

    def test_task_projection_exposes_safe_verification_wait_state(self) -> None:
        projected = project_task(
            {
                "id": 54,
                "taskNo": "T54",
                "mode": "oneclick_publish",
                "status": "running",
                "payloadJson": json.dumps(
                    [{"type": 10, "accountIds": [2]}],
                    ensure_ascii=False,
                ),
                "items": [
                    {
                        "platformType": 10,
                        "status": "running",
                        "message": "正在等待微信验证",
                    }
                ],
                "events": [
                    {
                        "eventType": "wechat_final_submit_clicked",
                        "message": "已提交公众号立即发表",
                    },
                    {
                        "eventType": "wechat_verification_required",
                        "message": "公众号发表需要微信验证，请在一键发客户端扫码",
                    },
                ],
            }
        )

        self.assertEqual(projected["stage"], "waiting_verification")
        self.assertEqual(projected["userAction"]["type"], "wechat_qr")
        self.assertNotIn("qrImage", projected["userAction"])
        self.assertNotIn("verificationCode", projected["userAction"])

    def test_task_projection_moves_to_reconciliation_after_verification(self) -> None:
        projected = project_task(
            {
                "id": 55,
                "taskNo": "T55",
                "mode": "oneclick_publish",
                "status": "running",
                "payloadJson": "[]",
                "items": [],
                "events": [
                    {"eventType": "wechat_final_submit_clicked", "message": ""},
                    {"eventType": "wechat_verification_required", "message": ""},
                    {"eventType": "wechat_verification_succeeded", "message": ""},
                ],
            }
        )

        self.assertEqual(projected["stage"], "reconciling")
        self.assertIsNone(projected["userAction"])

    def test_projection_maps_legacy_douyin_topic_failure_to_stable_code(self) -> None:
        projected = project_task(
            {
                "id": 12,
                "taskNo": "T12",
                "mode": "oneclick_publish",
                "status": "failed",
                "payloadJson": "[]",
                "items": [
                    {
                        "platformType": 3,
                        "status": "failed",
                        "message": (
                            "正式发布异常：RuntimeError："
                            "抖音没有返回任何可用的话题候选，已停止填写"
                        ),
                    }
                ],
            }
        )
        self.assertEqual(
            projected["platforms"][0]["errorCode"],
            "douyin_topic_candidates_unavailable",
        )

    def test_successful_silicon_formal_task_blocks_same_article_republish(self) -> None:
        matching_payload = json.dumps(
            [
                {
                    "type": 10,
                    "accountIds": [2],
                    "siliconEvolutionArticleId": "WX-20260826-001",
                    "siliconEvolutionPackageSha256": "a" * 64,
                }
            ]
        )
        match = _find_successful_silicon_formal_task(
            [
                {
                    "id": 50,
                    "mode": "oneclick_preflight",
                    "status": "success",
                    "payloadJson": matching_payload,
                },
                {
                    "id": 51,
                    "mode": "oneclick_publish",
                    "status": "success",
                    "payloadJson": matching_payload,
                },
            ],
            article_id="WX-20260826-001",
            package_sha256="a" * 64,
            account_id=2,
        )

        self.assertEqual(match["id"], 51)
        self.assertIsNone(
            _find_successful_silicon_formal_task(
                [
                    {
                        "id": 52,
                        "mode": "oneclick_publish",
                        "status": "failed",
                        "payloadJson": matching_payload,
                    }
                ],
                article_id="WX-20260826-001",
                package_sha256="a" * 64,
                account_id=2,
            )
        )

    def test_successful_generic_formal_scope_blocks_same_manifest_republish(self) -> None:
        payloads = [
            {
                "type": 10,
                "accountIds": [2],
                "contentType": "article",
                "title": "已发表文章",
                "description": "正文",
                "tags": [],
                "fileList": [],
                "coverPath": "",
                "scheduleTime": "",
                "scheduleTimezone": "Asia/Shanghai",
            }
        ]
        match = _find_successful_formal_scope_task(
            [
                {
                    "id": 51,
                    "mode": "oneclick_publish",
                    "status": "success",
                    "payloadJson": json.dumps(payloads, ensure_ascii=False),
                }
            ],
            payloads,
        )

        self.assertEqual(match["id"], 51)
        self.assertIsNone(
            _find_successful_formal_scope_task(
                [
                    {
                        "id": 52,
                        "mode": "oneclick_publish",
                        "status": "failed",
                        "payloadJson": json.dumps(payloads, ensure_ascii=False),
                    }
                ],
                payloads,
            )
        )

    def test_post_submit_ambiguous_scope_blocks_automatic_republish(self) -> None:
        payloads = [
            {
                "type": 10,
                "accountIds": [2],
                "contentType": "article",
                "title": "可能已发表的文章",
                "description": "正文",
                "tags": [],
                "fileList": [],
                "coverPath": "",
                "scheduleTime": "",
                "scheduleTimezone": "Asia/Shanghai",
            }
        ]
        match = _find_blocking_formal_scope_task(
            [
                {
                    "id": 53,
                    "mode": "oneclick_publish",
                    "status": "running",
                    "payloadJson": json.dumps(payloads, ensure_ascii=False),
                    "events": [
                        {"eventType": "wechat_final_submit_clicked"},
                        {"eventType": "wechat_publish_user_action_required"},
                    ],
                }
            ],
            payloads,
        )

        self.assertEqual(match["id"], 53)

    def test_tiktok_final_action_or_ambiguous_event_blocks_same_fingerprint(self) -> None:
        payloads = [
            {
                "type": 6,
                "accountIds": [61],
                "contentType": "video",
                "title": "title",
                "description": "body",
                "tags": ["topic"],
                "fileList": ["video.mp4"],
                "visibility": "public",
                "tiktokExpectedAccountReference": "expected.user",
                "tiktokExecutionIntent": "formal_public",
            }
        ]
        for event_type in (
            "tiktok_final_action_triggered",
            "tiktok_publish_outcome_ambiguous",
        ):
            with self.subTest(event_type=event_type):
                match = _find_blocking_formal_scope_task(
                    [
                        {
                            "id": 62,
                            "mode": "oneclick_publish",
                            "status": "failed",
                            "payloadJson": json.dumps(payloads),
                            "events": [{"eventType": event_type}],
                        }
                    ],
                    payloads,
                )
                self.assertEqual(match["id"], 62)


class DouyinGraphicMatrixAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.tempdir.name) / "database.db"
        )
        self.db_patch.start()
        database.ensure_schema()
        image = Path(self.tempdir.name) / "matrix.jpg"
        image.write_bytes(b"matrix")
        self.matrix = prepare_matrix(
            {
                "schemaVersion": "oneclick-douyin-graphic-matrix/v1",
                "workflow": "douyin-graphic-matrix",
                "runtimeMode": "local_check",
                "content": {
                    "images": [str(image)],
                    "common": {
                        "title": "矩阵标题",
                        "body": "矩阵正文",
                        "tags": ["图文矩阵"],
                    },
                },
                "targets": [
                    {"itemIndex": 1, "accountId": 31, "overrides": {}}
                ],
            },
            accounts=[{"id": 31, "type": 3, "profileName": "账号一"}],
            now=datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
        )
        self.task = task_service.create_douyin_graphic_matrix_task(
            self.matrix, mode="oneclick_matrix_local_check"
        )
        item = task_service.matrix_item_for_index(self.task["id"], 1)
        task_service.start_matrix_item(self.task["id"], item["id"])
        task_service.finish_matrix_item(
            self.task["id"], item["id"], ok=True, message="本地批量检查通过"
        )
        task_service.close_matrix_parent(self.task["id"])

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tempdir.cleanup()

    def test_matrix_authorization_binds_exact_snapshot_and_is_single_use(self) -> None:
        from app_core.controlled_publish import (
            authorize_completed_check,
            consume_matrix_authorization,
        )

        authorization = authorize_completed_check(self.task["id"], ttl_seconds=600)
        consume_matrix_authorization(
            authorization["authorizationId"], self.task["id"], self.matrix
        )
        with self.assertRaises(ControlledPublishError) as consumed:
            consume_matrix_authorization(
                authorization["authorizationId"], self.task["id"], self.matrix
            )
        self.assertEqual(
            consumed.exception.error_code, "controlled_authorization_consumed"
        )

    def test_matrix_authorization_rejects_changed_effective_title(self) -> None:
        from app_core.controlled_publish import (
            authorize_completed_check,
            consume_matrix_authorization,
        )

        authorization = authorize_completed_check(self.task["id"])
        changed = json.loads(json.dumps(self.matrix, ensure_ascii=False))
        changed["targets"][0]["effective"]["title"] = "被修改"
        with self.assertRaises(ControlledPublishError) as mismatch:
            consume_matrix_authorization(
                authorization["authorizationId"], self.task["id"], changed
            )
        self.assertEqual(
            mismatch.exception.error_code, "controlled_authorization_scope_mismatch"
        )

if __name__ == "__main__":
    unittest.main()
