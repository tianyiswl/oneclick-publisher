# -*- coding: utf-8 -*-
"""Facebook Page task lifecycle, stale recovery and read-only reconciliation."""

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

from app_core import controlled_publish, database, paths, publish_service, task_service
from app_core.controlled_publish import ControlledPublishError, project_task
from app_core.overseas_meta_errors import FacebookPagePublishError
from uploader.meta_uploader.content_list import (
    FacebookReelMatch,
    FacebookReelReceipt,
    FacebookReelRow,
    FacebookPageContentReader,
    _build_baseline,
    match_unique_new_facebook_reel,
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class _BombReadOnlyContext:
    """Any accidental write/composer operation is a test failure."""

    def __getattr__(self, name: str):
        lowered = name.casefold()
        if any(
            marker in lowered
            for marker in ("create", "upload", "publish", "delete", "click")
        ):
            raise AssertionError(f"read-only reconciliation attempted {name}")
        raise AttributeError(name)


class _ReadOnlyReader:
    def __init__(
        self,
        context,
        *,
        wait_for_verification,
        outcome: object,
        calls: list[dict[str, object]],
    ) -> None:
        if not isinstance(context, _BombReadOnlyContext):
            raise AssertionError("reconciliation did not use the read-only session")
        if not callable(wait_for_verification):
            raise AssertionError("verification seam is missing")
        self._outcome = outcome
        self._calls = calls

    async def readback_unique_reel(self, **kwargs):
        self._calls.append(dict(kwargs))
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class FacebookPageTaskServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.feature_patch = patch.dict(
            os.environ,
            {"ONECLICK_ENABLE_FACEBOOK_PAGE_V1": "1"},
            clear=False,
        )
        self.feature_patch.start()
        self.addCleanup(self.feature_patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db_path = Path(self.temporary.name) / "database.db"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.cookie_dir = Path(self.temporary.name) / "cookies"
        self.cookie_dir.mkdir()
        self.cookie_patch = patch.object(paths, "COOKIE_DIR", self.cookie_dir)
        self.cookie_patch.start()
        self.addCleanup(self.cookie_patch.stop)
        database.ensure_schema()
        self._page_sequence = 1000

    def test_read_only_session_uses_the_shared_meta_security_detector(self) -> None:
        session_file = self.cookie_dir / "facebook-page-session.json"
        session_file.write_text("{}", encoding="utf-8")
        browser = SimpleNamespace(close_count=0)
        context = SimpleNamespace(close_count=0)

        async def close_browser() -> None:
            browser.close_count += 1

        async def close_context() -> None:
            context.close_count += 1

        browser.close = close_browser
        context.close = close_context

        class PlaywrightContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, *_args) -> None:
                return None

        async def launch(_playwright):
            return browser

        async def new_context(_browser, *, storage_state):
            self.assertEqual(storage_state, str(session_file))
            return context

        async def init(current):
            return current

        class Body:
            async def inner_text(self, *, timeout):
                self.timeout = timeout
                return ""

        class Page:
            url = "https://www.facebook.com/checkpoint/"

            @staticmethod
            def locator(selector):
                if selector != "body":
                    raise AssertionError(selector)
                return Body()

        async def scenario() -> None:
            async with controlled_publish._facebook_page_read_only_session(
                str(session_file)
            ) as (actual_context, verifier):
                self.assertIs(actual_context, context)
                with self.assertRaises(ControlledPublishError) as raised:
                    await verifier(Page())
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_reconciliation_verification_required",
                )

        with (
            patch("playwright.async_api.async_playwright", return_value=PlaywrightContext()),
            patch("utils.base_social_media.launch_publish_browser", side_effect=launch),
            patch("utils.base_social_media.new_publish_context", side_effect=new_context),
            patch("utils.base_social_media.set_init_script", side_effect=init),
        ):
            asyncio.run(scenario())

        self.assertEqual(context.close_count, 1)
        self.assertEqual(browser.close_count, 1)

    def payload(self, *, page_id: str, account_id: int = 41) -> dict:
        account_file = f"facebook-page-{page_id}.json"
        (self.cookie_dir / account_file).write_text("{}", encoding="utf-8")
        return {
            "type": 9,
            "contentType": "video",
            "title": "Facebook Page task lifecycle",
            "description": "Exact caption",
            "accountList": [account_file],
            "accountIds": [account_id],
            "fileList": ["facebook.mp4"],
            "runtimeMode": "publish",
            "debugDryRun": False,
            "backgroundMode": False,
            "facebookControlledPublish": True,
            "facebookExpectedPageReference": page_id,
            "facebookFinalCaption": "Exact caption",
            "facebookVideoSha256": "a" * 64,
            "facebookVideoSize": 123,
            "facebookCaptionSha256": "b" * 64,
            "visibility": "public",
            "scheduleMode": "immediate",
            "scheduledAt": "",
            "scheduleTime": "",
            "scheduleTimezone": "Asia/Shanghai",
            "enableTimer": False,
        }

    @staticmethod
    def baseline(page_id: str) -> dict[str, object]:
        return {
            "pageId": page_id,
            "rows": [
                {
                    "reelId": "old-reel",
                    "url": "https://www.facebook.com/reel/old-reel",
                    "publishedAt": "2026-08-29T01:00:00+00:00",
                    "captionSha256": "c" * 64,
                }
            ],
        }

    @staticmethod
    def form_snapshot(page_id: str) -> dict[str, object]:
        return {
            "pageId": page_id,
            "videoName": "facebook.mp4",
            "videoSize": 123,
            "videoSha256": "a" * 64,
            "captionSha256": "b" * 64,
            "visibility": "public",
            "finalButtonLabel": "Publish",
            "finalButtonReady": True,
        }

    @staticmethod
    def success_receipt(
        page_id: str,
        *,
        baseline_hash: str = "d" * 64,
        form_snapshot_hash: str = "e" * 64,
    ) -> dict[str, object]:
        return {
            "pageId": page_id,
            "reelId": "new-reel",
            "url": "https://www.facebook.com/reel/new-reel",
            "publishedAt": "2026-08-30T02:00:01+00:00",
            "visibility": "public",
            "phase": "published_readback_confirmed",
            "platformWriteOccurred": True,
            "finalActionTriggered": True,
            "baselineHash": baseline_hash,
            "formSnapshotHash": form_snapshot_hash,
        }

    def facebook_task(
        self,
        *,
        state: str = "reserved",
        decision: str = "",
        repair_event: bool = False,
        stale: bool = False,
    ) -> tuple[int, str]:
        self._page_sequence += 1
        page_id = str(self._page_sequence)
        payload = self.payload(page_id=page_id)
        preflight = task_service.create_pending_task(
            [payload], mode="oneclick_preflight"
        )
        formal = task_service.create_pending_task(
            [payload], mode="oneclick_publish"
        )
        baseline = self.baseline(page_id)
        form_snapshot = self.form_snapshot(page_id)
        has_boundary = state not in {"reserved", "safe_failed"}
        baseline_json = _canonical_json(baseline) if has_boundary else "{}"
        baseline_hash = _canonical_hash(baseline) if has_boundary else ""
        form_json = _canonical_json(form_snapshot) if has_boundary else "{}"
        form_hash = _canonical_hash(form_snapshot) if has_boundary else ""
        clicked_at = (
            "2026-08-30T02:00:00+00:00"
            if state in {
                "final_action_clicked",
                "ambiguous",
                "succeeded",
                "confirmed_not_published",
            }
            else ""
        )
        decision_value: dict[str, object] | None = None
        if decision:
            decision_value = {
                "pageId": page_id,
                "kind": decision,
                "observedAt": "2026-08-30T02:00:00+00:00",
                "evidenceSha256": "f" * 64,
            }
        decision_json = (
            _canonical_json(decision_value) if decision_value is not None else "{}"
        )
        decision_hash = (
            _canonical_hash(decision_value) if decision_value is not None else ""
        )
        receipt = (
            self.success_receipt(
                page_id,
                baseline_hash=baseline_hash,
                form_snapshot_hash=form_hash,
            )
            if state == "succeeded"
            else {}
        )
        receipt_json = _canonical_json(receipt)
        receipt_hash = _canonical_hash(receipt)
        blocks_replay = int(
            state not in {"safe_failed", "confirmed_not_published"}
        )
        now = "2026-08-30T02:00:00+00:00"
        with database.connect() as conn:
            controlled_publish._ensure_facebook_page_claim_schema(conn)
            conn.execute(
                """
                INSERT INTO facebook_page_publish_claims (
                    pageReference, publishIntentFingerprint,
                    replayFingerprint, preflightTaskId,
                    preflightReceiptHash, taskId, state, blocksReplay,
                    workerStartedAt, baselineJson, baselineHash,
                    formSnapshotJson, formSnapshotHash, clickedAt,
                    platformDecisionJson, platformDecisionHash,
                    receiptJson, receiptHash, reelId, reelUrl,
                    createdAt, updatedAt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?)
                """,
                (
                    page_id,
                    f"intent-{page_id}",
                    f"replay-{page_id}",
                    preflight["id"],
                    "9" * 64,
                    formal["id"],
                    state,
                    blocks_replay,
                    now,
                    baseline_json,
                    baseline_hash,
                    form_json,
                    form_hash,
                    clicked_at,
                    decision_json,
                    decision_hash,
                    receipt_json,
                    receipt_hash,
                    str(receipt.get("reelId") or ""),
                    str(receipt.get("url") or ""),
                    now,
                    now,
                ),
            )
            item_status = "success" if state == "succeeded" else "running"
            task_status = "success" if state == "succeeded" else "running"
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = ?, startedAt = ?, accountId = 41,
                    receiptJson = ?, platformPostId = ?, postUrl = ?,
                    publishedAt = ?
                WHERE taskId = ? AND platformType = 9
                """,
                (
                    item_status,
                    now,
                    receipt_json if receipt else "",
                    str(receipt.get("reelId") or ""),
                    str(receipt.get("url") or ""),
                    str(receipt.get("publishedAt") or ""),
                    formal["id"],
                ),
            )
            heartbeat = (
                datetime.now() - timedelta(minutes=10)
                if stale
                else datetime.now()
            ).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """
                UPDATE publish_tasks
                SET status = ?, startedAt = ?, workerPid = ?,
                    workerHeartbeatAt = ?, successCount = ?, failedCount = 0
                WHERE id = ?
                """,
                (
                    task_status,
                    now,
                    999_999_999 if stale else None,
                    heartbeat,
                    int(state == "succeeded"),
                    formal["id"],
                ),
            )
            if has_boundary:
                conn.execute(
                    """
                    INSERT INTO publish_task_events
                        (taskId, level, eventType, message, createdAt)
                    VALUES (?, 'info', 'facebook_final_action_claimed', ?, ?)
                    """,
                    (formal["id"], "boundary", now),
                )
            if repair_event:
                conn.execute(
                    """
                    INSERT INTO publish_task_events
                        (taskId, level, eventType, message, createdAt)
                    VALUES (?, 'error',
                            'facebook_success_persistence_repair_required',
                            'safe repair required', ?)
                    """,
                    (formal["id"], now),
                )
            conn.commit()
        return int(formal["id"]), page_id

    def claim(self, task_id: int) -> dict:
        with database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM facebook_page_publish_claims WHERE taskId = ?",
                (int(task_id),),
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def projection(self, task_id: int) -> dict:
        return project_task(task_service.get_task(int(task_id)))

    def checkpoint_receipt(self, page_id: str) -> dict[str, object]:
        return {
            "pageId": page_id,
            "phase": "final_action_claimed",
            "platformWriteOccurred": True,
            "finalActionTriggered": False,
            "baseline": {
                **self.baseline(page_id),
                "dom": "forbidden DOM",
            },
            "formSnapshot": {
                **self.form_snapshot(page_id),
                "caption": "forbidden full caption",
                "sessionPath": "/private/facebook-session.json",
            },
            "verificationCode": "forbidden",
            "cookie": "forbidden",
        }

    def sealed_rejected_decision(self, page_id: str):
        class Element:
            async def is_visible(self) -> bool:
                return True

            async def inner_text(self) -> str:
                return "We couldn't publish your reel"

        class Locator:
            def __init__(self, elements: list[object]) -> None:
                self.elements = elements

            async def count(self) -> int:
                return len(self.elements)

            def nth(self, index: int):
                return self.elements[index]

        class Context:
            async def new_page(self):
                return page

        context = Context()

        class Page:
            def context(self):
                return context

            def get_by_role(self, role: str):
                return Locator([Element()] if role == "alert" else [])

        page = Page()

        async def no_verification(_page) -> None:
            return None

        async def same_page(_page, _account):
            return SimpleNamespace(page_id=page_id)

        reader = FacebookPageContentReader(
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

    def read_only_reconcile(
        self,
        task_id: int,
        outcome: object,
    ) -> tuple[dict, list[dict[str, object]], list[str]]:
        calls: list[dict[str, object]] = []
        sessions: list[str] = []

        @asynccontextmanager
        async def session(account_file: str):
            sessions.append(str(account_file))

            async def wait_for_verification(_page) -> None:
                return None

            yield _BombReadOnlyContext(), wait_for_verification

        class Reader:
            def __init__(self, context, *, wait_for_verification) -> None:
                self._reader = _ReadOnlyReader(
                    context,
                    wait_for_verification=wait_for_verification,
                    outcome=outcome,
                    calls=calls,
                )

            async def readback_unique_reel(self, **kwargs):
                return await self._reader.readback_unique_reel(**kwargs)

        with (
            patch.object(
                controlled_publish,
                "_facebook_page_read_only_session",
                session,
                create=True,
            ),
            patch(
                "uploader.meta_uploader.content_list.FacebookPageContentReader",
                Reader,
            ),
        ):
            result = controlled_publish.reconcile_facebook_page_publish_outcome(
                int(task_id)
            )
        return result, calls, sessions

    def concurrent_read_only_reconcile(
        self,
        task_id: int,
        outcomes: tuple[object, object],
    ) -> tuple[list[dict], list[BaseException]]:
        reader_started = threading.Event()
        release_reader = threading.Event()
        outcome_lock = threading.Lock()
        pending = list(outcomes)
        results: list[dict] = []
        errors: list[BaseException] = []

        @asynccontextmanager
        async def session(_account_file: str):
            async def wait_for_verification(_page) -> None:
                return None

            yield _BombReadOnlyContext(), wait_for_verification

        class Reader:
            def __init__(self, context, *, wait_for_verification) -> None:
                if not isinstance(context, _BombReadOnlyContext):
                    raise AssertionError("reconciliation did not use read-only context")
                if not callable(wait_for_verification):
                    raise AssertionError("verification seam is missing")
                with outcome_lock:
                    self.outcome = pending.pop(0)

            async def readback_unique_reel(self, **_kwargs):
                reader_started.set()
                if not release_reader.wait(timeout=5):
                    raise AssertionError("atomic reconciliation reader did not resume")
                return self.outcome

        def reconcile() -> None:
            try:
                result = controlled_publish.reconcile_facebook_page_publish_outcome(
                    int(task_id)
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                with outcome_lock:
                    errors.append(exc)
            else:
                with outcome_lock:
                    results.append(result)

        with (
            patch.object(
                controlled_publish,
                "_facebook_page_read_only_session",
                session,
                create=True,
            ),
            patch(
                "uploader.meta_uploader.content_list.FacebookPageContentReader",
                Reader,
            ),
        ):
            workers = [threading.Thread(target=reconcile) for _ in range(2)]
            workers[0].start()
            self.assertTrue(reader_started.wait(timeout=5))
            workers[1].start()
            workers[1].join(timeout=5)
            release_reader.set()
            workers[0].join(timeout=10)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        return results, errors

    def test_waiting_verification_has_action_required_and_no_error_code(self) -> None:
        task_id, page_id = self.facebook_task()

        task_service.record_facebook_progress(
            task_id,
            phase="waiting_user_verification",
            message=(
                "请在可见窗口完成验证 Cookie=session-secret "
                "verificationCode=654321"
            ),
            receipt={"pageId": page_id, "verificationCode": "forbidden"},
        )

        item = self.projection(task_id)["items"][0]
        self.assertEqual(item["phase"], "waiting_user_verification")
        self.assertEqual(item["status"], "waiting_user_verification")
        self.assertEqual(item["errorCode"], "")
        self.assertEqual(
            item["actionRequired"]["code"],
            "facebook_verification_required",
        )
        serialized = json.dumps(item, ensure_ascii=False)
        self.assertNotIn("verificationCode", serialized)
        self.assertNotIn("session-secret", serialized)
        self.assertNotIn("654321", serialized)

    def test_preflight_verification_wait_projects_and_resumes_without_failure(self) -> None:
        formal_task_id, page_id = self.facebook_task()
        preflight_task_id = int(self.claim(formal_task_id)["preflightTaskId"])

        task_service.record_facebook_verification_state(
            preflight_task_id,
            waiting=True,
            receipt={"pageId": page_id},
        )

        waiting = self.projection(preflight_task_id)
        self.assertEqual(waiting["status"], "waiting_user_verification")
        self.assertEqual(waiting["phase"], "waiting_user_verification")
        self.assertEqual(waiting["stage"], "waiting_verification")
        self.assertEqual(waiting["errorCode"], "")
        self.assertEqual(
            waiting["actionRequired"]["code"],
            "facebook_verification_required",
        )
        self.assertFalse(task_service.touch_task_heartbeat(preflight_task_id))
        with self.assertRaisesRegex(ValueError, "不能删除"):
            task_service.delete_tasks([preflight_task_id])

        task_service.record_facebook_verification_state(
            preflight_task_id,
            waiting=False,
            receipt={"pageId": page_id},
        )

        resumed = self.projection(preflight_task_id)
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["phase"], "checking")
        self.assertIsNone(resumed["actionRequired"])

    def test_waiting_projection_uses_executor_deadline_and_public_timeout(self) -> None:
        formal_task_id, page_id = self.facebook_task()
        preflight_task_id = int(self.claim(formal_task_id)["preflightTaskId"])
        timing = {
            "pageId": page_id,
            "verificationStartedAt": "2026-08-30T05:00:00+00:00",
            "deadlineAt": "2026-08-30T05:10:00+00:00",
            "timeoutSeconds": 600,
        }

        task_service.record_facebook_verification_state(
            preflight_task_id,
            waiting=True,
            receipt=timing,
        )

        projected = self.projection(preflight_task_id)
        self.assertEqual(projected["actionRequired"]["timeoutSeconds"], 600)
        self.assertEqual(
            projected["actionRequired"]["deadlineAt"],
            "2026-08-30T05:10:00+00:00",
        )
        self.assertEqual(projected["items"][0]["actionRequired"], projected["actionRequired"])
        self.assertNotIn("createdAt", projected["actionRequired"])

    def test_terminal_task_rejects_late_verification_callbacks(self) -> None:
        formal_task_id, page_id = self.facebook_task()
        preflight_task_id = int(self.claim(formal_task_id)["preflightTaskId"])
        task_service.fail_active_task(
            preflight_task_id,
            error_code="controlled_worker_interrupted",
            message="worker stopped",
        )

        for waiting in (True, False):
            with self.subTest(waiting=waiting), self.assertRaises(
                ControlledPublishError
            ):
                task_service.record_facebook_verification_state(
                    preflight_task_id,
                    waiting=waiting,
                    receipt={"pageId": page_id},
                )

        self.assertEqual(task_service.get_task(preflight_task_id)["status"], "failed")

    def test_verification_callback_requires_the_current_worker_token(self) -> None:
        formal_task_id, page_id = self.facebook_task()
        preflight_task_id = int(self.claim(formal_task_id)["preflightTaskId"])
        self.assertTrue(
            task_service.claim_facebook_worker(
                preflight_task_id,
                "current-worker",
                "worker started",
            )
        )

        with self.assertRaises(ControlledPublishError):
            task_service.record_facebook_verification_state(
                preflight_task_id,
                waiting=True,
                receipt={"pageId": page_id},
                worker_token="late-worker",
            )

        task_service.record_facebook_verification_state(
            preflight_task_id,
            waiting=True,
            receipt={"pageId": page_id},
            worker_token="current-worker",
        )
        self.assertEqual(
            task_service.get_task(preflight_task_id)["status"],
            "waiting_user_verification",
        )

    def test_worker_claim_is_atomic_and_cannot_replace_or_reopen_terminal_task(self) -> None:
        formal_task_id, _page_id = self.facebook_task()
        task_id = int(self.claim(formal_task_id)["preflightTaskId"])

        self.assertTrue(
            task_service.claim_facebook_worker(task_id, "current-token", "started")
        )
        self.assertFalse(
            task_service.claim_facebook_worker(task_id, "replacement-token", "late")
        )
        saved = task_service.get_task(task_id)
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["workerToken"], "current-token")

        task_service.fail_active_task(
            task_id,
            error_code="facebook_worker_interrupted",
            message="stopped",
        )
        self.assertFalse(
            task_service.claim_facebook_worker(task_id, "replacement-token", "late")
        )
        self.assertEqual(task_service.get_task(task_id)["status"], "failed")

        with self.assertRaises(ControlledPublishError):
            task_service.mark_task_running(
                task_id,
                "replacement must not reopen Page task",
                worker_token="replacement-token",
            )
        unchanged = task_service.get_task(task_id)
        self.assertEqual(unchanged["status"], "failed")
        self.assertEqual(unchanged["workerToken"], "current-token")

    def test_page_result_requires_current_worker_token_and_active_task(self) -> None:
        formal_task_id, page_id = self.facebook_task()
        task_id = int(self.claim(formal_task_id)["preflightTaskId"])
        self.assertTrue(task_service.claim_facebook_worker(task_id, "owner", "started"))

        with self.assertRaises(ControlledPublishError):
            task_service.mark_platform_result(
                task_id,
                9,
                ok=True,
                message="late",
                content_type="video",
                receipt={"pageId": page_id},
                worker_token="replacement",
            )
        task_service.mark_platform_result(
            task_id,
            9,
            ok=True,
            message="done",
            content_type="video",
            receipt={"pageId": page_id},
            worker_token="owner",
        )
        with self.assertRaises(ControlledPublishError):
            task_service.mark_platform_result(
                task_id,
                9,
                ok=False,
                message="late failure",
                content_type="video",
                receipt={"pageId": page_id},
                worker_token="owner",
            )
        self.assertEqual(task_service.get_task(task_id)["status"], "success")

    def test_post_claim_verification_preserves_claim_receipt_and_adds_deadline(self) -> None:
        for state in ("final_action_claimed", "final_action_clicked"):
            with self.subTest(state=state):
                task_id, page_id = self.facebook_task(state=state)
                timing = {
                    "pageId": page_id,
                    "verificationStartedAt": "2026-08-30T05:00:00+00:00",
                    "deadlineAt": "2026-08-30T05:10:00+00:00",
                    "timeoutSeconds": 600,
                }
                task_service.record_facebook_verification_state(
                    task_id,
                    waiting=True,
                    receipt=timing,
                )
                projected = self.projection(task_id)
                receipt = projected["receipt"]
                self.assertEqual(receipt["baselineHash"], self.claim(task_id)["baselineHash"])
                self.assertEqual(receipt["formSnapshotHash"], self.claim(task_id)["formSnapshotHash"])
                self.assertEqual(receipt["deadlineAt"], timing["deadlineAt"])
                self.assertEqual(projected["actionRequired"]["timeoutSeconds"], 600)

    def test_stale_wait_uses_deadline_to_distinguish_worker_loss_from_timeout(self) -> None:
        cases = (
            (
                "2026-08-30T05:10:00+00:00",
                datetime(2026, 8, 30, 5, 0, 45, tzinfo=timezone.utc),
                "facebook_worker_interrupted",
            ),
            (
                "2026-08-30T05:10:00+00:00",
                datetime(2026, 8, 30, 5, 10, 1, tzinfo=timezone.utc),
                "facebook_verification_timeout",
            ),
        )
        for deadline, now, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                formal_task_id, page_id = self.facebook_task()
                task_id = int(self.claim(formal_task_id)["preflightTaskId"])
                task_service.record_facebook_verification_state(
                    task_id,
                    waiting=True,
                    receipt={
                        "pageId": page_id,
                        "verificationStartedAt": "2026-08-30T05:00:00+00:00",
                        "deadlineAt": deadline,
                        "timeoutSeconds": 600,
                    },
                )
                with database.connect() as conn:
                    conn.execute(
                        "UPDATE publish_tasks SET workerHeartbeatAt = ? WHERE id = ?",
                        ("2026-08-30T04:59:00+00:00", task_id),
                    )
                    conn.commit()
                self.assertTrue(
                    task_service.reconcile_stale_controlled_task(
                        task_id,
                        lease_seconds=30,
                        now=now,
                    )
                )
                self.assertEqual(
                    task_service.get_task(task_id)["items"][0]["errorCode"],
                    expected_code,
                )

    def test_stale_formal_reserved_wait_uses_persisted_deadline(self) -> None:
        cases = (
            (
                datetime(2026, 8, 30, 5, 9, 59, tzinfo=timezone.utc),
                "facebook_worker_interrupted",
            ),
            (
                datetime(2026, 8, 30, 5, 10, 0, tzinfo=timezone.utc),
                "facebook_verification_timeout",
            ),
        )
        for now, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                task_id, page_id = self.facebook_task(state="reserved")
                task_service.record_facebook_verification_state(
                    task_id,
                    waiting=True,
                    receipt={
                        "pageId": page_id,
                        "verificationStartedAt": "2026-08-30T05:00:00+00:00",
                        "deadlineAt": "2026-08-30T05:10:00+00:00",
                        "timeoutSeconds": 600,
                    },
                )
                with database.connect() as conn:
                    conn.execute(
                        "UPDATE publish_tasks SET workerHeartbeatAt = ? WHERE id = ?",
                        ("2026-08-30T04:59:00+00:00", task_id),
                    )
                    conn.commit()

                self.assertTrue(
                    task_service.reconcile_stale_controlled_task(
                        task_id,
                        lease_seconds=30,
                        now=now,
                    )
                )
                saved = task_service.get_task(task_id)
                self.assertEqual(saved["items"][0]["errorCode"], expected_code)
                self.assertEqual(self.claim(task_id)["state"], "safe_failed")

    def test_read_only_reconcile_does_not_take_over_a_live_worker(self) -> None:
        task_id, _page_id = self.facebook_task(state="final_action_claimed")
        fresh = datetime.now(timezone.utc).isoformat()
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET status = 'running', workerToken = ?, "
                "workerHeartbeatAt = ? WHERE id = ?",
                ("live-owner", fresh, task_id),
            )
            conn.commit()

        projected = controlled_publish.reconcile_facebook_page_publish_outcome(
            task_id
        )
        self.assertEqual(projected["status"], "running")
        self.assertEqual(self.claim(task_id)["state"], "final_action_claimed")

        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET workerHeartbeatAt = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(), task_id),
            )
            conn.commit()
        reconciled = controlled_publish.reconcile_facebook_page_publish_outcome(
            task_id
        )
        self.assertEqual(reconciled["status"], "failed")
        self.assertEqual(self.claim(task_id)["state"], "ambiguous")

    def test_reconciliation_takeover_and_original_heartbeat_are_atomic(self) -> None:
        stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

        worker_first, _page_id = self.facebook_task(state="final_action_claimed")
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET status='running', workerToken=?, "
                "workerHeartbeatAt=? WHERE id=?",
                ("worker-first", stale, worker_first),
            )
            conn.commit()
        self.assertTrue(
            task_service.touch_task_heartbeat(
                worker_first, worker_token="worker-first"
            )
        )
        self.assertFalse(
            task_service.claim_facebook_reconciliation_worker(
                worker_first, "reconcile-loses", lease_seconds=30
            )
        )
        self.assertEqual(
            task_service.get_task(worker_first)["workerToken"], "worker-first"
        )

        reconcile_first, _page_id = self.facebook_task(state="final_action_claimed")
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET status='running', workerToken=?, "
                "workerHeartbeatAt=? WHERE id=?",
                ("stale-worker", stale, reconcile_first),
            )
            conn.commit()
        self.assertTrue(
            task_service.claim_facebook_reconciliation_worker(
                reconcile_first, "reconcile-wins", lease_seconds=30
            )
        )
        self.assertFalse(
            task_service.touch_task_heartbeat(
                reconcile_first, worker_token="stale-worker"
            )
        )
        self.assertTrue(
            task_service.touch_task_heartbeat(
                reconcile_first, worker_token="reconcile-wins"
            )
        )

    def test_reconciliation_takeover_normalizes_naive_and_aware_heartbeats(self) -> None:
        stale_values = (
            "2026-08-30 01:00:00",
            "2026-08-30T01:00:00+00:00",
        )
        for index, heartbeat in enumerate(stale_values):
            with self.subTest(heartbeat=heartbeat):
                task_id, _page_id = self.facebook_task(
                    state="final_action_clicked"
                )
                with database.connect() as conn:
                    conn.execute(
                        "UPDATE publish_tasks SET status='running', workerToken=?, "
                        "workerHeartbeatAt=? WHERE id=?",
                        (f"old-owner-{index}", heartbeat, task_id),
                    )
                    conn.commit()
                token = f"normalized-reconcile-{index}"
                self.assertTrue(
                    task_service.claim_facebook_reconciliation_worker(
                        task_id,
                        token,
                        lease_seconds=30,
                        now=datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc),
                    )
                )
                with patch.dict(publish_service._active_threads, {}, clear=True):
                    projected = controlled_publish.task_status(task_id)
                self.assertEqual(projected["status"], "running")
                self.assertEqual(
                    task_service.get_task(task_id)["workerToken"], token
                )

        invalid_task, _page_id = self.facebook_task(
            state="final_action_clicked"
        )
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET status='running', workerToken=?, "
                "workerHeartbeatAt=? WHERE id=?",
                ("invalid-owner", "not-a-time", invalid_task),
            )
            conn.commit()
        self.assertFalse(
            task_service.claim_facebook_reconciliation_worker(
                invalid_task,
                "must-not-take-over-invalid-time",
                lease_seconds=30,
                now=datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc),
            )
        )
        self.assertEqual(
            task_service.get_task(invalid_task)["workerToken"], "invalid-owner"
        )

    def test_reconcile_reader_failures_compensate_exact_owner_and_reraise(self) -> None:
        failures: tuple[BaseException, ...] = (
            RuntimeError("offline reader failed"),
            KeyboardInterrupt(),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                task_id, _page_id = self.facebook_task(
                    state="final_action_clicked", stale=True
                )
                baseline_hash = self.claim(task_id)["baselineHash"]
                with patch.object(
                    controlled_publish,
                    "_read_facebook_page_reconciliation",
                    side_effect=failure,
                ):
                    with self.assertRaises(type(failure)):
                        controlled_publish.reconcile_facebook_page_publish_outcome(
                            task_id
                        )
                saved = task_service.get_task(task_id)
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(
                    saved["items"][0]["errorCode"],
                    "facebook_publish_outcome_unknown",
                )
                self.assertEqual(self.claim(task_id)["state"], "ambiguous")
                self.assertEqual(self.claim(task_id)["baselineHash"], baseline_hash)

    def test_reconcile_illegal_reader_result_compensates_and_reraises(self) -> None:
        task_id, _page_id = self.facebook_task(
            state="final_action_clicked", stale=True
        )
        with patch.object(
            controlled_publish,
            "_read_facebook_page_reconciliation",
            return_value={"forged": True},
        ):
            with self.assertRaises(ControlledPublishError):
                controlled_publish.reconcile_facebook_page_publish_outcome(task_id)
        saved = task_service.get_task(task_id)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(
            saved["items"][0]["errorCode"],
            "facebook_publish_outcome_unknown",
        )
        self.assertEqual(self.claim(task_id)["state"], "ambiguous")

    def test_reconcile_compensation_does_not_overwrite_replacement_owner(self) -> None:
        task_id, _page_id = self.facebook_task(
            state="final_action_clicked", stale=True
        )

        async def replace_owner_then_fail(_snapshot):
            with database.connect() as conn:
                conn.execute(
                    "UPDATE publish_tasks SET workerToken=?, workerHeartbeatAt=? "
                    "WHERE id=?",
                    ("replacement-owner", datetime.now().isoformat(), task_id),
                )
                conn.commit()
            raise RuntimeError("reader failed after owner replacement")

        with patch.object(
            controlled_publish,
            "_read_facebook_page_reconciliation",
            side_effect=replace_owner_then_fail,
        ):
            with self.assertRaises(RuntimeError):
                controlled_publish.reconcile_facebook_page_publish_outcome(task_id)
        saved = task_service.get_task(task_id)
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["workerToken"], "replacement-owner")
        self.assertEqual(self.claim(task_id)["state"], "final_action_clicked")

    def test_cli_reconcile_keyboard_interrupt_is_compensated_by_service(self) -> None:
        import desktop_native_app

        task_id, _page_id = self.facebook_task(
            state="final_action_clicked", stale=True
        )
        args = SimpleNamespace(
            controlled_publish_action="reconcile",
            controlled_publish_task_id=task_id,
            controlled_publish_request=None,
            controlled_publish_authorization_id="",
        )
        with patch.object(
            controlled_publish,
            "_read_facebook_page_reconciliation",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                desktop_native_app.run_controlled_publish_cli(args)
        saved = task_service.get_task(task_id)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(
            saved["items"][0]["errorCode"],
            "facebook_publish_outcome_unknown",
        )
        self.assertEqual(self.claim(task_id)["state"], "ambiguous")

    def test_cli_unique_reconcile_interrupt_finalizes_authoritative_success(self) -> None:
        import desktop_native_app

        task_id, page_id = self.facebook_task(
            state="final_action_clicked", stale=True
        )
        match = FacebookReelMatch(
            status="unique",
            receipt=FacebookReelReceipt(
                page_id=page_id,
                reel_id="compensated-reel",
                url="https://www.facebook.com/reel/compensated-reel",
                published_at="2026-08-30T02:00:01+00:00",
            ),
            new_count=1,
            matching_count=1,
        )
        interruption = KeyboardInterrupt()

        async def unique_reader(_snapshot):
            return match

        args = SimpleNamespace(
            controlled_publish_action="reconcile",
            controlled_publish_task_id=task_id,
            controlled_publish_request=None,
            controlled_publish_authorization_id="",
        )
        publish_service._active_threads[task_id] = threading.current_thread()
        with (
            patch.object(
                controlled_publish,
                "_read_facebook_page_reconciliation",
                side_effect=unique_reader,
            ),
            patch.object(
                task_service,
                "mark_facebook_result",
                side_effect=interruption,
            ),
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                desktop_native_app.run_controlled_publish_cli(args)

        self.assertIs(caught.exception, interruption)
        saved = task_service.get_task(task_id)
        self.assertEqual(saved["status"], "success")
        self.assertEqual(saved["items"][0]["status"], "success")
        self.assertEqual(saved["items"][0]["platformPostId"], "compensated-reel")
        self.assertEqual(
            saved["items"][0]["postUrl"],
            "https://www.facebook.com/reel/compensated-reel",
        )
        self.assertEqual(self.claim(task_id)["state"], "succeeded")
        self.assertEqual(saved["workerToken"], "")
        self.assertNotIn(task_id, publish_service._active_threads)

    def test_succeeded_claim_compensation_does_not_overwrite_replacement_owner(self) -> None:
        task_id, page_id = self.facebook_task(
            state="final_action_clicked", stale=True
        )
        match = FacebookReelMatch(
            status="unique",
            receipt=FacebookReelReceipt(
                page_id=page_id,
                reel_id="replacement-race-reel",
                url="https://www.facebook.com/reel/replacement-race-reel",
                published_at="2026-08-30T02:00:01+00:00",
            ),
            new_count=1,
            matching_count=1,
        )
        interruption = KeyboardInterrupt()

        async def unique_reader(_snapshot):
            return match

        def replace_owner_then_interrupt(*_args, **_kwargs):
            with database.connect() as conn:
                conn.execute(
                    "UPDATE publish_tasks SET workerToken=?, workerHeartbeatAt=? "
                    "WHERE id=?",
                    ("replacement-after-success", datetime.now().isoformat(), task_id),
                )
                conn.commit()
            raise interruption

        with (
            patch.object(
                controlled_publish,
                "_read_facebook_page_reconciliation",
                side_effect=unique_reader,
            ),
            patch.object(
                task_service,
                "mark_facebook_result",
                side_effect=replace_owner_then_interrupt,
            ),
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                controlled_publish.reconcile_facebook_page_publish_outcome(task_id)

        self.assertIs(caught.exception, interruption)
        saved = task_service.get_task(task_id)
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["items"][0]["status"], "running")
        self.assertEqual(saved["workerToken"], "replacement-after-success")
        self.assertEqual(self.claim(task_id)["state"], "succeeded")

    def test_public_reconcile_cannot_take_over_worker_refreshed_after_snapshot(self) -> None:
        task_id, _page_id = self.facebook_task(state="final_action_claimed")
        stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET status='running', workerToken=?, "
                "workerHeartbeatAt=? WHERE id=?",
                ("snapshot-owner", stale, task_id),
            )
            conn.commit()
        original_snapshot = controlled_publish._facebook_reconciliation_snapshot

        def refresh_after_snapshot(current_task_id: int):
            snapshot = original_snapshot(current_task_id)
            self.assertTrue(
                task_service.touch_task_heartbeat(
                    current_task_id, worker_token="snapshot-owner"
                )
            )
            return snapshot

        with patch.object(
            controlled_publish,
            "_facebook_reconciliation_snapshot",
            side_effect=refresh_after_snapshot,
        ):
            projected = controlled_publish.reconcile_facebook_page_publish_outcome(
                task_id
            )
        self.assertEqual(projected["status"], "running")
        self.assertEqual(
            task_service.get_task(task_id)["workerToken"], "snapshot-owner"
        )
        self.assertEqual(self.claim(task_id)["state"], "final_action_claimed")

    def test_public_reconcile_rejects_live_preflight_before_owner_projection(self) -> None:
        payload = self.payload(page_id="preflight-only")
        preflight = task_service.create_pending_task(
            [payload], mode="oneclick_preflight"
        )
        self.assertTrue(
            task_service.claim_facebook_worker(
                int(preflight["id"]), "preflight-owner", "live preflight"
            )
        )

        with self.assertRaises(ControlledPublishError) as raised:
            controlled_publish.reconcile_facebook_page_publish_outcome(
                int(preflight["id"])
            )

        self.assertEqual(
            raised.exception.error_code,
            "facebook_claim_lifecycle_invalid",
        )

    def test_page_heartbeat_requires_exact_worker_token(self) -> None:
        formal_task_id, _page_id = self.facebook_task(state="reserved")
        task_id = int(self.claim(formal_task_id)["preflightTaskId"])
        self.assertTrue(
            task_service.claim_facebook_worker(task_id, "heartbeat-owner", "start")
        )
        before = task_service.get_task(task_id)["workerHeartbeatAt"]
        self.assertFalse(task_service.touch_task_heartbeat(task_id))
        self.assertFalse(task_service.touch_task_heartbeat(task_id, worker_token=""))
        self.assertFalse(
            task_service.touch_task_heartbeat(
                task_id, worker_token="wrong-heartbeat-owner"
            )
        )
        self.assertEqual(task_service.get_task(task_id)["workerHeartbeatAt"], before)
        self.assertTrue(
            task_service.touch_task_heartbeat(
                task_id, worker_token="heartbeat-owner"
            )
        )

    def test_waiting_verification_is_counted_as_active(self) -> None:
        formal_task_id, page_id = self.facebook_task()
        task_id = int(self.claim(formal_task_id)["preflightTaskId"])
        task_service.record_facebook_verification_state(
            task_id,
            waiting=True,
            receipt={"pageId": page_id},
        )
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET status = 'failed' WHERE id <> ?",
                (task_id,),
            )
            conn.commit()

        self.assertEqual(task_service.task_stats()["active"], 1)

    def test_legacy_stale_preflight_without_deadline_is_worker_interrupted(self) -> None:
        formal_task_id, page_id = self.facebook_task()
        preflight_task_id = int(self.claim(formal_task_id)["preflightTaskId"])
        task_service.record_facebook_verification_state(
            preflight_task_id,
            waiting=True,
            receipt={"pageId": page_id},
        )
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET workerHeartbeatAt = ? WHERE id = ?",
                ((datetime.now() - timedelta(minutes=5)).isoformat(), preflight_task_id),
            )
            conn.commit()

        self.assertTrue(
            task_service.reconcile_stale_controlled_task(
                preflight_task_id,
                lease_seconds=30,
            )
        )
        saved = task_service.get_task(preflight_task_id)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["items"][0]["errorCode"], "facebook_worker_interrupted")

    def test_formal_verification_is_claim_preserving_overlay_before_and_after_click(self) -> None:
        for claim_state, resumed_phase in (
            ("reserved", "checking"),
            ("final_action_claimed", "final_action_claimed"),
            ("final_action_clicked", "final_action_clicked"),
        ):
            with self.subTest(claim_state=claim_state):
                task_id, page_id = self.facebook_task(state=claim_state)

                task_service.record_facebook_verification_state(
                    task_id,
                    waiting=True,
                    receipt={"pageId": page_id},
                )
                self.assertEqual(self.claim(task_id)["state"], claim_state)
                self.assertEqual(
                    self.projection(task_id)["status"],
                    "waiting_user_verification",
                )

                task_service.record_facebook_verification_state(
                    task_id,
                    waiting=False,
                    receipt={"pageId": page_id},
                )
                resumed = self.projection(task_id)
                self.assertEqual(self.claim(task_id)["state"], claim_state)
                self.assertEqual(resumed["status"], "running")
                self.assertEqual(resumed["phase"], resumed_phase)

    def test_raw_message_secrets_never_enter_item_event_or_ui_projection(self) -> None:
        task_id, page_id = self.facebook_task()
        unsafe_message = (
            '验证码 654321 {"verificationCode": "778899"} '
            "Cookie=secret-cookie token:secret-token "
            "sessionPath=/private/session-secret.json "
            "/Users/andy/full-caption.txt "
            r"C:\Users\andy\facebook-session.json "
            "<div id='dom-secret'>private DOM</div> "
            "这是一整段不应保留的正文"
        )

        task_service.record_facebook_progress(
            task_id,
            phase="waiting_user_verification",
            message=unsafe_message,
            receipt={
                "pageId": page_id,
                "verificationCode": "receipt-secret",
                "caption": "这是一整段不应保留的正文",
                "dom": "<div>receipt DOM</div>",
            },
        )

        raw = json.dumps(
            task_service.get_task(task_id),
            ensure_ascii=False,
        )
        projected = json.dumps(self.projection(task_id), ensure_ascii=False)
        combined = f"{raw} {projected}"
        for forbidden in (
            "654321",
            "778899",
            "verificationCode",
            "secret-cookie",
            "secret-token",
            "session-secret",
            "/Users/andy",
            r"C:\\Users\\andy",
            "dom-secret",
            "private DOM",
            "这是一整段不应保留的正文",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertIn("Facebook Page", combined)

    def test_project_task_exposes_every_approved_facebook_phase(self) -> None:
        approved = (
            ("local_validation_passed", "running", "reserved", ""),
            (
                "waiting_user_verification",
                "waiting_user_verification",
                "reserved",
                "",
            ),
            ("platform_form_verified", "running", "reserved", ""),
            ("final_action_claimed", "running", "final_action_claimed", ""),
            ("final_action_clicked", "running", "final_action_clicked", ""),
            ("platform_accepted", "running", "final_action_clicked", "accepted"),
            (
                "published_readback_confirmed",
                "success",
                "succeeded",
                "",
            ),
            ("failed", "failed", "safe_failed", ""),
            (
                "confirmed_not_published",
                "failed",
                "confirmed_not_published",
                "rejected_no_creation",
            ),
            ("ambiguous", "failed", "ambiguous", ""),
        )
        for phase, status, claim_state, decision in approved:
            with self.subTest(phase=phase):
                task_id, page_id = self.facebook_task(
                    state=claim_state,
                    decision=decision,
                )
                receipt = {"pageId": page_id, "phase": phase}
                with database.connect() as conn:
                    conn.execute(
                        """
                        UPDATE publish_task_items
                        SET status = ?, receiptJson = ?, message = ?
                        WHERE taskId = ? AND platformType = 9
                        """,
                        (status, _canonical_json(receipt), "safe message", task_id),
                    )
                    conn.execute(
                        "UPDATE publish_tasks SET status = ? WHERE id = ?",
                        (status, task_id),
                    )
                    conn.commit()
                item = self.projection(task_id)["items"][0]
                self.assertEqual(item["phase"], phase)
                self.assertEqual(item["status"], status)
                self.assertEqual(
                    item["errorMessage"],
                    "Facebook Page 状态已更新" if status == "failed" else "",
                )

    def test_projection_repairs_historical_clicked_ambiguous_flags_without_db_write(
        self,
    ) -> None:
        task_id, page_id = self.facebook_task(state="ambiguous")
        historical_receipt = {
            "pageId": page_id,
            "phase": "ambiguous",
            "platformWriteOccurred": False,
            "finalActionTriggered": False,
        }
        historical_json = _canonical_json(historical_receipt)
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE facebook_page_publish_claims
                SET receiptJson = ?, receiptHash = ?
                WHERE taskId = ?
                """,
                (historical_json, _canonical_hash(historical_receipt), task_id),
            )
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = 'failed', receiptJson = ?,
                    errorCode = 'facebook_publish_outcome_unknown'
                WHERE taskId = ? AND platformType = 9
                """,
                (historical_json, task_id),
            )
            conn.execute(
                "UPDATE publish_tasks SET status = 'failed' WHERE id = ?",
                (task_id,),
            )
            conn.commit()

        item = self.projection(task_id)["items"][0]

        self.assertEqual(item["phase"], "ambiguous")
        self.assertIs(item["receipt"]["platformWriteOccurred"], True)
        self.assertIs(item["receipt"]["finalActionTriggered"], True)
        self.assertEqual(self.claim(task_id)["receiptJson"], historical_json)

    def test_progress_claim_item_event_and_heartbeat_roll_back_together(self) -> None:
        task_id, page_id = self.facebook_task()
        before = task_service.get_task(task_id)
        event_count = len(before["events"])

        with patch.object(
            task_service,
            "_insert_facebook_task_event",
            side_effect=RuntimeError("injected event failure"),
            create=True,
        ), self.assertRaises(RuntimeError):
            task_service.record_facebook_progress(
                task_id,
                phase="final_action_claimed",
                message="final boundary",
                receipt=self.checkpoint_receipt(page_id),
            )

        saved = task_service.get_task(task_id)
        self.assertEqual(self.claim(task_id)["state"], "reserved")
        self.assertEqual(saved["items"][0]["status"], "running")
        self.assertEqual(len(saved["events"]), event_count)
        self.assertEqual(saved["workerHeartbeatAt"], before["workerHeartbeatAt"])

    def test_progress_persists_only_safe_baseline_form_and_receipt(self) -> None:
        task_id, page_id = self.facebook_task()

        task_service.record_facebook_progress(
            task_id,
            phase="final_action_claimed",
            message="final boundary",
            receipt=self.checkpoint_receipt(page_id),
        )

        claim = self.claim(task_id)
        serialized = " ".join(
            str(claim[key])
            for key in (
                "baselineJson",
                "formSnapshotJson",
                "platformDecisionJson",
                "receiptJson",
            )
        )
        for forbidden in (
            "forbidden DOM",
            "forbidden full caption",
            "sessionPath",
            "facebook-session.json",
            "verificationCode",
            "cookie",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(len(str(claim["baselineHash"])), 64)
        self.assertEqual(len(str(claim["formSnapshotHash"])), 64)

    def test_typed_platform_decision_is_durable_without_a_claim_self_transition(self) -> None:
        task_id, page_id = self.facebook_task(state="final_action_clicked")
        decision = self.sealed_rejected_decision(page_id)
        with database.connect() as conn:
            conn.execute(
                "UPDATE publish_tasks SET workerHeartbeatAt = ? WHERE id = ?",
                ("2026-08-29 00:00:00", task_id),
            )
            conn.commit()
        before = task_service.get_task(task_id)

        task_service.record_facebook_progress(
            task_id,
            phase="final_action_clicked",
            message="raw decision DOM must not persist",
            receipt={"pageId": page_id, "platformDecision": decision},
        )

        claim = self.claim(task_id)
        persisted = json.loads(claim["platformDecisionJson"])
        self.assertEqual(claim["state"], "final_action_clicked")
        self.assertEqual(persisted["pageId"], page_id)
        self.assertEqual(persisted["kind"], "rejected_no_creation")
        self.assertEqual(claim["platformDecisionHash"], _canonical_hash(persisted))
        saved = task_service.get_task(task_id)
        self.assertNotEqual(saved["workerHeartbeatAt"], before["workerHeartbeatAt"])
        decision_events = [
            event
            for event in saved["events"]
            if event["eventType"] == "facebook_platform_decision_observed"
        ]
        self.assertEqual(len(decision_events), 1)
        self.assertNotIn("raw decision DOM", decision_events[0]["message"])

    def test_typed_platform_decision_rolls_back_with_injected_event_failure(self) -> None:
        task_id, page_id = self.facebook_task(state="final_action_clicked")
        decision = self.sealed_rejected_decision(page_id)
        before = task_service.get_task(task_id)

        with patch.object(
            task_service,
            "_insert_facebook_task_event",
            side_effect=RuntimeError("injected decision event failure"),
        ), self.assertRaises(RuntimeError):
            task_service.record_facebook_progress(
                task_id,
                phase="final_action_clicked",
                message="decision",
                receipt={"pageId": page_id, "platformDecision": decision},
            )

        claim = self.claim(task_id)
        self.assertEqual(claim["platformDecisionJson"], "{}")
        self.assertEqual(claim["platformDecisionHash"], "")
        saved = task_service.get_task(task_id)
        self.assertEqual(saved["workerHeartbeatAt"], before["workerHeartbeatAt"])
        self.assertEqual(len(saved["events"]), len(before["events"]))

    def test_platform_accepted_without_reel_remains_non_success(self) -> None:
        task_id, page_id = self.facebook_task(state="final_action_clicked")

        task_service.record_facebook_progress(
            task_id,
            phase="platform_accepted",
            message="Meta accepted the request",
            receipt={
                "pageId": page_id,
                "phase": "platform_accepted",
                "platformWriteOccurred": True,
                "finalActionTriggered": True,
                "reelId": None,
                "url": None,
                "publishedAt": None,
            },
        )

        item = self.projection(task_id)["items"][0]
        self.assertEqual(item["phase"], "platform_accepted")
        self.assertEqual(item["status"], "running")
        self.assertNotEqual(task_service.get_task(task_id)["status"], "success")

    def test_success_requires_exact_page_reel_url_and_published_time(self) -> None:
        task_id, page_id = self.facebook_task(state="succeeded")
        invalid_receipts = (
            {"pageId": page_id, "phase": "platform_accepted"},
            {
                **self.success_receipt(page_id),
                "pageId": str(int(page_id) + 1),
            },
            {**self.success_receipt(page_id), "reelId": None},
            {
                **self.success_receipt(page_id),
                "url": "https://www.facebook.com/posts/new-reel",
            },
            {**self.success_receipt(page_id), "publishedAt": None},
        )
        for receipt in invalid_receipts:
            with self.subTest(receipt=receipt), self.assertRaises(
                (ControlledPublishError, ValueError)
            ):
                task_service.mark_facebook_result(
                    task_id,
                    ok=True,
                    message="success",
                    receipt=receipt,
                )

    def test_target_plus_unrelated_new_reel_cannot_write_succeeded_claim(self) -> None:
        task_id, page_id = self.facebook_task(state="final_action_clicked")
        clicked_at = str(self.claim(task_id)["clickedAt"])
        old = FacebookReelRow(
            page_id=page_id,
            reel_id="old-reel",
            url="https://www.facebook.com/reel/old-reel",
            caption_sha256="c" * 64,
            published_at="2026-08-29T01:00:00+00:00",
        )
        baseline = _build_baseline(
            page_id=page_id,
            rows=(old,),
            captured_at="2026-08-30T01:59:00+00:00",
        )
        match = match_unique_new_facebook_reel(
            baseline=baseline,
            current_rows=(
                old,
                FacebookReelRow(
                    page_id=page_id,
                    reel_id="new-reel",
                    url="https://www.facebook.com/reel/new-reel",
                    caption_sha256="b" * 64,
                    published_at="2026-08-30T02:00:01+00:00",
                ),
                FacebookReelRow(
                    page_id=page_id,
                    reel_id="unrelated-new-reel",
                    url="https://www.facebook.com/reel/unrelated-new-reel",
                    caption_sha256="d" * 64,
                    published_at="2026-08-30T02:00:02+00:00",
                ),
            ),
            expected_page_id=page_id,
            expected_caption_sha256="b" * 64,
            clicked_at=clicked_at,
        )
        self.assertEqual(
            (match.status, match.new_count, match.matching_count),
            ("unique", 2, 1),
        )

        with self.assertRaises(ControlledPublishError):
            task_service.record_facebook_progress(
                task_id,
                phase="published_readback_confirmed",
                message="one target plus unrelated new reel",
                receipt={"pageId": page_id, "reelMatch": match},
                _expected_state="final_action_clicked",
            )

        self.assertEqual(self.claim(task_id)["state"], "final_action_clicked")

    def test_noncanonical_typed_reel_url_cannot_write_succeeded_claim(self) -> None:
        task_id, page_id = self.facebook_task(state="final_action_clicked")
        match = FacebookReelMatch(
            status="unique",
            receipt=FacebookReelReceipt(
                page_id=page_id,
                reel_id="new-reel",
                url="https://facebook.com/reel/new-reel",
                published_at="2026-08-30T02:00:01+00:00",
            ),
            new_count=1,
            matching_count=1,
        )

        with self.assertRaises(ControlledPublishError):
            task_service.record_facebook_progress(
                task_id,
                phase="published_readback_confirmed",
                message="noncanonical URL",
                receipt={"pageId": page_id, "reelMatch": match},
                _expected_state="final_action_clicked",
            )

        self.assertEqual(self.claim(task_id)["state"], "final_action_clicked")

    def test_stable_failure_code_and_redacted_message_are_preserved(self) -> None:
        task_id, page_id = self.facebook_task(state="ambiguous")

        task_service.mark_facebook_result(
            task_id,
            ok=False,
            message=(
                "readback mismatch token=private-token "
                "Cookie=private-cookie /Users/andy/private/session.json"
            ),
            error_code="facebook_publish_readback_mismatch",
            receipt={"pageId": page_id, "phase": "ambiguous"},
        )

        item = self.projection(task_id)["items"][0]
        self.assertEqual(
            item["errorCode"], "facebook_publish_readback_mismatch"
        )
        self.assertIn("readback mismatch", item["errorMessage"])
        serialized = json.dumps(item, ensure_ascii=False)
        self.assertNotIn("private-token", serialized)
        self.assertNotIn("private-cookie", serialized)
        self.assertNotIn("/Users/andy", serialized)

    def test_terminal_claims_reject_late_nonterminal_progress(self) -> None:
        phases = (
            "local_validation_passed",
            "waiting_user_verification",
            "platform_form_verified",
            "platform_accepted",
        )
        for state in ("succeeded", "safe_failed", "confirmed_not_published"):
            for phase in phases:
                with self.subTest(state=state, phase=phase):
                    task_id, page_id = self.facebook_task(
                        state=state,
                        decision=(
                            "rejected_no_creation"
                            if state == "confirmed_not_published"
                            else ""
                        ),
                    )
                    if state == "succeeded":
                        task_service.mark_facebook_result(
                            task_id,
                            ok=True,
                            message="authority success",
                            receipt=json.loads(self.claim(task_id)["receiptJson"]),
                        )
                    else:
                        task_service.mark_facebook_result(
                            task_id,
                            ok=False,
                            message="authority terminal",
                            receipt={"pageId": page_id},
                        )
                    before = task_service.get_task(task_id)

                    with self.assertRaises(ControlledPublishError):
                        task_service.record_facebook_progress(
                            task_id,
                            phase=phase,
                            message="late nonterminal progress",
                            receipt={"pageId": page_id, "phase": phase},
                        )

                    after = task_service.get_task(task_id)
                    self.assertEqual(self.claim(task_id)["state"], state)
                    self.assertEqual(after["status"], before["status"])
                    self.assertEqual(
                        after["items"][0]["status"],
                        before["items"][0]["status"],
                    )
                    self.assertEqual(
                        len(after["events"]),
                        len(before["events"]),
                    )

    def test_nontransition_progress_must_match_the_live_claim_phase(self) -> None:
        incompatible = (
            ("reserved", "platform_accepted"),
            ("final_action_claimed", "platform_form_verified"),
            ("final_action_clicked", "local_validation_passed"),
            ("ambiguous", "platform_accepted"),
        )
        for state, phase in incompatible:
            with self.subTest(state=state, phase=phase):
                task_id, page_id = self.facebook_task(state=state)
                before = task_service.get_task(task_id)

                with self.assertRaises(ControlledPublishError):
                    task_service.record_facebook_progress(
                        task_id,
                        phase=phase,
                        message="late progress",
                        receipt={"pageId": page_id, "phase": phase},
                    )

                after = task_service.get_task(task_id)
                self.assertEqual(self.claim(task_id)["state"], state)
                self.assertEqual(after["items"][0]["receiptJson"], before["items"][0]["receiptJson"])
                self.assertEqual(len(after["events"]), len(before["events"]))

    def test_result_claim_item_event_and_heartbeat_roll_back_together(self) -> None:
        task_id, page_id = self.facebook_task()
        before = task_service.get_task(task_id)

        with patch.object(
            task_service,
            "_insert_facebook_task_event",
            side_effect=RuntimeError("injected result event failure"),
            create=True,
        ), self.assertRaises(RuntimeError):
            task_service.mark_facebook_result(
                task_id,
                ok=False,
                message="worker interrupted",
                error_code="facebook_worker_interrupted",
                receipt={"pageId": page_id},
            )

        saved = task_service.get_task(task_id)
        self.assertEqual(self.claim(task_id)["state"], "reserved")
        self.assertEqual(saved["status"], before["status"])
        self.assertEqual(saved["items"][0]["status"], before["items"][0]["status"])
        self.assertEqual(len(saved["events"]), len(before["events"]))

    def test_stale_reserved_releases_slot_but_final_boundary_is_ambiguous(self) -> None:
        reserved_id, _ = self.facebook_task(state="reserved", stale=True)
        claimed_id, _ = self.facebook_task(
            state="final_action_claimed", stale=True
        )
        clicked_id, _ = self.facebook_task(
            state="final_action_clicked", stale=True
        )

        self.assertTrue(
            task_service.reconcile_stale_facebook_page_claim(reserved_id)
        )
        self.assertTrue(
            task_service.reconcile_stale_facebook_page_claim(claimed_id)
        )
        self.assertTrue(
            task_service.reconcile_stale_facebook_page_claim(clicked_id)
        )

        reserved = self.claim(reserved_id)
        self.assertEqual(
            (reserved["state"], reserved["blocksReplay"]),
            ("safe_failed", 0),
        )
        for task_id in (claimed_id, clicked_id):
            claim = self.claim(task_id)
            self.assertEqual((claim["state"], claim["blocksReplay"]), ("ambiguous", 1))
            saved = task_service.get_task(task_id)
            self.assertNotEqual(saved["status"], "running")
            self.assertEqual(
                saved["items"][0]["errorCode"],
                "facebook_publish_outcome_unknown",
            )
        self.assertEqual(
            task_service.get_task(reserved_id)["items"][0]["errorCode"],
            "facebook_worker_interrupted",
        )

    def test_every_irreversible_event_keeps_a_stale_reserved_claim_blocked(self) -> None:
        irreversible_events = (
            "facebook_final_action_claimed",
            "facebook_final_action_clicked",
            "facebook_platform_decision_observed",
            "facebook_platform_accepted",
            "facebook_readback_unique",
            "facebook_readback_none",
            "facebook_readback_mismatch",
            "facebook_publish_outcome_ambiguous",
            "facebook_publish_readback_confirmed",
            "facebook_success_persistence_repair_required",
            "facebook_confirmed_not_published",
            "facebook_success_projection_repaired",
        )
        for event_type in irreversible_events:
            with self.subTest(event_type=event_type):
                task_id, _ = self.facebook_task(state="reserved", stale=True)
                with database.connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO publish_task_events
                            (taskId, level, eventType, message, createdAt)
                        VALUES (?, 'info', ?, 'safe evidence', ?)
                        """,
                        (
                            task_id,
                            event_type,
                            "2026-08-30T02:00:00+00:00",
                        ),
                    )
                    conn.commit()

                self.assertTrue(
                    task_service.reconcile_stale_facebook_page_claim(task_id)
                )

                claim = self.claim(task_id)
                self.assertEqual(
                    (claim["state"], claim["blocksReplay"]),
                    ("ambiguous", 1),
                )
                item = task_service.get_task(task_id)["items"][0]
                self.assertEqual(item["errorCode"], "facebook_publish_outcome_unknown")

    def test_dead_pid_repairs_succeeded_claim_with_or_without_repair_event(self) -> None:
        for repair_event in (False, True):
            with self.subTest(repair_event=repair_event):
                task_id, page_id = self.facebook_task(
                    state="succeeded",
                    repair_event=repair_event,
                    stale=True,
                )
                with database.connect() as conn:
                    conn.execute(
                        """
                        UPDATE publish_tasks
                        SET status = 'running', successCount = 0,
                            finishedAt = NULL
                        WHERE id = ?
                        """,
                        (task_id,),
                    )
                    conn.execute(
                        """
                        UPDATE publish_task_items
                        SET status = 'running', receiptJson = '',
                            platformPostId = '', postUrl = '', publishedAt = ''
                        WHERE taskId = ? AND platformType = 9
                        """,
                        (task_id,),
                    )
                    conn.commit()

                changed = task_service.reconcile_stale_controlled_task(
                    task_id, lease_seconds=30
                )

                self.assertTrue(changed)
                saved = task_service.get_task(task_id)
                self.assertEqual(saved["status"], "success")
                self.assertEqual(saved["items"][0]["status"], "success")
                self.assertEqual(saved["items"][0]["platformPostId"], "new-reel")
                self.assertEqual(
                    json.loads(saved["items"][0]["receiptJson"])["pageId"],
                    page_id,
                )
                self.assertEqual(self.claim(task_id)["state"], "succeeded")
                self.assertEqual(self.claim(task_id)["blocksReplay"], 1)

    def test_stale_page_heartbeat_without_registered_worker_ignores_live_host_pid(self) -> None:
        task_id, _ = self.facebook_task(state="succeeded", stale=True)
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE publish_tasks
                SET status = 'running', successCount = 0, finishedAt = NULL,
                    workerPid = ?
                WHERE id = ?
                """,
                (os.getpid(), task_id),
            )
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = 'running', receiptJson = '',
                    platformPostId = '', postUrl = '', publishedAt = ''
                WHERE taskId = ? AND platformType = 9
                """,
                (task_id,),
            )
            conn.commit()

        with patch.dict(publish_service._active_threads, {}, clear=True):
            changed = task_service.reconcile_stale_controlled_task(
                task_id,
                lease_seconds=30,
            )

        self.assertTrue(changed)
        saved = task_service.get_task(task_id)
        self.assertEqual(saved["status"], "success")
        self.assertEqual(saved["items"][0]["status"], "success")
        self.assertEqual(self.claim(task_id)["state"], "succeeded")

    def test_stale_page_heartbeat_with_live_registered_worker_is_not_stolen(self) -> None:
        task_id, _ = self.facebook_task(state="reserved", stale=True)
        live_worker = SimpleNamespace(is_alive=lambda: True)
        with patch.dict(
            publish_service._active_threads,
            {task_id: live_worker},
            clear=True,
        ):
            controlled_publish.task_status(task_id)

        self.assertEqual(self.claim(task_id)["state"], "reserved")
        self.assertEqual(task_service.get_task(task_id)["status"], "running")

    def test_concurrent_page_claim_insert_makes_delete_fail_safely_and_atomically(self) -> None:
        formal_id, _ = self.facebook_task(state="safe_failed")
        with database.connect() as conn:
            claim = dict(
                conn.execute(
                    "SELECT * FROM facebook_page_publish_claims WHERE taskId = ?",
                    (formal_id,),
                ).fetchone()
            )
            conn.execute(
                "DELETE FROM facebook_page_publish_claims WHERE taskId = ?",
                (formal_id,),
            )
            conn.execute(
                "UPDATE publish_task_items SET status = 'failed' WHERE taskId = ?",
                (formal_id,),
            )
            conn.execute(
                "UPDATE publish_tasks SET status = 'failed' WHERE id = ?",
                (formal_id,),
            )
            conn.commit()

        insert_started = threading.Event()
        insert_finished = threading.Event()
        insert_errors: list[BaseException] = []
        insert_columns = [key for key in claim if key != "id"]
        insert_sql = (
            "INSERT INTO facebook_page_publish_claims ("
            + ", ".join(insert_columns)
            + ") VALUES ("
            + ", ".join("?" for _ in insert_columns)
            + ")"
        )

        def insert_claim() -> None:
            self.assertTrue(insert_started.wait(timeout=5))
            try:
                with database.connect() as conn:
                    conn.execute(insert_sql, tuple(claim[key] for key in insert_columns))
                    conn.commit()
            except BaseException as exc:
                insert_errors.append(exc)
            finally:
                insert_finished.set()

        writer = threading.Thread(target=insert_claim, daemon=True)
        writer.start()
        real_connect = task_service.connect

        class CoordinatedConnection:
            def __init__(self, conn) -> None:
                self._conn = conn
                self._began_immediate = False
                self._released_after_precheck = False

            def execute(self, sql, parameters=()):
                normalized = " ".join(str(sql).split()).upper()
                if normalized == "BEGIN IMMEDIATE":
                    insert_started.set()
                    self.assert_writer_finished()
                    self._began_immediate = True
                cursor = self._conn.execute(sql, parameters)
                if (
                    not self._began_immediate
                    and not self._released_after_precheck
                    and "FROM FACEBOOK_PAGE_PUBLISH_CLAIMS" in normalized
                    and "TASKID IN" in normalized
                ):
                    self._released_after_precheck = True
                    insert_started.set()
                    self.assert_writer_finished()
                return cursor

            def assert_writer_finished(self) -> None:
                if not insert_finished.wait(timeout=5):
                    raise AssertionError("concurrent claim writer did not finish")

            def __getattr__(self, name):
                return getattr(self._conn, name)

        @contextmanager
        def coordinated_connect():
            with real_connect() as conn:
                yield CoordinatedConnection(conn)

        expected = (
            "Facebook Page 预检或正式任务已有 claim 历史，"
            "不能删除，必须保留授权与防重复证据"
        )
        with (
            patch.object(task_service, "connect", coordinated_connect),
            self.assertRaisesRegex(ValueError, f"^{expected}$"),
        ):
            task_service.delete_tasks([formal_id])

        writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(insert_errors, [])
        self.assertIsNotNone(task_service.get_task(formal_id))
        self.assertEqual(self.claim(formal_id)["state"], "safe_failed")

    def test_delete_page_claim_task_or_preflight_is_stable_and_batch_atomic(self) -> None:
        formal_id, _ = self.facebook_task(state="safe_failed")
        claim = self.claim(formal_id)
        preflight_id = int(claim["preflightTaskId"])
        unrelated = task_service.create_pending_task(
            [
                {
                    "type": 3,
                    "contentType": "video",
                    "title": "unrelated terminal task",
                    "accountList": ["douyin.json"],
                    "fileList": ["unrelated.mp4"],
                }
            ],
            mode="desktop",
        )
        with database.connect() as conn:
            protected_ids = (formal_id, preflight_id)
            placeholders = ",".join("?" for _ in protected_ids)
            conn.execute(
                f"UPDATE publish_task_items SET status = 'failed' "
                f"WHERE taskId IN ({placeholders})",
                protected_ids,
            )
            conn.execute(
                f"UPDATE publish_tasks SET status = 'failed' "
                f"WHERE id IN ({placeholders})",
                protected_ids,
            )
            conn.execute(
                "UPDATE publish_task_items SET status = 'failed' WHERE taskId = ?",
                (unrelated["id"],),
            )
            conn.execute(
                "UPDATE publish_tasks SET status = 'failed' WHERE id = ?",
                (unrelated["id"],),
            )
            conn.commit()

        expected = (
            "Facebook Page 预检或正式任务已有 claim 历史，"
            "不能删除，必须保留授权与防重复证据"
        )
        with self.assertRaisesRegex(ValueError, f"^{expected}$"):
            task_service.delete_tasks([int(unrelated["id"]), formal_id])

        self.assertIsNotNone(task_service.get_task(int(unrelated["id"])))
        self.assertIsNotNone(task_service.get_task(formal_id))
        self.assertEqual(self.claim(formal_id)["state"], "safe_failed")

        with self.assertRaisesRegex(ValueError, f"^{expected}$"):
            task_service.delete_tasks([preflight_id])
        self.assertIsNotNone(task_service.get_task(preflight_id))
        self.assertEqual(self.claim(formal_id)["state"], "safe_failed")

    def test_generic_stale_route_closes_dead_waiting_facebook_worker(self) -> None:
        task_id, _ = self.facebook_task(state="reserved", stale=True)
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE publish_tasks SET status = 'waiting_user_verification'
                WHERE id = ?
                """,
                (task_id,),
            )
            conn.execute(
                """
                UPDATE publish_task_items
                SET status = 'waiting_user_verification'
                WHERE taskId = ? AND platformType = 9
                """,
                (task_id,),
            )
            conn.commit()

        self.assertTrue(
            task_service.reconcile_stale_controlled_task(
                task_id,
                lease_seconds=30,
            )
        )

        self.assertEqual(
            (self.claim(task_id)["state"], self.claim(task_id)["blocksReplay"]),
            ("safe_failed", 0),
        )
        saved = task_service.get_task(task_id)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["items"][0]["errorCode"], "facebook_worker_interrupted")

    def test_stale_post_click_verification_wait_becomes_ambiguous_not_retryable(self) -> None:
        task_id, page_id = self.facebook_task(
            state="final_action_clicked",
            stale=True,
        )
        task_service.record_facebook_verification_state(
            task_id,
            waiting=True,
            receipt={"pageId": page_id},
        )
        with database.connect() as conn:
            stale = (datetime.now() - timedelta(minutes=5)).isoformat()
            conn.execute(
                "UPDATE publish_tasks SET workerHeartbeatAt = ? WHERE id = ?",
                (stale, task_id),
            )
            conn.commit()

        self.assertTrue(
            task_service.reconcile_stale_controlled_task(
                task_id,
                lease_seconds=30,
            )
        )

        saved = task_service.get_task(task_id)
        self.assertEqual(self.claim(task_id)["state"], "ambiguous")
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(
            saved["items"][0]["errorCode"],
            "facebook_publish_outcome_unknown",
        )

    def test_verification_timeout_is_retryable_only_before_final_boundary(self) -> None:
        reserved_id, reserved_page = self.facebook_task(state="reserved")
        clicked_id, clicked_page = self.facebook_task(
            state="final_action_clicked"
        )

        task_service.mark_facebook_result(
            reserved_id,
            ok=False,
            message="verification timed out",
            error_code="facebook_verification_timeout",
            receipt={"pageId": reserved_page},
        )
        task_service.mark_facebook_result(
            clicked_id,
            ok=False,
            message="verification timed out after click",
            error_code="facebook_verification_timeout",
            receipt={"pageId": clicked_page},
        )

        self.assertEqual(
            task_service.get_task(reserved_id)["items"][0]["errorCode"],
            "facebook_verification_timeout",
        )
        self.assertEqual(
            task_service.get_task(clicked_id)["items"][0]["errorCode"],
            "facebook_publish_outcome_unknown",
        )

    def test_two_stale_reconcilers_are_idempotent(self) -> None:
        task_id, _ = self.facebook_task(state="reserved", stale=True)
        barrier = threading.Barrier(2)
        outcomes: list[bool] = []
        errors: list[BaseException] = []

        def reconcile() -> None:
            try:
                barrier.wait(timeout=5)
                outcomes.append(
                    task_service.reconcile_stale_facebook_page_claim(task_id)
                )
            except BaseException as exc:  # pragma: no cover - diagnostic path
                errors.append(exc)

        workers = [threading.Thread(target=reconcile) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(self.claim(task_id)["state"], "safe_failed")
        events = [
            row
            for row in task_service.get_task(task_id)["events"]
            if row["eventType"] == "facebook_worker_interrupted"
        ]
        self.assertEqual(len(events), 1)

    def test_read_only_unique_match_succeeds_without_any_write_action(self) -> None:
        task_id, page_id = self.facebook_task(state="ambiguous")
        match = FacebookReelMatch(
            status="unique",
            receipt=FacebookReelReceipt(
                page_id=page_id,
                reel_id="new-reel",
                url="https://www.facebook.com/reel/new-reel",
                published_at="2026-08-30T02:00:01+00:00",
            ),
            new_count=1,
            matching_count=1,
        )

        result, calls, sessions = self.read_only_reconcile(task_id, match)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["phase"], "published_readback_confirmed")
        self.assertEqual(self.claim(task_id)["state"], "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["expected_page_id"], page_id)
        self.assertEqual(calls[0]["baseline"].page_id, page_id)
        self.assertEqual(
            sessions,
            [f"facebook-page-{page_id}.json"],
        )

    def test_rejected_no_creation_needs_none_after_bounded_complete_read(self) -> None:
        for state in ("ambiguous", "final_action_clicked"):
            with self.subTest(state=state):
                task_id, _ = self.facebook_task(
                    state=state, decision="rejected_no_creation"
                )
                none = FacebookReelMatch("none", None, 0, 0)

                result, calls, _sessions = self.read_only_reconcile(task_id, none)

                self.assertEqual(result["phase"], "confirmed_not_published")
                self.assertEqual(
                    (
                        self.claim(task_id)["state"],
                        self.claim(task_id)["blocksReplay"],
                    ),
                    ("confirmed_not_published", 0),
                )
                self.assertEqual(len(calls), 1)

    def test_two_unique_reconcilers_converge_on_one_succeeded_claim(self) -> None:
        task_id, page_id = self.facebook_task(state="ambiguous")
        unique = FacebookReelMatch(
            status="unique",
            receipt=FacebookReelReceipt(
                page_id=page_id,
                reel_id="new-reel",
                url="https://www.facebook.com/reel/new-reel",
                published_at="2026-08-30T02:00:01+00:00",
            ),
            new_count=1,
            matching_count=1,
        )

        results, errors = self.concurrent_read_only_reconcile(
            task_id,
            (unique, unique),
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(result["status"] for result in results),
            ["failed", "success"],
        )
        self.assertEqual(self.claim(task_id)["state"], "succeeded")
        event_types = [
            event["eventType"]
            for event in task_service.get_task(task_id)["events"]
        ]
        self.assertEqual(event_types.count("facebook_readback_unique"), 1)
        self.assertEqual(
            event_types.count("facebook_publish_readback_confirmed"),
            1,
        )
        with patch.object(
            controlled_publish,
            "_facebook_page_read_only_session",
            side_effect=AssertionError("repeat terminal reconcile opened session"),
            create=True,
        ):
            repeated = controlled_publish.reconcile_facebook_page_publish_outcome(
                task_id
            )
        self.assertEqual(repeated["status"], "success")

    def test_two_rejected_none_reconcilers_converge_on_confirmed_not_published(self) -> None:
        task_id, _ = self.facebook_task(
            state="ambiguous",
            decision="rejected_no_creation",
        )
        none = FacebookReelMatch("none", None, 0, 0)

        results, errors = self.concurrent_read_only_reconcile(
            task_id,
            (none, none),
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(result["phase"] for result in results),
            ["ambiguous", "confirmed_not_published"],
        )
        self.assertEqual(
            (self.claim(task_id)["state"], self.claim(task_id)["blocksReplay"]),
            ("confirmed_not_published", 0),
        )
        event_types = [
            event["eventType"]
            for event in task_service.get_task(task_id)["events"]
        ]
        self.assertEqual(event_types.count("facebook_confirmed_not_published"), 1)
        with patch.object(
            controlled_publish,
            "_facebook_page_read_only_session",
            side_effect=AssertionError("repeat terminal reconcile opened session"),
            create=True,
        ):
            repeated = controlled_publish.reconcile_facebook_page_publish_outcome(
                task_id
            )
        self.assertEqual(repeated["phase"], "confirmed_not_published")

    def test_competing_reconcilers_cannot_accept_different_terminal_outcomes(self) -> None:
        task_id, page_id = self.facebook_task(
            state="ambiguous",
            decision="rejected_no_creation",
        )
        unique = FacebookReelMatch(
            status="unique",
            receipt=FacebookReelReceipt(
                page_id=page_id,
                reel_id="new-reel",
                url="https://www.facebook.com/reel/new-reel",
                published_at="2026-08-30T02:00:01+00:00",
            ),
            new_count=1,
            matching_count=1,
        )
        none = FacebookReelMatch("none", None, 0, 0)

        results, errors = self.concurrent_read_only_reconcile(
            task_id,
            (unique, none),
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(errors, [])
        self.assertEqual(self.claim(task_id)["state"], "succeeded")

    def test_absence_incomplete_unrelated_or_multiple_remain_ambiguous(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("absence", FacebookReelMatch("none", None, 0, 0)),
            (
                "incomplete",
                FacebookPagePublishError(
                    "facebook_page_baseline_read_failed",
                    "incomplete list",
                ),
            ),
            ("unrelated", FacebookReelMatch("mismatch", None, 1, 0)),
            ("multiple", FacebookReelMatch("mismatch", None, 2, 2)),
        )
        for label, outcome in cases:
            with self.subTest(label=label):
                task_id, _ = self.facebook_task(state="ambiguous")
                if isinstance(outcome, BaseException):
                    with self.assertRaises(type(outcome)):
                        self.read_only_reconcile(task_id, outcome)
                else:
                    result, _calls, _sessions = self.read_only_reconcile(
                        task_id, outcome
                    )
                    self.assertEqual(result["phase"], "ambiguous")
                self.assertEqual(
                    (self.claim(task_id)["state"], self.claim(task_id)["blocksReplay"]),
                    ("ambiguous", 1),
                )

    def test_ambiguous_none_or_mismatch_reconcile_releases_only_its_lease(
        self,
    ) -> None:
        outcomes = (
            ("none", FacebookReelMatch("none", None, 0, 0)),
            ("mismatch", FacebookReelMatch("mismatch", None, 2, 0)),
        )
        for label, outcome in outcomes:
            with self.subTest(label=label):
                task_id, _page_id = self.facebook_task(state="ambiguous")
                with database.connect() as conn:
                    conn.execute(
                        """
                        UPDATE publish_task_items
                        SET status='failed', errorCode='facebook_publish_outcome_unknown'
                        WHERE taskId=? AND platformType=9
                        """,
                        (task_id,),
                    )
                    conn.execute(
                        """
                        UPDATE publish_tasks
                        SET status='failed', workerToken='stale-formal-owner',
                            workerPid=12345,
                            workerHeartbeatAt='2026-08-30T01:00:00+00:00'
                        WHERE id=?
                        """,
                        (task_id,),
                    )
                    conn.commit()

                result, _calls, _sessions = self.read_only_reconcile(
                    task_id, outcome
                )

                saved = task_service.get_task(task_id)
                self.assertEqual((result["status"], result["phase"]), ("failed", "ambiguous"))
                self.assertEqual(saved["workerToken"], "")
                self.assertIsNone(saved["workerPid"])
                self.assertIsNone(saved["workerHeartbeatAt"])
                self.assertEqual(
                    (self.claim(task_id)["state"], self.claim(task_id)["blocksReplay"]),
                    ("ambiguous", 1),
                )
                unknown_events = [
                    event
                    for event in saved["events"]
                    if event["eventType"]
                    == "facebook_reconciliation_outcome_unknown"
                ]
                self.assertEqual(len(unknown_events), 1)

    def test_ambiguous_reconcile_cleanup_does_not_clear_replacement_owner(
        self,
    ) -> None:
        task_id, _page_id = self.facebook_task(state="ambiguous")
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE publish_task_items
                SET status='failed', errorCode='facebook_publish_outcome_unknown'
                WHERE taskId=? AND platformType=9
                """,
                (task_id,),
            )
            conn.execute(
                """
                UPDATE publish_tasks
                SET status='failed', workerToken='stale-formal-owner',
                    workerPid=12345,
                    workerHeartbeatAt='2026-08-30T01:00:00+00:00'
                WHERE id=?
                """,
                (task_id,),
            )
            conn.commit()

        async def replace_owner_then_return(_snapshot):
            with database.connect() as conn:
                conn.execute(
                    """
                    UPDATE publish_tasks
                    SET workerToken='replacement-owner', workerPid=67890,
                        workerHeartbeatAt='2026-08-30T03:00:00+00:00'
                    WHERE id=?
                    """,
                    (task_id,),
                )
                conn.commit()
            return FacebookReelMatch("mismatch", None, 1, 0)

        with patch.object(
            controlled_publish,
            "_read_facebook_page_reconciliation",
            side_effect=replace_owner_then_return,
        ):
            controlled_publish.reconcile_facebook_page_publish_outcome(task_id)

        saved = task_service.get_task(task_id)
        self.assertEqual(saved["workerToken"], "replacement-owner")
        self.assertEqual(saved["workerPid"], 67890)
        self.assertEqual(
            saved["workerHeartbeatAt"], "2026-08-30T03:00:00+00:00"
        )
        self.assertEqual(
            (self.claim(task_id)["state"], self.claim(task_id)["blocksReplay"]),
            ("ambiguous", 1),
        )
        self.assertFalse(
            any(
                event["eventType"] == "facebook_reconciliation_outcome_unknown"
                for event in saved["events"]
            )
        )

    def test_corrupted_snapshots_or_hashes_fail_closed_before_session(self) -> None:
        corruptions = (
            ("baselineJson", '{"pageId":"corrupt"}'),
            ("baselineHash", "0" * 64),
            ("formSnapshotJson", '{"pageId":"corrupt"}'),
            ("formSnapshotHash", "1" * 64),
            ("platformDecisionJson", '{"kind":"accepted"}'),
            ("platformDecisionHash", "2" * 64),
            ("receiptJson", '{"phase":"ambiguous","token":"secret"}'),
            ("receiptHash", "3" * 64),
        )
        for column, value in corruptions:
            with self.subTest(column=column):
                task_id, _ = self.facebook_task(
                    state="ambiguous", decision="accepted"
                )
                with database.connect() as conn:
                    conn.execute(
                        f"UPDATE facebook_page_publish_claims SET {column} = ? WHERE taskId = ?",
                        (value, task_id),
                    )
                    conn.commit()
                with patch.object(
                    controlled_publish,
                    "_facebook_page_read_only_session",
                    side_effect=AssertionError("session must not open"),
                    create=True,
                ), self.assertRaises(ControlledPublishError):
                    controlled_publish.reconcile_facebook_page_publish_outcome(
                        task_id
                    )
                self.assertEqual(self.claim(task_id)["state"], "ambiguous")

    def test_second_reconciliation_cannot_rewrite_terminal_claims(self) -> None:
        for state in ("succeeded", "safe_failed", "confirmed_not_published"):
            with self.subTest(state=state):
                task_id, _ = self.facebook_task(
                    state=state,
                    decision=(
                        "rejected_no_creation"
                        if state == "confirmed_not_published"
                        else ""
                    ),
                )
                before = self.claim(task_id)
                with patch.object(
                    controlled_publish,
                    "_facebook_page_read_only_session",
                    side_effect=AssertionError("terminal claim must not open session"),
                    create=True,
                ):
                    controlled_publish.reconcile_facebook_page_publish_outcome(
                        task_id
                    )
                after = self.claim(task_id)
                self.assertEqual(after["state"], before["state"])
                self.assertEqual(after["blocksReplay"], before["blocksReplay"])
                self.assertEqual(after["receiptJson"], before["receiptJson"])
                self.assertEqual(after["receiptHash"], before["receiptHash"])


if __name__ == "__main__":
    unittest.main()
