# -*- coding: utf-8 -*-

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app_core import (
    controlled_publish,
    database,
    overseas_browser_publish,
    task_service,
)
from app_core.controlled_publish import (
    ControlledPublishError,
    authorize_completed_check,
    build_controlled_payloads,
    facebook_replay_fingerprint,
    publish_intent_fingerprint,
    submit_request,
)
from app_core.overseas_meta_content import build_facebook_page_caption
from uploader.meta_uploader.page_form import (
    FacebookPageFormExpectation,
    FacebookPageFormSnapshot,
)


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

            self.assertEqual(
                preflight["facebookFinalCaption"],
                formal["facebookFinalCaption"],
            )
            self.assertEqual(
                preflight["facebookCaptionSha256"],
                formal["facebookCaptionSha256"],
            )
            self.assertEqual(
                publish_intent_fingerprint([preflight]),
                publish_intent_fingerprint([formal]),
            )
            self.assertEqual(
                facebook_replay_fingerprint([preflight]),
                facebook_replay_fingerprint([formal]),
            )

    def test_managed_video_basename_hydrates_to_runtime_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = self.build(root)
            managed = root / "managed-video"
            managed.mkdir()
            managed_video = managed / Path(payload["fileList"][0]).name
            managed_video.write_bytes(Path(payload["fileList"][0]).read_bytes())
            safe_payload = {
                **payload,
                "fileList": [managed_video.name],
                "facebookVideoSize": managed_video.stat().st_size,
            }
            safe_payload.pop("controlledManifestPath", None)

            with patch.object(controlled_publish, "VIDEO_DIR", managed, create=True):
                hydrate = getattr(
                    controlled_publish,
                    "_hydrate_facebook_page_runtime_payload",
                    lambda _payload: {"fileList": []},
                )
                hydrated = hydrate(safe_payload)

        self.assertEqual(hydrated["fileList"], [str(managed_video.resolve())])

    def test_request_preflight_and_formal_share_one_canonical_caption_hash(self) -> None:
        expected_caption = (
            "Facebook title\nsecond line\n\n"
            "Facebook body text\nlast line\n\n"
            "#TopicOne #TopicTwo"
        )
        expected_hash = hashlib.sha256(expected_caption.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.manifest(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["platformOverrides"]["Facebook"] = {
                "title": "  Facebook title\u00a0 \r\nsecond line  ",
                "body": " Facebook body\u202ftext \rlast line \t ",
                "tags": [" TopicOne\u00a0", "TopicOne", "#TopicTwo"],
            }
            manifest.write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
            preflight = build_controlled_payloads(
                self.request(manifest),
                accounts=[self.account()],
            )[0]
            formal = build_controlled_payloads(
                self.request(manifest, mode="formal"),
                accounts=[self.account()],
            )[0]

            for current in (preflight, formal):
                self.assertEqual(current["facebookFinalCaption"], expected_caption)
                self.assertEqual(current["facebookCaptionSha256"], expected_hash)
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
            override_cover_root = root / "override-cover"
            override_cover_root.mkdir()
            override_cover_manifest = self.manifest(override_cover_root)
            override_cover_data = json.loads(
                override_cover_manifest.read_text(encoding="utf-8")
            )
            override_cover_data["platformOverrides"]["Facebook"][
                "coverPath"
            ] = "cover.png"
            override_cover_manifest.write_text(
                json.dumps(override_cover_data, ensure_ascii=False),
                encoding="utf-8",
            )
            cases.append(
                ("platform_override_cover", self.request(override_cover_manifest))
            )
            private = self.request(manifest)
            private["targets"][0]["settings"]["visibility"] = "private"
            cases.append(("visibility", private))
            cases.append(("ai_statement", self.request(ai_manifest)))
            original_root = root / "original"
            original_root.mkdir()
            original_manifest = self.manifest(original_root)
            original_data = json.loads(
                original_manifest.read_text(encoding="utf-8")
            )
            original_data["originalDeclaration"] = True
            original_manifest.write_text(
                json.dumps(original_data, ensure_ascii=False),
                encoding="utf-8",
            )
            cases.append(("original_declaration", self.request(original_manifest)))
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
            video = (root / "facebook.mp4").resolve()
            path_type = type(video)
            real_open = path_type.open

            def open_with_video_permission_denied(path, *args, **kwargs):
                mode = args[0] if args else kwargs.get("mode", "r")
                if path == video and mode == "rb":
                    raise PermissionError("simulated unreadable video")
                return real_open(path, *args, **kwargs)

            with patch.object(
                path_type,
                "open",
                autospec=True,
                side_effect=open_with_video_permission_denied,
            ), self.assertRaises(ControlledPublishError) as raised:
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

    def test_replay_fingerprint_uses_only_the_canonical_publication_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = self.build(root)
            alternate_video = root / "renamed-source.mov"
            alternate_video.write_bytes(Path(payload["fileList"][0]).read_bytes())
            canonical_caption = str(payload["facebookFinalCaption"])
            source_variant = {
                **payload,
                "title": "  Facebook title\u00a0 ",
                "description": "Facebook body with raw @friend and #plain  ",
                "tags": ["#TopicOne", "TopicOne", "#TopicTwo", "#TopicTwo"],
                "fileList": [str(alternate_video)],
                "accountIds": [99],
                "accountDisplayNames": ["Another local display name"],
                "controlledManifestPath": "/different/source/manifest.json",
                "contentProjectId": "another-project",
                "facebookManifestIntentSha256": "f" * 64,
                "facebookFinalCaption": canonical_caption.replace(
                    "\n", "  \r\n"
                ).replace("Facebook body", "Facebook\u00a0body"),
                "facebookCaptionSha256": "0" * 64,
            }

            expected_projection = {
                "contentKind": "reel",
                "pageId": "1001",
                "videoSha256": hashlib.sha256(
                    alternate_video.read_bytes()
                ).hexdigest(),
                "captionSha256": hashlib.sha256(
                    canonical_caption.encode("utf-8")
                ).hexdigest(),
                "visibility": "public",
                "publishIntent": "immediate_public",
            }
            expected = hashlib.sha256(
                json.dumps(
                    expected_projection,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            self.assertEqual(facebook_replay_fingerprint([payload]), expected)
            self.assertEqual(
                facebook_replay_fingerprint([source_variant]),
                expected,
            )
            self.assertNotEqual(
                publish_intent_fingerprint([payload]),
                publish_intent_fingerprint([source_variant]),
            )

            changed_page = {
                **source_variant,
                "facebookExpectedPageReference": "1002",
            }
            changed_caption = {
                **source_variant,
                "facebookFinalCaption": "meaningfully changed caption",
            }
            changed_video = root / "different-video.mp4"
            changed_video.write_bytes(b"different-facebook-video-bytes")
            changed_bytes = {**source_variant, "fileList": [str(changed_video)]}
            for label, changed in (
                ("page", changed_page),
                ("caption", changed_caption),
                ("video_bytes", changed_bytes),
            ):
                with self.subTest(label=label):
                    self.assertNotEqual(
                        expected,
                        facebook_replay_fingerprint([changed]),
                    )

    def test_intent_fingerprint_remains_exact_while_replay_ignores_source_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = self.build(root)
            base = publish_intent_fingerprint([payload])
            replay = facebook_replay_fingerprint([payload])
            canonical_outcome_mutations = (
                ("page", {**payload, "facebookExpectedPageReference": "1002"}),
                ("caption", {**payload, "facebookFinalCaption": "changed caption"}),
            )
            for label, changed in canonical_outcome_mutations:
                with self.subTest(label=label):
                    self.assertNotEqual(base, publish_intent_fingerprint([changed]))
                    self.assertNotEqual(replay, facebook_replay_fingerprint([changed]))

            source_only_mutations = (
                (
                    "caption_hash",
                    {**payload, "facebookCaptionSha256": "f" * 64},
                ),
                ("topics", {**payload, "tags": ["ChangedTopic"]}),
                (
                    "manifest",
                    {**payload, "facebookManifestIntentSha256": "e" * 64},
                ),
            )
            for label, changed in source_only_mutations:
                with self.subTest(label=label):
                    self.assertNotEqual(base, publish_intent_fingerprint([changed]))
                    self.assertEqual(replay, facebook_replay_fingerprint([changed]))

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

    def test_page_v1_metadata_gate_rejects_non_default_values_and_leaves_type_8_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = self.build(Path(temporary))
            cases = (
                ("cover_path", {"coverPath": "/tmp/selected-cover.png"}),
                ("cover_paths", {"coverPaths": {"3:4": "selected.png"}}),
                ("collection", {"collectionName": "Selected collection"}),
                ("timer", {"enableTimer": True}),
                ("schedule_time", {"scheduleTime": "2026-09-01 09:00"}),
                ("scheduled_at", {"scheduledAt": "2026-09-01T09:00:00"}),
                ("schedule_mode", {"scheduleMode": "platform_native"}),
                ("schedule_timezone", {"scheduleTimezone": "Asia/Shanghai"}),
                ("daily_times", {"dailyTimes": ["09:00"]}),
                ("malformed_timer", {"enableTimer": {"enabled": False}}),
                ("malformed_frequency", {"videosPerDay": []}),
                ("visibility", {"visibility": "private"}),
                ("original", {"originalDeclaration": True}),
                ("ai_generated", {"aiGenerated": True}),
                (
                    "ai_confirmation",
                    {"aiDeclarationExplicitlyConfirmed": True},
                ),
                (
                    "ai_disclosure",
                    {
                        "aiDisclosure": {
                            "containsAiGeneratedContent": True,
                            "contentKinds": ["video"],
                            "assetPaths": [],
                            "allowPlatformAutoDeclaration": False,
                        }
                    },
                ),
                (
                    "false_ai_disclosure_metadata",
                    {"aiDisclosure": {"containsAiGeneratedContent": False}},
                ),
                ("nested_settings", {"settings": {"coverPath": "cover.png"}}),
            )
            for label, changes in cases:
                with self.subTest(label=label), self.assertRaises(
                    ControlledPublishError
                ) as raised:
                    controlled_publish.validate_facebook_page_v1_metadata(
                        {**payload, **changes}
                    )
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_unsupported_publish_setting",
                )

            type_8 = {
                **payload,
                "type": 8,
                "coverPath": "/tmp/instagram-cover.png",
                "collectionName": "Instagram collection",
                "visibility": "private",
                "originalDeclaration": True,
                "aiGenerated": True,
            }
            controlled_publish.validate_facebook_page_v1_metadata(type_8)

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


class FacebookPageFormalClaimTests(unittest.TestCase):
    """Facebook Page authorization evidence and claim atomicity contracts."""

    _DEFAULT_RECEIPT = object()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db_path = Path(self.temporary.name) / "database.db"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.feature_patch = patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
            clear=False,
        )
        self.feature_patch.start()
        self.addCleanup(self.feature_patch.stop)
        self.worker_start_patch = patch(
            "app_core.publish_service.start_controlled_facebook_publish",
            side_effect=lambda formal_task_id, *, runtime_video_path: (
                task_service.get_task(int(formal_task_id))
            ),
        )
        self.worker_start = self.worker_start_patch.start()
        self.addCleanup(self.worker_start_patch.stop)
        self.video = Path(self.temporary.name) / "facebook.mp4"
        self.video.write_bytes(b"facebook-page-claim-video")
        database.ensure_schema()

    def payload(
        self,
        *,
        account_id: int = 41,
        page_id: str = "1001",
        caption: str = "Facebook Page exact caption\n\n#OneClick",
        manifest_hash: str | None = None,
    ) -> dict:
        caption_hash = hashlib.sha256(caption.encode("utf-8")).hexdigest()
        return {
            "type": 9,
            "contentType": "video",
            "title": "Facebook Page exact title",
            "description": "Facebook Page exact caption",
            "tags": ["OneClick"],
            "fileList": [str(self.video)],
            "accountList": [f"facebook-page-{account_id}.json"],
            "accountIds": [account_id],
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "backgroundMode": True,
            "facebookExpectedPageReference": page_id,
            "facebookFinalCaption": caption,
            "facebookCaptionSha256": caption_hash,
            "facebookVideoSha256": hashlib.sha256(
                self.video.read_bytes()
            ).hexdigest(),
            "facebookVideoSize": self.video.stat().st_size,
            "facebookManifestIntentSha256": (
                manifest_hash
                or hashlib.sha256(
                    f"manifest:{page_id}:{caption}".encode("utf-8")
                ).hexdigest()
            ),
            "visibility": "public",
            "scheduleMode": "immediate",
            "scheduledAt": "",
            "scheduleTime": "",
            "scheduleTimezone": "",
            "enableTimer": False,
        }

    def formal_payload(self, payload: dict) -> dict:
        formal = json.loads(json.dumps(payload, ensure_ascii=False))
        formal.update(
            {
                "runtimeMode": "publish",
                "debugDryRun": False,
                "backgroundMode": False,
            }
        )
        return formal

    def install_task7_evidence_types(self):
        module_name = "uploader.meta_uploader.content_list"
        module = types.ModuleType(module_name)

        class FacebookReelReceipt:
            def __init__(self, page_id, reel_id, url, published_at):
                self.page_id = page_id
                self.reel_id = reel_id
                self.url = url
                self.published_at = published_at

        class FacebookReelMatch:
            def __init__(
                self,
                status,
                receipt,
                new_count,
                matching_count,
            ):
                self.status = status
                self.receipt = receipt
                self.new_count = new_count
                self.matching_count = matching_count

        class FacebookPlatformDecision:
            def __init__(
                self,
                kind,
                page_id,
                observed_at,
                evidence_sha256,
            ):
                self.kind = kind
                self.page_id = page_id
                self.observed_at = observed_at
                self.evidence_sha256 = evidence_sha256

        module.FacebookReelReceipt = FacebookReelReceipt
        module.FacebookReelMatch = FacebookReelMatch
        module.FacebookPlatformDecision = FacebookPlatformDecision
        module_patch = patch.dict(sys.modules, {module_name: module})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        return module

    def verified_form_receipt(
        self,
        payload: dict | None = None,
        **changes,
    ) -> dict:
        payload = payload or self.payload()
        expectation = FacebookPageFormExpectation(
            page_id=str(payload["facebookExpectedPageReference"]),
            content_kind="reel",
            video_name=self.video.name,
            video_size=self.video.stat().st_size,
            video_sha256=str(payload["facebookVideoSha256"]),
            caption=str(payload["facebookFinalCaption"]),
            visibility="public",
        )
        snapshot = FacebookPageFormSnapshot(
            page_id=expectation.page_id,
            content_kind="reel",
            video_name=self.video.name,
            video_count=1,
            caption=expectation.caption,
            visibility="public",
            final_action_label="Publish",
            final_action_ready=True,
        )
        receipt = overseas_browser_publish._public_form_receipt(
            {
                "accountId": int(payload["accountIds"][0]),
                "expectation": expectation,
            },
            snapshot,
            phase="platform_form_verified",
            final_action_triggered=False,
        )
        receipt.update(changes)
        return receipt

    def completed_preflight(
        self,
        *,
        payload: dict | None = None,
        receipt: object = _DEFAULT_RECEIPT,
        event_type: str = "facebook_platform_form_verified",
    ) -> int:
        payload = payload or self.payload()
        task = task_service.create_pending_task(
            [payload],
            mode="oneclick_preflight",
        )
        stored_receipt = (
            self.verified_form_receipt(payload)
            if receipt is self._DEFAULT_RECEIPT
            else receipt
        )
        receipt_json = (
            json.dumps(
                stored_receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if isinstance(stored_receipt, dict) and stored_receipt
            else ""
        )
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE publish_tasks
                SET status = 'success', successCount = 1, failedCount = 0,
                    finishedAt = '2026-08-30 10:00:00'
                WHERE id = ?
                """,
                (task["id"],),
            )
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = 'success', accountId = ?, receiptJson = ?,
                    finishedAt = '2026-08-30 10:00:00'
                WHERE taskId = ? AND platformType = 9
                """,
                (int(payload["accountIds"][0]), receipt_json, task["id"]),
            )
            if event_type:
                conn.execute(
                    """
                    INSERT INTO publish_task_events
                        (taskId, level, eventType, message, createdAt)
                    VALUES (?, 'info', ?, 'Facebook Page form route observed',
                            '2026-08-30 10:00:00')
                    """,
                    (task["id"], event_type),
                )
            conn.commit()
        return int(task["id"])

    def read_authorization(self, authorization_id: str) -> dict:
        with database.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM controlled_publish_authorizations
                WHERE authorizationId = ?
                """,
                (authorization_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def authorized_preflight(
        self,
        *,
        payload: dict | None = None,
    ) -> tuple[int, str, dict]:
        payload = payload or self.payload()
        task_id = self.completed_preflight(payload=payload)
        authorization = authorize_completed_check(task_id)
        return task_id, str(authorization["authorizationId"]), payload

    def create_formal(
        self,
        task_id: int,
        authorization_id: str,
        payload: dict,
        *,
        formal_payload: dict | None = None,
    ) -> dict:
        return controlled_publish._create_claimed_facebook_page_task(
            [
                formal_payload
                if formal_payload is not None
                else self.formal_payload(payload)
            ],
            preflight_task_id=task_id,
            authorization_id=authorization_id,
        )

    def mutate_preflight_receipt(self, task_id: int, **changes) -> None:
        with database.connect() as conn:
            row = conn.execute(
                """
                SELECT receiptJson FROM publish_task_items
                WHERE taskId = ? AND platformType = 9
                """,
                (task_id,),
            ).fetchone()
            receipt = json.loads(str(row["receiptJson"] or "{}"))
            receipt.update(changes)
            conn.execute(
                """
                UPDATE publish_task_items SET receiptJson = ?
                WHERE taskId = ? AND platformType = 9
                """,
                (
                    json.dumps(
                        receipt,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    task_id,
                ),
            )
            conn.commit()

    def checkpoint_receipt(self, payload: dict, **changes) -> dict:
        receipt = {
            "accountId": int(payload["accountIds"][0]),
            "pageId": str(payload["facebookExpectedPageReference"]),
            "videoName": self.video.name,
            "videoSize": self.video.stat().st_size,
            "videoSha256": str(payload["facebookVideoSha256"]),
            "captionSha256": str(payload["facebookCaptionSha256"]),
            "visibility": "public",
            "phase": "final_action_claimed",
            "platformWriteOccurred": True,
            "finalActionTriggered": False,
            "finalButtonEnabled": True,
            "baseline": {
                "pageId": str(payload["facebookExpectedPageReference"]),
                "rows": [
                    {
                        "reelId": "old-reel",
                        "url": "https://www.facebook.com/reel/old-reel",
                        "publishedAt": "2026-08-29T09:00:00+08:00",
                        "captionSha256": "a" * 64,
                        "caption": "must never be persisted",
                    }
                ],
                "dom": "must never be persisted",
            },
            "formSnapshot": {
                "pageId": str(payload["facebookExpectedPageReference"]),
                "videoName": self.video.name,
                "videoSize": self.video.stat().st_size,
                "videoSha256": str(payload["facebookVideoSha256"]),
                "captionSha256": str(payload["facebookCaptionSha256"]),
                "visibility": "public",
                "finalButtonLabel": "Publish",
                "finalButtonReady": True,
                "caption": "must never be persisted",
                "sessionPath": "/private/facebook-session.json",
            },
            "cookie": "must never be persisted",
            "blocksReplay": False,
            "baselineHash": "0" * 64,
            "formSnapshotHash": "0" * 64,
        }
        receipt.update(changes)
        return receipt

    def transition_to(self, task_id: int, payload: dict, state: str) -> None:
        if state == "reserved":
            return
        controlled_publish.mark_facebook_page_checkpoint(
            task_id,
            expected_state="reserved",
            new_state=("safe_failed" if state == "safe_failed" else "final_action_claimed"),
            receipt=(
                {"pageId": str(payload["facebookExpectedPageReference"])}
                if state == "safe_failed"
                else self.checkpoint_receipt(payload)
            ),
        )
        if state in {"safe_failed", "final_action_claimed"}:
            return
        if state == "ambiguous":
            controlled_publish.mark_facebook_page_checkpoint(
                task_id,
                expected_state="final_action_claimed",
                new_state="ambiguous",
                receipt={"pageId": str(payload["facebookExpectedPageReference"])},
            )
            return
        controlled_publish.mark_facebook_page_checkpoint(
            task_id,
            expected_state="final_action_claimed",
            new_state="final_action_clicked",
            receipt={"pageId": str(payload["facebookExpectedPageReference"])},
        )
        if state == "final_action_clicked":
            return
        task7 = self.install_task7_evidence_types()
        with database.connect() as conn:
            clicked_at = conn.execute(
                """
                SELECT clickedAt FROM facebook_page_publish_claims
                WHERE taskId = ?
                """,
                (task_id,),
            ).fetchone()[0]
        published_at = (
            datetime.fromisoformat(clicked_at) + timedelta(seconds=1)
        ).isoformat()
        controlled_publish.mark_facebook_page_checkpoint(
            task_id,
            expected_state="final_action_clicked",
            new_state=state,
            receipt={
                "reelMatch": task7.FacebookReelMatch(
                    status="unique",
                    receipt=task7.FacebookReelReceipt(
                        page_id=str(
                            payload["facebookExpectedPageReference"]
                        ),
                        reel_id="new-reel",
                        url="https://www.facebook.com/reel/new-reel",
                        published_at=published_at,
                    ),
                    new_count=1,
                    matching_count=1,
                )
            },
        )

    def test_facebook_authorization_requires_verified_platform_form_receipt(self) -> None:
        payload = self.payload()
        with patch.object(
            overseas_browser_publish,
            "_public_form_receipt",
            wraps=overseas_browser_publish._public_form_receipt,
        ) as real_preflight_receipt_builder:
            receipt = self.verified_form_receipt(payload)
        real_preflight_receipt_builder.assert_called_once()
        task_id = self.completed_preflight(payload=payload, receipt=receipt)

        authorization = authorize_completed_check(task_id)
        row = self.read_authorization(str(authorization["authorizationId"]))

        self.assertEqual(row.get("authorizationScope"), "formal")
        self.assertEqual(len(str(row.get("preflightReceiptHash") or "")), 64)

    def test_invalid_write_or_snapshot_hash_evidence_stops_before_side_effects(self) -> None:
        payload = self.payload()
        valid = self.verified_form_receipt(payload)
        mismatched_hash = self.verified_form_receipt(
            self.payload(page_id="1002")
        )["formSnapshotHash"]
        self.assertNotEqual(valid["formSnapshotHash"], mismatched_hash)

        def without(key: str) -> dict:
            return {name: value for name, value in valid.items() if name != key}

        cases = (
            ("write_false", {**valid, "platformWriteOccurred": False}),
            ("write_missing", without("platformWriteOccurred")),
            ("hash_missing", without("formSnapshotHash")),
            ("hash_placeholder", {**valid, "formSnapshotHash": "f" * 64}),
            (
                "hash_arbitrary",
                {
                    **valid,
                    "formSnapshotHash": hashlib.sha256(
                        b"caller-chosen-form-snapshot"
                    ).hexdigest(),
                },
            ),
            ("hash_mismatched", {**valid, "formSnapshotHash": mismatched_hash}),
        )
        with patch.object(
            overseas_browser_publish,
            "_facebook_page_session",
        ) as browser_session:
            for label, receipt in cases:
                with self.subTest(label=label):
                    self.worker_start.reset_mock()
                    browser_session.reset_mock()
                    task_id = self.completed_preflight(
                        payload=payload,
                        receipt=receipt,
                    )
                    with self.assertRaises(ControlledPublishError) as raised:
                        authorize_completed_check(task_id)
                    self.assertEqual(
                        raised.exception.error_code,
                        "facebook_publish_authorization_invalid",
                    )
                    with database.connect() as conn:
                        counts = {}
                        for table in (
                            "controlled_publish_authorizations",
                            "facebook_page_publish_claims",
                        ):
                            exists = conn.execute(
                                "SELECT 1 FROM sqlite_master "
                                "WHERE type = 'table' AND name = ?",
                                (table,),
                            ).fetchone()
                            counts[table] = (
                                int(
                                    conn.execute(
                                        f"SELECT COUNT(*) FROM {table}"
                                    ).fetchone()[0]
                                )
                                if exists is not None
                                else 0
                            )
                    self.assertEqual(counts["controlled_publish_authorizations"], 0)
                    self.assertEqual(counts["facebook_page_publish_claims"], 0)
                    self.worker_start.assert_not_called()
                    browser_session.assert_not_called()

    def test_text_or_route_success_cannot_authorize_facebook_formal_publish(self) -> None:
        payload = self.payload()
        incomplete = {
            "phase": "platform_form_verified",
            "pageId": "1001",
            "finalButtonEnabled": True,
            "finalActionTriggered": False,
        }
        cases = (
            ("route_only", None, "facebook_platform_form_verified"),
            ("text_only", incomplete, "platform_preflight"),
            (
                "wrong_phase",
                self.verified_form_receipt(payload, phase="local_validation_passed"),
                "facebook_platform_form_verified",
            ),
            (
                "missing_final_button",
                {
                    key: value
                    for key, value in self.verified_form_receipt(payload).items()
                    if key != "finalButtonEnabled"
                },
                "facebook_platform_form_verified",
            ),
        )
        for label, receipt, event_type in cases:
            with self.subTest(label=label):
                task_id = self.completed_preflight(
                    payload=payload,
                    receipt=receipt,
                    event_type=event_type,
                )
                with self.assertRaises(ControlledPublishError) as raised:
                    authorize_completed_check(task_id)
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_publish_authorization_invalid",
                )

    def test_changed_preflight_receipt_does_not_consume_authorization(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()
        self.mutate_preflight_receipt(task_id, pageId="1002")

        with self.assertRaises(ControlledPublishError) as raised:
            self.create_formal(task_id, authorization_id, payload)

        self.assertEqual(
            raised.exception.error_code,
            "facebook_publish_authorization_invalid",
        )
        self.assertIsNone(self.read_authorization(authorization_id)["consumedAt"])

    def test_changed_form_receipt_hash_does_not_consume_authorization(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()
        self.mutate_preflight_receipt(task_id, videoSha256="b" * 64)

        with self.assertRaises(ControlledPublishError) as raised:
            self.create_formal(task_id, authorization_id, payload)

        self.assertEqual(
            raised.exception.error_code,
            "facebook_publish_authorization_invalid",
        )
        self.assertIsNone(self.read_authorization(authorization_id)["consumedAt"])

    def test_changed_page_or_intent_does_not_consume_authorization(self) -> None:
        changes = (
            ("page", {"facebookExpectedPageReference": "1002"}),
            ("caption", {"facebookCaptionSha256": "c" * 64}),
        )
        for label, mutation in changes:
            with self.subTest(label=label):
                payload = self.payload(caption=f"intent-{label}")
                task_id, authorization_id, payload = self.authorized_preflight(
                    payload=payload
                )
                changed = {**payload, **mutation}
                with self.assertRaises(ControlledPublishError) as raised:
                    self.create_formal(task_id, authorization_id, changed)
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_publish_authorization_invalid",
                )
                self.assertIsNone(
                    self.read_authorization(authorization_id)["consumedAt"]
                )

    def test_expired_or_consumed_facebook_authorization_is_invalid(self) -> None:
        first_task, expired_id, payload = self.authorized_preflight(
            payload=self.payload(caption="expired authorization")
        )
        second_task, consumed_id, second_payload = self.authorized_preflight(
            payload=self.payload(caption="consumed authorization")
        )
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE controlled_publish_authorizations
                SET expiresAt = ? WHERE authorizationId = ?
                """,
                (
                    (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                    expired_id,
                ),
            )
            conn.execute(
                """
                UPDATE controlled_publish_authorizations
                SET consumedAt = ? WHERE authorizationId = ?
                """,
                (datetime.now(timezone.utc).isoformat(), consumed_id),
            )
            conn.commit()

        for label, task_id, authorization_id, current_payload in (
            ("expired", first_task, expired_id, payload),
            ("consumed", second_task, consumed_id, second_payload),
        ):
            with self.subTest(label=label), self.assertRaises(
                ControlledPublishError
            ) as raised:
                self.create_formal(task_id, authorization_id, current_payload)
            self.assertEqual(
                raised.exception.error_code,
                "facebook_publish_authorization_invalid",
            )

    def test_caller_supplied_receipt_hash_is_ignored(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()
        formal = self.formal_payload(payload)
        formal["preflightReceiptHash"] = "0" * 64
        formal["receipt"] = {
            "pageId": "9999",
            "finalButtonEnabled": False,
        }

        created = self.create_formal(
            task_id,
            authorization_id,
            payload,
            formal_payload=formal,
        )

        with database.connect() as conn:
            claim = conn.execute(
                """
                SELECT preflightReceiptHash FROM facebook_page_publish_claims
                WHERE taskId = ?
                """,
                (created["id"],),
            ).fetchone()
        self.assertNotEqual(claim["preflightReceiptHash"], "0" * 64)
        self.assertEqual(
            claim["preflightReceiptHash"],
            self.read_authorization(authorization_id)["preflightReceiptHash"],
        )

    def test_generic_authorization_consumer_cannot_bypass_page_claim(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()

        with database.connect() as conn, self.assertRaises(
            ControlledPublishError
        ) as raised:
            controlled_publish.consume_authorization(
                conn,
                authorization_id,
                task_id,
                [self.formal_payload(payload)],
            )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_publish_authorization_invalid",
        )
        self.assertIsNone(self.read_authorization(authorization_id)["consumedAt"])
        with database.connect() as conn:
            claim_count = conn.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'table' AND name = 'facebook_page_publish_claims'
                """
            ).fetchone()[0]
        self.assertEqual(claim_count, 0)

    def test_two_facebook_formal_authorizations_compete_for_one_page_claim(self) -> None:
        payload = self.payload()
        task_id = self.completed_preflight(payload=payload)
        grants = [authorize_completed_check(task_id) for _ in range(2)]
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        outcomes: list[tuple[str, object]] = []

        def submit(authorization_id: str) -> None:
            barrier.wait()
            try:
                task = self.create_formal(task_id, authorization_id, payload)
            except ControlledPublishError as exc:
                outcome: tuple[str, object] = ("error", exc.error_code)
            except Exception as exc:  # pragma: no cover - assertion diagnoses it
                outcome = ("unexpected", repr(exc))
            else:
                outcome = ("ok", task["id"])
            with lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(
                target=submit,
                args=(str(grant["authorizationId"]),),
            )
            for grant in grants
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        with database.connect() as conn:
            formal_tasks = conn.execute(
                "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
            ).fetchone()[0]
            claims = conn.execute(
                "SELECT COUNT(*) FROM facebook_page_publish_claims"
            ).fetchone()[0]
            consumed = conn.execute(
                """
                SELECT COUNT(*) FROM controlled_publish_authorizations
                WHERE consumedAt IS NOT NULL
                """
            ).fetchone()[0]

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(kind == "ok" for kind, _ in outcomes), 1)
        self.assertEqual(
            outcomes.count(("error", "facebook_duplicate_submit_blocked")),
            1,
        )
        self.assertEqual(formal_tasks, 1)
        self.assertEqual(claims, 1)
        self.assertEqual(consumed, 1)

    def test_claim_blocks_same_page_intent_across_two_local_account_ids(self) -> None:
        first_payload = self.payload(account_id=41)
        first_task, first_authorization, _ = self.authorized_preflight(
            payload=first_payload
        )
        self.create_formal(first_task, first_authorization, first_payload)
        second_payload = self.payload(account_id=42)
        second_task, second_authorization, _ = self.authorized_preflight(
            payload=second_payload
        )

        with self.assertRaises(ControlledPublishError) as raised:
            self.create_formal(second_task, second_authorization, second_payload)

        self.assertEqual(
            raised.exception.error_code,
            "facebook_duplicate_submit_blocked",
        )
        self.assertIsNone(
            self.read_authorization(second_authorization)["consumedAt"]
        )

    def test_atomic_creation_rolls_back_authorization_when_task_insert_fails(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()

        with patch.object(
            task_service,
            "_insert_pending_task",
            side_effect=RuntimeError("forced task insert failure"),
        ), self.assertRaisesRegex(RuntimeError, "forced task insert failure"):
            self.create_formal(task_id, authorization_id, payload)

        with database.connect() as conn:
            formal_tasks = conn.execute(
                "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
            ).fetchone()[0]
            claims = conn.execute(
                "SELECT COUNT(*) FROM facebook_page_publish_claims"
            ).fetchone()[0]
        self.assertIsNone(self.read_authorization(authorization_id)["consumedAt"])
        self.assertEqual(formal_tasks, 0)
        self.assertEqual(claims, 0)

    def test_atomic_creation_rolls_back_task_and_claim_when_binding_fails(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()

        with patch.object(
            controlled_publish,
            "_bind_facebook_page_task_item",
            side_effect=RuntimeError("forced item binding failure"),
            create=True,
        ), self.assertRaisesRegex(RuntimeError, "forced item binding failure"):
            self.create_formal(task_id, authorization_id, payload)

        with database.connect() as conn:
            formal_tasks = conn.execute(
                "SELECT COUNT(*) FROM publish_tasks WHERE mode = 'oneclick_publish'"
            ).fetchone()[0]
            formal_items = conn.execute(
                """
                SELECT COUNT(*) FROM publish_task_items
                WHERE taskId IN (
                    SELECT id FROM publish_tasks WHERE mode = 'oneclick_publish'
                )
                """
            ).fetchone()[0]
            claims = conn.execute(
                "SELECT COUNT(*) FROM facebook_page_publish_claims"
            ).fetchone()[0]
        self.assertIsNone(self.read_authorization(authorization_id)["consumedAt"])
        self.assertEqual((formal_tasks, formal_items, claims), (0, 0, 0))

    def test_authorization_task_item_and_claim_commit_together(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()

        task = self.create_formal(task_id, authorization_id, payload)
        expected_intent = publish_intent_fingerprint([payload])
        expected_replay = facebook_replay_fingerprint([payload])

        with database.connect() as conn:
            item = conn.execute(
                """
                SELECT accountId, authorizationSnapshotHash
                FROM publish_task_items WHERE taskId = ?
                """,
                (task["id"],),
            ).fetchone()
            claim = conn.execute(
                """
                SELECT * FROM facebook_page_publish_claims WHERE taskId = ?
                """,
                (task["id"],),
            ).fetchone()
        authorization = self.read_authorization(authorization_id)
        self.assertIsNotNone(authorization["consumedAt"])
        self.assertEqual(item["accountId"], 41)
        self.assertEqual(item["authorizationSnapshotHash"], expected_intent)
        self.assertEqual(claim["pageReference"], "1001")
        self.assertEqual(claim["publishIntentFingerprint"], expected_intent)
        self.assertEqual(claim["replayFingerprint"], expected_replay)
        self.assertEqual(claim["preflightTaskId"], task_id)
        self.assertEqual(
            claim["preflightReceiptHash"],
            authorization["preflightReceiptHash"],
        )
        self.assertEqual(claim["state"], "reserved")
        self.assertEqual(claim["blocksReplay"], 1)

    def test_safe_failed_history_releases_active_slot_but_is_preserved(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()
        first = self.create_formal(task_id, authorization_id, payload)
        self.transition_to(first["id"], payload, "safe_failed")
        second_authorization = authorize_completed_check(task_id)

        second = self.create_formal(
            task_id,
            str(second_authorization["authorizationId"]),
            payload,
        )

        with database.connect() as conn:
            rows = conn.execute(
                """
                SELECT taskId, state, blocksReplay
                FROM facebook_page_publish_claims ORDER BY id
                """
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (first["id"], "safe_failed", 0),
                (second["id"], "reserved", 1),
            ],
        )

    def test_confirmed_not_published_releases_active_slot_but_is_preserved(self) -> None:
        task7 = self.install_task7_evidence_types()
        task_id, authorization_id, payload = self.authorized_preflight()
        first = self.create_formal(task_id, authorization_id, payload)
        self.transition_to(first["id"], payload, "ambiguous")
        try:
            controlled_publish.mark_facebook_page_checkpoint(
                first["id"],
                expected_state="ambiguous",
                new_state="confirmed_not_published",
                receipt={
                    "platformDecision": task7.FacebookPlatformDecision(
                        kind="rejected_no_creation",
                        page_id="1001",
                        observed_at="2026-08-30T10:10:00+08:00",
                        evidence_sha256="d" * 64,
                    )
                },
            )
        except ControlledPublishError as exc:
            self.fail(f"trusted Task 7 platform decision rejected: {exc.error_code}")
        second_authorization = authorize_completed_check(task_id)

        second = self.create_formal(
            task_id,
            str(second_authorization["authorizationId"]),
            payload,
        )

        with database.connect() as conn:
            rows = conn.execute(
                """
                SELECT taskId, state, blocksReplay
                FROM facebook_page_publish_claims ORDER BY id
                """
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (first["id"], "confirmed_not_published", 0),
                (second["id"], "reserved", 1),
            ],
        )

    def test_caller_platform_decision_cannot_release_ambiguous_claim(self) -> None:
        self.install_task7_evidence_types()
        task_id, authorization_id, payload = self.authorized_preflight()
        task = self.create_formal(task_id, authorization_id, payload)
        self.transition_to(task["id"], payload, "ambiguous")

        with self.assertRaises(ControlledPublishError) as raised:
            controlled_publish.mark_facebook_page_checkpoint(
                task["id"],
                expected_state="ambiguous",
                new_state="confirmed_not_published",
                receipt={
                    "pageId": "1001",
                    "platformDecision": {
                        "pageId": "1001",
                        "kind": "rejected_no_creation",
                        "observedAt": "2026-08-30T10:10:00+08:00",
                        "evidenceSha256": "d" * 64,
                    },
                },
            )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_claim_lifecycle_invalid",
        )
        with database.connect() as conn:
            claim = conn.execute(
                """
                SELECT state, blocksReplay, platformDecisionJson
                FROM facebook_page_publish_claims WHERE taskId = ?
                """,
                (task["id"],),
            ).fetchone()
        self.assertEqual(tuple(claim), ("ambiguous", 1, "{}"))

    def test_same_named_forged_platform_decision_type_is_rejected(self) -> None:
        self.install_task7_evidence_types()
        task_id, authorization_id, payload = self.authorized_preflight()
        task = self.create_formal(task_id, authorization_id, payload)
        self.transition_to(task["id"], payload, "ambiguous")
        forged_type = type(
            "FacebookPlatformDecision",
            (),
            {
                "__module__": "uploader.meta_uploader.content_list",
                "kind": "rejected_no_creation",
                "page_id": "1001",
                "observed_at": "2026-08-30T10:10:00+08:00",
                "evidence_sha256": "d" * 64,
            },
        )

        with self.assertRaises(ControlledPublishError) as raised:
            controlled_publish.mark_facebook_page_checkpoint(
                task["id"],
                expected_state="ambiguous",
                new_state="confirmed_not_published",
                receipt={"platformDecision": forged_type()},
            )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_claim_lifecycle_invalid",
        )
        with database.connect() as conn:
            claim = conn.execute(
                """
                SELECT state, blocksReplay
                FROM facebook_page_publish_claims WHERE taskId = ?
                """,
                (task["id"],),
            ).fetchone()
        self.assertEqual(tuple(claim), ("ambiguous", 1))

    def test_ordinary_mapping_cannot_release_ambiguous_claim_as_succeeded(
        self,
    ) -> None:
        self.install_task7_evidence_types()
        task_id, authorization_id, payload = self.authorized_preflight()
        task = self.create_formal(task_id, authorization_id, payload)
        self.transition_to(task["id"], payload, "final_action_clicked")
        controlled_publish.mark_facebook_page_checkpoint(
            task["id"],
            expected_state="final_action_clicked",
            new_state="ambiguous",
            receipt={"pageId": "1001"},
        )

        with self.assertRaises(ControlledPublishError) as raised:
            controlled_publish.mark_facebook_page_checkpoint(
                task["id"],
                expected_state="ambiguous",
                new_state="succeeded",
                receipt={
                    "pageId": "1001",
                    "captionSha256": payload["facebookCaptionSha256"],
                    "reelId": "forged-reel",
                    "url": "https://www.facebook.com/reel/forged-reel",
                    "publishedAt": "2026-08-30T10:05:00+08:00",
                },
            )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_claim_lifecycle_invalid",
        )
        with database.connect() as conn:
            claim = conn.execute(
                """
                SELECT state, blocksReplay, reelId, reelUrl
                FROM facebook_page_publish_claims WHERE taskId = ?
                """,
                (task["id"],),
            ).fetchone()
        self.assertEqual(tuple(claim), ("ambiguous", 1, "", ""))

    def test_trusted_task7_unique_reel_match_allows_succeeded(self) -> None:
        task7 = self.install_task7_evidence_types()
        task_id, authorization_id, payload = self.authorized_preflight()
        task = self.create_formal(task_id, authorization_id, payload)
        self.transition_to(task["id"], payload, "final_action_clicked")
        controlled_publish.mark_facebook_page_checkpoint(
            task["id"],
            expected_state="final_action_clicked",
            new_state="ambiguous",
            receipt={"pageId": "1001"},
        )
        with database.connect() as conn:
            clicked_at = conn.execute(
                """
                SELECT clickedAt FROM facebook_page_publish_claims
                WHERE taskId = ?
                """,
                (task["id"],),
            ).fetchone()[0]
        published_at = (
            datetime.fromisoformat(clicked_at) + timedelta(seconds=1)
        ).isoformat()
        match = task7.FacebookReelMatch(
            status="unique",
            receipt=task7.FacebookReelReceipt(
                page_id="1001",
                reel_id="new-reel",
                url="https://www.facebook.com/reel/new-reel",
                published_at=published_at,
            ),
            new_count=1,
            matching_count=1,
        )

        try:
            controlled_publish.mark_facebook_page_checkpoint(
                task["id"],
                expected_state="ambiguous",
                new_state="succeeded",
                receipt={
                    "reelMatch": match,
                    "captionSha256": "f" * 64,
                },
            )
        except ControlledPublishError as exc:
            self.fail(f"trusted Task 7 Reel match was rejected: {exc.error_code}")

        with database.connect() as conn:
            claim = dict(
                conn.execute(
                    """
                    SELECT * FROM facebook_page_publish_claims
                    WHERE taskId = ?
                    """,
                    (task["id"],),
                ).fetchone()
            )
        persisted_receipt = json.loads(claim["receiptJson"])
        self.assertEqual(claim["state"], "succeeded")
        self.assertEqual(claim["reelId"], "new-reel")
        self.assertEqual(
            persisted_receipt["captionSha256"],
            payload["facebookCaptionSha256"],
        )
        self.assertEqual(persisted_receipt["publishedAt"], published_at)

    def test_trusted_reel_match_must_bind_unique_new_page_and_click_time(
        self,
    ) -> None:
        task7 = self.install_task7_evidence_types()
        cases = (
            ("wrong_page", {"page_id": "1002"}),
            ("baseline_reel", {"reel_id": "old-reel"}),
            ("before_click", {"published_delta": -1}),
            ("zero_new", {"new_count": 0}),
            ("multiple_matches", {"matching_count": 2}),
            ("not_unique", {"status": "mismatch"}),
        )
        for label, changes in cases:
            with self.subTest(label=label):
                payload = self.payload(caption=f"typed match {label}")
                task_id, authorization_id, _ = self.authorized_preflight(
                    payload=payload
                )
                task = self.create_formal(task_id, authorization_id, payload)
                self.transition_to(task["id"], payload, "final_action_clicked")
                controlled_publish.mark_facebook_page_checkpoint(
                    task["id"],
                    expected_state="final_action_clicked",
                    new_state="ambiguous",
                    receipt={"pageId": "1001"},
                )
                with database.connect() as conn:
                    clicked_at = conn.execute(
                        """
                        SELECT clickedAt FROM facebook_page_publish_claims
                        WHERE taskId = ?
                        """,
                        (task["id"],),
                    ).fetchone()[0]
                published_at = (
                    datetime.fromisoformat(clicked_at)
                    + timedelta(seconds=changes.get("published_delta", 1))
                ).isoformat()
                match = task7.FacebookReelMatch(
                    status=changes.get("status", "unique"),
                    receipt=task7.FacebookReelReceipt(
                        page_id=changes.get("page_id", "1001"),
                        reel_id=changes.get("reel_id", "new-reel"),
                        url="https://www.facebook.com/reel/new-reel",
                        published_at=published_at,
                    ),
                    new_count=changes.get("new_count", 1),
                    matching_count=changes.get("matching_count", 1),
                )

                with self.assertRaises(ControlledPublishError) as raised:
                    controlled_publish.mark_facebook_page_checkpoint(
                        task["id"],
                        expected_state="ambiguous",
                        new_state="succeeded",
                        receipt={"reelMatch": match},
                    )

                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_claim_lifecycle_invalid",
                )
                with database.connect() as conn:
                    state = conn.execute(
                        """
                        SELECT state FROM facebook_page_publish_claims
                        WHERE taskId = ?
                        """,
                        (task["id"],),
                    ).fetchone()[0]
                self.assertEqual(state, "ambiguous")

    def test_final_action_claimed_clicked_ambiguous_and_succeeded_block_republish(self) -> None:
        for state in (
            "final_action_claimed",
            "final_action_clicked",
            "ambiguous",
            "succeeded",
        ):
            with self.subTest(state=state):
                payload = self.payload(caption=f"blocking state {state}")
                task_id, authorization_id, _ = self.authorized_preflight(
                    payload=payload
                )
                first = self.create_formal(task_id, authorization_id, payload)
                self.transition_to(first["id"], payload, state)
                second_authorization = authorize_completed_check(task_id)
                with self.assertRaises(ControlledPublishError) as raised:
                    self.create_formal(
                        task_id,
                        str(second_authorization["authorizationId"]),
                        payload,
                    )
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_duplicate_submit_blocked",
                )
                self.assertIsNone(
                    self.read_authorization(
                        str(second_authorization["authorizationId"])
                    )["consumedAt"]
                )

    def test_checkpoint_persists_only_safe_json_and_recomputed_hashes(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()
        task = self.create_formal(task_id, authorization_id, payload)

        controlled_publish.mark_facebook_page_checkpoint(
            task["id"],
            expected_state="reserved",
            new_state="final_action_claimed",
            receipt=self.checkpoint_receipt(payload),
        )

        with database.connect() as conn:
            claim = dict(
                conn.execute(
                    """
                    SELECT * FROM facebook_page_publish_claims WHERE taskId = ?
                    """,
                    (task["id"],),
                ).fetchone()
            )
        baseline = json.loads(claim["baselineJson"])
        form = json.loads(claim["formSnapshotJson"])
        receipt = json.loads(claim["receiptJson"])
        canonical = lambda value: json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            set(baseline),
            {"pageId", "rows"},
        )
        self.assertEqual(
            set(baseline["rows"][0]),
            {"reelId", "url", "publishedAt", "captionSha256"},
        )
        self.assertEqual(
            set(form),
            {
                "pageId",
                "videoName",
                "videoSize",
                "videoSha256",
                "captionSha256",
                "visibility",
                "finalButtonLabel",
                "finalButtonReady",
            },
        )
        self.assertEqual(
            claim["baselineHash"],
            hashlib.sha256(canonical(baseline).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            claim["formSnapshotHash"],
            hashlib.sha256(canonical(form).encode("utf-8")).hexdigest(),
        )
        serialized = canonical(
            {
                "baseline": baseline,
                "form": form,
                "receipt": receipt,
            }
        )
        for forbidden in (
            "must never be persisted",
            "caption\"",
            "dom",
            "sessionPath",
            "cookie",
            "blocksReplay",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_checkpoint_hashes_platform_decision_and_receipt_json(self) -> None:
        task7 = self.install_task7_evidence_types()
        task_id, authorization_id, payload = self.authorized_preflight()
        task = self.create_formal(task_id, authorization_id, payload)
        self.transition_to(task["id"], payload, "final_action_claimed")
        controlled_publish.mark_facebook_page_checkpoint(
            task["id"],
            expected_state="final_action_claimed",
            new_state="ambiguous",
            receipt={
                "pageId": "1001",
                "platformDecision": task7.FacebookPlatformDecision(
                    kind="unknown",
                    page_id="1001",
                    observed_at="2026-08-30T10:10:00+08:00",
                    evidence_sha256="d" * 64,
                ),
            },
        )

        with database.connect() as conn:
            claim = dict(
                conn.execute(
                    """
                    SELECT * FROM facebook_page_publish_claims
                    WHERE taskId = ?
                    """,
                    (task["id"],),
                ).fetchone()
            )
        decision = json.loads(claim["platformDecisionJson"])
        receipt = json.loads(claim["receiptJson"])
        canonical = lambda value: json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            claim.get("platformDecisionHash"),
            hashlib.sha256(canonical(decision).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            claim.get("receiptHash"),
            hashlib.sha256(canonical(receipt).encode("utf-8")).hexdigest(),
        )

    def test_tampered_receipt_json_fails_closed_before_transition(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()
        task = self.create_formal(task_id, authorization_id, payload)
        self.transition_to(task["id"], payload, "final_action_claimed")
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE facebook_page_publish_claims
                SET receiptJson = ? WHERE taskId = ?
                """,
                ('{"pageId":"1001"}', task["id"]),
            )
            conn.commit()

        with self.assertRaises(ControlledPublishError) as raised:
            controlled_publish.mark_facebook_page_checkpoint(
                task["id"],
                expected_state="final_action_claimed",
                new_state="ambiguous",
                receipt={"pageId": "1001"},
            )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_claim_lifecycle_invalid",
        )
        with database.connect() as conn:
            state = conn.execute(
                """
                SELECT state FROM facebook_page_publish_claims
                WHERE taskId = ?
                """,
                (task["id"],),
            ).fetchone()[0]
        self.assertEqual(state, "final_action_claimed")

    def test_tampered_platform_decision_fails_closed_before_release(self) -> None:
        task7 = self.install_task7_evidence_types()
        task_id, authorization_id, payload = self.authorized_preflight()
        task = self.create_formal(task_id, authorization_id, payload)
        self.transition_to(task["id"], payload, "final_action_clicked")
        controlled_publish.mark_facebook_page_checkpoint(
            task["id"],
            expected_state="final_action_clicked",
            new_state="ambiguous",
            receipt={
                "pageId": "1001",
                "platformDecision": task7.FacebookPlatformDecision(
                    kind="unknown",
                    page_id="1001",
                    observed_at="2026-08-30T10:10:00+08:00",
                    evidence_sha256="d" * 64,
                ),
            },
        )
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE facebook_page_publish_claims
                SET platformDecisionJson = ? WHERE taskId = ?
                """,
                (
                    json.dumps(
                        {
                            "pageId": "1001",
                            "kind": "accepted",
                            "observedAt": "2026-08-30T10:11:00+08:00",
                            "evidenceSha256": "e" * 64,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    task["id"],
                ),
            )
            conn.commit()

        with self.assertRaises(ControlledPublishError) as raised:
            controlled_publish.mark_facebook_page_checkpoint(
                task["id"],
                expected_state="ambiguous",
                new_state="confirmed_not_published",
                receipt={
                    "platformDecision": task7.FacebookPlatformDecision(
                        kind="rejected_no_creation",
                        page_id="1001",
                        observed_at="2026-08-30T10:12:00+08:00",
                        evidence_sha256="f" * 64,
                    )
                },
            )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_claim_lifecycle_invalid",
        )
        with database.connect() as conn:
            claim = conn.execute(
                """
                SELECT state, blocksReplay
                FROM facebook_page_publish_claims WHERE taskId = ?
                """,
                (task["id"],),
            ).fetchone()
        self.assertEqual(tuple(claim), ("ambiguous", 1))

    def test_corrupted_snapshot_hash_fails_closed_before_recovery(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()
        task = self.create_formal(task_id, authorization_id, payload)
        self.transition_to(task["id"], payload, "ambiguous")
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE facebook_page_publish_claims
                SET baselineJson = ? WHERE taskId = ?
                """,
                (
                    json.dumps(
                        {
                            "pageId": "1001",
                            "rows": [
                                {
                                    "reelId": "tampered-reel",
                                    "url": (
                                        "https://www.facebook.com/reel/"
                                        "tampered-reel"
                                    ),
                                    "publishedAt": (
                                        "2026-08-29T09:00:00+08:00"
                                    ),
                                    "captionSha256": "e" * 64,
                                }
                            ],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    task["id"],
                ),
            )
            conn.commit()

        with self.assertRaises(ControlledPublishError) as raised:
            controlled_publish.mark_facebook_page_checkpoint(
                task["id"],
                expected_state="ambiguous",
                new_state="succeeded",
                receipt={
                    "pageId": "1001",
                    "reelId": "new-reel",
                    "url": "https://www.facebook.com/reel/new-reel",
                    "publishedAt": "2026-08-30T10:05:00+08:00",
                },
            )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_claim_lifecycle_invalid",
        )
        with database.connect() as conn:
            claim = conn.execute(
                """
                SELECT state, blocksReplay
                FROM facebook_page_publish_claims WHERE taskId = ?
                """,
                (task["id"],),
            ).fetchone()
        self.assertEqual(tuple(claim), ("ambiguous", 1))

    def test_execution_claim_is_single_start_and_matches_exact_intent(self) -> None:
        task_id, authorization_id, payload = self.authorized_preflight()
        task = self.create_formal(task_id, authorization_id, payload)

        controlled_publish.require_facebook_page_execution_claim(
            task["id"],
            [self.formal_payload(payload)],
        )
        with self.assertRaises(ControlledPublishError) as repeated:
            controlled_publish.require_facebook_page_execution_claim(
                task["id"],
                [self.formal_payload(payload)],
            )
        changed = self.formal_payload(payload)
        changed["facebookCaptionSha256"] = "e" * 64
        with self.assertRaises(ControlledPublishError) as mismatch:
            controlled_publish.require_facebook_page_execution_claim(
                task["id"],
                [changed],
            )
        self.assertEqual(
            repeated.exception.error_code,
            "facebook_publish_authorization_invalid",
        )
        self.assertEqual(
            mismatch.exception.error_code,
            "facebook_publish_authorization_invalid",
        )

    def test_execution_claim_rejects_terminal_task_or_item_without_start(
        self,
    ) -> None:
        cases = (
            ("terminal_task", "task", "success"),
            ("terminal_item", "item", "failed"),
        )
        for label, target, status in cases:
            with self.subTest(label=label):
                payload = self.payload(caption=f"worker state {label}")
                task_id, authorization_id, _ = self.authorized_preflight(
                    payload=payload
                )
                task = self.create_formal(task_id, authorization_id, payload)
                with database.connect() as conn:
                    if target == "task":
                        conn.execute(
                            "UPDATE publish_tasks SET status = ? WHERE id = ?",
                            (status, task["id"]),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE publish_task_items SET status = ?
                            WHERE taskId = ?
                            """,
                            (status, task["id"]),
                        )
                    conn.commit()

                with self.assertRaises(ControlledPublishError) as raised:
                    controlled_publish.require_facebook_page_execution_claim(
                        task["id"],
                        [self.formal_payload(payload)],
                    )

                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_publish_authorization_invalid",
                )
                with database.connect() as conn:
                    worker_started_at = conn.execute(
                        """
                        SELECT workerStartedAt
                        FROM facebook_page_publish_claims WHERE taskId = ?
                        """,
                        (task["id"],),
                    ).fetchone()[0]
                self.assertIsNone(worker_started_at)

    def test_execution_claim_requires_exactly_one_pending_target_item(
        self,
    ) -> None:
        for item_count in (0, 2):
            with self.subTest(item_count=item_count):
                payload = self.payload(caption=f"worker item count {item_count}")
                task_id, authorization_id, _ = self.authorized_preflight(
                    payload=payload
                )
                task = self.create_formal(task_id, authorization_id, payload)
                with database.connect() as conn:
                    if item_count == 0:
                        conn.execute(
                            "DELETE FROM publish_task_items WHERE taskId = ?",
                            (task["id"],),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO publish_task_items (
                                taskId, platformType, platformName, accountId,
                                accountFile, accountLabel, profileName,
                                userName, accountRemark, contentType, filePath,
                                fileName, status, authorizationSnapshotHash,
                                createdAt
                            )
                            SELECT taskId, platformType, platformName, accountId,
                                   accountFile, accountLabel, profileName,
                                   userName, accountRemark, contentType, filePath,
                                   fileName, status,
                                   authorizationSnapshotHash, createdAt
                            FROM publish_task_items WHERE taskId = ?
                            """,
                            (task["id"],),
                        )
                    conn.commit()

                with self.assertRaises(ControlledPublishError) as raised:
                    controlled_publish.require_facebook_page_execution_claim(
                        task["id"],
                        [self.formal_payload(payload)],
                    )

                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_publish_authorization_invalid",
                )
                with database.connect() as conn:
                    worker_started_at = conn.execute(
                        """
                        SELECT workerStartedAt
                        FROM facebook_page_publish_claims WHERE taskId = ?
                        """,
                        (task["id"],),
                    ).fetchone()[0]
                self.assertIsNone(worker_started_at)


if __name__ == "__main__":
    unittest.main()
