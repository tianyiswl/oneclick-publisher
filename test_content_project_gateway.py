# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import database
from app_core.controlled_publish import ControlledPublishError
from app_core.controlled_publish_process import submit_authorized_preflight_task
from app_core.content_project_gateway import (
    ContentProjectGateway,
    ContentProjectGatewayError,
    PublishProfileStore,
)
from test_controlled_publish_process import FacebookPagePublicEntryFixture


class _MetricsService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def sync_project(self, project_id, profile):
        self.calls.append(("sync", project_id, dict(profile)))
        return {"projectId": project_id, "accounts": []}

    def get_project_metrics(self, project_id, profile, days):
        self.calls.append(("get", project_id, dict(profile), days))
        return {"projectId": project_id, "days": days, "accounts": [], "contents": []}

    def sync_status(self, project_id, profile):
        self.calls.append(("status", project_id, dict(profile)))
        return {"projectId": project_id, "accounts": []}


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
        matrix_submitted: list[dict] | None = None,
        metrics_service: _MetricsService | None = None,
        direct_authorizer=None,
        silicon_direct_authorizer=None,
        accounts_provider=None,
        formal_submitter=None,
        reconciler=None,
    ) -> ContentProjectGateway:
        optional = {}
        if formal_submitter is not None:
            optional["formal_submitter"] = formal_submitter
        if reconciler is not None:
            optional["reconciler"] = reconciler
        return ContentProjectGateway(
            profile_store=PublishProfileStore(root / "publish-profiles.json"),
            accounts_provider=accounts_provider or self._accounts,
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
            direct_authorizer=(
                direct_authorizer
                or (
                    lambda request: {
                        "authorizationId": "direct-one-time-grant",
                        "expiresAt": "2026-08-27T12:01:00+00:00",
                        "singleUse": True,
                    }
                )
            ),
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
            silicon_direct_authorizer=(
                silicon_direct_authorizer
                or (
                    lambda request: {
                        "authorizationId": "silicon-direct-one-time-grant",
                        "expiresAt": "2026-08-27T12:01:00+00:00",
                        "singleUse": True,
                    }
                )
            ),
            matrix_submitter=(
                (lambda request: matrix_submitted.append(dict(request)) or {
                    "taskId": 61,
                    "taskNo": "T61",
                    "phase": (
                        "formal"
                        if request.get("runtimeMode") == "publish"
                        else "local_check"
                    ),
                    "status": "pending",
                    "platforms": [],
                })
                if matrix_submitted is not None
                else None
            ),
            metrics_service=metrics_service,
            **optional,
        )

    @staticmethod
    def _facebook_account() -> dict:
        return {
            "id": 91,
            "type": 9,
            "filePath": "facebook-page.json",
            "profileName": "品牌主体",
            "userName": "Saved Facebook Page",
            "healthStatus": "normal",
            "statusText": "正常",
            "status": 1,
            "authMode": "browser",
            "accountReference": "1000000000001001",
        }

    @staticmethod
    def _article_bundle(root: Path) -> Path:
        (root / "01.png").write_bytes(b"first-image")
        (root / "02.png").write_bytes(b"second-image")
        (root / "正文.md").write_text("通用正文", encoding="utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": "oneclick-content/v1",
                    "contentType": "article",
                    "title": "通用标题",
                    "bodyFile": "正文.md",
                    "tags": ["通用话题"],
                    "assets": ["01.png", "02.png"],
                    "covers": {"3:4": "01.png"},
                    "preferredPlatforms": ["抖音"],
                    "platformOverrides": {
                        "抖音": {
                            "title": "抖音标题",
                            "body": "抖音正文",
                            "tags": ["抖音话题"],
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

    def test_silicon_evolution_direct_route_does_not_require_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            submitted: list[dict] = []
            authorized: list[dict] = []
            gateway = self._gateway(
                Path(directory),
                [],
                silicon_submitted=submitted,
                silicon_direct_authorizer=lambda request: authorized.append(
                    dict(request)
                )
                or {
                    "authorizationId": "silicon-direct-one-time-grant",
                    "expiresAt": "2026-08-27T12:01:00+00:00",
                    "singleUse": True,
                },
            )
            gateway.save_profile(
                "silicon-evolution",
                "硅基进化",
                [{"platform": "公众号", "accountId": 2}],
            )
            result = gateway.auto_publish_silicon_evolution_release(
                "WX-20260827-001",
                "/content/WX-20260827-001",
                "b" * 64,
            )

        self.assertEqual(result["taskId"], 51)
        self.assertEqual(authorized[0]["mode"], "direct")
        self.assertNotIn("confirmedPreflightTaskId", authorized[0])
        self.assertEqual(submitted[0]["mode"], "direct")
        self.assertEqual(
            submitted[0]["directAuthorizationId"],
            "silicon-direct-one-time-grant",
        )

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
                    "projectId": "silicon-exploration",
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

    def test_matrix_gateway_forwards_explicit_per_account_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._article_bundle(root)
            submitted: list[dict] = []
            gateway = self._gateway(
                root,
                [],
                matrix_submitted=submitted,
            )

            result = gateway.check_douyin_graphic_matrix(
                str(manifest),
                [
                    {
                        "accountId": 31,
                        "title": "账号一标题",
                        "body": None,
                        "tags": None,
                        "schedule": {
                            "localTime": "2026-08-27 18:00",
                            "timezone": "Asia/Shanghai",
                        },
                    }
                ],
            )

        self.assertEqual(result["phase"], "local_check")
        self.assertEqual(submitted[0]["workflow"], "douyin-graphic-matrix")
        self.assertEqual(submitted[0]["runtimeMode"], "local_check")
        self.assertEqual(submitted[0]["content"]["common"]["title"], "抖音标题")
        self.assertEqual(submitted[0]["content"]["common"]["body"], "抖音正文")
        self.assertEqual(submitted[0]["targets"][0]["overrides"]["title"], "账号一标题")
        self.assertIsNone(submitted[0]["targets"][0]["overrides"]["body"])
        self.assertEqual(
            submitted[0]["targets"][0]["scheduleTime"],
            "2026-08-27 18:00",
        )

    def test_matrix_formal_requires_check_and_forwards_one_time_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._article_bundle(root)
            submitted: list[dict] = []
            gateway = self._gateway(root, [], matrix_submitted=submitted)

            authorization = gateway.authorize_douyin_graphic_matrix(61)
            result = gateway.publish_douyin_graphic_matrix(
                str(manifest),
                [{"accountId": 31}],
                confirmed_check_task_id=61,
                authorization_id="one-time-grant",
            )

        self.assertEqual(authorization["authorizationId"], "one-time-grant")
        self.assertEqual(result["phase"], "formal")
        self.assertEqual(submitted[0]["runtimeMode"], "publish")
        self.assertEqual(submitted[0]["confirmedCheckTaskId"], 61)
        self.assertEqual(submitted[0]["authorizationId"], "one-time-grant")

    def test_gateway_metrics_methods_resolve_saved_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metrics = _MetricsService()
            gateway = self._gateway(
                Path(directory), [], metrics_service=metrics
            )
            gateway.save_profile(
                "silicon-exploration",
                "硅基探索",
                [{"platform": "抖音", "accountId": 31}],
            )

            synced = gateway.sync_project_metrics("SILICON-EXPLORATION")
            queried = gateway.get_project_metrics("silicon-exploration", 7)
            status = gateway.metrics_sync_status("silicon-exploration")

        self.assertEqual(synced["projectId"], "silicon-exploration")
        self.assertEqual(queried["days"], 7)
        self.assertEqual(status["projectId"], "silicon-exploration")
        self.assertEqual([call[0] for call in metrics.calls], ["sync", "get", "status"])

    def test_metrics_queries_are_read_only_during_source_live_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metrics = _MetricsService()
            gateway = self._gateway(
                Path(directory),
                [],
                metrics_service=metrics,
                runtime_conflict_checker=lambda: True,
            )
            gateway.save_profile(
                "silicon-exploration",
                "硅基探索",
                [{"platform": "抖音", "accountId": 31}],
            )

            queried = gateway.get_project_metrics("silicon-exploration", 1)
            status = gateway.metrics_sync_status("silicon-exploration")
            with self.assertRaises(ContentProjectGatewayError) as raised:
                gateway.sync_project_metrics("silicon-exploration")

        self.assertEqual(queried["projectId"], "silicon-exploration")
        self.assertEqual(status["projectId"], "silicon-exploration")
        self.assertEqual(raised.exception.error_code, "source_live_session_active")

    def test_formal_publish_cannot_bypass_preflight_and_one_time_authorization(self) -> None:
        with FacebookPagePublicEntryFixture() as fixture:
            gateway = ContentProjectGateway(
                profile_store=PublishProfileStore(
                    fixture.root / "publish-profiles.json"
                ),
            )
            with self.assertRaises(ContentProjectGatewayError) as missing:
                gateway.formal_publish(
                    preflight_task_id=0,
                    authorization_id="",
                )
            authorization_id = fixture.authorize()
            with fixture.stop_at_worker_start() as started:
                result = gateway.formal_publish(
                    preflight_task_id=fixture.preflight_task_id,
                    authorization_id=authorization_id,
                )

            self.assertEqual(
                missing.exception.error_code,
                "content_project_authorization_required",
            )
            fixture.assert_real_dispatch(
                self,
                result,
                started,
                authorization_id=authorization_id,
            )

    def test_direct_service_and_gateway_race_for_one_global_page_replay_claim(self) -> None:
        with FacebookPagePublicEntryFixture() as fixture:
            equivalent_preflight_id = fixture.create_equivalent_preflight()
            direct_authorization = fixture.authorize(fixture.preflight_task_id)
            gateway_authorization = fixture.authorize(equivalent_preflight_id)
            gateway = ContentProjectGateway(
                profile_store=PublishProfileStore(
                    fixture.root / "race-publish-profiles.json"
                ),
                runtime_conflict_checker=lambda: False,
                formal_submitter=submit_authorized_preflight_task,
            )
            gate = threading.Barrier(3)
            result_lock = threading.Lock()
            results: list[dict] = []
            errors: list[Exception] = []

            def capture(callable_) -> None:
                try:
                    gate.wait(timeout=2)
                    result = callable_()
                except Exception as exc:  # Preserve thread assertions for the test.
                    with result_lock:
                        errors.append(exc)
                else:
                    with result_lock:
                        results.append(result)

            direct_thread = threading.Thread(
                target=capture,
                args=(
                    lambda: submit_authorized_preflight_task(
                        fixture.preflight_task_id,
                        direct_authorization,
                    ),
                ),
            )
            gateway_thread = threading.Thread(
                target=capture,
                args=(
                    lambda: gateway.formal_publish(
                        preflight_task_id=equivalent_preflight_id,
                        authorization_id=gateway_authorization,
                    ),
                ),
            )
            with fixture.stop_at_worker_start() as started:
                direct_thread.start()
                gateway_thread.start()
                gate.wait(timeout=2)
                direct_thread.join(timeout=3)
                gateway_thread.join(timeout=3)

            self.assertFalse(direct_thread.is_alive())
            self.assertFalse(gateway_thread.is_alive())
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ControlledPublishError)
            self.assertEqual(
                getattr(errors[0], "error_code", ""),
                "facebook_duplicate_submit_blocked",
            )
            started.assert_called_once()
            self.assertEqual(len(fixture.started_task_ids), 1)

            with database.connect() as conn:
                claims = conn.execute(
                    """
                    SELECT taskId, state, blocksReplay
                    FROM facebook_page_publish_claims
                    """
                ).fetchall()
                formal_count = conn.execute(
                    "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
                ).fetchone()[0]
                authorizations = conn.execute(
                    """
                    SELECT preflightTaskId, consumedAt
                    FROM controlled_publish_authorizations
                    WHERE preflightTaskId IN (?, ?)
                    ORDER BY preflightTaskId
                    """,
                    (fixture.preflight_task_id, equivalent_preflight_id),
                ).fetchall()

            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0]["state"], "reserved")
            self.assertEqual(int(claims[0]["blocksReplay"]), 1)
            self.assertEqual(int(formal_count), 1)
            self.assertEqual(len(authorizations), 2)
            self.assertEqual(
                sum(bool(str(row["consumedAt"] or "")) for row in authorizations),
                1,
            )

    def test_facebook_direct_requires_preflight_before_authorizer_or_submitter(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
            clear=False,
        ):
            submitted: list[dict] = []
            authorized: list[dict] = []
            gateway = self._gateway(
                Path(directory),
                submitted,
                accounts_provider=lambda: [self._facebook_account()],
                direct_authorizer=lambda request: authorized.append(dict(request))
                or {"authorizationId": "must-not-be-created"},
            )
            gateway.save_profile(
                "facebook-page",
                "Facebook Page",
                [{"platform": "Facebook Reels", "accountId": 91}],
            )

            with self.assertRaises(ContentProjectGatewayError) as raised:
                gateway.direct_publish_content(
                    "facebook-page",
                    "/content/manifest.json",
                    settings={"Facebook Reels": {"visibility": "public"}},
                )

        self.assertEqual(raised.exception.error_code, "facebook_preflight_required")
        self.assertEqual(authorized, [])
        self.assertEqual(submitted, [])

    def test_default_off_hides_page_accounts_and_blocks_new_page_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            gateway = self._gateway(
                Path(directory),
                [],
                accounts_provider=lambda: [self._facebook_account()],
            )

            self.assertEqual(gateway.list_accounts(), [])
            with self.assertRaises(ContentProjectGatewayError) as raised:
                gateway.save_profile(
                    "facebook-page",
                    "Facebook Page",
                    [{"platform": "Facebook Reels", "accountId": 91}],
                )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_feature_disabled",
        )

    def test_read_only_reconcile_forwards_only_task_id_and_rejects_preflight(self) -> None:
        calls: list[int] = []

        def reconcile(task_id: int) -> dict:
            calls.append(task_id)
            raise ControlledPublishError(
                "facebook_claim_lifecycle_invalid",
                "Facebook Page 预检任务不允许只读核对。",
            )

        with tempfile.TemporaryDirectory() as directory:
            gateway = self._gateway(
                Path(directory),
                [],
                reconciler=reconcile,
            )
            with self.assertRaises(ControlledPublishError) as raised:
                gateway.reconcile_publish_outcome(17)

        self.assertEqual(calls, [17])
        self.assertEqual(
            raised.exception.error_code,
            "facebook_claim_lifecycle_invalid",
        )

    def test_direct_publish_creates_bound_authorization_without_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            submitted: list[dict] = []
            authorized: list[dict] = []
            gateway = self._gateway(
                Path(directory),
                submitted,
                direct_authorizer=lambda request: authorized.append(dict(request))
                or {
                    "authorizationId": "direct-one-time-grant",
                    "expiresAt": "2026-08-27T12:01:00+00:00",
                    "singleUse": True,
                },
            )
            gateway.save_profile(
                "silicon-exploration",
                "硅基探索",
                [{"platform": "抖音", "accountId": 31}],
            )
            result = gateway.direct_publish_content(
                "silicon-exploration",
                "/content/manifest.json",
                {"抖音": None},
            )

        self.assertEqual(result["taskId"], 42)
        self.assertEqual(authorized[0]["mode"], "direct")
        self.assertNotIn("confirmedPreflightTaskId", authorized[0])
        self.assertEqual(submitted[0]["mode"], "direct")
        self.assertEqual(
            submitted[0]["directAuthorizationId"],
            "direct-one-time-grant",
        )

    def test_direct_publish_forwards_youtube_settings(self) -> None:
        youtube_account = {
            "id": 71,
            "type": 7,
            "filePath": "youtube-oauth:test",
            "profileName": "YouTube 测试",
            "userName": "YouTube 测试",
            "authMode": "youtube_oauth",
            "oauthScopeVersion": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            submitted: list[dict] = []
            gateway = self._gateway(
                Path(directory),
                submitted,
                accounts_provider=lambda: [youtube_account],
            )
            gateway.save_profile(
                "youtube-test",
                "YouTube 测试",
                [{"platform": "YouTube", "accountId": 71}],
            )
            gateway.direct_publish_content(
                "youtube-test",
                "/content/manifest.json",
                settings={
                    "YouTube": {
                        "visibility": "private",
                        "madeForKids": False,
                        "notifySubscribers": False,
                    }
                },
            )

        target = submitted[0]["targets"][0]
        self.assertEqual(target["settings"]["visibility"], "private")
        self.assertIs(target["settings"]["madeForKids"], False)
        self.assertIs(target["settings"]["notifySubscribers"], False)

    def test_settings_for_unconfigured_platform_are_rejected(self) -> None:
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
                    settings={"YouTube": {"visibility": "private"}},
                )

        self.assertEqual(
            raised.exception.error_code,
            "content_project_settings_mismatch",
        )

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
