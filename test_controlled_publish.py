# -*- coding: utf-8 -*-

import json
import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

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
from app_core import controlled_publish, database, publish_service, task_service
from app_core.douyin_graphic_matrix_service import prepare_matrix
from app_core.tiktok_schedule_contract import tiktok_irreversible_evidence_sql


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

    def _seed_tiktok_preflight(
        self,
        root: Path,
        *,
        authorization_count: int,
        target_schedule: Mapping[str, str] | None = None,
    ) -> tuple[Path, dict, list[dict]]:
        manifest = self._tiktok_bundle(root)
        request = self._tiktok_request(manifest)
        request["targets"][0]["schedule"] = (
            dict(target_schedule) if target_schedule is not None else None
        )
        preflight_payloads = build_controlled_payloads(
            request,
            accounts=[self._tiktok_account()],
        )
        preflight_payload = preflight_payloads[0]
        preflight = task_service.create_pending_task(
            preflight_payloads,
            mode="oneclick_preflight",
        )
        task_service.mark_platform_result(
            preflight["id"],
            6,
            ok=True,
            message="TikTok 本地预检通过",
            content_type="video",
            event_type="tiktok_local_preflight_passed",
            receipt={
                "accountId": 61,
                "visibility": "public",
                "platformWriteOccurred": False,
                "finalActionTriggered": False,
                "phase": "local_preflight_passed",
                "scheduleMode": preflight_payload.get("scheduleMode"),
                "scheduledAt": preflight_payload.get("scheduledAt"),
                "scheduleTimezone": preflight_payload.get("scheduleTimezone"),
            },
        )
        grants = []
        with database.connect() as conn:
            for _ in range(authorization_count):
                grants.append(
                    create_authorization(
                        conn,
                        preflight["id"],
                        preflight_payloads,
                        ttl_seconds=600,
                    )
                )
        return manifest, preflight, grants

    @staticmethod
    def _claim_count(conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM tiktok_controlled_execution_claims"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0])

    def _assert_tiktok_startup_failure(
        self,
        task_id: int,
        *,
        authorization_id: str | None = None,
    ) -> None:
        with database.connect() as conn:
            task = conn.execute(
                "SELECT status FROM publish_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            item = conn.execute(
                "SELECT status, errorCode FROM publish_task_items WHERE taskId = ?",
                (task_id,),
            ).fetchone()
            claim_count = conn.execute(
                "SELECT COUNT(*) FROM tiktok_controlled_execution_claims WHERE taskId = ?",
                (task_id,),
            ).fetchone()[0]
            failure = conn.execute(
                "SELECT eventType FROM publish_task_events "
                "WHERE taskId = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            irreversible = conn.execute(
                f"""
                SELECT {tiktok_irreversible_evidence_sql('task.id')} AS present
                FROM publish_tasks AS task
                WHERE task.id = ?
                """,
                (task_id,),
            ).fetchone()
            authorization = None
            if authorization_id is not None:
                authorization = conn.execute(
                    "SELECT consumedAt FROM controlled_publish_authorizations "
                    "WHERE authorizationId = ?",
                    (authorization_id,),
                ).fetchone()

        self.assertEqual(task["status"], "failed")
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["errorCode"], "tiktok_worker_start_failed")
        self.assertEqual(claim_count, 0)
        self.assertEqual(failure["eventType"], "tiktok_worker_start_failed")
        self.assertEqual(irreversible["present"], 0)
        if authorization_id is not None:
            self.assertIsNotNone(authorization["consumedAt"])

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

    def test_tiktok_target_accepts_one_beijing_platform_schedule(self) -> None:
        now = datetime(2026, 8, 29, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._tiktok_bundle(Path(temporary))
            request = self._tiktok_request(manifest)
            request["targets"][0]["schedule"] = {
                "localTime": "2026-08-29 15:00",
                "timezone": "Asia/Shanghai",
            }
            payload = build_controlled_payloads(
                request,
                accounts=[self._tiktok_account()],
                schedule_now=now,
            )[0]

        self.assertTrue(payload["enableTimer"])
        self.assertEqual(payload["scheduleTime"], "2026-08-29 15:00")
        self.assertEqual(payload["dailyTimes"], ["15:00"])
        self.assertEqual(payload["scheduleMode"], "platform_native")
        self.assertEqual(payload["scheduledAt"], "2026-08-29 15:00")
        self.assertEqual(payload["tiktokExecutionIntent"], "formal_public")

    def test_tiktok_schedule_change_invalidates_preflight_authorization_without_consuming_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ), patch.object(
            controlled_publish,
            "_shanghai_now",
            return_value=datetime(
                2026,
                8,
                29,
                14,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        ):
            database.ensure_schema()
            schedule = {
                "localTime": "2026-08-29 15:00",
                "timezone": "Asia/Shanghai",
            }
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=1,
                target_schedule=schedule,
            )
            formal = self._tiktok_request(
                manifest,
                mode="formal",
                confirmedPreflightTaskId=preflight["id"],
                authorizationId=grants[0]["authorizationId"],
            )
            formal["targets"][0]["schedule"] = {
                "localTime": "2026-08-29 15:01",
                "timezone": "Asia/Shanghai",
            }
            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), self.assertRaises(ControlledPublishError) as raised:
                submit_request(formal)
            with database.connect() as conn:
                authorization = conn.execute(
                    "SELECT consumedAt FROM controlled_publish_authorizations WHERE authorizationId = ?",
                    (grants[0]["authorizationId"],),
                ).fetchone()
                formal_tasks = conn.execute(
                    "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
                ).fetchone()[0]

        self.assertEqual(
            raised.exception.error_code,
            "controlled_authorization_scope_mismatch",
        )
        self.assertIsNone(authorization["consumedAt"])
        self.assertEqual(formal_tasks, 0)

    def test_tiktok_schedule_rejects_creation_window_boundaries(self) -> None:
        now = datetime(2026, 8, 29, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        cases = (
            ("29_minutes", "2026-08-29 14:29"),
            ("10_days_plus_1_minute", "2026-09-08 14:01"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._tiktok_bundle(Path(temporary))
            for label, local_time in cases:
                request = self._tiktok_request(manifest)
                request["targets"][0]["schedule"] = {
                    "localTime": local_time,
                    "timezone": "Asia/Shanghai",
                }
                with self.subTest(label=label), self.assertRaises(
                    ControlledPublishError
                ) as raised:
                    build_controlled_payloads(
                        request,
                        accounts=[self._tiktok_account()],
                        schedule_now=now,
                    )
                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_schedule_out_of_range",
                )

    def test_tiktok_schedule_rejects_wrong_timezone_and_extra_key(self) -> None:
        now = datetime(2026, 8, 29, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        cases = (
            ("empty", {}),
            (
                "timezone",
                {
                    "localTime": "2026-08-29 15:00",
                    "timezone": "UTC",
                },
            ),
            (
                "extra_key",
                {
                    "localTime": "2026-08-29 15:00",
                    "timezone": "Asia/Shanghai",
                    "fold": "0",
                },
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._tiktok_bundle(Path(temporary))
            for label, schedule in cases:
                request = self._tiktok_request(manifest)
                request["targets"][0]["schedule"] = schedule
                with self.subTest(label=label), self.assertRaises(
                    ControlledPublishError
                ) as raised:
                    build_controlled_payloads(
                        request,
                        accounts=[self._tiktok_account()],
                        schedule_now=now,
                    )
                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_schedule_invalid",
                )

    def test_tiktok_immediate_schedule_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._tiktok_bundle(Path(temporary))
            payload = build_controlled_payloads(
                self._tiktok_request(manifest),
                accounts=[self._tiktok_account()],
            )[0]

        self.assertFalse(payload["enableTimer"])
        self.assertIsNone(payload["scheduleTime"])
        self.assertEqual(payload["dailyTimes"], [])
        self.assertEqual(payload["scheduleMode"], "immediate")
        self.assertIsNone(payload["scheduledAt"])
        self.assertEqual(payload["tiktokExecutionIntent"], "formal_public")

    def test_tiktok_forbidden_schedule_aliases_fail_before_task_creation(self) -> None:
        aliases = (
            ("root_scheduledAt", "root", "scheduledAt", "2026-08-29 15:00"),
            ("root_publishAt", "root", "publishAt", "2026-08-29 15:00"),
            (
                "nested_scheduleTime",
                "schedule",
                "scheduleTime",
                "2026-08-29 15:00",
            ),
            (
                "nested_scheduledAt",
                "schedule",
                "scheduledAt",
                "2026-08-29 15:00",
            ),
            (
                "nested_publishAt",
                "schedule",
                "publishAt",
                "2026-08-29 15:00",
            ),
            ("runtime_enableTimer", "target", "enableTimer", True),
            (
                "runtime_scheduleTime",
                "target",
                "scheduleTime",
                "2026-08-29 15:00",
            ),
            (
                "runtime_scheduleTimezone",
                "target",
                "scheduleTimezone",
                "Asia/Shanghai",
            ),
            ("runtime_dailyTimes", "target", "dailyTimes", ["15:00"]),
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest = self._tiktok_bundle(Path(temporary))
            for label, location, key, value in aliases:
                request = self._tiktok_request(manifest)
                if location == "root":
                    request[key] = value
                elif location == "schedule":
                    request["targets"][0]["schedule"] = {
                        "localTime": "2026-08-29 15:00",
                        "timezone": "Asia/Shanghai",
                        key: value,
                    }
                else:
                    request["targets"][0][key] = value
                with patch(
                    "app_core.account_service.list_publishable_accounts",
                    return_value=[self._tiktok_account()],
                ), self.subTest(label=label), self.assertRaises(
                    ControlledPublishError
                ) as raised:
                    submit_request(request)
                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_schedule_invalid",
                )
            with database.connect() as conn:
                task_count = conn.execute(
                    "SELECT COUNT(*) FROM publish_tasks"
                ).fetchone()[0]

        self.assertEqual(task_count, 0)

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
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest = self._tiktok_bundle(Path(temporary))
            request = self._tiktok_request(
                manifest,
                mode="platform_form_check",
                platformFormCheckConfirmed=True,
            )

            def start(task_id: int):
                return task_service.get_task(task_id)

            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=start,
            ), patch(
                "app_core.controlled_publish.create_authorization"
            ) as create_formal, patch(
                "app_core.controlled_publish.consume_authorization"
            ) as consume_formal, patch(
                "app_core.controlled_publish.consume_direct_authorization"
            ) as consume_direct:
                result = submit_request(request)
            with database.connect() as conn:
                claim = conn.execute(
                    """
                    SELECT taskId, mode, state, scopeFingerprint
                    FROM tiktok_controlled_execution_claims
                    """
                ).fetchone()

        self.assertEqual(result["phase"], "platform_form_check")
        self.assertEqual(claim["taskId"], result["taskId"])
        self.assertEqual(claim["mode"], "platform_form_check")
        self.assertEqual(claim["state"], "claimed")
        create_formal.assert_not_called()
        consume_formal.assert_not_called()
        consume_direct.assert_not_called()

    def test_tiktok_claim_allows_only_one_worker_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest = self._tiktok_bundle(Path(temporary))
            request = self._tiktok_request(
                manifest,
                mode="platform_form_check",
                platformFormCheckConfirmed=True,
            )
            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=lambda task_id: task_service.get_task(task_id),
            ):
                created = submit_request(request)

            starts: list[int] = []
            constructed_states: list[str] = []

            class Worker:
                def __init__(self, *, target, args, daemon, name):
                    self.task_id = int(args[0]["id"])
                    with database.connect() as conn:
                        state = conn.execute(
                            "SELECT state FROM tiktok_controlled_execution_claims WHERE taskId = ?",
                            (self.task_id,),
                        ).fetchone()[0]
                    constructed_states.append(state)

                def start(self):
                    starts.append(self.task_id)

            with patch.object(
                publish_service,
                "_validate_payloads",
                side_effect=lambda payloads: [dict(payloads[0])],
            ), patch.object(publish_service.threading, "Thread", Worker):
                publish_service.start_controlled_tiktok_publish(created["taskId"])
                with self.assertRaisesRegex(ValueError, "claim.*已经启动"):
                    publish_service.start_controlled_tiktok_publish(created["taskId"])

            with database.connect() as conn:
                claim = conn.execute(
                    "SELECT state FROM tiktok_controlled_execution_claims WHERE taskId = ?",
                    (created["taskId"],),
                ).fetchone()
                task = conn.execute(
                    "SELECT status FROM publish_tasks WHERE id = ?",
                    (created["taskId"],),
                ).fetchone()
                startup_failures = conn.execute(
                    """
                    SELECT COUNT(*) FROM publish_task_events
                    WHERE taskId = ? AND eventType = 'tiktok_worker_start_failed'
                    """,
                    (created["taskId"],),
                ).fetchone()[0]

        self.assertEqual(starts, [created["taskId"]])
        self.assertEqual(constructed_states, ["claimed", "started"])
        self.assertEqual(claim["state"], "started")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(startup_failures, 0)

    def test_two_tiktok_formal_authorizations_compete_for_one_atomic_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=2,
            )
            barrier = threading.Barrier(2)
            outcomes: list[tuple[str, object]] = []
            outcome_lock = threading.Lock()

            def start_claimed(task_id: int):
                return task_service.get_task(task_id)

            def submit(grant: dict) -> None:
                request = self._tiktok_request(
                    manifest,
                    mode="formal",
                    confirmedPreflightTaskId=preflight["id"],
                    authorizationId=grant["authorizationId"],
                )
                barrier.wait()
                try:
                    result = submit_request(request)
                except ControlledPublishError as exc:
                    outcome = ("error", exc.error_code)
                else:
                    outcome = ("ok", result["taskId"])
                with outcome_lock:
                    outcomes.append(outcome)

            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_desktop_publish",
                return_value={
                    "id": 900,
                    "taskNo": "legacy-bypass",
                    "mode": "oneclick_publish",
                    "status": "pending",
                    "payloadJson": "[]",
                    "items": [],
                    "events": [],
                },
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=start_claimed,
                create=True,
            ):
                threads = [threading.Thread(target=submit, args=(grant,)) for grant in grants]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            with database.connect() as conn:
                formal_tasks = conn.execute(
                    "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
                ).fetchone()[0]
                consumed = conn.execute(
                    "SELECT COUNT(*) FROM controlled_publish_authorizations WHERE consumedAt IS NOT NULL"
                ).fetchone()[0]
                claims = self._claim_count(conn)

        self.assertEqual(sum(kind == "ok" for kind, _ in outcomes), 1)
        self.assertEqual(sum(kind == "error" for kind, _ in outcomes), 1)
        self.assertEqual(formal_tasks, 1)
        self.assertEqual(consumed, 1)
        self.assertEqual(claims, 1)

    def test_tiktok_atomic_task_creation_rolls_back_authorization_when_insert_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=1,
            )
            request = self._tiktok_request(
                manifest,
                mode="formal",
                confirmedPreflightTaskId=preflight["id"],
                authorizationId=grants[0]["authorizationId"],
            )
            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.task_service._insert_pending_task",
                side_effect=RuntimeError("forced insert failure"),
            ):
                with self.assertRaises(RuntimeError):
                    submit_request(request)

            with database.connect() as conn:
                authorization = conn.execute(
                    "SELECT consumedAt FROM controlled_publish_authorizations WHERE authorizationId = ?",
                    (grants[0]["authorizationId"],),
                ).fetchone()
                formal_tasks = conn.execute(
                    "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
                ).fetchone()[0]
                claims = self._claim_count(conn)

        self.assertIsNone(authorization["consumedAt"])
        self.assertEqual(formal_tasks, 0)
        self.assertEqual(claims, 0)

    def test_tiktok_worker_start_failure_after_claim_marks_failed_and_releases_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=1,
            )
            request = self._tiktok_request(
                manifest,
                mode="formal",
                confirmedPreflightTaskId=preflight["id"],
                authorizationId=grants[0]["authorizationId"],
            )
            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=lambda task_id: task_service.get_task(task_id),
            ):
                created = submit_request(request)

            class Worker:
                def __init__(self, *, target, args, daemon, name):
                    self.task_id = int(args[0]["id"])
                    with database.connect() as conn:
                        state = conn.execute(
                            "SELECT state FROM tiktok_controlled_execution_claims WHERE taskId = ?",
                            (self.task_id,),
                        ).fetchone()[0]
                    if state != "claimed":
                        raise AssertionError(
                            "thread constructed after claim became started"
                        )

                def start(self):
                    with database.connect() as conn:
                        state = conn.execute(
                            "SELECT state FROM tiktok_controlled_execution_claims WHERE taskId = ?",
                            (self.task_id,),
                        ).fetchone()[0]
                    if state != "started":
                        raise AssertionError("worker.start called before claim became started")
                    raise RuntimeError("forced worker start failure")

            with patch.object(
                publish_service,
                "_validate_payloads",
                side_effect=lambda payloads: [dict(payloads[0])],
            ), patch.object(publish_service.threading, "Thread", Worker):
                with self.assertRaises(RuntimeError):
                    publish_service.start_controlled_tiktok_publish(
                        created["taskId"]
                    )

            with database.connect() as conn:
                formal_task = conn.execute(
                    "SELECT id, status FROM publish_tasks WHERE id = ?",
                    (created["taskId"],),
                ).fetchone()
                authorization = conn.execute(
                    "SELECT consumedAt FROM controlled_publish_authorizations WHERE authorizationId = ?",
                    (grants[0]["authorizationId"],),
                ).fetchone()
                claims = self._claim_count(conn)
                failure = conn.execute(
                    """
                    SELECT eventType FROM publish_task_events
                    WHERE taskId = ? ORDER BY id DESC LIMIT 1
                    """,
                    (created["taskId"],),
                ).fetchone()

        self.assertIsNotNone(formal_task)
        self.assertEqual(formal_task["status"], "failed")
        self.assertIsNotNone(authorization["consumedAt"])
        self.assertEqual(claims, 0)
        self.assertEqual(failure["eventType"], "tiktok_worker_start_failed")

    def test_new_formal_submit_reclaims_expired_dead_worker_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=2,
            )

            def request_for(grant: dict) -> dict:
                return self._tiktok_request(
                    manifest,
                    mode="formal",
                    confirmedPreflightTaskId=preflight["id"],
                    authorizationId=grant["authorizationId"],
                )

            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=lambda task_id: task_service.get_task(task_id),
            ):
                first = submit_request(request_for(grants[0]))
                with database.connect() as conn:
                    expired = (
                        datetime.now() - timedelta(minutes=5)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        """
                        UPDATE publish_tasks
                        SET status = 'running', workerPid = 99999999,
                            workerHeartbeatAt = ?, startedAt = ?
                        WHERE id = ?
                        """,
                        (expired, expired, first["taskId"]),
                    )
                    conn.execute(
                        "UPDATE publish_task_items SET status = 'running' WHERE taskId = ?",
                        (first["taskId"],),
                    )
                    conn.execute(
                        "UPDATE tiktok_controlled_execution_claims SET state = 'started' WHERE taskId = ?",
                        (first["taskId"],),
                    )
                    conn.commit()

                second = submit_request(request_for(grants[1]))

            old = task_service.get_task(first["taskId"])
            with database.connect() as conn:
                claim = conn.execute(
                    "SELECT taskId, state FROM tiktok_controlled_execution_claims WHERE mode = 'formal'",
                ).fetchone()
                second_authorization = conn.execute(
                    "SELECT consumedAt FROM controlled_publish_authorizations WHERE authorizationId = ?",
                    (grants[1]["authorizationId"],),
                ).fetchone()

        self.assertNotEqual(second["taskId"], first["taskId"])
        self.assertEqual(old["status"], "failed")
        self.assertEqual(
            old["items"][0]["errorCode"],
            "controlled_worker_lease_expired",
        )
        self.assertEqual(
            old["events"][-1]["eventType"],
            "controlled_worker_lease_expired",
        )
        self.assertEqual(claim["taskId"], second["taskId"])
        self.assertEqual(claim["state"], "claimed")
        self.assertIsNotNone(second_authorization["consumedAt"])

    def test_tiktok_formal_validation_failure_before_worker_terminalizes_claimed_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=1,
            )
            request = self._tiktok_request(
                manifest,
                mode="formal",
                confirmedPreflightTaskId=preflight["id"],
                authorizationId=grants[0]["authorizationId"],
            )

            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=lambda task_id: task_service.get_task(task_id),
            ):
                first = submit_request(request)

            with patch.object(
                publish_service,
                "_validate_payloads",
                side_effect=ValueError("local validation failed"),
            ), patch.object(
                publish_service.threading,
                "Thread",
                side_effect=AssertionError("worker must not be constructed"),
            ) as worker:
                with self.assertRaisesRegex(ValueError, "local validation failed"):
                    publish_service.start_controlled_tiktok_publish(first["taskId"])

            worker.assert_not_called()
            self._assert_tiktok_startup_failure(
                first["taskId"],
                authorization_id=grants[0]["authorizationId"],
            )

    def test_tiktok_platform_form_check_validation_failure_before_worker_terminalizes_claimed_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest = self._tiktok_bundle(Path(temporary))
            request = self._tiktok_request(
                manifest,
                mode="platform_form_check",
                platformFormCheckConfirmed=True,
            )
            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=lambda task_id: task_service.get_task(task_id),
            ):
                created = submit_request(request)

            with patch.object(
                publish_service,
                "_validate_payloads",
                side_effect=ValueError("local validation failed"),
            ), patch.object(
                publish_service.threading,
                "Thread",
                side_effect=AssertionError("worker must not be constructed"),
            ) as worker:
                with self.assertRaisesRegex(ValueError, "local validation failed"):
                    publish_service.start_controlled_tiktok_publish(created["taskId"])

            worker.assert_not_called()
            self._assert_tiktok_startup_failure(created["taskId"])

    def test_tiktok_formal_runtime_mode_mismatch_before_worker_terminalizes_claimed_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=1,
            )
            request = self._tiktok_request(
                manifest,
                mode="formal",
                confirmedPreflightTaskId=preflight["id"],
                authorizationId=grants[0]["authorizationId"],
            )
            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=lambda task_id: task_service.get_task(task_id),
            ):
                created = submit_request(request)

            with patch.object(
                publish_service,
                "_validate_payloads",
                side_effect=lambda payloads: [
                    {**payloads[0], "runtimeMode": "platform_form_check"}
                ],
            ), patch.object(
                publish_service.threading,
                "Thread",
                side_effect=AssertionError("worker must not be constructed"),
            ) as worker:
                with self.assertRaisesRegex(ValueError, "模式与 claim 不一致"):
                    publish_service.start_controlled_tiktok_publish(created["taskId"])

            worker.assert_not_called()
            self._assert_tiktok_startup_failure(
                created["taskId"],
                authorization_id=grants[0]["authorizationId"],
            )

    def test_new_formal_submit_does_not_reclaim_live_worker_after_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=2,
            )

            def request_for(grant: dict) -> dict:
                return self._tiktok_request(
                    manifest,
                    mode="formal",
                    confirmedPreflightTaskId=preflight["id"],
                    authorizationId=grant["authorizationId"],
                )

            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=lambda task_id: task_service.get_task(task_id),
            ):
                first = submit_request(request_for(grants[0]))
                with database.connect() as conn:
                    expired = (
                        datetime.now() - timedelta(minutes=5)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        """
                        UPDATE publish_tasks
                        SET status = 'running', workerPid = ?,
                            workerHeartbeatAt = ?, startedAt = ?
                        WHERE id = ?
                        """,
                        (os.getpid(), expired, expired, first["taskId"]),
                    )
                    conn.execute(
                        "UPDATE publish_task_items SET status = 'running' WHERE taskId = ?",
                        (first["taskId"],),
                    )
                    conn.execute(
                        "UPDATE tiktok_controlled_execution_claims SET state = 'started' WHERE taskId = ?",
                        (first["taskId"],),
                    )
                    conn.commit()

                with self.assertRaises(ControlledPublishError) as blocked:
                    submit_request(request_for(grants[1]))

            with database.connect() as conn:
                second_authorization = conn.execute(
                    "SELECT consumedAt FROM controlled_publish_authorizations WHERE authorizationId = ?",
                    (grants[1]["authorizationId"],),
                ).fetchone()
                claim = conn.execute(
                    "SELECT taskId, state FROM tiktok_controlled_execution_claims WHERE mode = 'formal'",
                ).fetchone()
            old = task_service.get_task(first["taskId"])

        self.assertEqual(
            blocked.exception.error_code,
            "controlled_publish_in_progress",
        )
        self.assertEqual(old["status"], "running")
        self.assertIsNone(second_authorization["consumedAt"])
        self.assertEqual(claim["taskId"], first["taskId"])
        self.assertEqual(claim["state"], "started")

    def test_tiktok_claim_releases_only_terminal_failure_before_final_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=3,
            )

            def request_for(grant: dict) -> dict:
                return self._tiktok_request(
                    manifest,
                    mode="formal",
                    confirmedPreflightTaskId=preflight["id"],
                    authorizationId=grant["authorizationId"],
                )

            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=lambda task_id: task_service.get_task(task_id),
            ):
                first = submit_request(request_for(grants[0]))
                task_service.fail_active_task(
                    first["taskId"],
                    error_code="tiktok_platform_execution_failed",
                    message="TikTok 最终动作前失败",
                )
                second = submit_request(request_for(grants[1]))
                task_service.record_task_event(
                    second["taskId"],
                    "tiktok_final_action_triggered",
                    "TikTok 最终动作已触发",
                )
                task_service.fail_active_task(
                    second["taskId"],
                    error_code="tiktok_publish_outcome_unknown",
                    message="TikTok 最终动作后结果不明",
                )
                with self.assertRaises(ControlledPublishError) as blocked:
                    submit_request(request_for(grants[2]))

            with database.connect() as conn:
                formal_tasks = conn.execute(
                    "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
                ).fetchone()[0]
                claim = conn.execute(
                    "SELECT taskId FROM tiktok_controlled_execution_claims WHERE mode = 'formal'"
                ).fetchone()
                third = conn.execute(
                    "SELECT consumedAt FROM controlled_publish_authorizations WHERE authorizationId = ?",
                    (grants[2]["authorizationId"],),
                ).fetchone()

        self.assertEqual(blocked.exception.error_code, "controlled_publish_outcome_ambiguous")
        self.assertEqual(formal_tasks, 2)
        self.assertEqual(claim["taskId"], second["taskId"])
        self.assertIsNone(third["consumedAt"])

    def test_scheduled_item_only_ambiguous_evidence_never_releases_formal_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=2,
                target_schedule={
                    "localTime": "2026-08-30 09:00",
                    "timezone": "Asia/Shanghai",
                },
            )

            def request_for(grant: dict) -> dict:
                return self._tiktok_request(
                    manifest,
                    mode="formal",
                    confirmedPreflightTaskId=preflight["id"],
                    authorizationId=grant["authorizationId"],
                    targets=[
                        {
                            "platform": "TikTok",
                            "accountId": 61,
                            "schedule": {
                                "localTime": "2026-08-30 09:00",
                                "timezone": "Asia/Shanghai",
                            },
                            "settings": {"visibility": "public"},
                        }
                    ],
                )

            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=lambda task_id: task_service.get_task(task_id),
            ):
                first = submit_request(request_for(grants[0]))
                task_service.fail_active_task(
                    first["taskId"],
                    error_code="tiktok_schedule_outcome_unknown",
                    message="TikTok 定时最终动作后结果不明",
                    receipt={
                        "scheduleMode": "platform_native",
                        "scheduledAt": "2026-08-30 09:00",
                        "scheduleTimezone": "Asia/Shanghai",
                        "finalActionTriggered": True,
                        "phase": "ambiguous",
                        "publishedAt": None,
                    },
                )
                with self.assertRaises(ControlledPublishError) as blocked:
                    submit_request(request_for(grants[1]))

            with database.connect() as conn:
                formal_tasks = conn.execute(
                    "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
                ).fetchone()[0]
                claim = conn.execute(
                    "SELECT taskId FROM tiktok_controlled_execution_claims WHERE mode = 'formal'"
                ).fetchone()
                authorization = conn.execute(
                    "SELECT consumedAt FROM controlled_publish_authorizations WHERE authorizationId = ?",
                    (grants[1]["authorizationId"],),
                ).fetchone()

        self.assertEqual(
            blocked.exception.error_code,
            "controlled_publish_outcome_ambiguous",
        )
        self.assertEqual(formal_tasks, 1)
        self.assertEqual(claim["taskId"], first["taskId"])
        self.assertIsNone(authorization["consumedAt"])

    def test_scheduled_receipt_only_acceptance_never_releases_formal_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=2,
                target_schedule={
                    "localTime": "2026-08-30 09:00",
                    "timezone": "Asia/Shanghai",
                },
            )

            def request_for(grant: dict) -> dict:
                request = self._tiktok_request(
                    manifest,
                    mode="formal",
                    confirmedPreflightTaskId=preflight["id"],
                    authorizationId=grant["authorizationId"],
                )
                request["targets"][0]["schedule"] = {
                    "localTime": "2026-08-30 09:00",
                    "timezone": "Asia/Shanghai",
                }
                return request

            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), patch(
                "app_core.publish_service.start_controlled_tiktok_publish",
                side_effect=lambda task_id: task_service.get_task(task_id),
            ):
                first = submit_request(request_for(grants[0]))
                task_service.fail_active_task(
                    first["taskId"],
                    error_code="tiktok_platform_execution_failed",
                    message="TikTok 回读后收口失败",
                    receipt={
                        "scheduleMode": "platform_native",
                        "scheduledAt": "2026-08-30 09:00",
                        "scheduleTimezone": "Asia/Shanghai",
                        "platformAccepted": True,
                        "phase": "scheduled_accepted",
                        "publishedAt": None,
                    },
                )
                with self.assertRaises(ControlledPublishError) as blocked:
                    submit_request(request_for(grants[1]))

            with database.connect() as conn:
                claim = conn.execute(
                    "SELECT taskId FROM tiktok_controlled_execution_claims WHERE mode = 'formal'"
                ).fetchone()
                authorization = conn.execute(
                    "SELECT consumedAt FROM controlled_publish_authorizations WHERE authorizationId = ?",
                    (grants[1]["authorizationId"],),
                ).fetchone()

        self.assertEqual(
            blocked.exception.error_code,
            "controlled_publish_outcome_ambiguous",
        )
        self.assertEqual(claim["taskId"], first["taskId"])
        self.assertIsNone(authorization["consumedAt"])

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
                ("tiktokExpectedAccountReference", "other.user"),
            ):
                mutations.append([{**base[0], key: value}])

        self.assertNotEqual(scope_fingerprint(base), scope_fingerprint(form_check))
        for changed in mutations:
            self.assertNotEqual(scope_fingerprint(base), scope_fingerprint(changed))

    def test_tiktok_fingerprint_binds_normalized_schedule_fields(self) -> None:
        now = datetime(2026, 8, 29, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._tiktok_bundle(Path(temporary))
            request = self._tiktok_request(manifest)
            request["targets"][0]["schedule"] = {
                "localTime": "2026-08-29 15:00",
                "timezone": "Asia/Shanghai",
            }
            scheduled = build_controlled_payloads(
                request,
                accounts=[self._tiktok_account()],
                schedule_now=now,
            )
            wrong_mode = [
                {**scheduled[0], "scheduleMode": "immediate"}
            ]
            wrong_projection = [
                {**scheduled[0], "scheduledAt": "2026-08-29 15:01"}
            ]

        self.assertNotEqual(
            scope_fingerprint(scheduled),
            scope_fingerprint(wrong_mode),
        )
        self.assertNotEqual(
            scope_fingerprint(scheduled),
            scope_fingerprint(wrong_projection),
        )

    def test_tiktok_fingerprint_uses_video_bytes_not_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir()
            second_root.mkdir()
            first_manifest = self._tiktok_bundle(first_root)
            second_manifest = self._tiktok_bundle(second_root)
            first = build_controlled_payloads(
                self._tiktok_request(first_manifest),
                accounts=[self._tiktok_account()],
            )
            second = build_controlled_payloads(
                self._tiktok_request(second_manifest),
                accounts=[self._tiktok_account()],
            )

        self.assertNotEqual(first[0]["fileList"], second[0]["fileList"])
        self.assertEqual(first[0]["tiktokVideoSha256"], second[0]["tiktokVideoSha256"])
        self.assertEqual(scope_fingerprint(first), scope_fingerprint(second))

    def test_tiktok_fingerprint_changes_for_new_bytes_at_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._tiktok_bundle(root)
            first = build_controlled_payloads(
                self._tiktok_request(manifest),
                accounts=[self._tiktok_account()],
            )
            (root / "tiktok.mp4").write_bytes(b"replacement-video")
            second = build_controlled_payloads(
                self._tiktok_request(manifest),
                accounts=[self._tiktok_account()],
            )

        self.assertEqual(first[0]["fileList"], second[0]["fileList"])
        self.assertNotEqual(first[0]["tiktokVideoSha256"], second[0]["tiktokVideoSha256"])
        self.assertNotEqual(scope_fingerprint(first), scope_fingerprint(second))

    def test_tiktok_video_bytes_are_frozen_into_payload_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._tiktok_bundle(root)
            preflight = build_controlled_payloads(
                self._tiktok_request(manifest),
                accounts=[self._tiktok_account()],
            )
            expected = hashlib.sha256(b"tiktok-video").hexdigest()
            (root / "tiktok.mp4").write_bytes(b"changed-after-preflight")
            formal = build_controlled_payloads(
                self._tiktok_request(
                    manifest,
                    mode="formal",
                    confirmedPreflightTaskId=71,
                    authorizationId="authorization",
                ),
                accounts=[self._tiktok_account()],
            )

        self.assertEqual(preflight[0]["tiktokVideoSha256"], expected)
        self.assertNotEqual(
            preflight[0]["tiktokVideoSha256"],
            formal[0]["tiktokVideoSha256"],
        )
        self.assertNotEqual(scope_fingerprint(preflight), scope_fingerprint(formal))

    def test_tiktok_formal_rejects_video_mutation_without_consuming_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            database,
            "DB_PATH",
            Path(temporary) / "database.db",
        ):
            database.ensure_schema()
            manifest, preflight, grants = self._seed_tiktok_preflight(
                Path(temporary),
                authorization_count=1,
            )
            (Path(temporary) / "tiktok.mp4").write_bytes(b"mutated-video")
            request = self._tiktok_request(
                manifest,
                mode="formal",
                confirmedPreflightTaskId=preflight["id"],
                authorizationId=grants[0]["authorizationId"],
            )
            with patch(
                "app_core.account_service.list_publishable_accounts",
                return_value=[self._tiktok_account()],
            ), self.assertRaises(ControlledPublishError) as raised:
                submit_request(request)
            with database.connect() as conn:
                authorization = conn.execute(
                    "SELECT consumedAt FROM controlled_publish_authorizations WHERE authorizationId = ?",
                    (grants[0]["authorizationId"],),
                ).fetchone()
                formal_tasks = conn.execute(
                    "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
                ).fetchone()[0]
                claims = self._claim_count(conn)

        self.assertEqual(
            raised.exception.error_code,
            "controlled_authorization_scope_mismatch",
        )
        self.assertIsNone(authorization["consumedAt"])
        self.assertEqual(formal_tasks, 0)
        self.assertEqual(claims, 0)

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

    def test_tiktok_project_task_projects_canonical_scheduled_at(self) -> None:
        projected = project_task(
            {
                "id": 62,
                "taskNo": "T62",
                "mode": "oneclick_publish",
                "status": "pending",
                "payloadJson": json.dumps(
                    [
                        {
                            "type": 6,
                            "accountIds": [61],
                            "scheduleMode": "platform_native",
                            "scheduledAt": "2026-08-29 15:00",
                        }
                    ],
                    ensure_ascii=False,
                ),
                "items": [
                    {
                        "platformType": 6,
                        "status": "pending",
                        "accountLabel": "TikTok saved account",
                    }
                ],
                "events": [],
            }
        )

        self.assertEqual(
            projected["platforms"][0]["scheduledAt"],
            "2026-08-29 15:00",
        )

    def test_tiktok_scheduled_receipt_does_not_claim_published_at(self) -> None:
        projected = project_task(
            {
                "id": 63,
                "taskNo": "T63",
                "mode": "oneclick_publish",
                "status": "success",
                "payloadJson": json.dumps(
                    [
                        {
                            "type": 6,
                            "accountIds": [61],
                            "scheduleMode": "platform_native",
                            "scheduledAt": "2026-08-29 15:00",
                        }
                    ],
                    ensure_ascii=False,
                ),
                "items": [
                    {
                        "platformType": 6,
                        "status": "success",
                        "receiptJson": json.dumps(
                            {
                                "phase": "platform_accepted",
                                "scheduledAt": "2026-08-29 15:00",
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
                "events": [
                    {"eventType": "tiktok_platform_accepted"}
                ],
            }
        )

        receipt = projected["platforms"][0]["receipt"]
        self.assertEqual(receipt["scheduledAt"], "2026-08-29 15:00")
        self.assertNotIn("publishedAt", receipt)

    def test_tiktok_schedule_lifecycle_projects_before_generic_final_action(self) -> None:
        payload = json.dumps(
            [
                {
                    "type": 6,
                    "accountIds": [61],
                    "scheduleMode": "platform_native",
                    "scheduledAt": "2026-08-29 15:00",
                }
            ],
            ensure_ascii=False,
        )
        base = {
            "id": 631,
            "taskNo": "T631",
            "mode": "oneclick_publish",
            "status": "running",
            "payloadJson": payload,
        }
        event_only = project_task(
            {
                **base,
                "items": [{"platformType": 6, "status": "running"}],
                "events": [
                    {"eventType": "tiktok_final_action_triggered"},
                    {"eventType": "tiktok_scheduled_accepted"},
                ],
            }
        )
        receipt_only = project_task(
            {
                **base,
                "items": [
                    {
                        "platformType": 6,
                        "status": "running",
                        "receiptJson": json.dumps(
                            {
                                "phase": "scheduled_accepted",
                                "publishedAt": None,
                            }
                        ),
                    }
                ],
                "events": [{"eventType": "tiktok_final_action_triggered"}],
            }
        )
        success = project_task(
            {
                **base,
                "status": "success",
                "items": [
                    {
                        "platformType": 6,
                        "status": "success",
                        "receiptJson": json.dumps(
                            {
                                "phase": "scheduled_readback_confirmed",
                                "publishedAt": None,
                            }
                        ),
                    }
                ],
                "events": [
                    {"eventType": "tiktok_final_action_triggered"},
                    {"eventType": "tiktok_scheduled_readback_confirmed"},
                ],
            }
        )

        self.assertEqual(event_only["stage"], "scheduled_accepted")
        self.assertEqual(receipt_only["stage"], "scheduled_accepted")
        self.assertEqual(success["stage"], "scheduled_readback_confirmed")
        self.assertIsNone(success["platforms"][0]["receipt"]["publishedAt"])

    def test_tiktok_immediate_final_action_projection_remains_reconciling(self) -> None:
        projected = project_task(
            {
                "id": 632,
                "taskNo": "T632",
                "mode": "oneclick_publish",
                "status": "running",
                "payloadJson": json.dumps(
                    [{"type": 6, "accountIds": [61], "scheduleMode": "immediate"}]
                ),
                "items": [{"platformType": 6, "status": "running"}],
                "events": [{"eventType": "tiktok_final_action_triggered"}],
            }
        )

        self.assertEqual(projected["stage"], "reconciling")

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

    def test_tiktok_terminal_failures_are_not_overridden_by_progress_events(self) -> None:
        cases = (
            (
                "form_verified_then_failed",
                [{"eventType": "tiktok_platform_form_verified"}],
                "platform_form_verified",
                "tiktok_platform_execution_failed",
            ),
            (
                "explicit_rejection_after_final_action",
                [
                    {"eventType": "tiktok_platform_form_verified"},
                    {"eventType": "tiktok_final_action_triggered"},
                    {"eventType": "tiktok_publish_rejected"},
                ],
                "final_action_triggered",
                "tiktok_publish_rejected",
            ),
        )
        for label, events, receipt_phase, error_code in cases:
            with self.subTest(label=label):
                projected = project_task(
                    {
                        "id": 64,
                        "taskNo": "T64",
                        "mode": "oneclick_publish",
                        "status": "failed",
                        "payloadJson": json.dumps(
                            [{"type": 6, "accountIds": [61]}],
                            ensure_ascii=False,
                        ),
                        "items": [
                            {
                                "platformType": 6,
                                "status": "failed",
                                "errorCode": error_code,
                                "receiptJson": json.dumps(
                                    {"phase": receipt_phase},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        "events": events,
                    }
                )

                self.assertEqual(projected["stage"], "failed")
                self.assertIsNone(projected["userAction"])

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
