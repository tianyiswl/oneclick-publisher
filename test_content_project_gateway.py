# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app_core.content_project_gateway import (
    ContentProjectGateway,
    ContentProjectGatewayError,
    PublishProfileStore,
)


class ContentProjectGatewayTests(unittest.TestCase):
    @staticmethod
    def _accounts() -> list[dict]:
        return [
            {
                "id": 31,
                "type": 3,
                "filePath": "/private/douyin-session.json",
                "profileName": "硅基探索",
                "userName": "douyin-user",
                "healthStatus": "normal",
                "statusText": "正常",
            },
            {
                "id": 11,
                "type": 1,
                "filePath": "/private/xhs-session.json",
                "profileName": "硅基探索",
                "userName": "xhs-user",
                "healthStatus": "normal",
                "statusText": "正常",
            },
            {
                "id": 2,
                "type": 10,
                "filePath": "/private/wechat-session.json",
                "profileName": "硅基进化",
                "userName": "硅基进化",
                "healthStatus": "normal",
                "statusText": "正常",
            },
        ]

    def _gateway(
        self,
        root: Path,
        submitted: list[dict],
        *,
        runtime_conflict_checker=lambda: False,
        silicon_submitted: list[dict] | None = None,
    ) -> ContentProjectGateway:
        return ContentProjectGateway(
            profile_store=PublishProfileStore(root / "publish-profiles.json"),
            accounts_provider=self._accounts,
            submitter=lambda request: submitted.append(dict(request)) or {
                "taskId": 42,
                "taskNo": "T42",
                "phase": request["mode"],
                "status": "pending",
                "platforms": [],
            },
            status_reader=lambda task_id: {
                "taskId": task_id,
                "taskNo": f"T{task_id}",
                "phase": "preflight",
                "status": "success",
                "platforms": [],
            },
            authorizer=lambda task_id: {
                "authorizationId": "one-time-grant",
                "preflightTaskId": task_id,
                "expiresAt": "2026-08-25T12:10:00+00:00",
                "singleUse": True,
            },
            runtime_conflict_checker=runtime_conflict_checker,
            silicon_submitter=(
                (lambda request: silicon_submitted.append(dict(request)) or {
                    "taskId": 51,
                    "phase": request["mode"],
                    "status": "pending",
                })
                if silicon_submitted is not None
                else None
            ),
        )

    def test_account_catalog_never_exposes_login_session_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = self._gateway(Path(directory), [])
            catalog = gateway.list_accounts()

        self.assertEqual(
            catalog,
            [
                {
                    "accountId": 31,
                    "platform": "抖音",
                    "platformType": 3,
                    "account": "硅基探索",
                    "healthStatus": "normal",
                    "statusText": "正常",
                },
                {
                    "accountId": 11,
                    "platform": "小红书",
                    "platformType": 1,
                    "account": "硅基探索",
                    "healthStatus": "normal",
                    "statusText": "正常",
                },
                {
                    "accountId": 2,
                    "platform": "公众号",
                    "platformType": 10,
                    "account": "硅基进化",
                    "healthStatus": "normal",
                    "statusText": "正常",
                },
            ],
        )
        self.assertNotIn("filePath", json.dumps(catalog, ensure_ascii=False))

    def test_silicon_evolution_route_uses_one_bound_wechat_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            submitted: list[dict] = []
            gateway = self._gateway(
                Path(directory), [], silicon_submitted=submitted
            )
            gateway.save_profile(
                "silicon-evolution",
                "硅基进化",
                [{"platform": "公众号", "accountId": 2}],
            )
            result = gateway.preflight_silicon_evolution_release(
                "WX-20260826-001",
                "/content/WX-20260826-001",
                "a" * 64,
            )
            formal = gateway.auto_publish_silicon_evolution_release(
                "WX-20260826-001",
                "/content/WX-20260826-001",
                "a" * 64,
                confirmed_preflight_task_id=51,
            )

        self.assertEqual(result["taskId"], 51)
        self.assertEqual(formal["taskId"], 51)
        self.assertEqual(submitted[0]["accountId"], 2)
        self.assertEqual(submitted[0]["mode"], "preflight")
        self.assertEqual(submitted[1]["mode"], "formal")
        self.assertEqual(submitted[1]["confirmedPreflightTaskId"], 51)

    def test_project_profile_drives_preflight_with_explicit_accounts_and_schedules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            submitted: list[dict] = []
            gateway = self._gateway(Path(directory), submitted)
            saved = gateway.save_profile(
                "silicon-exploration",
                "硅基探索",
                [
                    {"platform": "抖音", "accountId": 31},
                    {"platform": "小红书", "accountId": 11},
                ],
            )
            result = gateway.preflight_content(
                "silicon-exploration",
                "/content/manifest.json",
                {"小红书": {"localTime": "2026-08-26 09:00", "timezone": "Asia/Shanghai"}},
            )

            reloaded = PublishProfileStore(
                Path(directory) / "publish-profiles.json"
            ).get("silicon-exploration")

        self.assertEqual(saved["schemaVersion"], 1)
        self.assertEqual(reloaded, saved)
        self.assertEqual(result["taskId"], 42)
        self.assertEqual(
            submitted,
            [
                {
                    "manifestPath": "/content/manifest.json",
                    "mode": "preflight",
                    "targets": [
                        {"platform": "抖音", "accountId": 31, "schedule": None},
                        {
                            "platform": "小红书",
                            "accountId": 11,
                            "schedule": {
                                "localTime": "2026-08-26 09:00",
                                "timezone": "Asia/Shanghai",
                            },
                        },
                    ],
                }
            ],
        )

    def test_formal_publish_cannot_bypass_preflight_and_one_time_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            submitted: list[dict] = []
            gateway = self._gateway(Path(directory), submitted)
            gateway.save_profile(
                "silicon-exploration",
                "硅基探索",
                [{"platform": "抖音", "accountId": 31}],
            )
            with self.assertRaises(ContentProjectGatewayError) as missing:
                gateway.formal_publish(
                    "silicon-exploration",
                    "/content/manifest.json",
                    confirmed_preflight_task_id=0,
                    authorization_id="",
                )
            result = gateway.formal_publish(
                "silicon-exploration",
                "/content/manifest.json",
                confirmed_preflight_task_id=41,
                authorization_id="one-time-grant",
            )

        self.assertEqual(missing.exception.error_code, "content_project_authorization_required")
        self.assertEqual(result["taskId"], 42)
        self.assertEqual(submitted[0]["mode"], "formal")
        self.assertEqual(submitted[0]["confirmedPreflightTaskId"], 41)
        self.assertEqual(submitted[0]["authorizationId"], "one-time-grant")

    def test_profile_rejects_account_that_belongs_to_another_platform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = self._gateway(Path(directory), [])
            with self.assertRaises(ContentProjectGatewayError) as raised:
                gateway.save_profile(
                    "silicon-exploration",
                    "硅基探索",
                    [{"platform": "小红书", "accountId": 31}],
                )

        self.assertEqual(raised.exception.error_code, "content_project_account_mismatch")

    def test_unknown_schedule_platform_is_rejected_instead_of_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = self._gateway(Path(directory), [])
            gateway.save_profile(
                "silicon-exploration",
                "硅基探索",
                [{"platform": "抖音", "accountId": 31}],
            )
            with self.assertRaises(ContentProjectGatewayError) as raised:
                gateway.preflight_content(
                    "silicon-exploration",
                    "/content/manifest.json",
                    {"小红书": None},
                )

        self.assertEqual(raised.exception.error_code, "content_project_schedule_mismatch")

    def test_installed_mcp_cannot_start_platform_work_while_source_live_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            submitted: list[dict] = []
            gateway = self._gateway(
                Path(directory), submitted, runtime_conflict_checker=lambda: True
            )
            gateway.save_profile(
                "silicon-exploration",
                "硅基探索",
                [{"platform": "抖音", "accountId": 31}],
            )

            with self.assertRaises(ContentProjectGatewayError) as raised:
                gateway.preflight_content(
                    "silicon-exploration", "/content/manifest.json"
                )

        self.assertEqual(raised.exception.error_code, "source_live_session_active")
        self.assertEqual(submitted, [])


if __name__ == "__main__":
    unittest.main()
