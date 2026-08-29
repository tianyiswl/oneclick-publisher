# -*- coding: utf-8 -*-

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_core import controlled_publish, publish_service, task_service
from app_core.controlled_publish import ControlledPublishError
from app_core.controlled_publish_process import (
    submit_authorized_preflight_task,
    submit_douyin_graphic_matrix_request_in_process,
    submit_request_in_process,
)


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

    def test_authorized_facebook_submission_reuses_snapshot_and_starts_controlled_service_once(self) -> None:
        payload = self._facebook_payload()
        preflight = {
            "id": 17,
            "mode": "oneclick_preflight",
            "status": "success",
            "payloadJson": json.dumps([payload]),
        }
        formal = {
            "id": 18,
            "mode": "oneclick_publish",
            "status": "pending",
            "payloadJson": json.dumps([{**payload, "runtimeMode": "publish"}]),
        }

        def read_task(task_id: int):
            return preflight if task_id == 17 else formal if task_id == 18 else None

        def create_claim(payloads, *, preflight_task_id, authorization_id):
            self.assertEqual(preflight_task_id, 17)
            self.assertEqual(authorization_id, "single-use-grant")
            self.assertEqual(payloads[0]["runtimeMode"], "publish")
            self.assertIs(payloads[0]["debugDryRun"], False)
            self.assertNotIn("metaBrowserPublishConfirmed", payloads[0])
            self.assertNotIn("metaBrowserAutomationAcknowledged", payloads[0])
            return publish_service.start_controlled_facebook_publish(18)

        with (
            patch.dict(
                os.environ,
                {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                clear=False,
            ),
            patch.object(task_service, "get_task", side_effect=read_task),
            patch.object(
                controlled_publish,
                "_create_claimed_facebook_page_task",
                side_effect=create_claim,
            ) as claimed,
            patch.object(
                publish_service,
                "start_controlled_facebook_publish",
                return_value=formal,
            ) as started,
            patch.object(
                controlled_publish,
                "project_task",
                return_value={
                    "taskId": 18,
                    "taskNo": "T18",
                    "phase": "formal",
                    "status": "pending",
                    "platforms": [],
                },
            ),
        ):
            result = submit_authorized_preflight_task(17, "single-use-grant")

        self.assertEqual(result["taskId"], 18)
        claimed.assert_called_once()
        started.assert_called_once_with(18)

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

        envelope = {
            "taskId": 18,
            "taskNo": "T18",
            "phase": "formal",
            "status": "success",
            "platforms": [],
        }
        args = SimpleNamespace(
            controlled_publish_action="formal",
            controlled_publish_request=None,
            controlled_publish_task_id=17,
            controlled_publish_authorization_id="single-use-grant",
        )

        with (
            patch.object(desktop_native_app, "ensure_schema"),
            patch("utils.log.redirect_console_logger"),
            patch(
                "app_core.controlled_publish_process.submit_authorized_preflight_task",
                return_value=envelope,
            ) as submit,
            patch.object(desktop_native_app, "_wait_for_controlled_task"),
            patch.object(controlled_publish, "task_status", return_value=envelope),
            patch.object(desktop_native_app, "_controlled_json"),
        ):
            exit_code = desktop_native_app.run_controlled_publish_cli(args)

        self.assertEqual(exit_code, 0)
        submit.assert_called_once_with(17, "single-use-grant")

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
