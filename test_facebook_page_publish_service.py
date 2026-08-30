# -*- coding: utf-8 -*-
"""Offline contracts for the controlled Facebook Page Reel worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app_core import (
    controlled_publish,
    database,
    overseas_browser_publish,
    overseas_preflight,
    publish_service,
    task_service,
)
from app_core.controlled_publish_process import submit_authorized_preflight_task
from app_core.overseas_meta_errors import FacebookPagePublishError
from uploader.meta_uploader.content_list import (
    FacebookPageContentBaseline,
    FacebookPageContentReader as Task7FacebookPageContentReader,
    FacebookReelMatch,
    FacebookReelReceipt,
)
from uploader.meta_uploader.page_form import (
    FacebookPageFormAdapter as PageFormContract,
    FacebookPageFormExpectation,
    FacebookPageFormSnapshot,
)
from utils import publish_observer, publish_tasks


class FacebookPageExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db_path = Path(self.temporary.name) / "database.db"
        self.database_patch = patch.object(database, "DB_PATH", self.db_path)
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        self.publish_tasks_patch = patch.object(
            publish_tasks,
            "DB_PATH",
            self.db_path,
        )
        self.publish_tasks_patch.start()
        self.addCleanup(self.publish_tasks_patch.stop)
        database.ensure_schema()
        self.feature_patch = patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
        )
        self.feature_patch.start()
        self.addCleanup(self.feature_patch.stop)
        self.cookie_dir = Path(self.temporary.name) / "cookies"
        self.cookie_dir.mkdir()
        (self.cookie_dir / "facebook-page.json").write_text(
            '{"cookies":[],"origins":[]}',
            encoding="utf-8",
        )
        self.cookie_dir_patch = patch.object(
            overseas_browser_publish,
            "COOKIE_DIR",
            self.cookie_dir,
        )
        self.cookie_dir_patch.start()
        self.addCleanup(self.cookie_dir_patch.stop)
        with database.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_info (
                    id, type, filePath, userName, status,
                    profileName, authMode, accountReference
                ) VALUES (?, 9, ?, ?, 1, ?, 'browser', ?)
                """,
                (
                    91,
                    "facebook-page.json",
                    "Saved Facebook Page",
                    "Saved Facebook Page",
                    "1001",
                ),
            )
            conn.commit()
        self.video = Path(self.temporary.name) / "facebook.mp4"
        self.video.write_bytes(b"facebook-page-worker-video")
        self.page_id = "1001"
        self.caption = "Exact Facebook caption\n\n#OneClick"
        self.video_hash = hashlib.sha256(self.video.read_bytes()).hexdigest()
        self.caption_hash = hashlib.sha256(self.caption.encode("utf-8")).hexdigest()
        self.log: list[str] = []
        self.click_count = 0
        self.click_error: BaseException | None = None
        self.snapshot_caption = self.caption
        self.snapshot_kind = "reel"
        self.baseline_error: BaseException | None = None
        self.readback_error: BaseException | None = None
        self.match_status = "unique"
        self.platform_decision: object | None = None
        self.adapter_classes: list[type] = []

    def payload(self, mode: str) -> dict:
        return {
            "type": 9,
            "contentType": "video",
            "runtimeMode": mode,
            "debugDryRun": mode == "preflight",
            "backgroundMode": mode == "preflight",
            "title": "Exact Facebook title",
            "description": "Exact Facebook caption",
            "tags": ["OneClick"],
            "fileList": [str(self.video)],
            "coverPath": "",
            "coverPaths": {},
            "collectionName": "",
            "accountList": ["facebook-page.json"],
            "accountIds": [91],
            "originalDeclaration": False,
            "contentDeclaration": "",
            "aiGenerated": False,
            "aiDeclarationExplicitlyConfirmed": False,
            "aiDisclosure": {},
            "facebookControlledPublish": True,
            "facebookExpectedPageReference": self.page_id,
            "facebookFinalCaption": self.caption,
            "facebookCaptionSha256": self.caption_hash,
            "facebookVideoSha256": self.video_hash,
            "facebookVideoSize": self.video.stat().st_size,
            "facebookManifestIntentSha256": "d" * 64,
            "visibility": "public",
            "enableTimer": False,
            "scheduleMode": "immediate",
            "scheduleTime": "",
            "scheduleTimezone": "",
            "scheduledAt": "",
            "dailyTimes": [],
            "videosPerDay": 1,
            "startDays": 0,
            "timeJitterMinutes": 0,
        }

    def _prepared(self, payload: dict) -> dict:
        return {
            "payload": dict(payload),
            "accountId": 91,
            "accountFile": "facebook-page.json",
            "pageId": self.page_id,
            "videoPath": self.video,
            "expectation": FacebookPageFormExpectation(
                page_id=self.page_id,
                content_kind="reel",
                video_name=self.video.name,
                video_size=self.video.stat().st_size,
                video_sha256=self.video_hash,
                caption=self.caption,
                visibility="public",
                video_path=str(self.video),
            ),
        }

    @contextmanager
    def patched_runtime(
        self,
        authorized_receipt: dict | None = None,
        *,
        real_clicked_at: bool = False,
        real_contracts: bool = False,
    ):
        owner = self

        class Button:
            async def click(self) -> None:
                owner.click_count += 1
                owner.log.append("click:start")
                if owner.click_error is not None:
                    raise owner.click_error
                owner.log.append("click:return")

            async def inner_text(self) -> str:
                return "Publish"

            async def is_enabled(self) -> bool:
                return True

        class Adapter(PageFormContract):
            def __init__(self, page, *, wait_for_verification) -> None:
                owner.adapter_classes.append(type(self))
                self.button = Button()

            async def fill_and_readback(self, expected):
                owner.log.append("form:verified")
                return FacebookPageFormSnapshot(
                    page_id=owner.page_id,
                    content_kind=owner.snapshot_kind,
                    video_name=owner.video.name,
                    video_count=1,
                    caption=owner.snapshot_caption,
                    visibility="public",
                    final_action_label="Publish",
                    final_action_ready=True,
                )

            async def final_action_button(self):
                owner.log.append("button:resolved")
                return self.button

            @staticmethod
            async def _button_label(button) -> str:
                return await button.inner_text()

            @staticmethod
            async def _button_ready(button) -> bool:
                return await button.is_enabled()

        class Reader:
            def __init__(self, context, *, wait_for_verification) -> None:
                pass

            async def capture_baseline(self, expected_page_id):
                owner.log.append("baseline:read")
                if owner.baseline_error is not None:
                    raise owner.baseline_error
                return FacebookPageContentBaseline(
                    page_id=owner.page_id,
                    rows=(),
                    captured_at="2026-08-30T00:00:00+00:00",
                    snapshot_sha256="e" * 64,
                )

            async def read_platform_decision(self, expected_page_id, *, page):
                owner.log.append("decision:accepted")
                return owner.platform_decision or SimpleNamespace(kind="accepted")

            async def readback_unique_reel(
                self,
                baseline,
                expected_page_id,
                expected_caption_sha256,
                clicked_at,
            ):
                owner.log.append(f"readback:{owner.match_status}")
                if owner.readback_error is not None:
                    raise owner.readback_error
                receipt = None
                if owner.match_status == "unique":
                    receipt = FacebookReelReceipt(
                        page_id=owner.page_id,
                        reel_id="new-reel-1",
                        url="https://www.facebook.com/reel/new-reel-1",
                        published_at=(
                            datetime.fromisoformat(clicked_at)
                            + timedelta(seconds=1)
                        ).isoformat(),
                    )
                return FacebookReelMatch(
                    status=owner.match_status,
                    receipt=receipt,
                    new_count=1 if owner.match_status != "none" else 0,
                    matching_count=1 if owner.match_status == "unique" else 0,
                )

        @asynccontextmanager
        async def session(prepared):
            owner.log.append("session:open")

            async def verify(page) -> None:
                return None

            try:
                yield SimpleNamespace(), SimpleNamespace(), verify
            finally:
                owner.log.append("session:closed")

        def validate(payload, *, mode):
            return owner._prepared(payload)

        patches = [
            patch.object(
                overseas_browser_publish,
                "_facebook_page_session",
                side_effect=session,
                create=True,
            ),
            patch.object(overseas_browser_publish, "FacebookPageFormAdapter", Adapter),
            patch.object(overseas_browser_publish, "FacebookPageContentReader", Reader),
        ]
        if not real_contracts:
            patches.extend(
                [
                    patch.object(
                        overseas_browser_publish,
                        "_validate_facebook_page_payload",
                        side_effect=validate,
                        create=True,
                    ),
                    patch.object(
                        overseas_browser_publish,
                        "_load_authorized_preflight_receipt",
                        return_value=authorized_receipt,
                        create=True,
                    ),
                ]
            )
        if not real_clicked_at:
            patches.append(
                patch.object(
                    overseas_browser_publish,
                    "_load_clicked_at",
                    return_value="2026-08-30T00:00:01+00:00",
                    create=True,
                )
            )
        started = []
        try:
            for current in patches:
                current.start()
                started.append(current)
            yield
        finally:
            for current in reversed(started):
                current.stop()

    def _claimed_formal_task(self) -> tuple[dict, dict]:
        preflight_payload = self.payload("preflight")
        preflight = task_service.create_pending_task(
            [preflight_payload],
            mode="oneclick_preflight",
        )
        task_service.mark_task_running(
            int(preflight["id"]),
            "Facebook Page preflight",
        )
        with self.patched_runtime(real_clicked_at=True, real_contracts=True):
            result = overseas_preflight.run_facebook_page_preflight_sync(
                preflight_payload,
                task_id=int(preflight["id"]),
            )
        task_service.mark_platform_result(
            int(preflight["id"]),
            9,
            ok=True,
            message=str(result["message"]),
            content_type="video",
            event_type="platform_preflight",
            receipt=dict(result["receipt"]),
        )
        authorization = controlled_publish.authorize_completed_check(
            int(preflight["id"])
        )
        payload = self.payload("publish")

        def lease_without_thread(
            task_id: int,
            *,
            runtime_video_path: str,
        ) -> dict:
            self.assertEqual(
                Path(runtime_video_path),
                self.video.resolve(),
            )
            stored = task_service.get_task(int(task_id))
            stored_payloads = json.loads(str(stored["payloadJson"]))
            controlled_publish.require_facebook_page_execution_claim(
                int(task_id),
                stored_payloads,
            )
            return stored

        with patch.object(
            publish_service,
            "start_controlled_facebook_publish",
            side_effect=lease_without_thread,
        ):
            task = controlled_publish._create_claimed_facebook_page_task(
                [payload],
                preflight_task_id=int(preflight["id"]),
                authorization_id=str(authorization["authorizationId"]),
            )
        return task, payload

    @staticmethod
    def _sealed_accepted_decision(page_id: str):
        class Element:
            async def is_visible(self) -> bool:
                return True

            async def inner_text(self) -> str:
                return "Your reel is being published"

        class Locator:
            async def count(self) -> int:
                return 1

            def nth(self, index: int):
                if index != 0:
                    raise IndexError(index)
                return Element()

        class Context:
            async def new_page(self):
                return page

        context = Context()

        class Page:
            def context(self):
                return context

            def get_by_role(self, role: str):
                return Locator() if role == "alert" else EmptyLocator()

        class EmptyLocator:
            async def count(self) -> int:
                return 0

            def nth(self, index: int):  # pragma: no cover - empty by contract
                raise IndexError(index)

        page = Page()

        async def no_verification(_page) -> None:
            return None

        async def same_page(_page, _account):
            return SimpleNamespace(page_id=page_id)

        reader = Task7FacebookPageContentReader(
            context,
            wait_for_verification=no_verification,
        )
        with patch(
            "uploader.meta_uploader.content_list.validate_facebook_page_binding",
            side_effect=same_page,
        ):
            return asyncio.run(
                reader.read_platform_decision(
                    expected_page_id=page_id,
                    page=page,
                )
            )

    @staticmethod
    def _facebook_event_types(task_id: int) -> list[str]:
        with database.connect() as conn:
            rows = conn.execute(
                """
                SELECT eventType FROM publish_task_events
                WHERE taskId = ? AND eventType LIKE 'facebook_%'
                ORDER BY id
                """,
                (int(task_id),),
            ).fetchall()
        return [str(row["eventType"]) for row in rows]

    def test_preflight_and_formal_share_adapter_and_checkpoint_click_order(self) -> None:
        events = []
        with self.patched_runtime():
            preflight = overseas_preflight.run_facebook_page_preflight_sync(
                self.payload("preflight"),
                task_id=501,
            )
            self.assertEqual(preflight["phase"], "platform_form_verified")
            self.assertFalse(preflight["receipt"]["finalActionTriggered"])
            self.assertEqual(self.click_count, 0)

        self.log.clear()

        def progress(stage, receipt) -> None:
            self.log.append(f"commit:{stage}")
            events.append(stage)

        with self.patched_runtime(preflight["receipt"]):
            result = overseas_browser_publish.run_facebook_page_publish_sync(
                {
                    **self.payload("publish"),
                    "metaBrowserPublishConfirmed": False,
                    "metaBrowserAutomationAcknowledged": False,
                },
                task_id=502,
                progress=progress,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["receipt"]["baselineHash"],
            hashlib.sha256(
                b'{"pageId":"1001","rows":[]}'
            ).hexdigest(),
        )
        self.assertEqual(len(result["receipt"]["formSnapshotHash"]), 64)
        self.assertEqual(len(self.adapter_classes), 2)
        self.assertTrue(
            all(issubclass(adapter, PageFormContract) for adapter in self.adapter_classes)
        )
        self.assertLess(
            self.log.index("commit:final_action_claimed"),
            self.log.index("click:start"),
        )
        returned = self.log.index("click:return")
        self.assertEqual(self.log[returned + 1], "commit:final_action_clicked")
        self.assertEqual(self.click_count, 1)
        self.assertEqual(
            events,
            [
                "final_action_claimed",
                "final_action_clicked",
                "platform_decision_observed",
                "readback_unique",
            ],
        )
        self.assertEqual(self.log[-1], "session:closed")
        with database.connect() as conn:
            persisted_events = conn.execute(
                """
                SELECT taskId, eventType FROM publish_task_events
                WHERE taskId IN (501, 502) ORDER BY id
                """
            ).fetchall()
        preflight_events = [
            str(row["eventType"])
            for row in persisted_events
            if int(row["taskId"]) == 501
        ]
        formal_events = [
            str(row["eventType"])
            for row in persisted_events
            if int(row["taskId"]) == 502
        ]
        self.assertEqual(preflight_events, ["facebook_platform_form_verified"])
        self.assertEqual(
            formal_events,
            [],
        )

    def test_external_absolute_video_reaches_real_form_adapter_with_safe_projections(
        self,
    ) -> None:
        try:
            self.video.relative_to(Path.cwd())
        except ValueError:
            pass
        else:  # pragma: no cover - the fixture is deliberately outside cwd
            self.fail("production wiring video must live outside process cwd")

        upload_inputs: list[str] = []
        prepared_rows: list[dict] = []
        expected_rows: list[FacebookPageFormExpectation] = []
        snapshot_rows: list[FacebookPageFormSnapshot] = []

        class Button:
            async def inner_text(self) -> str:
                return "Publish"

            async def is_enabled(self) -> bool:
                return True

        class BoundaryOnlyAdapter(PageFormContract):
            """Keep production orchestration; replace only browser/page actions."""

            def __init__(self, page, *, wait_for_verification) -> None:
                super().__init__(page, wait_for_verification=wait_for_verification)
                self.caption = ""
                self.uploaded = False
                self.button = Button()

            async def fill_and_readback(self, expected):
                expected_rows.append(expected)
                snapshot = await super().fill_and_readback(expected)
                snapshot_rows.append(snapshot)
                return snapshot

            async def open_fresh_reel_composer(self, expected_page_id: str) -> None:
                return None

            async def _read_content_kind(self) -> str:
                return "reel"

            async def _read_restored_draft(self) -> bool:
                return False

            async def _read_video_previews(self):
                if not self.uploaded:
                    return []
                return [(upload_inputs[-1], "completed")]

            async def _read_caption_editor(self) -> str:
                return self.caption

            async def _clear_caption_editor(self) -> None:
                self.caption = ""

            async def _upload_video_once(self, file_path: str) -> None:
                upload_inputs.append(file_path)
                self.uploaded = True

            async def _write_caption_once(self, caption: str) -> None:
                self.caption = caption

            async def _select_public_visibility(self) -> None:
                return None

            async def _read_visibility(self) -> str:
                return "public"

            async def _recheck_expected_page(self, expected):
                return SimpleNamespace(page_id=expected.page_id)

            async def _final_action_buttons(self):
                return [self.button]

            async def _sleep(self) -> None:
                return None

        @asynccontextmanager
        async def boundary_session(prepared):
            prepared_rows.append(prepared)

            async def no_verification(_page) -> None:
                return None

            yield SimpleNamespace(), SimpleNamespace(), no_verification

        with (
            patch.object(
                overseas_browser_publish,
                "_facebook_page_session",
                side_effect=boundary_session,
            ),
            patch.object(
                overseas_browser_publish,
                "FacebookPageFormAdapter",
                BoundaryOnlyAdapter,
            ),
        ):
            try:
                result = overseas_preflight.run_facebook_page_preflight_sync(
                    self.payload("preflight"),
                    task_id=601,
                )
            except FacebookPagePublishError as exc:
                self.fail(
                    "cwd-external absolute video did not reach the upload boundary: "
                    f"{exc.error_code}"
                )

        formal_prepared = overseas_browser_publish._validate_facebook_page_payload(
            self.payload("publish"),
            mode="formal",
        )

        async def no_verification(_page) -> None:
            return None

        formal_adapter = BoundaryOnlyAdapter(
            SimpleNamespace(),
            wait_for_verification=no_verification,
        )
        formal_snapshot = asyncio.run(
            formal_adapter.fill_and_readback(formal_prepared["expectation"])
        )

        self.assertEqual(upload_inputs, [str(self.video), str(self.video)])
        self.assertEqual(len(prepared_rows), 1)
        self.assertEqual(len(expected_rows), 2)
        self.assertEqual(len(snapshot_rows), 2)
        expected = expected_rows[0]
        snapshot = snapshot_rows[0]
        for current_expected in expected_rows:
            self.assertEqual(
                getattr(current_expected, "video_path", None),
                str(self.video),
            )
            self.assertEqual(current_expected.video_name, self.video.name)
            self.assertNotIn(str(self.video), repr(current_expected))
        for current_snapshot in (snapshot, formal_snapshot):
            self.assertEqual(current_snapshot.video_name, self.video.name)

        absolute_snapshot = FacebookPageFormSnapshot(
            page_id=snapshot.page_id,
            content_kind=snapshot.content_kind,
            video_name=str(self.video),
            video_count=snapshot.video_count,
            caption=snapshot.caption,
            visibility=snapshot.visibility,
            final_action_label=snapshot.final_action_label,
            final_action_ready=snapshot.final_action_ready,
        )
        try:
            projections = [
                result["receipt"],
                overseas_browser_publish._form_snapshot_projection(
                    expected,
                    absolute_snapshot,
                ),
                overseas_browser_publish._public_form_receipt(
                    prepared_rows[0],
                    absolute_snapshot,
                    phase="platform_form_verified",
                    final_action_triggered=False,
                ),
                overseas_browser_publish._claim_form_snapshot(
                    prepared_rows[0],
                    absolute_snapshot,
                ),
            ]
        except Exception as exc:  # pragma: no cover - RED guard
            self.fail(
                "absolute local path escaped into a public projection: "
                f"{type(exc).__name__}"
            )
        for projection in projections:
            self.assertEqual(projection["videoName"], self.video.name)
            self.assertEqual(projection["videoSize"], self.video.stat().st_size)
            self.assertEqual(projection["videoSha256"], self.video_hash)
        public_json = json.dumps(
            {"result": result, "projections": projections},
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(str(self.video), public_json)
        self.assertNotIn(str(self.video.parent), public_json)

    def test_controlled_preflight_and_formal_keep_video_path_runtime_only(self) -> None:
        """Catch any absolute-path persistence between the real service layers."""

        body = self.video.parent / "facebook-body.md"
        cover = self.video.parent / "facebook-cover.png"
        manifest = self.video.parent / "manifest.json"
        body.write_text("Exact Facebook caption", encoding="utf-8")
        cover.write_bytes(b"bundle-cover")
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": "oneclick-content/v1",
                    "contentType": "video",
                    "title": "Exact Facebook title",
                    "bodyFile": body.name,
                    "tags": ["OneClick"],
                    "assets": [self.video.name],
                    "covers": {"3:4": cover.name},
                    "preferredPlatforms": ["Facebook"],
                    "platformOverrides": {
                        "Facebook": {
                            "title": "Exact Facebook title",
                            "body": "Exact Facebook caption",
                            "tags": ["OneClick"],
                        }
                    },
                    "aiDisclosure": {
                        "containsAiGeneratedContent": False,
                        "contentKinds": [],
                        "assetPaths": [],
                        "allowPlatformAutoDeclaration": False,
                    },
                    "debugDryRun": True,
                    "publishAllowed": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        request = {
            "projectId": "facebook-page-runtime-path-test",
            "manifestPath": str(manifest),
            "mode": "preflight",
            "targets": [
                {
                    "platform": "Facebook",
                    "accountId": 91,
                    "schedule": None,
                    "settings": {"visibility": "public"},
                }
            ],
        }
        account = {
            "id": 91,
            "type": 9,
            "filePath": "facebook-page.json",
            "profileName": "Saved Facebook Page",
            "userName": "Saved Facebook Page",
            "authMode": "browser",
            "accountReference": self.page_id,
            "status": 1,
        }
        payloads = controlled_publish.build_controlled_payloads(
            request,
            accounts=[account],
        )
        expected_path = str(self.video.resolve())
        upload_inputs: list[str] = []
        session_paths: list[str] = []

        class ImmediateThread:
            def __init__(self, *, target, args, daemon, name, kwargs=None) -> None:
                self.target = target
                self.args = args
                self.kwargs = kwargs or {}
                self.running = False

            def start(self) -> None:
                self.running = True
                try:
                    self.target(*self.args, **self.kwargs)
                finally:
                    self.running = False

            def is_alive(self) -> bool:
                return self.running

        class Button:
            async def click(self) -> None:
                return None

            async def inner_text(self) -> str:
                return "Publish"

            async def is_enabled(self) -> bool:
                return True

        class BoundaryOnlyAdapter(PageFormContract):
            def __init__(self, page, *, wait_for_verification) -> None:
                super().__init__(page, wait_for_verification=wait_for_verification)
                self.caption = ""
                self.uploaded = False
                self.button = Button()

            async def open_fresh_reel_composer(self, expected_page_id: str) -> None:
                return None

            async def _read_content_kind(self) -> str:
                return "reel"

            async def _read_restored_draft(self) -> bool:
                return False

            async def _read_video_previews(self):
                return [(upload_inputs[-1], "completed")] if self.uploaded else []

            async def _read_caption_editor(self) -> str:
                return self.caption

            async def _clear_caption_editor(self) -> None:
                self.caption = ""

            async def _upload_video_once(self, file_path: str) -> None:
                upload_inputs.append(file_path)
                self.uploaded = True

            async def _write_caption_once(self, caption: str) -> None:
                self.caption = caption

            async def _select_public_visibility(self) -> None:
                return None

            async def _read_visibility(self) -> str:
                return "public"

            async def _recheck_expected_page(self, expected):
                return SimpleNamespace(page_id=expected.page_id)

            async def _final_action_buttons(self):
                return [self.button]

            async def _sleep(self) -> None:
                return None

        accepted_decision = self._sealed_accepted_decision(self.page_id)

        class Reader:
            def __init__(self, context, *, wait_for_verification) -> None:
                return None

            async def capture_baseline(self, expected_page_id):
                return FacebookPageContentBaseline(
                    page_id=expected_page_id,
                    rows=(),
                    captured_at="2026-08-30T00:00:00+00:00",
                    snapshot_sha256="e" * 64,
                )

            async def read_platform_decision(self, expected_page_id, *, page):
                return accepted_decision

            async def readback_unique_reel(
                self,
                baseline,
                expected_page_id,
                expected_caption_sha256,
                clicked_at,
            ):
                return FacebookReelMatch(
                    status="unique",
                    receipt=FacebookReelReceipt(
                        page_id=expected_page_id,
                        reel_id="runtime-path-reel",
                        url="https://www.facebook.com/reel/runtime-path-reel",
                        published_at=(
                            datetime.fromisoformat(clicked_at)
                            + timedelta(seconds=1)
                        ).isoformat(),
                    ),
                    new_count=1,
                    matching_count=1,
                )

        @asynccontextmanager
        async def boundary_session(prepared):
            session_paths.append(str(prepared["videoPath"]))

            async def no_verification(_page) -> None:
                return None

            yield SimpleNamespace(), SimpleNamespace(), no_verification

        publish_service._active_threads.clear()
        self.addCleanup(publish_service._active_threads.clear)
        with (
            patch.object(publish_service.threading, "Thread", ImmediateThread),
            patch.object(
                overseas_browser_publish,
                "_facebook_page_session",
                side_effect=boundary_session,
            ),
            patch.object(
                overseas_browser_publish,
                "FacebookPageFormAdapter",
                BoundaryOnlyAdapter,
            ),
            patch.object(
                overseas_browser_publish,
                "FacebookPageContentReader",
                Reader,
            ),
        ):
            preflight = publish_service.start_desktop_publish(payloads)
            authorization = controlled_publish.authorize_completed_check(
                int(preflight["id"])
            )
            formal = submit_authorized_preflight_task(
                int(preflight["id"]),
                str(authorization["authorizationId"]),
            )

        self.assertEqual(upload_inputs, [expected_path, expected_path])
        self.assertEqual(session_paths, [expected_path, expected_path])
        self.assertEqual(task_service.get_task(int(preflight["id"]))["status"], "success")
        self.assertEqual(
            task_service.get_task(int(formal["taskId"]))["status"],
            "success",
        )
        for task_id in (int(preflight["id"]), int(formal["taskId"])):
            stored = task_service.get_task(task_id)
            stored_payload = json.loads(str(stored["payloadJson"]))[0]
            self.assertEqual(stored_payload["fileList"], [self.video.name])
            self.assertEqual(
                stored_payload["facebookVideoSize"],
                self.video.stat().st_size,
            )
            self.assertEqual(stored["items"][0]["filePath"], self.video.name)
            persisted_json = json.dumps(stored, ensure_ascii=False, sort_keys=True)
            public_json = json.dumps(
                controlled_publish.project_task(stored),
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn(expected_path, persisted_json)
            self.assertNotIn(expected_path, public_json)

    def test_missing_video_is_stable_and_stops_before_page_session(self) -> None:
        payload = self.payload("preflight")
        missing = self.video.parent / "missing-video.mp4"
        payload["fileList"] = [str(missing)]
        session_calls: list[dict] = []

        @asynccontextmanager
        async def forbidden_session(prepared):
            session_calls.append(prepared)
            raise FacebookPagePublishError(
                "facebook_page_session_reached",
                "Missing local video reached the Page session.",
            )
            yield  # pragma: no cover - async context manager shape only

        with (
            patch.object(
                overseas_browser_publish,
                "_facebook_page_session",
                side_effect=forbidden_session,
            ),
            self.assertRaises(FacebookPagePublishError) as raised,
        ):
            overseas_preflight.run_facebook_page_preflight_sync(
                payload,
                task_id=602,
            )

        self.assertEqual(raised.exception.error_code, "facebook_video_file_invalid")
        self.assertEqual(session_calls, [])
        safe_error_json = json.dumps(
            {
                "message": str(raised.exception),
                "receipt": raised.exception.receipt,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(str(missing), safe_error_json)
        self.assertNotIn(str(missing.parent), safe_error_json)

    def test_unreadable_video_is_stable_and_stops_before_page_session(self) -> None:
        payload = self.payload("preflight")
        session_calls: list[dict] = []
        path_type = type(self.video)
        real_open = path_type.open

        def open_with_video_permission_denied(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == self.video and mode == "rb":
                raise PermissionError("simulated unreadable Page video")
            return real_open(path, *args, **kwargs)

        @asynccontextmanager
        async def forbidden_session(prepared):
            session_calls.append(prepared)
            raise FacebookPagePublishError(
                "facebook_page_session_reached",
                "Unreadable local video reached the Page session.",
            )
            yield  # pragma: no cover - async context manager shape only

        with (
            patch.object(
                path_type,
                "open",
                autospec=True,
                side_effect=open_with_video_permission_denied,
            ),
            patch.object(
                overseas_browser_publish,
                "_facebook_page_session",
                side_effect=forbidden_session,
            ),
            self.assertRaises(FacebookPagePublishError) as raised,
        ):
            overseas_preflight.run_facebook_page_preflight_sync(
                payload,
                task_id=604,
            )

        self.assertEqual(raised.exception.error_code, "facebook_video_file_invalid")
        self.assertEqual(session_calls, [])
        safe_error_json = json.dumps(
            {
                "message": str(raised.exception),
                "receipt": raised.exception.receipt,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(str(self.video), safe_error_json)
        self.assertNotIn(str(self.video.parent), safe_error_json)

    def test_relative_existing_video_is_rejected_before_page_session(self) -> None:
        payload = self.payload("preflight")
        payload["fileList"] = [self.video.name]
        session_calls: list[dict] = []

        @asynccontextmanager
        async def forbidden_session(prepared):
            session_calls.append(prepared)
            raise FacebookPagePublishError(
                "facebook_page_session_reached",
                "Relative local video reached the Page session.",
            )
            yield  # pragma: no cover - async context manager shape only

        previous_cwd = Path.cwd()
        try:
            os.chdir(self.video.parent)
            with (
                patch.object(
                    overseas_browser_publish,
                    "_facebook_page_session",
                    side_effect=forbidden_session,
                ),
                self.assertRaises(FacebookPagePublishError) as raised,
            ):
                overseas_preflight.run_facebook_page_preflight_sync(
                    payload,
                    task_id=603,
                )
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(raised.exception.error_code, "facebook_video_file_invalid")
        self.assertEqual(session_calls, [])

    def test_real_formal_runner_persists_each_lifecycle_event_once(self) -> None:
        task, payload = self._claimed_formal_task()
        self.platform_decision = self._sealed_accepted_decision(self.page_id)

        with (
            patch.object(
                overseas_browser_publish,
                "_validate_facebook_page_payload",
                wraps=overseas_browser_publish._validate_facebook_page_payload,
            ) as real_validator,
            patch.object(
                overseas_browser_publish,
                "_load_authorized_preflight_receipt",
                wraps=overseas_browser_publish._load_authorized_preflight_receipt,
            ) as real_receipt_loader,
            patch.object(
                publish_observer,
                "record_task_event",
                wraps=publish_observer.record_task_event,
            ) as legacy_event_writer,
            patch.object(
                task_service,
                "touch_task_heartbeat",
                wraps=task_service.touch_task_heartbeat,
            ) as standalone_heartbeat,
            self.patched_runtime(real_clicked_at=True, real_contracts=True),
        ):
            publish_service._run_facebook_page_publish(task, [payload])

        real_validator.assert_called_once_with(payload, mode="formal")
        real_receipt_loader.assert_called_once_with(int(task["id"]), payload)
        self.assertEqual(
            self._facebook_event_types(task["id"]),
            [
                "facebook_final_action_claimed",
                "facebook_final_action_clicked",
                "facebook_platform_decision_observed",
                "facebook_readback_unique",
                "facebook_publish_readback_confirmed",
            ],
        )
        self.assertEqual(legacy_event_writer.call_count, 0)
        self.assertEqual(standalone_heartbeat.call_count, 0)
        saved = task_service.get_task(task["id"])
        self.assertEqual(saved["status"], "success")
        with database.connect() as conn:
            item = conn.execute(
                """
                SELECT id, accountId, authorizationSnapshotHash,
                       message, errorCode, receiptJson
                FROM publish_task_items WHERE taskId = ?
                """,
                (int(task["id"]),),
            ).fetchone()
            claim = dict(
                conn.execute(
                    """
                    SELECT * FROM facebook_page_publish_claims WHERE taskId = ?
                    """,
                    (int(task["id"]),),
                ).fetchone()
            )
            preflight_item = conn.execute(
                """
                SELECT receiptJson FROM publish_task_items
                WHERE taskId = ? AND platformType = 9 AND status = 'success'
                """,
                (int(claim["preflightTaskId"]),),
            ).fetchone()
            authorization = conn.execute(
                """
                SELECT preflightReceiptHash, consumedAt
                FROM controlled_publish_authorizations
                WHERE preflightTaskId = ?
                """,
                (int(claim["preflightTaskId"]),),
            ).fetchone()
            item_id = int(item["id"])
            lifecycle_rows = conn.execute(
                """
                SELECT itemId, message, detailJson FROM publish_task_events
                WHERE taskId = ? AND eventType LIKE 'facebook_%'
                ORDER BY id
                """,
                (int(task["id"]),),
            ).fetchall()
            lifecycle_item_ids = [int(row["itemId"]) for row in lifecycle_rows]
            terminal = conn.execute(
                """
                SELECT workerPid, workerHeartbeatAt, finishedAt
                FROM publish_tasks WHERE id = ?
                """,
                (int(task["id"]),),
            ).fetchone()
            recomputed_preflight_hash = (
                controlled_publish.facebook_preflight_receipt_hash(
                    conn,
                    int(claim["preflightTaskId"]),
                    [payload],
                )
            )
        self.assertEqual(int(item["accountId"]), 91)
        self.assertEqual(
            item["authorizationSnapshotHash"],
            claim["publishIntentFingerprint"],
        )
        preflight_receipt_json = str(preflight_item["receiptJson"])
        self.assertEqual(
            hashlib.sha256(preflight_receipt_json.encode("utf-8")).hexdigest(),
            claim["preflightReceiptHash"],
        )
        self.assertEqual(recomputed_preflight_hash, claim["preflightReceiptHash"])
        self.assertEqual(
            authorization["preflightReceiptHash"],
            claim["preflightReceiptHash"],
        )
        self.assertTrue(str(authorization["consumedAt"] or ""))
        self.assertEqual(
            hashlib.sha256(
                str(claim["formSnapshotJson"]).encode("utf-8")
            ).hexdigest(),
            claim["formSnapshotHash"],
        )
        evidence = controlled_publish._validated_facebook_page_claim_evidence(
            claim
        )
        self.assertEqual(evidence["formSnapshot"]["pageId"], self.page_id)
        persisted_safe_values = [
            preflight_receipt_json,
            str(item["message"] or ""),
            str(item["errorCode"] or ""),
            str(item["receiptJson"] or ""),
            str(claim["formSnapshotJson"] or ""),
            str(claim["platformDecisionJson"] or ""),
            *[
                str(value or "")
                for row in lifecycle_rows
                for value in (row["message"], row["detailJson"])
            ],
        ]
        for persisted in persisted_safe_values:
            self.assertNotIn(str(self.video), persisted)
            self.assertNotIn(str(self.video.parent), persisted)
        self.assertEqual(lifecycle_item_ids, [item_id] * 5)
        self.assertIsNone(terminal["workerPid"])
        self.assertEqual(terminal["workerHeartbeatAt"], terminal["finishedAt"])

    def test_real_formal_runner_keeps_one_atomic_decision_when_readback_crashes(
        self,
    ) -> None:
        task, payload = self._claimed_formal_task()
        self.platform_decision = self._sealed_accepted_decision(self.page_id)
        self.readback_error = FacebookPagePublishError(
            "facebook_page_baseline_read_failed",
            "offline readback crash after decision",
            receipt={"pageId": self.page_id},
            outcome_ambiguous=True,
        )

        with (
            patch.object(
                overseas_browser_publish,
                "_validate_facebook_page_payload",
                wraps=overseas_browser_publish._validate_facebook_page_payload,
            ) as real_validator,
            patch.object(
                overseas_browser_publish,
                "_load_authorized_preflight_receipt",
                wraps=overseas_browser_publish._load_authorized_preflight_receipt,
            ) as real_receipt_loader,
            patch.object(
                publish_observer,
                "record_task_event",
                wraps=publish_observer.record_task_event,
            ) as legacy_event_writer,
            self.patched_runtime(real_clicked_at=True, real_contracts=True),
        ):
            publish_service._run_facebook_page_publish(task, [payload])

        real_validator.assert_called_once_with(payload, mode="formal")
        real_receipt_loader.assert_called_once_with(int(task["id"]), payload)
        with database.connect() as conn:
            claim = dict(
                conn.execute(
                    "SELECT * FROM facebook_page_publish_claims WHERE taskId = ?",
                    (int(task["id"]),),
                ).fetchone()
            )
        decision_json = str(claim["platformDecisionJson"] or "")
        self.assertEqual(
            hashlib.sha256(decision_json.encode("utf-8")).hexdigest(),
            str(claim["platformDecisionHash"]),
        )
        evidence = controlled_publish._validated_facebook_page_claim_evidence(claim)
        self.assertEqual(evidence["decision"]["kind"], "accepted")
        self.assertEqual(claim["state"], "ambiguous")
        self.assertEqual(
            self._facebook_event_types(task["id"]).count(
                "facebook_platform_decision_observed"
            ),
            1,
        )
        self.assertEqual(legacy_event_writer.call_count, 0)

    def test_publish_context_rejects_invalid_and_nested_task_switches(self) -> None:
        for invalid in (0, -1, True, "501"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                with publish_observer.publish_context(task_id=invalid):
                    pass

        with publish_observer.publish_context(task_id=501):
            self.assertEqual(
                publish_observer.get_publish_context()["task_id"],
                501,
            )
            with publish_observer.publish_context(task_id=501):
                self.assertEqual(
                    publish_observer.get_publish_context()["task_id"],
                    501,
                )
            with self.assertRaises(ValueError):
                with publish_observer.publish_context(task_id=502):
                    pass
            self.assertEqual(
                publish_observer.get_publish_context()["task_id"],
                501,
            )
        self.assertNotIn("task_id", publish_observer.get_publish_context())

    def test_formal_validation_removes_legacy_confirmation_booleans(self) -> None:
        session = Path(self.temporary.name) / "facebook-page.json"
        session.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        payload = {
            **self.payload("publish"),
            "metaBrowserPublishConfirmed": False,
            "metaBrowserAutomationAcknowledged": True,
            "overseasVideoPublishConfirmed": False,
        }
        account = {
            "id": 91,
            "type": 9,
            "status": 1,
            "authMode": "browser",
            "filePath": session.name,
            "accountReference": self.page_id,
        }
        with (
            patch.object(
                overseas_browser_publish,
                "facebook_page_v1_enabled",
                return_value=True,
            ),
            patch.object(
                overseas_browser_publish.account_service,
                "list_accounts",
                return_value=[account],
            ),
            patch.object(
                overseas_browser_publish,
                "COOKIE_DIR",
                Path(self.temporary.name),
            ),
        ):
            prepared = overseas_browser_publish._validate_facebook_page_payload(
                payload,
                mode="formal",
            )

        for key in (
            "metaBrowserPublishConfirmed",
            "metaBrowserAutomationAcknowledged",
            "overseasVideoPublishConfirmed",
        ):
            self.assertNotIn(key, prepared["payload"])

    def test_page_v1_rejects_malformed_platform_types_before_session(self) -> None:
        for label, malformed_type in (("string", "9"), ("float", 9.0)):
            with self.subTest(label=label):
                payload = {
                    **self.payload("preflight"),
                    "type": malformed_type,
                    "coverPath": str(self.video.parent / "cover.png"),
                }
                session_calls: list[dict] = []

                @asynccontextmanager
                async def forbidden_session(prepared):
                    session_calls.append(prepared)
                    raise FacebookPagePublishError(
                        "facebook_page_session_reached",
                        "Malformed Page type reached the browser session.",
                    )
                    yield  # pragma: no cover - async context manager shape only

                with patch.object(
                    overseas_browser_publish,
                    "_facebook_page_session",
                    side_effect=forbidden_session,
                ):
                    with self.assertRaises(FacebookPagePublishError) as raised:
                        overseas_preflight.run_facebook_page_preflight_sync(
                            payload,
                            task_id=690,
                        )

                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_publish_authorization_invalid",
                )
                self.assertEqual(session_calls, [])
                self.assertEqual(
                    raised.exception.receipt,
                    {
                        "phase": "local_validation",
                        "platformWriteOccurred": False,
                        "finalActionTriggered": False,
                        "pageId": self.page_id,
                    },
                )

    def test_page_v1_service_rejects_unsupported_metadata_before_session(self) -> None:
        cases = (
            ("cover_path", {"coverPath": str(self.video.parent / "cover.png")}),
            ("cover_paths", {"coverPaths": {"3:4": "cover.png"}}),
            ("collection", {"collectionName": "Page collection"}),
            ("schedule_object", {"schedule": {"localTime": "2026-09-01 10:00"}}),
            ("schedule_mode", {"scheduleMode": "platform_native"}),
            ("scheduled_at", {"scheduledAt": "2026-09-01 10:00"}),
            ("schedule_time", {"scheduleTime": "2026-09-01 10:00"}),
            ("schedule_timezone", {"scheduleTimezone": "Asia/Shanghai"}),
            ("daily_times", {"dailyTimes": ["10:00"]}),
            ("videos_per_day", {"videosPerDay": 2}),
            ("malformed_enable_timer", {"enableTimer": {"enabled": False}}),
            ("malformed_videos_per_day", {"videosPerDay": []}),
            ("visibility", {"visibility": "private"}),
            ("original", {"originalDeclaration": True}),
            ("content_declaration", {"contentDeclaration": "原创内容"}),
            ("ai_generated", {"aiGenerated": True}),
            (
                "ai_confirmation",
                {"aiDeclarationExplicitlyConfirmed": True},
            ),
            (
                "ai_disclosure",
                {"aiDisclosure": {"containsAiGeneratedContent": False}},
            ),
            (
                "nested_settings",
                {"settings": {"coverPath": "cover.png"}},
            ),
        )
        for index, (label, changes) in enumerate(cases):
            with self.subTest(label=label):
                payload = {**self.payload("preflight"), **changes}
                session_calls: list[dict] = []

                @asynccontextmanager
                async def forbidden_session(prepared):
                    session_calls.append(prepared)
                    raise FacebookPagePublishError(
                        "facebook_page_session_reached",
                        "Unsupported metadata reached the Page session.",
                    )
                    yield  # pragma: no cover - async context manager shape only

                with patch.object(
                    overseas_browser_publish,
                    "_facebook_page_session",
                    side_effect=forbidden_session,
                ):
                    try:
                        overseas_preflight.run_facebook_page_preflight_sync(
                            payload,
                            task_id=700 + index,
                        )
                    except FacebookPagePublishError as exc:
                        raised_error = exc
                    except Exception as exc:  # pragma: no cover - RED guard
                        self.fail(
                            "unsupported Facebook metadata leaked a non-stable "
                            f"{type(exc).__name__}"
                        )
                    else:  # pragma: no cover - unsupported input must stop
                        self.fail("unsupported Facebook metadata was accepted")

                self.assertEqual(
                    raised_error.error_code,
                    "facebook_unsupported_publish_setting",
                )
                self.assertEqual(session_calls, [])
                self.assertEqual(
                    raised_error.receipt,
                    {
                        "phase": "local_validation",
                        "platformWriteOccurred": False,
                        "finalActionTriggered": False,
                        "pageId": self.page_id,
                    },
                )

    def test_formal_rejects_malformed_persisted_evidence_before_session(self) -> None:
        task, payload = self._claimed_formal_task()
        with database.connect() as conn:
            claim = conn.execute(
                "SELECT preflightTaskId FROM facebook_page_publish_claims "
                "WHERE taskId = ?",
                (int(task["id"]),),
            ).fetchone()
            preflight_task_id = int(claim["preflightTaskId"])
            item = conn.execute(
                "SELECT receiptJson FROM publish_task_items "
                "WHERE taskId = ? AND platformType = 9",
                (preflight_task_id,),
            ).fetchone()
            receipt = json.loads(str(item["receiptJson"]))
            receipt["platformWriteOccurred"] = False
            receipt_json = json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            forged_receipt_hash = hashlib.sha256(
                receipt_json.encode("utf-8")
            ).hexdigest()
            conn.execute(
                "UPDATE publish_task_items SET receiptJson = ? "
                "WHERE taskId = ? AND platformType = 9",
                (receipt_json, preflight_task_id),
            )
            conn.execute(
                "UPDATE facebook_page_publish_claims "
                "SET preflightReceiptHash = ? WHERE taskId = ?",
                (forged_receipt_hash, int(task["id"])),
            )
            conn.commit()

        @asynccontextmanager
        async def forbidden_session(_prepared):
            raise FacebookPagePublishError(
                "facebook_page_form_readback_failed",
                "Malformed evidence reached the browser session.",
            )
            yield  # pragma: no cover - required by async context manager shape

        with (
            patch.object(
                overseas_browser_publish,
                "_facebook_page_session",
                side_effect=forbidden_session,
            ) as browser_session,
            self.assertRaises(FacebookPagePublishError) as raised,
        ):
            overseas_browser_publish.run_facebook_page_publish_sync(
                payload,
                task_id=int(task["id"]),
                progress=lambda _stage, _receipt: None,
            )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_publish_authorization_invalid",
        )
        browser_session.assert_not_called()

    def test_form_drift_stops_before_claim_and_click(self) -> None:
        with self.patched_runtime():
            preflight = overseas_preflight.run_facebook_page_preflight_sync(
                self.payload("preflight"),
                task_id=503,
            )
        self.snapshot_kind = "story"
        stages: list[str] = []
        with self.patched_runtime(preflight["receipt"]):
            with self.assertRaises(FacebookPagePublishError) as raised:
                overseas_browser_publish.run_facebook_page_publish_sync(
                    self.payload("publish"),
                    task_id=504,
                    progress=lambda stage, receipt: stages.append(stage),
                )
        self.assertEqual(raised.exception.error_code, "facebook_page_form_readback_failed")
        self.assertNotIn("final_action_claimed", stages)
        self.assertEqual(self.click_count, 0)

    def test_baseline_failure_prevents_claim_and_click(self) -> None:
        self.baseline_error = FacebookPagePublishError(
            "facebook_page_baseline_read_failed",
            "baseline unavailable",
            receipt={"pageId": self.page_id, "phase": "content_list_readback"},
        )
        stages: list[str] = []
        with self.patched_runtime({"formSnapshotHash": "f" * 64}):
            with (
                patch.object(
                    overseas_browser_publish,
                    "_assert_authorized_form_snapshot",
                    return_value=None,
                    create=True,
                ),
                self.assertRaises(FacebookPagePublishError),
            ):
                overseas_browser_publish.run_facebook_page_publish_sync(
                    self.payload("publish"),
                    task_id=505,
                    progress=lambda stage, receipt: stages.append(stage),
                )
        self.assertEqual(stages, [])
        self.assertEqual(self.click_count, 0)

    def test_click_exception_is_not_retried_and_session_is_closed(self) -> None:
        self.click_error = RuntimeError("offline click failure")
        stages: list[str] = []
        with self.patched_runtime({"formSnapshotHash": "f" * 64}):
            with (
                patch.object(
                    overseas_browser_publish,
                    "_assert_authorized_form_snapshot",
                    return_value=None,
                    create=True,
                ),
                self.assertRaisesRegex(RuntimeError, "offline click failure"),
            ):
                overseas_browser_publish.run_facebook_page_publish_sync(
                    self.payload("publish"),
                    task_id=506,
                    progress=lambda stage, receipt: stages.append(stage),
                )
        self.assertEqual(stages, ["final_action_claimed"])
        self.assertEqual(self.click_count, 1)
        self.assertEqual(self.log[-1], "session:closed")

    def test_none_and_mismatch_readbacks_are_ambiguous(self) -> None:
        expected = {
            "none": "facebook_publish_outcome_unknown",
            "mismatch": "facebook_publish_readback_mismatch",
        }
        for status, error_code in expected.items():
            with self.subTest(status=status):
                self.match_status = status
                self.click_count = 0
                stages: list[str] = []
                with self.patched_runtime({"formSnapshotHash": "f" * 64}):
                    with (
                        patch.object(
                            overseas_browser_publish,
                            "_assert_authorized_form_snapshot",
                            return_value=None,
                            create=True,
                        ),
                        self.assertRaises(FacebookPagePublishError) as raised,
                    ):
                        overseas_browser_publish.run_facebook_page_publish_sync(
                            self.payload("publish"),
                            task_id=507,
                            progress=lambda stage, receipt: stages.append(stage),
                        )
                self.assertEqual(raised.exception.error_code, error_code)
                self.assertEqual(self.click_count, 1)
                self.assertEqual(stages[-1], f"readback_{status}")


class FacebookPagePublishServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temporary.name) / "database.db",
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.publish_tasks_patch = patch.object(
            publish_tasks,
            "DB_PATH",
            Path(self.temporary.name) / "database.db",
        )
        self.publish_tasks_patch.start()
        self.addCleanup(self.publish_tasks_patch.stop)
        database.ensure_schema()
        publish_service._active_threads.clear()
        self.addCleanup(publish_service._active_threads.clear)
        self.video = Path(self.temporary.name) / "facebook.mp4"
        self.video.write_bytes(b"facebook-service-video")

    def payload(self) -> dict:
        caption = "Controlled Facebook caption"
        return {
            "type": 9,
            "contentType": "video",
            "runtimeMode": "publish",
            "debugDryRun": False,
            "backgroundMode": False,
            "title": "Controlled Facebook title",
            "description": caption,
            "tags": [],
            "fileList": [str(self.video)],
            "accountList": ["facebook-page.json"],
            "accountIds": [91],
            "facebookControlledPublish": True,
            "facebookExpectedPageReference": "1001",
            "facebookFinalCaption": caption,
            "facebookCaptionSha256": hashlib.sha256(caption.encode()).hexdigest(),
            "facebookVideoSha256": hashlib.sha256(self.video.read_bytes()).hexdigest(),
            "facebookManifestIntentSha256": "d" * 64,
            "visibility": "public",
            "enableTimer": False,
            "scheduleMode": "immediate",
            "scheduleTime": "",
            "scheduledAt": "",
        }

    def claimed_task(self) -> tuple[dict, dict]:
        payload = self.payload()
        preflight = task_service.create_pending_task(
            [{**payload, "runtimeMode": "preflight", "debugDryRun": True}],
            mode="oneclick_preflight",
        )
        task = task_service.create_pending_task([payload], mode="oneclick_publish")
        intent = controlled_publish.publish_intent_fingerprint([payload])
        replay = controlled_publish.facebook_replay_fingerprint([payload])
        now = datetime.now(timezone.utc).isoformat()
        with database.connect() as conn:
            controlled_publish._ensure_facebook_page_claim_schema(conn)
            conn.execute(
                """
                UPDATE publish_task_items
                SET accountId = 91, authorizationSnapshotHash = ?
                WHERE taskId = ? AND platformType = 9
                """,
                (intent, task["id"]),
            )
            conn.execute(
                """
                INSERT INTO facebook_page_publish_claims (
                    pageReference, publishIntentFingerprint, replayFingerprint,
                    preflightTaskId, preflightReceiptHash, taskId, state,
                    blocksReplay, createdAt, updatedAt
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', 1, ?, ?)
                """,
                (
                    "1001",
                    intent,
                    replay,
                    preflight["id"],
                    "f" * 64,
                    task["id"],
                    now,
                    now,
                ),
            )
            conn.commit()
        return task, payload

    def claim(self, task_id: int) -> dict:
        with database.connect() as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM facebook_page_publish_claims WHERE taskId = ?",
                    (task_id,),
                ).fetchone()
            )

    def checkpoint_receipt(self, payload: dict) -> dict:
        return {
            "accountId": 91,
            "pageId": "1001",
            "videoName": self.video.name,
            "videoSize": self.video.stat().st_size,
            "videoSha256": payload["facebookVideoSha256"],
            "captionSha256": payload["facebookCaptionSha256"],
            "visibility": "public",
            "phase": "final_action_claimed",
            "platformWriteOccurred": True,
            "finalActionTriggered": False,
            "finalButtonEnabled": True,
            "baseline": {"pageId": "1001", "rows": []},
            "formSnapshot": {
                "pageId": "1001",
                "videoName": self.video.name,
                "videoSize": self.video.stat().st_size,
                "videoSha256": payload["facebookVideoSha256"],
                "captionSha256": payload["facebookCaptionSha256"],
                "visibility": "public",
                "finalButtonLabel": "Publish",
                "finalButtonReady": True,
            },
        }

    def test_one_worker_start_per_reserved_claim(self) -> None:
        task, payload = self.claimed_task()
        starts: list[str] = []

        class Worker:
            def __init__(self, *, target, args, daemon, name) -> None:
                self.name = name

            def start(self) -> None:
                starts.append(self.name)

            def is_alive(self) -> bool:
                return True

        with (
            patch.object(
                publish_service,
                "_validate_payloads",
                return_value=[dict(payload)],
            ),
            patch.object(publish_service.threading, "Thread", Worker),
        ):
            returned = publish_service.start_controlled_facebook_publish(
                task["id"],
                runtime_video_path=str(self.video.resolve()),
            )
            with self.assertRaises(Exception) as repeated:
                publish_service.start_controlled_facebook_publish(
                    task["id"],
                    runtime_video_path=str(self.video.resolve()),
                )

        self.assertEqual(returned["id"], task["id"])
        self.assertEqual(len(starts), 1)
        self.assertEqual(
            getattr(repeated.exception, "error_code", ""),
            "facebook_publish_authorization_invalid",
        )
        self.assertTrue(self.claim(task["id"])["workerStartedAt"])

    def test_recovered_task_without_runtime_video_path_fails_before_session(self) -> None:
        task, payload = self.claimed_task()
        safe_payload = {
            **payload,
            "fileList": [self.video.name],
            "facebookVideoSize": self.video.stat().st_size,
        }
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET payloadJson = ? WHERE id = ?",
                (json.dumps([safe_payload], ensure_ascii=False), int(task["id"])),
            )
            conn.execute(
                "UPDATE publish_task_items SET filePath = ?, fileName = ? "
                "WHERE taskId = ? AND platformType = 9",
                (self.video.name, self.video.name, int(task["id"])),
            )
            conn.commit()
        session_calls: list[dict] = []

        @asynccontextmanager
        async def forbidden_session(prepared):
            session_calls.append(prepared)
            raise AssertionError("recovered task must not open a Page session")
            yield  # pragma: no cover - async context manager shape only

        empty_managed_dir = Path(self.temporary.name) / "managed-video"
        empty_managed_dir.mkdir()
        with (
            patch.dict(
                os.environ,
                {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
                clear=False,
            ),
            patch.object(publish_service, "VIDEO_DIR", empty_managed_dir),
            patch.object(
                overseas_browser_publish,
                "_facebook_page_session",
                side_effect=forbidden_session,
            ),
            self.assertRaises(Exception) as raised,
        ):
            publish_service.start_controlled_facebook_publish(int(task["id"]))

        self.assertEqual(
            getattr(raised.exception, "error_code", ""),
            "facebook_video_runtime_path_unavailable",
        )
        self.assertEqual(session_calls, [])
        self.assertNotIn(int(task["id"]), publish_service._active_threads)

    def test_runtime_video_must_keep_the_persisted_basename_before_lease(self) -> None:
        task, payload = self.claimed_task()
        renamed_video = self.video.with_name("renamed-facebook.mp4")
        renamed_video.write_bytes(self.video.read_bytes())
        runtime_payload = {
            **payload,
            "fileList": [str(renamed_video.resolve())],
            "facebookVideoSize": renamed_video.stat().st_size,
        }

        with (
            patch.object(
                publish_service,
                "_validate_payloads",
                return_value=[runtime_payload],
            ) as validate,
            patch.object(
                controlled_publish,
                "require_facebook_page_execution_claim",
            ) as lease,
            patch.object(publish_service.threading, "Thread") as worker,
            patch.object(
                overseas_browser_publish,
                "_facebook_page_session",
            ) as session,
            self.assertRaises(Exception) as raised,
        ):
            publish_service.start_controlled_facebook_publish(
                int(task["id"]),
                runtime_video_path=str(renamed_video.resolve()),
            )

        self.assertEqual(
            getattr(raised.exception, "error_code", ""),
            "facebook_video_runtime_path_unavailable",
        )
        validate.assert_not_called()
        worker.assert_not_called()
        lease.assert_not_called()
        session.assert_not_called()
        self.assertFalse(self.claim(int(task["id"]))["workerStartedAt"])

    def test_worker_start_failure_becomes_safe_failed_and_cleans_registry(self) -> None:
        task, payload = self.claimed_task()

        class Worker:
            def __init__(self, *, target, args, daemon, name) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("offline worker startup failure")

        with (
            patch.object(
                publish_service,
                "_validate_payloads",
                return_value=[dict(payload)],
            ),
            patch.object(publish_service.threading, "Thread", Worker),
            self.assertRaisesRegex(RuntimeError, "offline worker startup failure"),
        ):
            publish_service.start_controlled_facebook_publish(
                task["id"],
                runtime_video_path=str(self.video.resolve()),
            )

        self.assertEqual(self.claim(task["id"])["state"], "safe_failed")
        self.assertNotIn(task["id"], publish_service._active_threads)
        self.assertEqual(task_service.get_task(task["id"])["status"], "failed")

    def test_service_persists_claim_before_click_and_click_checkpoint_immediately(self) -> None:
        task, payload = self.claimed_task()
        controlled_publish.require_facebook_page_execution_claim(task["id"], [payload])
        observed: list[str] = []

        def runner(current, *, task_id, progress):
            progress("final_action_claimed", self.checkpoint_receipt(payload))
            observed.append(self.claim(task_id)["state"])
            progress("final_action_clicked", {"pageId": "1001"})
            clicked_at = self.claim(task_id)["clickedAt"]
            observed.append("clicked:" + str(bool(clicked_at)))
            match = FacebookReelMatch(
                status="unique",
                receipt=FacebookReelReceipt(
                    page_id="1001",
                    reel_id="new-reel-1",
                    url="https://www.facebook.com/reel/new-reel-1",
                    published_at=(
                        datetime.fromisoformat(clicked_at) + timedelta(seconds=1)
                    ).isoformat(),
                ),
                new_count=1,
                matching_count=1,
            )
            progress("readback_unique", {"reelMatch": match})
            return {
                "ok": True,
                "message": "Facebook Page unique Reel confirmed",
                "receipt": {
                    "accountId": 91,
                    "pageId": "1001",
                    "videoName": self.video.name,
                    "videoSize": self.video.stat().st_size,
                    "videoSha256": payload["facebookVideoSha256"],
                    "captionSha256": payload["facebookCaptionSha256"],
                    "visibility": "public",
                    "phase": "published_readback_confirmed",
                    "platformWriteOccurred": True,
                    "finalActionTriggered": True,
                    "reelId": "new-reel-1",
                    "url": "https://www.facebook.com/reel/new-reel-1",
                    "publishedAt": match.receipt.published_at,
                },
            }

        with patch.object(
            publish_service.overseas_browser_publish,
            "run_facebook_page_publish_sync",
            side_effect=runner,
        ):
            publish_service._run_facebook_page_publish(task, [payload])

        self.assertEqual(observed, ["final_action_claimed", "clicked:True"])
        self.assertEqual(self.claim(task["id"])["state"], "succeeded")
        self.assertEqual(task_service.get_task(task["id"])["status"], "success")

    def test_preclaim_failure_is_safe_and_postclaim_failure_is_ambiguous(self) -> None:
        cases = ("before", "after")
        for location in cases:
            with self.subTest(location=location):
                task, payload = self.claimed_task()
                controlled_publish.require_facebook_page_execution_claim(
                    task["id"], [payload]
                )

                def runner(current, *, task_id, progress):
                    if location == "after":
                        progress(
                            "final_action_claimed",
                            self.checkpoint_receipt(payload),
                        )
                    raise RuntimeError(f"offline {location} claim failure")

                with patch.object(
                    publish_service.overseas_browser_publish,
                    "run_facebook_page_publish_sync",
                    side_effect=runner,
                ):
                    publish_service._run_facebook_page_publish(task, [payload])

                self.assertEqual(
                    self.claim(task["id"])["state"],
                    "safe_failed" if location == "before" else "ambiguous",
                )
                self.assertEqual(task_service.get_task(task["id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main()
