# -*- coding: utf-8 -*-

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import controlled_publish
from app_core.controlled_publish import (
    ControlledPublishError,
    build_controlled_payloads,
    facebook_replay_fingerprint,
    publish_intent_fingerprint,
    submit_request,
)
from app_core.overseas_meta_content import build_facebook_page_caption


class FacebookPageControlledPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self._feature = patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
            clear=False,
        )
        self._feature.start()
        self.addCleanup(self._feature.stop)

    @staticmethod
    def account(**changes) -> dict:
        return {
            "id": 41,
            "type": 9,
            "filePath": "facebook-page-session.json",
            "profileName": "Saved Facebook Page",
            "userName": "Saved Facebook Page",
            "authMode": "browser",
            "accountReference": "1001",
            "status": 1,
            **changes,
        }

    @staticmethod
    def request(manifest: Path, *, mode: str = "preflight", **changes) -> dict:
        request = {
            "projectId": "facebook-page-offline-test",
            "manifestPath": str(manifest),
            "mode": mode,
            "targets": [
                {
                    "platform": "Facebook",
                    "accountId": 41,
                    "schedule": None,
                    "settings": {"visibility": "public"},
                }
            ],
        }
        if mode == "formal":
            request.update(
                {
                    "confirmedPreflightTaskId": 71,
                    "authorizationId": "authorization",
                }
            )
        if mode == "direct":
            request["directAuthorizationId"] = "direct-authorization"
        request.update(changes)
        return request

    @staticmethod
    def manifest(
        root: Path,
        *,
        assets: tuple[str, ...] = ("facebook.mp4",),
        ai_generated: bool = False,
        preferred_platforms: tuple[str, ...] = ("Facebook",),
    ) -> Path:
        for index, name in enumerate(assets):
            (root / name).write_bytes(f"facebook-video-{index}".encode("utf-8"))
        (root / "cover.png").write_bytes(b"required-bundle-cover")
        (root / "body.md").write_text("Common body", encoding="utf-8")
        overrides = {
            "Facebook": {
                "title": "Facebook title",
                "body": "Facebook body with raw @friend and #plain",
                "tags": ["TopicOne", "#TopicTwo", "TopicOne"],
            }
        }
        if "抖音" in preferred_platforms:
            overrides["抖音"] = {
                "title": "Douyin title",
                "body": "Douyin body",
                "tags": ["douyin"],
            }
        data = {
            "schemaVersion": "oneclick-content/v1",
            "contentType": "video",
            "title": "Common title",
            "bodyFile": "body.md",
            "tags": ["common"],
            "assets": list(assets),
            "covers": {"3:4": "cover.png"},
            "preferredPlatforms": list(preferred_platforms),
            "platformOverrides": overrides,
            "aiDisclosure": {
                "containsAiGeneratedContent": ai_generated,
                "contentKinds": ["video"] if ai_generated else [],
                "assetPaths": [],
                "allowPlatformAutoDeclaration": False,
            },
            "debugDryRun": True,
            "publishAllowed": False,
        }
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return manifest

    def build(self, root: Path, *, mode: str = "preflight", account=None) -> dict:
        manifest = self.manifest(root)
        return build_controlled_payloads(
            self.request(manifest, mode=mode),
            accounts=[account or self.account()],
        )[0]

    def test_feature_is_default_off_before_account_or_browser_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            manifest = self.manifest(Path(temporary))
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    self.request(manifest),
                    accounts=[self.account()],
                )

        self.assertEqual(raised.exception.error_code, "facebook_page_feature_disabled")

    def test_page_reference_is_loaded_from_the_account_not_the_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = self.build(Path(temporary))

        self.assertEqual(payload["facebookExpectedPageReference"], "1001")
        self.assertNotIn("facebookExpectedPageReference", self.request(Path("manifest.json")))

    def test_caller_supplied_page_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.manifest(Path(temporary))
            request = self.request(manifest)
            request["targets"][0]["facebookExpectedPageReference"] = "attacker-page"
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(request, accounts=[self.account()])

        self.assertEqual(
            raised.exception.error_code,
            "facebook_unsupported_publish_setting",
        )

    def test_stores_the_one_exact_final_caption_and_hash(self) -> None:
        expected = (
            "Facebook title\n\n"
            "Facebook body with raw @friend and #plain\n\n"
            "#TopicOne #TopicTwo"
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            controlled_publish,
            "build_facebook_page_caption",
            wraps=build_facebook_page_caption,
        ) as compose:
            payload = self.build(Path(temporary))
            compose.assert_called_once_with(
                title="Facebook title",
                body="Facebook body with raw @friend and #plain",
                topics=["TopicOne", "TopicTwo"],
            )

        self.assertEqual(payload["facebookFinalCaption"], expected)
        self.assertEqual(
            payload["facebookCaptionSha256"],
            hashlib.sha256(expected.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(payload["title"], "Facebook title")
        self.assertEqual(
            payload["description"],
            "Facebook body with raw @friend and #plain",
        )
        self.assertEqual(payload["tags"], ["TopicOne", "TopicTwo"])
        self.assertEqual(payload["coverPath"], "")
        self.assertEqual(payload["coverPaths"], {})

    def test_preflight_and_formal_store_the_same_caption_and_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.manifest(root)
            preflight = build_controlled_payloads(
                self.request(manifest),
                accounts=[self.account()],
            )[0]
            formal = build_controlled_payloads(
                self.request(manifest, mode="formal"),
                accounts=[self.account()],
            )[0]

        self.assertEqual(preflight["facebookFinalCaption"], formal["facebookFinalCaption"])
        self.assertEqual(preflight["facebookCaptionSha256"], formal["facebookCaptionSha256"])
        self.assertEqual(
            publish_intent_fingerprint([preflight]),
            publish_intent_fingerprint([formal]),
        )
        self.assertEqual(
            facebook_replay_fingerprint([preflight]),
            facebook_replay_fingerprint([formal]),
        )

    def test_account_failures_use_facebook_specific_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.manifest(Path(temporary))
            cases = (
                ("missing", [], "facebook_account_invalid"),
                ("blank_page", [self.account(accountReference="")], "facebook_account_invalid"),
                ("unhealthy", [self.account(status=0)], "facebook_account_invalid"),
                ("wrong_type", [self.account(type=8)], "facebook_account_invalid"),
                ("missing_session", [self.account(filePath="")], "facebook_session_missing"),
            )
            for label, accounts, error_code in cases:
                with self.subTest(label=label), self.assertRaises(
                    ControlledPublishError
                ) as raised:
                    build_controlled_payloads(self.request(manifest), accounts=accounts)
                self.assertEqual(raised.exception.error_code, error_code)

    def test_unsupported_page_v1_settings_fail_before_task_or_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.manifest(root)
            two_video_root = root / "two-videos"
            two_video_root.mkdir()
            two_videos = self.manifest(
                two_video_root,
                assets=("first.mp4", "second.mp4"),
            )
            ai_root = root / "ai"
            ai_root.mkdir()
            ai_manifest = self.manifest(ai_root, ai_generated=True)
            cases = []
            platform_form_check = self.request(
                manifest,
                mode="platform_form_check",
                platformFormCheckConfirmed=True,
            )
            cases.append(("platform_form_check", platform_form_check))
            scheduled = self.request(manifest)
            scheduled["targets"][0]["schedule"] = {
                "localTime": "2026-08-31 10:00",
                "timezone": "Asia/Shanghai",
            }
            cases.append(("schedule", scheduled))
            custom_cover = self.request(manifest)
            custom_cover["targets"][0]["settings"]["coverPath"] = "cover.png"
            cases.append(("cover", custom_cover))
            private = self.request(manifest)
            private["targets"][0]["settings"]["visibility"] = "private"
            cases.append(("visibility", private))
            cases.append(("ai_statement", self.request(ai_manifest)))
            cases.append(("two_videos", self.request(two_videos)))
            two_accounts = self.request(manifest)
            two_accounts["targets"].append(
                {
                    "platform": "Facebook",
                    "accountId": 42,
                    "schedule": None,
                    "settings": {"visibility": "public"},
                }
            )
            cases.append(("two_accounts", two_accounts))
            for label, request in cases:
                with self.subTest(label=label), self.assertRaises(
                    ControlledPublishError
                ) as raised:
                    build_controlled_payloads(
                        request,
                        accounts=[self.account(), self.account(id=42, accountReference="1002")],
                    )
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_unsupported_publish_setting",
                )

    def test_unreadable_video_uses_stable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.manifest(root)
            video = root / "facebook.mp4"
            video.chmod(0)
            self.addCleanup(lambda: video.chmod(0o600) if video.exists() else None)
            with self.assertRaises(ControlledPublishError) as raised:
                build_controlled_payloads(
                    self.request(manifest),
                    accounts=[self.account()],
                )

        self.assertEqual(raised.exception.error_code, "facebook_video_file_invalid")

    def test_mixed_facebook_request_fails_before_task_or_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.manifest(
                root,
                preferred_platforms=("Facebook", "抖音"),
            )
            request = self.request(manifest)
            request["targets"].append(
                {
                    "platform": "抖音",
                    "accountId": 31,
                    "schedule": None,
                }
            )
            with patch(
                "app_core.publish_service.start_desktop_publish"
            ) as start, self.assertRaises(ControlledPublishError) as raised:
                submit_request(request)

        self.assertEqual(
            raised.exception.error_code,
            "facebook_unsupported_publish_setting",
        )
        start.assert_not_called()

    def test_direct_page_request_requires_preflight_before_task_or_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.manifest(Path(temporary))
            request = self.request(manifest, mode="direct")
            with patch(
                "app_core.publish_service.start_desktop_publish"
            ) as start, self.assertRaises(ControlledPublishError) as raised:
                submit_request(request)

        self.assertEqual(raised.exception.error_code, "facebook_preflight_required")
        start.assert_not_called()

    def test_intent_fingerprint_binds_page_account_content_and_video_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = self.build(root)
            base = publish_intent_fingerprint([payload])
            replay = facebook_replay_fingerprint([payload])
            mutations = (
                ("page", {**payload, "facebookExpectedPageReference": "1002"}),
                ("caption", {**payload, "facebookFinalCaption": "changed caption"}),
                ("caption_hash", {**payload, "facebookCaptionSha256": "f" * 64}),
                ("topics", {**payload, "tags": ["ChangedTopic"]}),
                ("visibility", {**payload, "visibility": "private"}),
                ("timing", {**payload, "scheduleMode": "platform_native"}),
            )
            for label, changed in mutations:
                with self.subTest(label=label):
                    self.assertNotEqual(base, publish_intent_fingerprint([changed]))
                    self.assertNotEqual(replay, facebook_replay_fingerprint([changed]))

            changed_account = {**payload, "accountIds": [99]}
            self.assertNotEqual(base, publish_intent_fingerprint([changed_account]))
            self.assertEqual(replay, facebook_replay_fingerprint([changed_account]))

            (root / "facebook.mp4").write_bytes(b"replacement-facebook-video")
            rebuilt = build_controlled_payloads(
                self.request(root / "manifest.json"),
                accounts=[self.account()],
            )[0]
            self.assertEqual(payload["fileList"], rebuilt["fileList"])
            self.assertNotEqual(payload["facebookVideoSha256"], rebuilt["facebookVideoSha256"])
            self.assertNotEqual(base, publish_intent_fingerprint([rebuilt]))
            self.assertNotEqual(replay, facebook_replay_fingerprint([rebuilt]))

    def test_replay_fingerprint_cannot_be_bypassed_with_a_second_local_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = self.build(Path(temporary))
            second = {**first, "accountIds": [99]}

        self.assertNotEqual(
            publish_intent_fingerprint([first]),
            publish_intent_fingerprint([second]),
        )
        self.assertEqual(
            facebook_replay_fingerprint([first]),
            facebook_replay_fingerprint([second]),
        )

    def test_fingerprints_ignore_runtime_auth_session_credentials_and_old_meta_booleans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = self.build(Path(temporary))
            changed = {
                **payload,
                "runtimeMode": "publish",
                "debugDryRun": False,
                "backgroundMode": True,
                "taskId": 999,
                "taskNo": "T999",
                "authorizationId": "other",
                "directAuthorizationId": "other-direct",
                "confirmedPreflightTaskId": 998,
                "accountList": ["another-session.json"],
                "facebookSessionRef": "secret-session",
                "cookies": [{"name": "secret"}],
                "password": "secret",
                "metaBrowserPublishConfirmed": True,
                "metaBrowserAutomationAcknowledged": True,
                "overseasVideoPublishConfirmed": True,
            }

        self.assertEqual(
            publish_intent_fingerprint([payload]),
            publish_intent_fingerprint([changed]),
        )
        self.assertEqual(
            facebook_replay_fingerprint([payload]),
            facebook_replay_fingerprint([changed]),
        )


if __name__ == "__main__":
    unittest.main()
