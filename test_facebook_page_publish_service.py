# -*- coding: utf-8 -*-
"""Offline contracts for the controlled Facebook Page Reel worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
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
        self.final_snapshot: FacebookPageFormSnapshot | None = None
        self.baseline_error: BaseException | None = None
        self.decision_error: BaseException | None = None
        self.readback_error: BaseException | None = None
        self.match_status = "unique"
        self.post_click_state = "unknown"
        self.platform_decision: object | None = None
        self.adapter_classes: list[type] = []
        self.reader_timezones: list[str] = []

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

    def _real_session_lifecycle(
        self,
        *,
        mode: str,
        failure: BaseException | None = None,
    ) -> tuple[list[tuple[str, dict[str, object]]], int, int]:
        events: list[tuple[str, dict[str, object]]] = []
        browser = SimpleNamespace(close_count=0)
        context = SimpleNamespace(close_count=0)
        page = SimpleNamespace()

        async def close_browser() -> None:
            browser.close_count += 1

        async def close_context() -> None:
            context.close_count += 1

        async def new_page():
            return page

        async def goto(_url, *, wait_until):
            self.assertEqual(wait_until, "domcontentloaded")

        browser.close = close_browser
        context.close = close_context
        context.new_page = new_page
        page.goto = goto

        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, *_args) -> None:
                return None

        class Verifier:
            def __init__(self, *_args, **_kwargs) -> None:
                return None

            async def _wait_for_manual_intervention(self, _page) -> None:
                return None

        async def launch(_playwright):
            return browser

        async def new_context(_browser, *, storage_state, timezone_id):
            self.assertEqual(
                storage_state,
                str(self.cookie_dir / "facebook-page.json"),
            )
            self.assertEqual(timezone_id, "Asia/Shanghai")
            return context

        async def init(current) -> None:
            self.assertIs(current, context)

        async def scenario() -> None:
            prepared = self._prepared(self.payload(mode))
            async with overseas_browser_publish._facebook_page_session(
                prepared,
                progress=lambda stage, receipt: events.append(
                    (stage, dict(receipt))
                ),
            ):
                if failure is not None:
                    raise failure

        with (
            patch.object(
                overseas_browser_publish,
                "async_playwright",
                return_value=PlaywrightContext(),
            ),
            patch.object(
                overseas_browser_publish,
                "launch_publish_browser",
                side_effect=launch,
            ),
            patch.object(
                overseas_browser_publish,
                "new_publish_context",
                side_effect=new_context,
            ),
            patch.object(
                overseas_browser_publish,
                "set_init_script",
                side_effect=init,
            ),
            patch.object(overseas_browser_publish, "MetaReelVideo", Verifier),
        ):
            if failure is None:
                asyncio.run(scenario())
            else:
                with self.assertRaises(type(failure)):
                    asyncio.run(scenario())
        return events, context.close_count, browser.close_count

    def test_visible_preflight_session_reports_normal_close_reason(self) -> None:
        events, context_closes, browser_closes = self._real_session_lifecycle(
            mode="preflight"
        )

        self.assertEqual(
            events,
            [
                (
                    "browser_session_opened",
                    {"pageId": "1001", "purpose": "preflight"},
                ),
                (
                    "browser_session_closed",
                    {
                        "pageId": "1001",
                        "purpose": "preflight",
                        "closeReason": "preflight_completed",
                    },
                ),
            ],
        )
        self.assertEqual((context_closes, browser_closes), (1, 1))

    def test_visible_formal_session_reports_outcome_unknown_close_reason(self) -> None:
        failure = FacebookPagePublishError(
            "facebook_publish_outcome_unknown",
            "offline ambiguous outcome",
            outcome_ambiguous=True,
        )
        events, context_closes, browser_closes = self._real_session_lifecycle(
            mode="publish",
            failure=failure,
        )

        self.assertEqual(
            events,
            [
                (
                    "browser_session_opened",
                    {"pageId": "1001", "purpose": "formal"},
                ),
                (
                    "browser_session_closed",
                    {
                        "pageId": "1001",
                        "purpose": "formal",
                        "closeReason": "outcome_unknown",
                    },
                ),
            ],
        )
        self.assertEqual((context_closes, browser_closes), (1, 1))

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

            async def is_visible(self) -> bool:
                return True

        class Locator:
            def __init__(self, elements) -> None:
                self.elements = list(elements)

            async def count(self) -> int:
                return len(self.elements)

            def nth(self, index: int):
                return self.elements[index]

        class Page:
            url = (
                "https://business.facebook.com/latest/reels_composer/"
                f"?asset_id={owner.page_id}"
            )

            def __init__(self) -> None:
                self.handlers: dict[str, list] = {}
                self.button = Button()

            def on(self, event: str, callback) -> None:
                self.handlers.setdefault(event, []).append(callback)

            def remove_listener(self, event: str, callback) -> None:
                self.handlers.get(event, []).remove(callback)

            def get_by_role(self, role: str, *, name=None, exact=None):
                if role == "button" and name == "Publish" and exact is True:
                    return Locator([self.button])
                return Locator([])

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

            async def verify_final_form(self, expected):
                owner.log.append("form:final-verified")
                snapshot = owner.final_snapshot or FacebookPageFormSnapshot(
                    page_id=owner.page_id,
                    content_kind=owner.snapshot_kind,
                    video_name=owner.video.name,
                    video_count=1,
                    caption=owner.snapshot_caption,
                    visibility="public",
                    final_action_label="Publish",
                    final_action_ready=True,
                )
                return snapshot, self.button

            async def observe_post_click_state(self, expected_page_id):
                owner.log.append(f"post-click:{owner.post_click_state}")
                return owner.post_click_state

            @staticmethod
            async def _button_label(button) -> str:
                return await button.inner_text()

            @staticmethod
            async def _button_ready(button) -> bool:
                return await button.is_enabled()

        class Reader:
            def __init__(
                self,
                context,
                *,
                wait_for_verification,
                trusted_display_timezone=None,
            ) -> None:
                owner.reader_timezones.append(str(trusted_display_timezone or ""))

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
                if owner.decision_error is not None:
                    raise owner.decision_error
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
        async def session(prepared, *, progress=None):
            owner.log.append("session:open")

            async def verify(page) -> None:
                return None

            try:
                yield SimpleNamespace(), Page(), verify
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
        preflight_worker_token = "executor-preflight-worker"
        self.assertTrue(
            task_service.claim_facebook_worker(
                int(preflight["id"]),
                preflight_worker_token,
                "Facebook Page preflight",
            )
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
            worker_token=preflight_worker_token,
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
            worker_token = f"executor-formal-{task_id}"
            self.assertTrue(
                task_service.claim_facebook_worker(
                    int(task_id), worker_token, "executor formal worker"
                )
            )
            controlled_publish.require_facebook_page_execution_claim(
                int(task_id),
                stored_payloads,
                worker_token=worker_token,
            )
            stored["_workerToken"] = worker_token
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
        self.assertEqual(self.reader_timezones, ["Asia/Shanghai"])
        self.assertTrue(
            all(issubclass(adapter, PageFormContract) for adapter in self.adapter_classes)
        )
        self.assertLess(
            self.log.index("baseline:read"),
            self.log.index("form:verified"),
        )
        self.assertLess(
            self.log.index("form:verified"),
            self.log.index("form:final-verified"),
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
                "post_click_diagnostic",
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

    def test_formal_post_click_diagnostic_spans_click_readback_and_session(self) -> None:
        """The evidence recorder must cover the one click through list readback."""

        events: list[tuple[str, dict[str, object]]] = []

        class DiagnosticRecorder:
            def __init__(
                recorder_self,
                page,
                *,
                expected_page_id: str,
                final_button_label: str,
            ) -> None:
                self.assertEqual(expected_page_id, self.page_id)
                self.assertEqual(final_button_label, "Publish")
                recorder_self.page = page
                self.log.append("diagnostic:init")

            def start(recorder_self) -> None:
                self.log.append("diagnostic:start")

            def mark_final_action_started(recorder_self) -> None:
                self.log.append("diagnostic:final-action-started")

            async def sample(
                recorder_self,
                phase: str,
                *,
                post_click_state: str,
            ) -> None:
                self.log.append(
                    f"diagnostic:sample:{phase}:{post_click_state}"
                )

            async def drain(recorder_self) -> None:
                self.log.append("diagnostic:drain")

            def finish(recorder_self) -> dict[str, object]:
                self.log.append("diagnostic:finish")
                return {
                    "schemaVersion": "facebook-post-click-diagnostic/v1",
                    "pageId": self.page_id,
                    "samples": [],
                    "popups": [],
                    "networkResults": [],
                    "networkResultCount": 0,
                    "networkDroppedCount": 0,
                }

            def stop(recorder_self) -> None:
                self.log.append("diagnostic:stop")

        with (
            self.patched_runtime({"formSnapshotHash": "f" * 64}),
            patch.object(
                overseas_browser_publish,
                "FacebookPostClickDiagnosticRecorder",
                DiagnosticRecorder,
                create=True,
            ),
        ):
            with patch.object(
                overseas_browser_publish,
                "_assert_authorized_form_snapshot",
                return_value=None,
                create=True,
            ):
                result = overseas_browser_publish.run_facebook_page_publish_sync(
                    self.payload("publish"),
                    task_id=512,
                    progress=lambda stage, receipt: events.append(
                        (stage, dict(receipt))
                    ),
                )

        self.assertTrue(result["ok"])
        stages = [stage for stage, _receipt in events]
        self.assertIn("post_click_diagnostic", stages)
        self.assertLess(
            self.log.index("diagnostic:start"),
            self.log.index("diagnostic:final-action-started"),
        )
        self.assertLess(
            self.log.index("diagnostic:final-action-started"),
            self.log.index("click:start"),
        )
        self.assertLess(
            self.log.index("diagnostic:sample:post_click_observed:unknown"),
            self.log.index("readback:unique"),
        )
        self.assertLess(
            self.log.index("readback:unique"),
            self.log.index("diagnostic:sample:readback_finished:unknown"),
        )
        self.assertLess(
            self.log.index("diagnostic:sample:readback_finished:unknown"),
            self.log.index("diagnostic:drain"),
        )
        self.assertLess(
            self.log.index("diagnostic:drain"),
            self.log.index("diagnostic:finish"),
        )
        self.assertLess(
            self.log.index("diagnostic:finish"),
            self.log.index("session:closed"),
        )
        self.assertEqual(self.click_count, 1)

    def test_formal_does_not_claim_or_click_when_diagnostic_cannot_arm(self) -> None:
        events: list[str] = []

        class BrokenDiagnosticRecorder:
            def __init__(
                recorder_self,
                page,
                *,
                expected_page_id: str,
                final_button_label: str,
            ) -> None:
                pass

            def start(recorder_self) -> None:
                raise RuntimeError("offline diagnostic listener failure")

        with (
            self.patched_runtime({"formSnapshotHash": "f" * 64}),
            patch.object(
                overseas_browser_publish,
                "FacebookPostClickDiagnosticRecorder",
                BrokenDiagnosticRecorder,
            ),
            patch.object(
                overseas_browser_publish,
                "_assert_authorized_form_snapshot",
                return_value=None,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "offline diagnostic listener failure",
            ),
        ):
            overseas_browser_publish.run_facebook_page_publish_sync(
                self.payload("publish"),
                task_id=513,
                progress=lambda stage, receipt: events.append(stage),
            )

        self.assertEqual(events, [])
        self.assertEqual(self.click_count, 0)

    def test_diagnostic_finishes_when_post_click_progress_write_fails(self) -> None:
        stages: list[str] = []

        def progress(stage: str, receipt: object) -> None:
            stages.append(stage)
            if stage == "platform_decision_observed":
                raise RuntimeError("offline progress persistence failure")

        with (
            self.patched_runtime({"formSnapshotHash": "f" * 64}),
            patch.object(
                overseas_browser_publish,
                "_assert_authorized_form_snapshot",
                return_value=None,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "offline progress persistence failure",
            ),
        ):
            overseas_browser_publish.run_facebook_page_publish_sync(
                self.payload("publish"),
                task_id=514,
                progress=progress,
            )

        self.assertEqual(self.click_count, 1)
        self.assertEqual(
            stages[-2:],
            ["platform_decision_observed", "post_click_diagnostic"],
        )
        self.assertEqual(self.log[-1], "session:closed")

    def test_diagnostic_keeps_prior_sample_when_final_sample_is_partial(self) -> None:
        stages: list[str] = []

        class PartialDiagnosticRecorder:
            def __init__(
                recorder_self,
                page,
                *,
                expected_page_id: str,
                final_button_label: str,
            ) -> None:
                recorder_self.samples: list[str] = []

            def start(recorder_self) -> None:
                return None

            async def sample(
                recorder_self,
                phase: str,
                *,
                post_click_state: str,
            ) -> None:
                if phase == "readback_finished":
                    raise RuntimeError("offline final DOM sample failure")
                recorder_self.samples.append(phase)

            def finish(recorder_self) -> dict[str, object]:
                return {
                    "schemaVersion": "facebook-post-click-diagnostic/v1",
                    "pageId": self.page_id,
                    "samples": list(recorder_self.samples),
                    "popupUrls": [],
                    "networkResults": [],
                    "networkResultCount": 0,
                    "networkDroppedCount": 0,
                }

            def stop(recorder_self) -> None:
                return None

        with (
            self.patched_runtime({"formSnapshotHash": "f" * 64}),
            patch.object(
                overseas_browser_publish,
                "FacebookPostClickDiagnosticRecorder",
                PartialDiagnosticRecorder,
            ),
            patch.object(
                overseas_browser_publish,
                "_assert_authorized_form_snapshot",
                return_value=None,
            ),
        ):
            result = overseas_browser_publish.run_facebook_page_publish_sync(
                self.payload("publish"),
                task_id=515,
                progress=lambda stage, receipt: stages.append(stage),
            )

        self.assertTrue(result["ok"])
        self.assertIn("post_click_diagnostic", stages)
        self.assertEqual(stages[-1], "readback_unique")

    def test_diagnostic_listener_stops_when_click_checkpoint_fails(self) -> None:
        stopped: list[bool] = []

        class DiagnosticRecorder:
            def __init__(recorder_self, page, **_kwargs) -> None:
                return None

            def start(recorder_self) -> None:
                return None

            def stop(recorder_self) -> None:
                stopped.append(True)

        def progress(stage: str, receipt: object) -> None:
            if stage == "final_action_clicked":
                raise RuntimeError("offline clicked checkpoint failure")

        with (
            self.patched_runtime({"formSnapshotHash": "f" * 64}),
            patch.object(
                overseas_browser_publish,
                "FacebookPostClickDiagnosticRecorder",
                DiagnosticRecorder,
            ),
            patch.object(
                overseas_browser_publish,
                "_assert_authorized_form_snapshot",
                return_value=None,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "offline clicked checkpoint failure",
            ),
        ):
            overseas_browser_publish.run_facebook_page_publish_sync(
                self.payload("publish"),
                task_id=516,
                progress=progress,
            )

        self.assertEqual(self.click_count, 1)
        self.assertEqual(stopped, [True])

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
        async def boundary_session(prepared, *, progress=None):
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

        class EmptyLocator:
            async def count(self) -> int:
                return 0

            def nth(self, index: int):  # pragma: no cover - empty by contract
                raise IndexError(index)

        class BoundaryPage:
            url = (
                "https://business.facebook.com/latest/reels_composer/"
                f"?asset_id={self.page_id}"
            )

            def __init__(self) -> None:
                self.handlers: dict[str, list] = {}

            def on(self, event: str, callback) -> None:
                self.handlers.setdefault(event, []).append(callback)

            def remove_listener(self, event: str, callback) -> None:
                self.handlers.get(event, []).remove(callback)

            def get_by_role(self, role: str, *, name=None, exact=None):
                return EmptyLocator()

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
            def __init__(
                self,
                context,
                *,
                wait_for_verification,
                trusted_display_timezone=None,
            ) -> None:
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
        async def boundary_session(prepared, *, progress=None):
            session_paths.append(str(prepared["videoPath"]))

            async def no_verification(_page) -> None:
                return None

            yield SimpleNamespace(), BoundaryPage(), no_verification

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
        async def forbidden_session(prepared, *, progress=None):
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
        async def forbidden_session(prepared, *, progress=None):
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
        async def forbidden_session(prepared, *, progress=None):
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
                "facebook_worker_claimed",
                "facebook_final_action_claimed",
                "facebook_final_action_clicked",
                "facebook_platform_decision_observed",
                "facebook_post_click_diagnostic_captured",
                "facebook_readback_unique",
                "facebook_publish_readback_confirmed",
            ],
        )
        self.assertEqual(legacy_event_writer.call_count, 0)
        self.assertEqual(standalone_heartbeat.call_count, 1)
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
        self.assertEqual(lifecycle_item_ids, [item_id] * 7)
        projected = controlled_publish.project_task(saved)
        self.assertEqual(
            projected["postClickDiagnostic"]["schemaVersion"],
            "facebook-post-click-diagnostic/v2",
        )
        self.assertEqual(
            [
                sample["phase"]
                for sample in projected["postClickDiagnostic"]["samples"]
            ],
            ["post_click_observed", "readback_finished"],
        )
        self.assertEqual(
            projected["postClickDiagnostic"]["graphqlResults"],
            [],
        )
        self.assertEqual(
            projected["postClickDiagnostic"]["graphqlResultCount"],
            0,
        )
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
        saved = task_service.get_task(int(task["id"]))
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(
            saved["items"][0]["errorCode"],
            "facebook_publish_outcome_unknown",
        )
        item_receipt = json.loads(str(saved["items"][0]["receiptJson"]))
        self.assertEqual(item_receipt["phase"], "ambiguous")
        self.assertTrue(item_receipt["finalActionTriggered"])
        self.assertEqual(
            self._facebook_event_types(task["id"]).count(
                "facebook_platform_decision_observed"
            ),
            1,
        )
        self.assertEqual(legacy_event_writer.call_count, 0)

    def test_real_formal_runner_persists_post_click_confirmation_error_code(
        self,
    ) -> None:
        task, payload = self._claimed_formal_task()
        self.platform_decision = self._sealed_accepted_decision(self.page_id)
        self.post_click_state = "confirmation_pending"
        self.match_status = "none"

        with self.patched_runtime(real_clicked_at=True, real_contracts=True):
            publish_service._run_facebook_page_publish(task, [payload])

        saved = task_service.get_task(int(task["id"]))
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(
            saved["items"][0]["errorCode"],
            "facebook_post_click_confirmation_pending",
        )
        self.assertEqual(self.click_count, 1)
        with database.connect() as conn:
            claim = dict(
                conn.execute(
                    "SELECT * FROM facebook_page_publish_claims WHERE taskId = ?",
                    (int(task["id"]),),
                ).fetchone()
            )
        self.assertEqual(claim["state"], "ambiguous")
        self.assertEqual(int(claim["blocksReplay"]), 1)

    def test_missing_platform_decision_preserves_post_click_state(self) -> None:
        """A missing optional decision must not erase the observed dialog state."""

        task, payload = self._claimed_formal_task()
        self.decision_error = FacebookPagePublishError(
            "facebook_page_baseline_read_failed",
            "offline composer transition hid the optional decision",
            receipt={"pageId": self.page_id},
            outcome_ambiguous=True,
        )
        self.post_click_state = "confirmation_pending"
        self.match_status = "none"

        with self.patched_runtime(real_clicked_at=True, real_contracts=True):
            publish_service._run_facebook_page_publish(task, [payload])

        saved = task_service.get_task(int(task["id"]))
        item = saved["items"][0]
        receipt = json.loads(str(item["receiptJson"]))
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(
            item["errorCode"],
            "facebook_post_click_confirmation_pending",
        )
        self.assertEqual(receipt["postClickState"], "confirmation_pending")
        self.assertTrue(receipt["finalActionTriggered"])
        with database.connect() as conn:
            claim = dict(
                conn.execute(
                    "SELECT * FROM facebook_page_publish_claims WHERE taskId = ?",
                    (int(task["id"]),),
                ).fetchone()
            )
        self.assertEqual(claim["state"], "ambiguous")
        self.assertEqual(int(claim["blocksReplay"]), 1)
        self.assertEqual(str(claim["platformDecisionJson"] or "{}"), "{}")

    def test_unique_readback_succeeds_without_optional_platform_decision(
        self,
    ) -> None:
        """A unique Reel receipt is authoritative even if the toast is gone."""

        task, payload = self._claimed_formal_task()
        self.decision_error = FacebookPagePublishError(
            "facebook_page_baseline_read_failed",
            "offline composer transition hid the optional decision",
            receipt={"pageId": self.page_id},
            outcome_ambiguous=True,
        )

        with self.patched_runtime(real_clicked_at=True, real_contracts=True):
            publish_service._run_facebook_page_publish(task, [payload])

        saved = task_service.get_task(int(task["id"]))
        item = saved["items"][0]
        self.assertEqual(saved["status"], "success")
        self.assertEqual(item["status"], "success")
        self.assertEqual(item["platformPostId"], "new-reel-1")
        self.assertEqual(
            item["postUrl"],
            "https://www.facebook.com/reel/new-reel-1",
        )
        self.assertEqual(self.click_count, 1)
        with database.connect() as conn:
            claim = dict(
                conn.execute(
                    "SELECT * FROM facebook_page_publish_claims WHERE taskId = ?",
                    (int(task["id"]),),
                ).fetchone()
            )
        self.assertEqual(claim["state"], "succeeded")
        self.assertEqual(str(claim["platformDecisionJson"] or "{}"), "{}")

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
                async def forbidden_session(prepared, *, progress=None):
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
                async def forbidden_session(prepared, *, progress=None):
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

    def test_final_form_drift_after_baseline_stops_before_click(self) -> None:
        cases = (
            (
                "page_and_caption",
                {
                    "page_id": "1002",
                    "caption": "A different Facebook caption\n\n#OneClick",
                },
            ),
            ("video", {"video_name": "different-video.mp4"}),
            ("visibility", {"visibility": "private"}),
        )
        for label, changes in cases:
            with self.subTest(drift=label):
                self.click_count = 0
                self.final_snapshot = FacebookPageFormSnapshot(
                    page_id=changes.get("page_id", self.page_id),
                    content_kind="reel",
                    video_name=changes.get("video_name", self.video.name),
                    video_count=1,
                    caption=changes.get("caption", self.caption),
                    visibility=changes.get("visibility", "public"),
                    final_action_label="Publish",
                    final_action_ready=True,
                )
                stages: list[str] = []

                def progress(stage, receipt) -> None:
                    stages.append(stage)

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
                            task_id=580,
                            progress=progress,
                        )

                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_page_form_readback_failed",
                )
                self.assertEqual(raised.exception.receipt["phase"], "form_readback")
                self.assertFalse(raised.exception.receipt["finalActionTriggered"])
                self.assertNotEqual(raised.exception.receipt["phase"], "ambiguous")
                self.assertEqual(stages, [])
                self.assertEqual(self.click_count, 0)

    def test_final_form_drift_safely_fails_claim_and_task_before_click(self) -> None:
        task, payload = self._claimed_formal_task()
        self.final_snapshot = FacebookPageFormSnapshot(
            page_id="1002",
            content_kind="reel",
            video_name=self.video.name,
            video_count=1,
            caption="A different Facebook caption\n\n#OneClick",
            visibility="public",
            final_action_label="Publish",
            final_action_ready=True,
        )

        with self.patched_runtime(real_clicked_at=True, real_contracts=True):
            publish_service._run_facebook_page_publish(task, [payload])

        saved = task_service.get_task(int(task["id"]))
        self.assertIsNotNone(saved)
        self.assertEqual(self.click_count, 0)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["items"][0]["errorCode"], "facebook_page_form_readback_failed")
        item_receipt = json.loads(str(saved["items"][0]["receiptJson"]))
        with database.connect() as conn:
            claim = conn.execute(
                "SELECT state, receiptJson FROM facebook_page_publish_claims "
                "WHERE taskId = ?",
                (int(task["id"]),),
            ).fetchone()
        self.assertIsNotNone(claim)
        self.assertEqual(claim["state"], "safe_failed")
        claim_receipt = json.loads(str(claim["receiptJson"]))
        self.assertEqual(item_receipt["phase"], "failed")
        self.assertEqual(claim_receipt["phase"], "form_readback")
        for receipt in (item_receipt, claim_receipt):
            self.assertFalse(receipt["finalActionTriggered"])
            self.assertNotEqual(receipt["phase"], "ambiguous")
        self.assertNotIn(
            "facebook_final_action_claimed",
            self._facebook_event_types(int(task["id"])),
        )

    def test_preflight_exception_is_sanitized_before_task_persistence(self) -> None:
        task = task_service.create_pending_task(
            [self.payload("preflight")],
            mode="oneclick_preflight",
        )
        sensitive_path = "/Users/andy/private/cookiesFile/facebook-session.json"
        raw_exception = f"browser context failed at {sensitive_path}"
        worker_token = "preflight-exception-worker"
        self.assertTrue(
            task_service.claim_facebook_worker(
                int(task["id"]), worker_token, "preflight exception test"
            )
        )
        task["_workerToken"] = worker_token

        @asynccontextmanager
        async def failing_session(_prepared):
            raise RuntimeError(raw_exception)
            yield  # pragma: no cover - async context manager shape only

        with patch.object(
            overseas_browser_publish,
            "_facebook_page_session",
            side_effect=failing_session,
        ):
            publish_service._run_preflight(task, [self.payload("preflight")])

        saved = task_service.get_task(int(task["id"]))
        self.assertIsNotNone(saved)
        serialized = json.dumps(
            {
                "item": saved["items"][0],
                "lastError": saved["lastError"],
                "events": saved["events"],
                "taskProjection": saved,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(sensitive_path, serialized)
        self.assertNotIn(raw_exception, serialized)
        self.assertEqual(
            saved["items"][0]["errorCode"],
            "facebook_page_preflight_failed",
        )
        self.assertEqual(
            saved["items"][0]["message"],
            "Facebook Page 发布未完成",
        )

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

    def test_decision_read_failure_does_not_skip_unique_content_readback(self) -> None:
        self.decision_error = RuntimeError("offline decision read failure")
        stages: list[str] = []

        with self.patched_runtime({"formSnapshotHash": "f" * 64}):
            with patch.object(
                overseas_browser_publish,
                "_assert_authorized_form_snapshot",
                return_value=None,
                create=True,
            ):
                result = overseas_browser_publish.run_facebook_page_publish_sync(
                    self.payload("publish"),
                    task_id=508,
                    progress=lambda stage, receipt: stages.append(stage),
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["receipt"]["phase"], "published_readback_confirmed")
        self.assertTrue(result["receipt"]["finalActionTriggered"])
        self.assertEqual(self.click_count, 1)
        self.assertLess(
            self.log.index("decision:accepted"),
            self.log.index("readback:unique"),
        )
        self.assertEqual(stages[-1], "readback_unique")

    def test_post_click_verification_timeout_stays_ambiguous_and_blocks_readback(
        self,
    ) -> None:
        self.decision_error = FacebookPagePublishError(
            "facebook_verification_timeout",
            "offline post-click verification timeout",
            receipt={"phase": "verification"},
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
                self.assertRaises(FacebookPagePublishError) as raised,
            ):
                overseas_browser_publish.run_facebook_page_publish_sync(
                    self.payload("publish"),
                    task_id=510,
                    progress=lambda stage, receipt: stages.append(stage),
                )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_publish_outcome_unknown",
        )
        self.assertTrue(raised.exception.outcome_ambiguous)
        self.assertTrue(raised.exception.receipt["finalActionTriggered"])
        self.assertEqual(self.click_count, 1)
        self.assertNotIn("readback:unique", self.log)
        self.assertEqual(stages[-2:], ["final_action_clicked", "post_click_diagnostic"])
        self.assertEqual(self.log[-1], "session:closed")

    def test_post_click_readback_exception_is_ambiguous_with_click_evidence(self) -> None:
        self.readback_error = RuntimeError("offline content list read failure")
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
                    task_id=509,
                    progress=lambda stage, receipt: stages.append(stage),
                )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_publish_outcome_unknown",
        )
        self.assertTrue(raised.exception.outcome_ambiguous)
        self.assertEqual(raised.exception.receipt["phase"], "ambiguous")
        self.assertTrue(raised.exception.receipt["finalActionTriggered"])
        self.assertEqual(self.click_count, 1)
        self.assertEqual(
            stages[-2:],
            ["platform_decision_observed", "post_click_diagnostic"],
        )
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

    def test_confirmation_pending_has_specific_error_and_never_clicks_twice(
        self,
    ) -> None:
        self.post_click_state = "confirmation_pending"
        self.match_status = "none"
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
                    task_id=511,
                    progress=lambda stage, receipt: stages.append(stage),
                )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_post_click_confirmation_pending",
        )
        self.assertTrue(raised.exception.outcome_ambiguous)
        self.assertEqual(raised.exception.receipt["postClickState"], "confirmation_pending")
        self.assertEqual(self.click_count, 1)
        self.assertEqual(stages[-1], "readback_none")


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

    def lease_worker(self, task: dict) -> dict:
        worker_token = f"test-worker-{task['id']}"
        self.assertTrue(
            task_service.claim_facebook_worker(
                int(task["id"]), worker_token, "test worker"
            )
        )
        task["_workerToken"] = worker_token
        return task

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

    @staticmethod
    def post_click_diagnostic() -> dict[str, object]:
        return {
            "schemaVersion": "facebook-post-click-diagnostic/v1",
            "pageId": "1001",
            "samples": [
                {
                    "phase": "readback_finished",
                    "capturedAt": "2026-08-30T02:00:01+00:00",
                    "captureStatus": "ok",
                    "pageUrl": (
                        "https://business.facebook.com/latest/reels_composer/"
                        "?asset_id=1001"
                    ),
                    "postClickState": "composer_unchanged",
                    "dialogs": [],
                    "notices": [],
                    "finalButton": {
                        "label": "Publish",
                        "visibleCount": 1,
                        "enabled": False,
                    },
                }
            ],
            "popupUrls": [],
            "networkResults": [],
            "networkResultCount": 0,
            "networkDroppedCount": 0,
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

    def test_running_worker_requires_the_exact_persisted_token_for_formal_claim(self) -> None:
        task, payload = self.claimed_task()
        with self.assertRaises(controlled_publish.ControlledPublishError):
            controlled_publish.require_facebook_page_execution_claim(
                int(task["id"]), [payload]
            )
        self.assertTrue(
            task_service.claim_facebook_worker(
                int(task["id"]), "owner-token", "test owner"
            )
        )
        for token in ("", "replacement-token"):
            with self.subTest(token=token), self.assertRaises(
                controlled_publish.ControlledPublishError
            ):
                controlled_publish.require_facebook_page_execution_claim(
                    int(task["id"]), [payload], worker_token=token
                )
        controlled_publish.require_facebook_page_execution_claim(
            int(task["id"]), [payload], worker_token="owner-token"
        )
        self.assertTrue(self.claim(int(task["id"]))["workerStartedAt"])

    def test_busy_formal_worker_closes_its_owned_task_immediately(self) -> None:
        task, payload = self.claimed_task()
        self.lease_worker(task)
        controlled_publish.require_facebook_page_execution_claim(
            task["id"], [payload], worker_token=str(task["_workerToken"])
        )
        self.assertTrue(publish_service._publish_lock.acquire(blocking=False))

        try:
            publish_service._run_facebook_page_publish(task, [payload])
        finally:
            publish_service._publish_lock.release()

        saved = task_service.get_task(int(task["id"]))
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["items"][0]["errorCode"], "controlled_publish_busy")
        self.assertEqual(self.claim(int(task["id"]))["state"], "safe_failed")
        self.assertNotIn(int(task["id"]), publish_service._active_threads)

    def test_busy_preflight_worker_closes_its_owned_task_immediately(self) -> None:
        payload = self.payload()
        payload["runtimeMode"] = "preflight"
        task = task_service.create_pending_task(
            [payload], mode="oneclick_preflight"
        )
        worker_token = f"busy-preflight-{task['id']}"
        self.assertTrue(
            task_service.claim_facebook_worker(
                int(task["id"]), worker_token, "busy preflight worker"
            )
        )
        task["_workerToken"] = worker_token
        publish_service._active_threads[int(task["id"])] = threading.current_thread()
        self.assertTrue(publish_service._publish_lock.acquire(blocking=False))

        try:
            publish_service._run_preflight(task, [payload])
        finally:
            publish_service._publish_lock.release()

        saved = task_service.get_task(int(task["id"]))
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["items"][0]["errorCode"], "controlled_publish_busy")
        self.assertNotIn(int(task["id"]), publish_service._active_threads)

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
        async def forbidden_session(prepared, *, progress=None):
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

    def test_runtime_video_symlink_cannot_alias_the_persisted_basename(self) -> None:
        task, payload = self.claimed_task()
        renamed_target = self.video.with_name("renamed-target.mp4")
        self.video.replace(renamed_target)
        try:
            self.video.symlink_to(renamed_target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlink creation is unavailable: {type(exc).__name__}")
        runtime_payload = {
            **payload,
            "fileList": [str(self.video)],
            "facebookVideoSize": renamed_target.stat().st_size,
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
                runtime_video_path=str(self.video),
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
        self.lease_worker(task)
        controlled_publish.require_facebook_page_execution_claim(
            task["id"], [payload], worker_token=str(task["_workerToken"])
        )
        observed: list[str] = []

        def runner(current, *, task_id, progress):
            progress("waiting_user_verification", {"pageId": "1001"})
            observed.append(task_service.get_task(task_id)["status"])
            progress("verification_heartbeat", {"pageId": "1001"})
            progress("verification_resolved", {"pageId": "1001"})
            observed.append(task_service.get_task(task_id)["status"])
            progress("final_action_claimed", self.checkpoint_receipt(payload))
            observed.append(self.claim(task_id)["state"])
            progress("final_action_clicked", {"pageId": "1001"})
            clicked_at = self.claim(task_id)["clickedAt"]
            observed.append("clicked:" + str(bool(clicked_at)))
            progress("post_click_diagnostic", self.post_click_diagnostic())
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

        self.assertEqual(
            observed,
            [
                "waiting_user_verification",
                "running",
                "final_action_claimed",
                "clicked:True",
            ],
        )
        self.assertEqual(self.claim(task["id"])["state"], "succeeded")
        self.assertEqual(task_service.get_task(task["id"])["status"], "success")
        projected = controlled_publish.project_task(
            task_service.get_task(task["id"])
        )
        self.assertEqual(
            projected["postClickDiagnostic"],
            self.post_click_diagnostic(),
        )
        diagnostic_events = [
            event
            for event in task_service.get_task(task["id"])["events"]
            if event["eventType"]
            == "facebook_post_click_diagnostic_captured"
        ]
        self.assertEqual(len(diagnostic_events), 1)
        self.assertEqual(int(self.claim(task["id"])["blocksReplay"]), 1)

    def test_preflight_worker_persists_verification_wait_and_resumes(self) -> None:
        formal, _payload = self.claimed_task()
        preflight_id = int(self.claim(formal["id"])["preflightTaskId"])
        preflight = task_service.get_task(preflight_id)
        payloads = json.loads(preflight["payloadJson"])
        worker_token = f"preflight-resume-{preflight_id}"
        self.assertTrue(
            task_service.claim_facebook_worker(
                preflight_id, worker_token, "preflight resume test"
            )
        )
        preflight["_workerToken"] = worker_token
        observed: list[str] = []

        def runner(current, *, task_id, progress):
            progress("waiting_user_verification", {"pageId": "1001"})
            observed.append(task_service.get_task(task_id)["status"])
            progress("verification_heartbeat", {"pageId": "1001"})
            progress("verification_resolved", {"pageId": "1001"})
            observed.append(task_service.get_task(task_id)["status"])
            return {
                "ok": True,
                "message": "Facebook Page form verified",
                "receipt": {
                    "pageId": "1001",
                    "phase": "platform_form_verified",
                    "platformWriteOccurred": True,
                    "finalActionTriggered": False,
                },
            }

        with patch.object(
            publish_service.overseas_preflight,
            "run_facebook_page_preflight_sync",
            side_effect=runner,
        ):
            publish_service._active_threads[preflight_id] = threading.current_thread()
            publish_service._run_preflight(preflight, payloads)

        self.assertEqual(observed, ["waiting_user_verification", "running"])
        saved = task_service.get_task(preflight_id)
        self.assertEqual(saved["status"], "success")
        self.assertEqual(saved["items"][0]["status"], "success")

    def test_preclaim_failure_is_safe_and_postclaim_failure_is_ambiguous(self) -> None:
        cases = ("before", "after")
        for location in cases:
            with self.subTest(location=location):
                task, payload = self.claimed_task()
                self.lease_worker(task)
                controlled_publish.require_facebook_page_execution_claim(
                    task["id"],
                    [payload],
                    worker_token=str(task["_workerToken"]),
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
