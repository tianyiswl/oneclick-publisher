# -*- coding: utf-8 -*-

from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import hashlib
from io import StringIO
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_core import (
    controlled_publish,
    database,
    overseas_browser_publish,
    publish_service,
    task_service,
)
from app_core.controlled_publish import ControlledPublishError
from app_core.controlled_publish_process import (
    submit_authorized_preflight_task,
    submit_douyin_graphic_matrix_request_in_process,
    submit_request_in_process,
)
from app_core.overseas_meta_content import facebook_page_caption_sha256
from uploader.meta_uploader.page_form import (
    FacebookPageFormExpectation,
    FacebookPageFormSnapshot,
)
from utils import publish_tasks


class FacebookPagePublicEntryFixture:
    """One real DB-backed Page authorization, stopped only at worker start."""

    def __init__(self, *, source_kind: str = "managed") -> None:
        if source_kind not in {"managed", "manifest"}:
            raise ValueError("unsupported Page fixture source")
        self.source_kind = source_kind

    def __enter__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "database.db"
        self._patches = [
            patch.object(database, "DB_PATH", self.db_path),
            patch.object(publish_tasks, "DB_PATH", self.db_path),
            patch.object(controlled_publish, "VIDEO_DIR", self.root),
            patch.dict(
                os.environ,
                {
                    "ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1",
                    "QT_QPA_PLATFORM": "offscreen",
                },
                clear=False,
            ),
        ]
        for current in self._patches:
            current.start()
        database.ensure_schema()
        with database.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_info (
                    id, type, filePath, userName, status,
                    profileName, authMode, accountReference
                ) VALUES (91, 9, 'facebook-page.json', ?, 1, ?, 'browser', ?)
                """,
                (
                    "Saved Facebook Page",
                    "Saved Facebook Page",
                    "1000000000001001",
                ),
            )
            conn.commit()
        self.video = self.root / "facebook.mp4"
        self.video.write_bytes(b"facebook-page-public-entry-video")
        self.caption = "Facebook Page 标题\n\nFacebook Page 正文\n\n#OneClick"
        managed_payload = {
            "type": 9,
            "contentType": "video",
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "debugDryRunHoldBrowser": False,
            "backgroundMode": False,
            "title": "Facebook Page 标题",
            "description": "Facebook Page 正文",
            "tags": ["OneClick"],
            "fileList": [str(self.video)],
            "accountList": ["facebook-page.json"],
            "accountIds": [91],
            "coverPath": "",
            "coverPaths": {},
            "facebookControlledPublish": True,
            "facebookExpectedPageReference": "1000000000001001",
            "facebookFinalCaption": self.caption,
            "facebookCaptionSha256": hashlib.sha256(
                self.caption.encode("utf-8")
            ).hexdigest(),
            "facebookVideoSha256": hashlib.sha256(
                self.video.read_bytes()
            ).hexdigest(),
            "facebookVideoSize": self.video.stat().st_size,
            "facebookManifestIntentSha256": "c" * 64,
            "visibility": "public",
            "enableTimer": False,
            "scheduleMode": "immediate",
            "scheduledAt": None,
            "scheduleTime": None,
            "originalDeclaration": False,
            "aiGenerated": False,
        }
        self.manifest: Path | None = None
        if self.source_kind == "manifest":
            body = self.root / "body.md"
            cover = self.root / "cover.png"
            self.manifest = self.root / "manifest.json"
            body.write_text("Facebook Page 正文", encoding="utf-8")
            cover.write_bytes(b"facebook-page-cover")
            self.manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": "oneclick-content/v1",
                        "contentType": "video",
                        "title": "Facebook Page 标题",
                        "bodyFile": body.name,
                        "tags": ["OneClick"],
                        "assets": [self.video.name],
                        "covers": {"3:4": cover.name},
                        "preferredPlatforms": ["Facebook"],
                        "platformOverrides": {
                            "Facebook": {
                                "title": "Facebook Page 标题",
                                "body": "Facebook Page 正文",
                                "tags": ["OneClick"],
                            }
                        },
                        "debugDryRun": True,
                        "publishAllowed": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.payload = controlled_publish.build_controlled_payloads(
                {
                    "projectId": "facebook-page-public-entry",
                    "manifestPath": str(self.manifest),
                    "mode": "preflight",
                    "targets": [
                        {
                            "platform": "Facebook",
                            "accountId": 91,
                            "schedule": None,
                            "settings": {"visibility": "public"},
                        }
                    ],
                },
                accounts=[
                    {
                        "id": 91,
                        "type": 9,
                        "filePath": "facebook-page.json",
                        "profileName": "Saved Facebook Page",
                        "userName": "Saved Facebook Page",
                        "authMode": "browser",
                        "accountReference": "1000000000001001",
                        "status": 1,
                    }
                ],
            )[0]
        else:
            self.payload = managed_payload
        preflight = task_service.create_pending_task(
            [self.payload],
            mode="oneclick_preflight",
        )
        self.preflight_task_id = int(preflight["id"])
        task_service.mark_task_running(
            self.preflight_task_id,
            "Facebook Page public-entry fixture",
        )
        expectation = FacebookPageFormExpectation(
            page_id=str(self.payload["facebookExpectedPageReference"]),
            content_kind="reel",
            video_name=self.video.name,
            video_size=self.video.stat().st_size,
            video_sha256=str(self.payload["facebookVideoSha256"]),
            caption=str(self.payload["facebookFinalCaption"]),
            visibility="public",
        )
        snapshot = FacebookPageFormSnapshot(
            page_id=expectation.page_id,
            content_kind=expectation.content_kind,
            video_name=expectation.video_name,
            video_count=1,
            caption=expectation.caption,
            visibility=expectation.visibility,
            final_action_label="Publish",
            final_action_ready=True,
        )
        receipt = overseas_browser_publish._public_form_receipt(
            {"accountId": 91, "expectation": expectation},
            snapshot,
            phase="platform_form_verified",
            final_action_triggered=False,
        )
        task_service.mark_platform_result(
            self.preflight_task_id,
            9,
            ok=True,
            message="Facebook Page form verified offline",
            content_type="video",
            event_type="facebook_platform_form_verified",
            receipt=receipt,
        )
        self.started_task_ids: list[int] = []
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        for current in reversed(self._patches):
            current.stop()
        self.temporary.cleanup()

    def create_equivalent_preflight(self) -> int:
        """Create a second exact preflight with different source representation."""

        alternate_video = self.root / "renamed-facebook-source.mov"
        alternate_video.write_bytes(self.video.read_bytes())
        alternate_caption = (
            "Facebook Page 标题  \r\n\r\n"
            "Facebook\u00a0Page 正文  \r\n\r\n"
            "#OneClick   "
        )
        payload = {
            **self.payload,
            "title": "Different raw source title",
            "description": "Different raw source body",
            "tags": ["#OneClick", "OneClick", "#OneClick"],
            "fileList": [str(alternate_video)],
            "accountDisplayNames": ["Another local display name"],
            "contentProjectId": "gateway-equivalent-source",
            "facebookFinalCaption": alternate_caption,
            "facebookCaptionSha256": facebook_page_caption_sha256(
                alternate_caption
            ),
            "facebookManifestIntentSha256": "d" * 64,
        }
        preflight = task_service.create_pending_task(
            [payload],
            mode="oneclick_preflight",
        )
        task_id = int(preflight["id"])
        task_service.mark_task_running(
            task_id,
            "Facebook Page equivalent public-entry fixture",
        )
        expectation = FacebookPageFormExpectation(
            page_id=str(payload["facebookExpectedPageReference"]),
            content_kind="reel",
            video_name=alternate_video.name,
            video_size=alternate_video.stat().st_size,
            video_sha256=str(payload["facebookVideoSha256"]),
            caption=str(payload["facebookFinalCaption"]),
            visibility="public",
        )
        snapshot = FacebookPageFormSnapshot(
            page_id=expectation.page_id,
            content_kind=expectation.content_kind,
            video_name=expectation.video_name,
            video_count=1,
            caption=expectation.caption,
            visibility=expectation.visibility,
            final_action_label="Publish",
            final_action_ready=True,
        )
        receipt = overseas_browser_publish._public_form_receipt(
            {"accountId": 91, "expectation": expectation},
            snapshot,
            phase="platform_form_verified",
            final_action_triggered=False,
        )
        task_service.mark_platform_result(
            task_id,
            9,
            ok=True,
            message="Facebook Page equivalent form verified offline",
            content_type="video",
            event_type="facebook_platform_form_verified",
            receipt=receipt,
        )
        return task_id

    def authorize(self, task_id: int | None = None) -> str:
        authorization = controlled_publish.authorize_completed_check(
            int(task_id or self.preflight_task_id)
        )
        return str(authorization["authorizationId"])

    @contextmanager
    def stop_at_worker_start(self):
        def stop_before_worker(
            task_id: int,
            *,
            runtime_video_path: str,
        ) -> dict:
            runtime_path = Path(runtime_video_path)
            if (
                runtime_path.parent != self.root.resolve()
                or not runtime_path.is_file()
            ):
                raise AssertionError("formal worker received the wrong runtime video")
            self.started_task_ids.append(int(task_id))
            return task_service.get_task(int(task_id))

        with patch.object(
            publish_service,
            "start_controlled_facebook_publish",
            side_effect=stop_before_worker,
        ) as start_spy:
            yield start_spy

    def assert_real_dispatch(
        self,
        case: unittest.TestCase,
        envelope: dict,
        start_spy,
        *,
        authorization_id: str | None = None,
    ) -> None:
        task_id = int(envelope["taskId"])
        case.assertEqual(envelope, controlled_publish.task_status(task_id))
        case.assertEqual(self.started_task_ids, [task_id])
        start_spy.assert_called_once_with(
            task_id,
            runtime_video_path=str(self.video.resolve()),
        )
        with database.connect() as conn:
            authorizations = conn.execute(
                """
                SELECT authorizationId, consumedAt
                FROM controlled_publish_authorizations
                WHERE preflightTaskId = ?
                """,
                (self.preflight_task_id,),
            ).fetchall()
            claim = conn.execute(
                """
                SELECT taskId, preflightTaskId, state, blocksReplay
                FROM facebook_page_publish_claims WHERE taskId = ?
                """,
                (task_id,),
            ).fetchone()
        case.assertEqual(len(authorizations), 1)
        if authorization_id is not None:
            case.assertEqual(authorizations[0]["authorizationId"], authorization_id)
        case.assertTrue(str(authorizations[0]["consumedAt"] or ""))
        case.assertIsNotNone(claim)
        case.assertEqual(int(claim["preflightTaskId"]), self.preflight_task_id)
        case.assertEqual(claim["state"], "reserved")
        case.assertEqual(int(claim["blocksReplay"]), 1)
        projected = json.dumps(envelope, ensure_ascii=False).casefold()
        for forbidden in (
            "cookie",
            "password",
            "verificationcode",
            "storage_state",
            "metabrowserpublishconfirmed",
            "metabrowserautomationacknowledged",
            self.caption.casefold(),
        ):
            case.assertNotIn(forbidden, projected)


class ControlledPublishProcessTests(unittest.TestCase):
    @staticmethod
    def _facebook_payload() -> dict:
        return {
            "type": 9,
            "contentType": "video",
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "debugDryRunHoldBrowser": False,
            "backgroundMode": False,
            "title": "Facebook Page 标题",
            "description": "Facebook Page 正文",
            "tags": ["OneClick"],
            "fileList": ["/content/facebook.mp4"],
            "accountList": ["facebook-page.json"],
            "accountIds": [91],
            "facebookControlledPublish": True,
            "facebookExpectedPageReference": "1000000000001001",
            "facebookFinalCaption": "Facebook Page 标题\n\nFacebook Page 正文\n\n#OneClick",
            "facebookCaptionSha256": "a" * 64,
            "facebookVideoSha256": "b" * 64,
            "facebookManifestIntentSha256": "c" * 64,
            "visibility": "public",
            "enableTimer": False,
            "scheduleMode": "immediate",
            "scheduledAt": None,
            "scheduleTime": None,
        }

    def test_authorized_submission_public_signature_only_accepts_preflight_and_authorization(self) -> None:
        signature = inspect.signature(submit_authorized_preflight_task)

        self.assertEqual(
            list(signature.parameters),
            ["preflight_task_id", "authorization_id"],
        )

    def test_authorized_facebook_submission_uses_real_db_claim_and_starts_once(self) -> None:
        with FacebookPagePublicEntryFixture() as fixture:
            authorization_id = fixture.authorize()
            with fixture.stop_at_worker_start() as started:
                result = submit_authorized_preflight_task(
                    fixture.preflight_task_id,
                    authorization_id,
                )

            fixture.assert_real_dispatch(
                self,
                result,
                started,
                authorization_id=authorization_id,
            )

    def test_used_page_preflight_rejects_new_and_presigned_grants_after_safe_terminal(self) -> None:
        for terminal_state in ("safe_failed", "confirmed_not_published"):
            with self.subTest(terminal_state=terminal_state), FacebookPagePublicEntryFixture() as fixture:
                first_grant = fixture.authorize()
                presigned_grant = fixture.authorize()
                with fixture.stop_at_worker_start():
                    first = submit_authorized_preflight_task(
                        fixture.preflight_task_id,
                        first_grant,
                    )
                first_task_id = int(first["taskId"])
                with database.connect() as conn:
                    conn.execute(
                        """
                        UPDATE facebook_page_publish_claims
                        SET state = ?, blocksReplay = 0
                        WHERE taskId = ?
                        """,
                        (terminal_state, first_task_id),
                    )
                    conn.execute(
                        "UPDATE publish_task_items SET status = 'failed' WHERE taskId = ?",
                        (first_task_id,),
                    )
                    conn.execute(
                        "UPDATE publish_tasks SET status = 'failed' WHERE id = ?",
                        (first_task_id,),
                    )
                    conn.commit()

                with self.assertRaises(ControlledPublishError) as new_grant_error:
                    fixture.authorize()
                with (
                    fixture.stop_at_worker_start() as restarted,
                    self.assertRaises(ControlledPublishError) as presigned_error,
                ):
                    submit_authorized_preflight_task(
                        fixture.preflight_task_id,
                        presigned_grant,
                    )

                self.assertEqual(
                    new_grant_error.exception.error_code,
                    "facebook_preflight_already_used",
                )
                self.assertEqual(
                    presigned_error.exception.error_code,
                    "facebook_preflight_already_used",
                )
                restarted.assert_not_called()
                with database.connect() as conn:
                    formal_count = conn.execute(
                        "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
                    ).fetchone()[0]
                    presigned = conn.execute(
                        "SELECT consumedAt FROM controlled_publish_authorizations "
                        "WHERE authorizationId = ?",
                        (presigned_grant,),
                    ).fetchone()
                self.assertEqual(int(formal_count), 1)
                self.assertIsNone(presigned["consumedAt"])

    def test_new_page_preflight_and_authorization_can_follow_safe_failed_attempt(self) -> None:
        with FacebookPagePublicEntryFixture() as fixture:
            first_grant = fixture.authorize()
            with fixture.stop_at_worker_start():
                first = submit_authorized_preflight_task(
                    fixture.preflight_task_id,
                    first_grant,
                )
            controlled_publish.mark_facebook_page_checkpoint(
                int(first["taskId"]),
                expected_state="reserved",
                new_state="safe_failed",
                receipt={
                    "pageId": str(
                        fixture.payload["facebookExpectedPageReference"]
                    )
                },
            )

            next_preflight_id = fixture.create_equivalent_preflight()
            next_grant = fixture.authorize(next_preflight_id)
            with fixture.stop_at_worker_start() as started:
                second = submit_authorized_preflight_task(
                    next_preflight_id,
                    next_grant,
                )

            second_task_id = int(second["taskId"])
            self.assertNotEqual(second_task_id, int(first["taskId"]))
            started.assert_called_once()
            with database.connect() as conn:
                claims = conn.execute(
                    """
                    SELECT preflightTaskId, state
                    FROM facebook_page_publish_claims
                    ORDER BY taskId
                    """
                ).fetchall()
            self.assertEqual(
                [(int(row["preflightTaskId"]), row["state"]) for row in claims],
                [
                    (fixture.preflight_task_id, "safe_failed"),
                    (next_preflight_id, "reserved"),
                ],
            )

    def test_lost_runtime_sources_do_not_consume_or_create_formal_state(self) -> None:
        for source_kind in ("manifest", "managed"):
            with self.subTest(source_kind=source_kind), FacebookPagePublicEntryFixture(
                source_kind=source_kind
            ) as fixture:
                authorization_id = fixture.authorize()
                if fixture.manifest is not None:
                    fixture.manifest.unlink()
                else:
                    fixture.video.unlink()

                with (
                    fixture.stop_at_worker_start() as started,
                    patch.object(
                        overseas_browser_publish,
                        "_facebook_page_session",
                    ) as session,
                    self.assertRaises(ControlledPublishError) as raised,
                ):
                    submit_authorized_preflight_task(
                        fixture.preflight_task_id,
                        authorization_id,
                    )

                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_video_runtime_path_unavailable",
                )
                started.assert_not_called()
                session.assert_not_called()
                with database.connect() as conn:
                    authorization = conn.execute(
                        "SELECT consumedAt FROM controlled_publish_authorizations "
                        "WHERE authorizationId = ?",
                        (authorization_id,),
                    ).fetchone()
                    formal_count = conn.execute(
                        "SELECT COUNT(*) FROM publish_tasks "
                        "WHERE mode = 'oneclick_publish'"
                    ).fetchone()[0]
                    claim_count = conn.execute(
                        "SELECT COUNT(*) FROM facebook_page_publish_claims"
                    ).fetchone()[0]
                self.assertIsNone(authorization["consumedAt"])
                self.assertEqual(int(formal_count), 0)
                self.assertEqual(int(claim_count), 0)

    def test_page_metadata_is_rejected_before_public_authorization_or_worker(self) -> None:
        with FacebookPagePublicEntryFixture() as fixture:
            unsupported = {
                **fixture.payload,
                "coverPath": "/tmp/legacy-selected-cover.png",
                "collectionName": "Legacy collection",
                "originalDeclaration": True,
            }
            with database.connect() as conn:
                conn.execute(
                    "UPDATE publish_tasks SET payloadJson = ? WHERE id = ?",
                    (
                        json.dumps([unsupported], ensure_ascii=False),
                        fixture.preflight_task_id,
                    ),
                )
                conn.commit()

            with (
                fixture.stop_at_worker_start() as started,
                self.assertRaises(ControlledPublishError) as raised,
            ):
                controlled_publish.authorize_completed_check(
                    fixture.preflight_task_id
                )

            self.assertEqual(
                raised.exception.error_code,
                "facebook_unsupported_publish_setting",
            )
            started.assert_not_called()
            with database.connect() as conn:
                table_exists = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table'
                      AND name = 'controlled_publish_authorizations'
                    """
                ).fetchone()
                authorization_count = (
                    conn.execute(
                        "SELECT COUNT(*) FROM controlled_publish_authorizations"
                    ).fetchone()[0]
                    if table_exists
                    else 0
                )
            self.assertEqual(int(authorization_count), 0)

    def test_feature_disabled_blocks_new_authorized_page_submission_before_claim(self) -> None:
        payload = self._facebook_payload()
        preflight = {
            "id": 17,
            "mode": "oneclick_preflight",
            "status": "success",
            "payloadJson": json.dumps([payload]),
        }
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(task_service, "get_task", return_value=preflight),
            patch.object(
                controlled_publish,
                "_create_claimed_facebook_page_task",
            ) as claimed,
            self.assertRaises(ControlledPublishError) as raised,
        ):
            submit_authorized_preflight_task(17, "single-use-grant")

        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_feature_disabled",
        )
        claimed.assert_not_called()

    def test_cli_schema_exposes_no_page_identity_or_meta_confirmation_arguments(self) -> None:
        root = Path(__file__).resolve().parent
        completed = subprocess.run(
            [sys.executable, str(root / "desktop_native_app.py"), "--help"],
            cwd=root,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        help_text = completed.stdout.casefold()
        for forbidden in (
            "--page-id",
            "--facebook-page-id",
            "--cookie",
            "--token",
            "--meta-browser-publish-confirmed",
            "--meta-browser-automation-acknowledged",
        ):
            self.assertNotIn(forbidden, help_text)

    def test_cli_formal_forwards_only_two_ids_to_the_shared_service_once(self) -> None:
        import desktop_native_app

        with FacebookPagePublicEntryFixture() as fixture:
            authorization_id = fixture.authorize()
            args = SimpleNamespace(
                controlled_publish_action="formal",
                controlled_publish_request=None,
                controlled_publish_task_id=fixture.preflight_task_id,
                controlled_publish_authorization_id=authorization_id,
            )
            output = StringIO()
            with (
                fixture.stop_at_worker_start() as started,
                redirect_stdout(output),
            ):
                exit_code = desktop_native_app.run_controlled_publish_cli(args)
            envelope = json.loads(output.getvalue().strip())

            self.assertEqual(exit_code, 2)
            fixture.assert_real_dispatch(
                self,
                envelope,
                started,
                authorization_id=authorization_id,
            )

    def test_cli_interrupt_after_page_final_action_claim_converges_to_ambiguous(self) -> None:
        import desktop_native_app

        with FacebookPagePublicEntryFixture() as fixture:
            authorization_id = fixture.authorize()
            retry_grant = fixture.authorize()
            page_id = str(fixture.payload["facebookExpectedPageReference"])
            args = SimpleNamespace(
                controlled_publish_action="formal",
                controlled_publish_request=None,
                controlled_publish_task_id=fixture.preflight_task_id,
                controlled_publish_authorization_id=authorization_id,
            )

            def interrupt_after_claim(
                task_id: int,
                *,
                interactive_verification: bool,
            ) -> None:
                self.assertTrue(interactive_verification)
                task_service.record_facebook_progress(
                    task_id,
                    phase="final_action_claimed",
                    message="Facebook Page final action claim persisted",
                    receipt={
                        "pageId": page_id,
                        "baseline": {"pageId": page_id, "rows": []},
                        "formSnapshot": {
                            "pageId": page_id,
                            "videoName": fixture.video.name,
                            "videoSize": fixture.video.stat().st_size,
                            "videoSha256": str(
                                fixture.payload["facebookVideoSha256"]
                            ),
                            "captionSha256": str(
                                fixture.payload["facebookCaptionSha256"]
                            ),
                            "visibility": "public",
                            "finalButtonLabel": "Publish",
                            "finalButtonReady": True,
                        },
                    },
                )
                raise KeyboardInterrupt

            output = StringIO()
            with (
                fixture.stop_at_worker_start(),
                patch.object(
                    desktop_native_app,
                    "_wait_for_controlled_task",
                    side_effect=interrupt_after_claim,
                ),
                redirect_stdout(output),
            ):
                exit_code = desktop_native_app.run_controlled_publish_cli(args)

            envelopes = [
                json.loads(line)
                for line in output.getvalue().splitlines()
                if line.strip()
            ]
            final = envelopes[-1]
            task_id = int(final["taskId"])
            with database.connect() as conn:
                claim = conn.execute(
                    """
                    SELECT state, blocksReplay
                    FROM facebook_page_publish_claims WHERE taskId = ?
                    """,
                    (task_id,),
                ).fetchone()

            self.assertEqual(exit_code, 130)
            self.assertEqual(final["status"], "failed")
            self.assertEqual(final["phase"], "ambiguous")
            self.assertEqual(final["items"][0]["status"], "failed")
            self.assertEqual(
                final["items"][0]["errorCode"],
                "facebook_publish_outcome_unknown",
            )
            persisted = task_service.get_task(task_id)
            self.assertEqual(
                persisted["events"][-1]["eventType"],
                "facebook_publish_outcome_ambiguous",
            )
            self.assertEqual(
                (claim["state"], int(claim["blocksReplay"])),
                ("ambiguous", 1),
            )

            with (
                fixture.stop_at_worker_start() as restarted,
                self.assertRaises(ControlledPublishError) as retry_error,
            ):
                submit_authorized_preflight_task(
                    fixture.preflight_task_id,
                    retry_grant,
                )
            self.assertEqual(
                retry_error.exception.error_code,
                "facebook_preflight_already_used",
            )
            restarted.assert_not_called()

    def test_shared_service_preserves_generic_ambiguous_duplicate_error(self) -> None:
        payload = {
            "type": 3,
            "contentType": "video",
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "accountIds": [31],
            "fileList": ["/content/douyin.mp4"],
        }
        preflight = {
            "id": 17,
            "mode": "oneclick_preflight",
            "status": "success",
            "payloadJson": json.dumps([payload]),
        }
        existing = {
            "id": 29,
            "mode": "oneclick_publish",
            "status": "failed",
            "events": [{"eventType": "wechat_final_submit_clicked"}],
        }

        with (
            patch.object(task_service, "get_task", return_value=preflight),
            patch.object(task_service, "list_tasks", return_value=[]),
            patch.object(
                controlled_publish,
                "_find_blocking_formal_scope_task",
                return_value=existing,
            ),
            patch.object(publish_service, "start_desktop_publish") as started,
            self.assertRaises(ControlledPublishError) as raised,
        ):
            submit_authorized_preflight_task(17, "single-use-grant")

        self.assertEqual(
            raised.exception.error_code,
            "controlled_publish_outcome_ambiguous",
        )
        self.assertIn("taskId=29", str(raised.exception))
        started.assert_not_called()

    def test_old_generic_facebook_formal_task_cannot_bypass_claim_enforcement(self) -> None:
        with self.assertRaises(Exception) as raised:
            publish_service.start_desktop_publish(
                [{"type": 9, "runtimeMode": "publish"}]
            )

        self.assertEqual(
            getattr(raised.exception, "error_code", ""),
            "facebook_publish_authorization_invalid",
        )

    def test_legacy_full_payload_facebook_formal_entry_is_rejected_before_work(self) -> None:
        request = {
            "mode": "formal",
            "manifestPath": "/must-not-be-read.json",
            "targets": [
                {
                    "platform": "Facebook",
                    "accountId": 91,
                    "schedule": None,
                    "settings": {"visibility": "public"},
                }
            ],
            "confirmedPreflightTaskId": 17,
            "authorizationId": "must-not-be-consumed",
        }
        with (
            patch.object(
                controlled_publish,
                "build_controlled_payloads",
                side_effect=AssertionError("legacy builder must not run"),
            ) as builder,
            patch.object(
                controlled_publish,
                "_create_claimed_facebook_page_task",
            ) as claim,
            patch.object(controlled_publish, "consume_authorization") as consume,
            patch.object(
                publish_service,
                "start_controlled_facebook_publish",
            ) as start,
            self.assertRaises(ControlledPublishError) as raised,
        ):
            controlled_publish.submit_request(request)

        self.assertEqual(
            raised.exception.error_code,
            "facebook_formal_entry_required",
        )
        builder.assert_not_called()
        claim.assert_not_called()
        consume.assert_not_called()
        start.assert_not_called()

    def test_process_adapter_preserves_cli_error_code_without_opening_desktop_ui(self) -> None:
        with self.assertRaises(ControlledPublishError) as raised:
            submit_request_in_process(
                {
                    "mode": "preflight",
                    "manifestPath": "",
                    "targets": [],
                },
                startup_timeout_seconds=10,
            )

        self.assertEqual(raised.exception.error_code, "controlled_manifest_required")
        self.assertIn("manifestPath", raised.exception.public_message)

    def test_matrix_process_adapter_uses_headless_cli_action(self) -> None:
        with self.assertRaises(ControlledPublishError) as raised:
            submit_douyin_graphic_matrix_request_in_process(
                {
                    "schemaVersion": "oneclick-douyin-graphic-matrix/v1",
                    "workflow": "douyin-graphic-matrix",
                    "runtimeMode": "local_check",
                    "content": {"images": [], "common": {}},
                    "targets": [],
                },
                startup_timeout_seconds=10,
            )

        self.assertEqual(raised.exception.error_code, "douyin_graphic_matrix_invalid")


if __name__ == "__main__":
    unittest.main()
