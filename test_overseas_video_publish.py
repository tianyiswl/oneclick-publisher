# -*- coding: utf-8 -*-
"""TikTok/YouTube 正式发布的离线契约测试，不访问平台。"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app_core import overseas_tiktok_publish, overseas_video_publish
from app_core.overseas_tiktok_publish import TikTokPublishError
from uploader.tk_uploader import main as tiktok_uploader
from uploader.youtube_uploader import main as youtube_uploader
from uploader.tk_uploader.main import (
    TiktokVideo,
    tiktok_publish_success_signal,
    tiktok_security_intervention_reason,
)
from uploader.tk_uploader.schedule_form import (
    TikTokScheduleAcceptance,
    TikTokScheduleForm,
    TikTokScheduleFormSnapshot,
    TikTokScheduledContentExpectation,
    TikTokScheduledContentReadback,
    TikTokScheduleTarget,
)
from uploader.youtube_uploader.main import (
    YouTubeVideo,
    youtube_content_list_receipt,
    youtube_preflight_receipt,
    youtube_publish_success_signal,
    youtube_security_intervention_reason,
)


class FakeContentLeaf:
    def __init__(
        self,
        *,
        text: str | None = None,
        href: str | None = None,
        visible: bool = True,
    ) -> None:
        self.text = text
        self.href = href
        self.visible = visible

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return int(self.text is not None or self.href is not None)

    async def is_visible(self) -> bool:
        return self.visible

    async def inner_text(self, timeout: int = 0) -> str:
        del timeout
        if self.text is None:
            raise RuntimeError("field unreadable")
        return self.text

    async def get_attribute(self, name: str) -> str | None:
        return self.href if name == "href" else None


class FakeContentRow:
    def __init__(
        self,
        *,
        video_id: str,
        title: str | None,
        visibility: str | None,
        visible: bool = True,
    ) -> None:
        self.visible = visible
        self.title = FakeContentLeaf(
            text=title,
            href=f"/video/{video_id}" if video_id else None,
        )
        self.visibility = FakeContentLeaf(text=visibility)

    async def is_visible(self) -> bool:
        return self.visible

    def locator(self, selector: str) -> FakeContentLeaf:
        if selector == "#video-title":
            return self.title
        return self.visibility


class FakeContentRows:
    def __init__(self, rows: list[FakeContentRow]) -> None:
        self.rows = rows

    async def count(self) -> int:
        return len(self.rows)

    def nth(self, index: int) -> FakeContentRow:
        return self.rows[index]


class FakeContentPage:
    def __init__(
        self,
        rows: list[FakeContentRow],
        *,
        empty_state_visible: bool = False,
    ) -> None:
        self.url = "https://studio.youtube.com/channel/test/videos/"
        self.rows = FakeContentRows(rows)
        self.empty_state_visible = empty_state_visible

    def locator(self, selector: str):
        if selector == "ytcp-video-row":
            return self.rows
        return FakeContentLeaf(
            text="No videos" if self.empty_state_visible else None,
            visible=self.empty_state_visible,
        )


class FakeTikTokCollection:
    def __init__(self, items: list[object]) -> None:
        self.items = items

    @property
    def first(self):
        return self.items[0] if self.items else FakeTikTokLeaf(visible=False)

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int):
        return self.items[index]


class FakeTikTokLeaf:
    def __init__(
        self,
        text: str = "",
        *,
        visible: bool = True,
        editable: bool = False,
        on_click=None,
    ) -> None:
        self.text = text
        self.visible = visible
        self.editable = editable
        self.on_click = on_click

    async def is_visible(self) -> bool:
        return self.visible

    async def count(self) -> int:
        return 1

    async def is_editable(self) -> bool:
        return self.editable

    async def is_enabled(self) -> bool:
        return True

    async def inner_text(self, timeout: int = 0) -> str:
        del timeout
        return self.text

    async def get_attribute(self, name: str) -> str | None:
        del name
        return None

    async def click(self) -> None:
        if self.on_click is not None:
            self.on_click()


class FakeTikTokEditor(FakeTikTokLeaf):
    def __init__(
        self,
        page,
        text: str = "legacy caption",
        *,
        visible: bool = True,
        clear_blocked: bool = False,
    ) -> None:
        super().__init__(text, visible=visible, editable=True)
        self.page = page
        self.clear_blocked = clear_blocked
        self.entity_nodes: list[str] = []
        self.entity_snapshots: list[list[str]] | None = None
        self.active_entity_snapshot: list[str] | None = None
        self.normalized_text_before_topics: str | None = None

    async def click(self) -> None:
        self.page.active_editor = self

    async def focus(self) -> None:
        self.page.active_editor = self

    def locator(self, selector: str) -> FakeTikTokCollection:
        if self.entity_snapshots is not None:
            if (
                selector == tiktok_uploader.TOPIC_ENTITY_SELECTORS[0]
                or self.active_entity_snapshot is None
            ):
                self.active_entity_snapshot = self.entity_snapshots[0]
                if len(self.entity_snapshots) > 1:
                    self.active_entity_snapshot = self.entity_snapshots.pop(0)
            values = self.active_entity_snapshot
            return FakeTikTokCollection(
                [FakeTikTokLeaf(text=value) for value in values]
            )
        return FakeTikTokCollection(
            [FakeTikTokLeaf(text=value) for value in self.entity_nodes]
        )


class FakeTikTokKeyboard:
    def __init__(self, page) -> None:
        self.page = page
        self.select_all = False

    async def press(self, key: str) -> None:
        editor = self.page.active_editor
        if editor is None:
            raise AssertionError("keyboard used without active editor")
        if key == "ControlOrMeta+A":
            self.select_all = True
        elif key == "Backspace" and self.select_all:
            if not editor.clear_blocked:
                editor.text = ""
            self.select_all = False
        elif key == "ControlOrMeta+End":
            self.select_all = False

    async def insert_text(self, value: str) -> None:
        editor = self.page.active_editor
        if editor is None:
            raise AssertionError("keyboard used without active editor")
        if value.startswith("#"):
            if editor.normalized_text_before_topics is None:
                editor.normalized_text_before_topics = editor.text.rstrip()
            self.page.current_topic = value[1:]
        editor.text += value


class FakeTikTokCandidate(FakeTikTokLeaf):
    def __init__(
        self,
        page,
        label: str,
        *,
        entity_values: list[str] | None = None,
        mutate_text: str | None = None,
        focused: bool = False,
    ) -> None:
        self.clicked = False
        self.focused = focused

        def select() -> None:
            self.clicked = True
            if entity_values is not None:
                page.editor.entity_nodes.extend(entity_values)
            if mutate_text is not None:
                page.editor.text = mutate_text

        super().__init__(label, visible=True, on_click=select)

    async def get_attribute(self, name: str) -> str | None:
        if name == "class":
            suffix = " focused" if self.focused else ""
            return f"hashtag-suggestion-item{suffix}"
        if name == "aria-selected":
            return "true" if self.focused else "false"
        return None


class FakeTikTokBase:
    def __init__(self, page, editors: list[FakeTikTokEditor]) -> None:
        self.page = page
        self.editors = editors

    def locator(self, selector: str) -> FakeTikTokCollection:
        if "contenteditable" in selector or "DraftEditor" in selector:
            return FakeTikTokCollection(self.editors)
        return FakeTikTokCollection(
            list(self.page.candidates.get(self.page.current_topic, []))
        )


class FakeTikTokPage:
    def __init__(
        self,
        *,
        editor_count: int = 1,
        editor_text: str = "legacy caption",
        clear_blocked: bool = False,
    ) -> None:
        self.url = tiktok_uploader.UPLOAD_URL
        self.active_editor: FakeTikTokEditor | None = None
        self.current_topic = ""
        self.candidates: dict[str, list[FakeTikTokCandidate]] = {}
        self.keyboard = FakeTikTokKeyboard(self)
        self.editor = FakeTikTokEditor(
            self,
            editor_text,
            clear_blocked=clear_blocked,
        )
        editors = [self.editor]
        if editor_count == 0:
            editors = []
        elif editor_count > 1:
            editors.extend(
                FakeTikTokEditor(self, editor_text) for _ in range(editor_count - 1)
            )
        self.base = FakeTikTokBase(self, editors)

    def locator(self, selector: str):
        if selector == 'iframe[data-tt="Upload_index_iframe"]':
            return FakeTikTokCollection([])
        return self.base.locator(selector)

    async def wait_for_timeout(self, milliseconds: int) -> None:
        del milliseconds


class OverseasVideoPublishPolicyTests(unittest.TestCase):
    def _payload(self, root: Path, platform_type: int) -> dict:
        video = root / "video.mp4"
        video.write_bytes(b"offline-video")
        account = root / f"account-{platform_type}.json"
        account.write_text("{}", encoding="utf-8")
        return {
            "type": platform_type,
            "contentType": "video",
            "runtimeMode": "publish",
            "debugDryRun": False,
            "overseasVideoPublishConfirmed": True,
            "title": "海外正式发布测试",
            "description": "只验证离线契约",
            "tags": ["oneclick"],
            "fileList": [str(video)],
            "accountList": [account.name],
            "visibility": "public",
            "enableTimer": False,
            "notifySubscribers": True,
        }

    def test_tiktok_and_youtube_accept_minimum_immediate_publish_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with patch.object(overseas_video_publish, "COOKIE_DIR", root):
                for platform_type in (6, 7):
                    checked = overseas_video_publish.validate_overseas_video_publish_payload(
                        self._payload(root, platform_type)
                    )
                    self.assertTrue(checked["ok"], checked["errors"])

    def test_confirmation_and_immediate_publish_are_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 6)
            payload["overseasVideoPublishConfirmed"] = False
            payload["enableTimer"] = True
            payload["scheduleTime"] = "2026-08-12 10:00"
            with patch.object(overseas_video_publish, "COOKIE_DIR", root):
                checked = overseas_video_publish.validate_overseas_video_publish_payload(
                    payload
                )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("正式发布确认" in item for item in checked["errors"]))
        self.assertTrue(any("立即发布" in item for item in checked["errors"]))

    def test_unreadable_tiktok_fields_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 6)
            payload.update(
                {
                    "visibility": "private",
                    "aiGenerated": True,
                    "coverPath": str(root / "cover.png"),
                }
            )
            with patch.object(overseas_video_publish, "COOKIE_DIR", root):
                checked = overseas_video_publish.validate_overseas_video_publish_payload(
                    payload
                )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("公开可见" in item for item in checked["errors"]))
        self.assertTrue(any("AI 内容声明" in item for item in checked["errors"]))
        self.assertTrue(any("自定义封面" in item for item in checked["errors"]))

    def test_youtube_requires_supported_notification_readback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 7)
            payload["notifySubscribers"] = False
            with patch.object(overseas_video_publish, "COOKIE_DIR", root):
                checked = overseas_video_publish.validate_overseas_video_publish_payload(
                    payload
                )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("不通知订阅者" in item for item in checked["errors"]))

    def test_service_requires_verified_platform_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 7)
            with (
                patch.object(overseas_video_publish, "COOKIE_DIR", root),
                patch.dict(
                    overseas_video_publish.HANDLERS,
                    {
                        7: lambda *_args, **_kwargs: [
                            {
                                "status": "published",
                                "evidence": "studio_content_list:new-id:public",
                            }
                        ]
                    },
                ),
            ):
                result = overseas_video_publish.run_overseas_video_publish_sync(
                    payload
                )
        self.assertTrue(result["ok"])
        self.assertEqual(result["visibility"], "public")

    def test_empty_platform_receipt_is_never_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 6)
            with (
                patch.object(overseas_video_publish, "COOKIE_DIR", root),
                patch.dict(
                    overseas_video_publish.HANDLERS,
                    {6: lambda *_args, **_kwargs: [None]},
                ),
            ):
                with self.assertRaisesRegex(
                    overseas_video_publish.OverseasVideoPublishError,
                    "平台成功回执",
                ):
                    overseas_video_publish.run_overseas_video_publish_sync(payload)

    def test_youtube_private_save_is_not_reported_as_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 7)
            payload["visibility"] = "private"
            with (
                patch.object(overseas_video_publish, "COOKIE_DIR", root),
                patch.dict(
                    overseas_video_publish.HANDLERS,
                    {
                        7: lambda *_args, **_kwargs: [
                            {
                                "status": "saved",
                                "evidence": "studio_content_list:new-id:private",
                            }
                        ]
                    },
                ),
            ):
                result = overseas_video_publish.run_overseas_video_publish_sync(
                    payload
                )
        self.assertTrue(result["ok"])
        self.assertTrue(result["saved"])
        self.assertFalse(result["published"])
        self.assertEqual(result["visibility"], "private")

    def test_youtube_dialog_closed_without_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = self._payload(root, 7)
            with (
                patch.object(overseas_video_publish, "COOKIE_DIR", root),
                patch.dict(
                    overseas_video_publish.HANDLERS,
                    {
                        7: lambda *_args, **_kwargs: [
                            {
                                "status": "published",
                                "evidence": "studio_upload_dialog_closed:public",
                            }
                        ]
                    },
                ),
            ):
                with self.assertRaisesRegex(
                    overseas_video_publish.OverseasVideoPublishError,
                    "平台成功回执",
                ):
                    overseas_video_publish.run_overseas_video_publish_sync(
                        payload
                    )


class TikTokFormAdapterTests(unittest.TestCase):
    def uploader(
        self,
        *,
        title: str = "Title",
        description: str = "Body",
        tags: list[str] | None = None,
        execution_mode: str = "platform_form_check",
        publish_date: object = None,
    ) -> TiktokVideo:
        app = TiktokVideo(
            title,
            "/not/used.mp4",
            tags if tags is not None else ["AI", "效率"],
            publish_date,
            "/not/used.json",
            description=description,
            expected_account_reference="expected.user",
            execution_mode=execution_mode,
        )
        app._upload_file = AsyncMock(return_value=None)
        app._wait_until_ready = AsyncMock(return_value=None)
        app._ensure_public_visibility = AsyncMock(return_value="public")
        app._post_button = AsyncMock(return_value=FakeTikTokLeaf(text="Post"))
        return app

    def test_publish_date_activation_is_type_strict(self) -> None:
        for immediate in (None, 0):
            with self.subTest(immediate=immediate):
                app = self.uploader(publish_date=immediate)
                self.assertIsNone(app.publish_date)
                self.assertIsNone(app._schedule_form)
        scheduled = self.uploader(publish_date="2026-08-29 15:00")
        self.assertEqual(scheduled.publish_date, "2026-08-29 15:00")
        for invalid in (
            datetime(2026, 8, 29, 15, 0),
            datetime(2026, 8, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            False,
            0.0,
            "",
            "2026-8-29 15:00",
            "not-a-time",
            1,
        ):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(TikTokPublishError) as raised:
                    self.uploader(publish_date=invalid)
                self.assertEqual(raised.exception.error_code, "tiktok_schedule_invalid")

    def test_prepare_form_configures_schedule_after_visibility_and_before_snapshot(self) -> None:
        page = FakeTikTokPage()
        app = self.uploader(tags=[], publish_date="2026-08-29 15:00")
        target = TikTokScheduleTarget("2026-08-29 15:00", "Asia/Shanghai")
        snapshot = TikTokScheduleFormSnapshot(
            schedule_mode="platform_native",
            scheduled_at="2026-08-29 15:00",
            timezone="Asia/Shanghai",
            toggle_enabled=True,
            final_action_label="Schedule",
            final_action_ready=True,
        )
        form = AsyncMock(spec=TikTokScheduleForm)
        clicks: list[str] = []
        order: list[str] = []

        async def visibility(*_args):
            order.append("visibility")
            return "public"

        async def configure(_target):
            order.append("configure")
            return snapshot

        async def verify(_target):
            order.append("snapshot")
            return snapshot

        app._ensure_public_visibility = AsyncMock(side_effect=visibility)
        form.configure.side_effect = configure
        form.verify.side_effect = verify
        form.final_button.return_value = FakeTikTokLeaf(
            text="Schedule",
            on_click=lambda: clicks.append("schedule"),
        )
        with patch.object(tiktok_uploader, "TikTokScheduleForm", return_value=form):
            receipt = asyncio.run(app.prepare_form(page, page.base))
        form.configure.assert_awaited_once_with(target)
        form.verify.assert_awaited_once_with(target)
        form.final_button.assert_awaited_once_with(target)
        self.assertEqual(clicks, [])
        self.assertLess(order.index("visibility"), order.index("configure"))
        self.assertLess(order.index("configure"), order.index("snapshot"))
        self.assertEqual(receipt["scheduledAt"], "2026-08-29 15:00")

    def test_scheduled_submit_clicks_schedule_once_and_never_post(self) -> None:
        clicks: list[str] = []
        schedule_button = FakeTikTokLeaf(
            text="Schedule",
            on_click=lambda: clicks.append("schedule"),
        )
        app = self.uploader(
            tags=[],
            publish_date="2026-08-29 15:00",
            execution_mode="formal",
        )
        app.publish_confirmed = True
        form = AsyncMock(spec=TikTokScheduleForm)
        form.final_button.return_value = schedule_button
        form.wait_for_acceptance.return_value = TikTokScheduleAcceptance(
            evidence="platform_feedback:scheduled",
            accepted_at=datetime(2026, 8, 29, 14, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        form.readback_scheduled_content.return_value = TikTokScheduledContentReadback(
            content_id=None,
            content_url=None,
            scheduled_at="2026-08-29 15:00",
            schedule_timezone="Asia/Shanghai",
            evidence="scheduled_list:unique",
        )
        app._schedule_form = form
        app._schedule_target = TikTokScheduleTarget(
            "2026-08-29 15:00", "Asia/Shanghai"
        )
        app.authorized_snapshot_validator = lambda snapshot: snapshot
        app.schedule_checkpoint_observer = lambda stage: None
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        app._post_button = AsyncMock(
            side_effect=AssertionError("scheduled mode must not resolve Post")
        )
        clicked_at = datetime(2026, 8, 29, 14, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
        snapshot = {
            "finalActionLabel": "Schedule",
            "scheduledAt": "2026-08-29 15:00",
        }
        with (
            patch.object(tiktok_uploader, "_uploader_shanghai_now", return_value=clicked_at),
            patch.object(app, "_verify_form_snapshot", new=AsyncMock(return_value=snapshot)),
        ):
            result = asyncio.run(app.submit_once(AsyncMock(), AsyncMock()))
        expected_caption = app._caption()
        expectation = TikTokScheduledContentExpectation(
            account_reference=app.expected_account_reference,
            expected_caption=expected_caption,
            caption_sha256=hashlib.sha256(expected_caption.encode("utf-8")).hexdigest(),
            target=app._schedule_target,
            submitted_after=clicked_at,
        )
        self.assertEqual(clicks, ["schedule"])
        self.assertEqual(result["phase"], "scheduled_readback_confirmed")
        form.wait_for_acceptance.assert_awaited_once_with(app._schedule_target)
        form.readback_scheduled_content.assert_awaited_once_with(expectation)
        app._post_button.assert_not_awaited()

    def test_scheduled_click_exception_is_outcome_unknown(self) -> None:
        clicks: list[str] = []
        raw_button = FakeTikTokLeaf(
            on_click=lambda: (_ for _ in ()).throw(RuntimeError("detached"))
        )
        button = overseas_tiktok_publish._FinalActionButton(
            raw_button,
            lambda: clicks.append("final"),
        )
        app = self.uploader(tags=[], publish_date="2026-08-29 15:00", execution_mode="formal")
        app.publish_confirmed = True
        app._schedule_target = TikTokScheduleTarget("2026-08-29 15:00", "Asia/Shanghai")
        form = AsyncMock(spec=TikTokScheduleForm)
        form.final_button.return_value = button
        app._schedule_form = form
        app.authorized_snapshot_validator = lambda value: value
        app.schedule_checkpoint_observer = lambda stage: clicks.append(stage)
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        app._verify_form_snapshot = AsyncMock(return_value={"finalActionLabel": "Schedule"})
        with self.assertRaises(TikTokPublishError) as raised:
            asyncio.run(app.submit_once(AsyncMock(), AsyncMock()))
        self.assertEqual(raised.exception.error_code, "tiktok_schedule_outcome_unknown")
        self.assertTrue(raised.exception.outcome_ambiguous)
        self.assertEqual(clicks, ["final"])
        form.wait_for_acceptance.assert_not_awaited()

    def test_schedule_checkpoint_write_failure_blocks_success_and_readback(self) -> None:
        app = self.uploader(tags=[], publish_date="2026-08-29 15:00", execution_mode="formal")
        app.publish_confirmed = True
        app._schedule_target = TikTokScheduleTarget("2026-08-29 15:00", "Asia/Shanghai")
        form = AsyncMock(spec=TikTokScheduleForm)
        form.final_button.return_value = FakeTikTokLeaf(text="Schedule")
        form.wait_for_acceptance.return_value = TikTokScheduleAcceptance(
            "scheduled", datetime(2026, 8, 29, 14, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        app._schedule_form = form
        app.authorized_snapshot_validator = lambda value: value
        app.schedule_checkpoint_observer = lambda stage: (_ for _ in ()).throw(RuntimeError("db"))
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        app._verify_form_snapshot = AsyncMock(return_value={"finalActionLabel": "Schedule"})
        with self.assertRaises(TikTokPublishError) as raised:
            asyncio.run(app.submit_once(AsyncMock(), AsyncMock()))
        self.assertEqual(raised.exception.error_code, "tiktok_schedule_outcome_unknown")
        self.assertTrue(raised.exception.outcome_ambiguous)
        form.readback_scheduled_content.assert_not_awaited()

    def test_schedule_mutation_after_prepare_is_blocked_before_click(self) -> None:
        clicked: list[str] = []
        app = self.uploader(tags=[], publish_date="2026-08-29 15:00", execution_mode="formal")
        app.publish_confirmed = True
        app._schedule_target = TikTokScheduleTarget("2026-08-29 15:00", "Asia/Shanghai")
        form = AsyncMock(spec=TikTokScheduleForm)
        form.final_button.return_value = FakeTikTokLeaf(
            text="Schedule", on_click=lambda: clicked.append("schedule")
        )
        app._schedule_form = form
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        app._verify_form_snapshot = AsyncMock(
            return_value={
                "scheduleMode": "platform_native",
                "scheduledAt": "2026-08-29 15:01",
                "scheduleTimezone": "Asia/Shanghai",
                "scheduleToggleEnabled": True,
                "finalActionLabel": "Schedule",
                "finalActionReady": True,
            }
        )
        app.authorized_snapshot_validator = lambda snapshot: (_ for _ in ()).throw(
            TikTokPublishError(
                "tiktok_form_snapshot_mismatch",
                "changed",
            )
        )
        app.schedule_checkpoint_observer = lambda stage: clicked.append(stage)
        app._post_button = AsyncMock(side_effect=AssertionError("must not resolve Post"))
        with self.assertRaises(TikTokPublishError) as raised:
            asyncio.run(app.submit_once(AsyncMock(), AsyncMock()))
        self.assertEqual(raised.exception.error_code, "tiktok_form_snapshot_mismatch")
        self.assertEqual(clicked, [])
        form.final_button.assert_not_awaited()
        app._post_button.assert_not_awaited()

    def test_final_schedule_gate_failure_stays_pre_click_and_non_ambiguous(self) -> None:
        clicked: list[str] = []
        raw_button = FakeTikTokLeaf(
            text="Schedule", on_click=lambda: clicked.append("schedule")
        )

        def reject_window() -> None:
            raise TikTokPublishError(
                "tiktok_schedule_out_of_range",
                "too late",
            )

        wrapped = overseas_tiktok_publish._FinalActionButton(
            raw_button,
            reject_window,
        )
        app = self.uploader(tags=[], publish_date="2026-08-29 15:00", execution_mode="formal")
        app.publish_confirmed = True
        app._schedule_target = TikTokScheduleTarget("2026-08-29 15:00", "Asia/Shanghai")
        form = AsyncMock(spec=TikTokScheduleForm)
        form.final_button.return_value = wrapped
        app._schedule_form = form
        app.authorized_snapshot_validator = lambda value: value
        app.schedule_checkpoint_observer = lambda stage: None
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        app._verify_form_snapshot = AsyncMock(return_value={"finalActionLabel": "Schedule"})
        with self.assertRaises(TikTokPublishError) as raised:
            asyncio.run(app.submit_once(AsyncMock(), AsyncMock()))
        self.assertEqual(raised.exception.error_code, "tiktok_schedule_out_of_range")
        self.assertFalse(raised.exception.outcome_ambiguous)
        self.assertEqual(clicked, [])
        form.wait_for_acceptance.assert_not_awaited()

    def test_upload_waits_for_slow_page_before_selecting_video(self) -> None:
        class EmptyLocator:
            @property
            def first(self):
                return self

            async def count(self) -> int:
                return 0

        class DelayedFileInput:
            def __init__(self) -> None:
                self.reads = 0
                self.selected_path: str | None = None

            @property
            def first(self):
                return self

            async def count(self) -> int:
                self.reads += 1
                return int(self.reads >= 3)

            async def set_input_files(self, path: str) -> None:
                self.selected_path = path

        class SlowUploadPage:
            def __init__(self) -> None:
                self.url = tiktok_uploader.UPLOAD_URL
                self.file_input = DelayedFileInput()
                self.waits = 0

            def locator(self, selector: str):
                if selector == 'iframe[data-tt="Upload_index_iframe"]':
                    return EmptyLocator()
                if selector == 'input[type="file"]':
                    return self.file_input
                return EmptyLocator()

            def get_by_role(self, *_args, **_kwargs):
                return EmptyLocator()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                self.waits += 1

        page = SlowUploadPage()
        app = TiktokVideo(
            "Title",
            "/tmp/slow-upload.mp4",
            [],
            0,
            "/not/used.json",
            execution_mode="platform_form_check",
        )
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        asyncio.run(app._upload_file(page, page))

        self.assertEqual(page.file_input.selected_path, "/tmp/slow-upload.mp4")
        self.assertGreaterEqual(page.waits, 2)
        self.assertGreaterEqual(app._wait_for_manual_intervention.await_count, 2)

    def test_upload_entry_timeout_has_a_stable_error_code(self) -> None:
        class EmptyLocator:
            @property
            def first(self):
                return self

            async def count(self) -> int:
                return 0

        class MissingUploadPage:
            url = tiktok_uploader.UPLOAD_URL

            def locator(self, _selector: str):
                return EmptyLocator()

            def get_by_role(self, *_args, **_kwargs):
                return EmptyLocator()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        page = MissingUploadPage()
        app = TiktokVideo(
            "Title",
            "/tmp/missing-upload.mp4",
            [],
            0,
            "/not/used.json",
            execution_mode="platform_form_check",
        )
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        with (
            patch.object(tiktok_uploader, "UPLOAD_ENTRY_TIMEOUT_SECONDS", 0),
            self.assertRaises(TikTokPublishError) as raised,
        ):
            asyncio.run(app._upload_file(page, page))

        self.assertEqual(raised.exception.error_code, "tiktok_upload_entry_timeout")

    def test_upload_preserves_missing_file_error_instead_of_rewriting_it_as_timeout(self) -> None:
        class EmptyLocator:
            @property
            def first(self):
                return self

            async def count(self) -> int:
                return 0

        class MissingFileInput:
            @property
            def first(self):
                return self

            async def count(self) -> int:
                return 1

            async def set_input_files(self, _path: str) -> None:
                raise FileNotFoundError("video disappeared")

        class MissingFilePage:
            url = tiktok_uploader.UPLOAD_URL

            def locator(self, selector: str):
                if selector == 'iframe[data-tt="Upload_index_iframe"]':
                    return EmptyLocator()
                return MissingFileInput()

            def get_by_role(self, *_args, **_kwargs):
                return EmptyLocator()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        page = MissingFilePage()
        app = TiktokVideo(
            "Title",
            "/tmp/deleted-video.mp4",
            [],
            0,
            "/not/used.json",
            execution_mode="platform_form_check",
        )
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        with self.assertRaises(FileNotFoundError):
            asyncio.run(app._upload_file(page, page))

    def test_upload_entry_uses_one_absolute_deadline_before_polling(self) -> None:
        class UnexpectedLookupPage:
            url = tiktok_uploader.UPLOAD_URL

            def locator(self, _selector: str):
                raise AssertionError("expired upload deadline must not poll")

        app = TiktokVideo(
            "Title",
            "/tmp/missing-upload.mp4",
            [],
            0,
            "/not/used.json",
            execution_mode="platform_form_check",
        )
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        with (
            patch.object(
                tiktok_uploader,
                "UPLOAD_ENTRY_TIMEOUT_SECONDS",
                0,
            ),
            self.assertRaises(TikTokPublishError) as raised,
        ):
            asyncio.run(app._upload_file(UnexpectedLookupPage(), UnexpectedLookupPage()))

        self.assertEqual(raised.exception.error_code, "tiktok_upload_entry_timeout")

    def test_upload_does_not_click_a_button_when_deadline_expires_after_lookup(self) -> None:
        class Clock:
            now = 0.0

            def time(self) -> float:
                return self.now

        class EmptyLocator:
            @property
            def first(self):
                return self

            async def count(self) -> int:
                return 0

        class ExpiringButton:
            @property
            def first(self):
                return self

            async def count(self) -> int:
                clock.now = 10.0
                return 1

            async def click(self, **_kwargs) -> None:
                raise AssertionError("click attempted after deadline")

        class ExpiringPage:
            url = tiktok_uploader.UPLOAD_URL

            def locator(self, _selector: str):
                return EmptyLocator()

            def get_by_role(self, *_args, **_kwargs):
                return ExpiringButton()

            def expect_file_chooser(self, **_kwargs):
                raise AssertionError("chooser entered after deadline")

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        clock = Clock()
        app = TiktokVideo(
            "Title",
            "/tmp/missing-upload.mp4",
            [],
            0,
            "/not/used.json",
            execution_mode="platform_form_check",
        )
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        with (
            patch.object(tiktok_uploader, "UPLOAD_ENTRY_TIMEOUT_SECONDS", 10),
            patch.object(tiktok_uploader.asyncio, "get_running_loop", return_value=clock),
            self.assertRaises(TikTokPublishError) as raised,
        ):
            asyncio.run(app._upload_file(ExpiringPage(), ExpiringPage()))

        self.assertEqual(raised.exception.error_code, "tiktok_upload_entry_timeout")

    def test_upload_button_click_uses_the_remaining_deadline_budget(self) -> None:
        class Clock:
            now = 0.0

            def time(self) -> float:
                return self.now

        class EmptyLocator:
            @property
            def first(self):
                return self

            async def count(self) -> int:
                clock.now = 6.0
                return 0

        class UploadButton:
            @property
            def first(self):
                return self

            async def count(self) -> int:
                return 1

            async def click(self, *, timeout=None) -> None:
                self.click_timeout = timeout
                raise TikTokPublishError("test_stop", "stop after recording timeout")

        class ChooserContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *_args):
                return False

        class UploadButtonPage:
            url = tiktok_uploader.UPLOAD_URL

            def __init__(self) -> None:
                self.button = UploadButton()
                self.chooser_timeout = None

            def locator(self, _selector: str):
                return EmptyLocator()

            def get_by_role(self, *_args, **_kwargs):
                return self.button

            def expect_file_chooser(self, *, timeout):
                self.chooser_timeout = timeout
                return ChooserContext()

        clock = Clock()
        page = UploadButtonPage()
        app = TiktokVideo(
            "Title",
            "/tmp/missing-upload.mp4",
            [],
            0,
            "/not/used.json",
            execution_mode="platform_form_check",
        )
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        with (
            patch.object(tiktok_uploader, "UPLOAD_ENTRY_TIMEOUT_SECONDS", 10),
            patch.object(tiktok_uploader.asyncio, "get_running_loop", return_value=clock),
            self.assertRaises(TikTokPublishError) as raised,
        ):
            asyncio.run(app._upload_file(page, page))

        self.assertEqual(raised.exception.error_code, "test_stop")
        self.assertEqual(page.chooser_timeout, 4_000)
        self.assertEqual(page.button.click_timeout, 4_000)

    def test_caption_editor_waits_for_uploaded_video_form_to_render(self) -> None:
        class EmptyLocator:
            @property
            def first(self):
                return self

            async def count(self) -> int:
                return 0

        class DelayedCaptionPage:
            def __init__(self) -> None:
                self.url = tiktok_uploader.UPLOAD_URL
                self.waits = 0
                self.editor = FakeTikTokLeaf(editable=True)

            def locator(self, selector: str):
                if selector == 'iframe[data-tt="Upload_index_iframe"]':
                    return EmptyLocator()
                if (
                    selector == tiktok_uploader.CAPTION_EDITOR_SELECTORS[0]
                    and self.waits >= 2
                ):
                    return FakeTikTokCollection([self.editor])
                return EmptyLocator()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                self.waits += 1

        page = DelayedCaptionPage()
        app = TiktokVideo(
            "Title",
            "/tmp/slow-form.mp4",
            [],
            0,
            "/not/used.json",
            execution_mode="platform_form_check",
        )
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        base, editor = asyncio.run(app._wait_for_caption_editor(page, page))

        self.assertIs(base, page)
        self.assertIs(editor, page.editor)
        self.assertGreaterEqual(page.waits, 2)

    @staticmethod
    def add_candidates(
        page: FakeTikTokPage,
        topic: str,
        *labels: str,
        entity_values: list[str] | None = None,
        mutate_text: str | None = None,
    ) -> None:
        values = [topic] if entity_values is None else entity_values
        page.candidates[topic] = [
            FakeTikTokCandidate(
                page,
                label,
                entity_values=list(values),
                mutate_text=mutate_text,
            )
            for label in labels
        ]

    def test_prepare_form_keeps_plain_caption_first_and_topics_as_entities(self) -> None:
        page = FakeTikTokPage()
        self.add_candidates(page, "AI", "#AI")
        self.add_candidates(page, "效率", "#效率")
        app = self.uploader()
        stages: list[str] = []
        app.form_stage_observer = stages.append

        with (
            patch.object(tiktok_uploader, "_body_text", new=AsyncMock(return_value="")),
            patch.object(
                tiktok_uploader,
                "reveal_page_window",
                new=AsyncMock(return_value=None),
            ) as reveal,
        ):
            receipt = asyncio.run(app.prepare_form(page, page.base))

        app._upload_file.assert_awaited_once_with(page, page.base)
        reveal.assert_not_awaited()
        self.assertEqual(page.editor.normalized_text_before_topics, "Title\n\nBody")
        self.assertEqual(receipt["topicEntities"], ["AI", "效率"])
        self.assertEqual(receipt["plainCaption"], "Title\n\nBody")
        self.assertEqual(receipt["visibility"], "public")
        self.assertEqual(
            stages,
            [
                "upload_entry_waiting",
                "video_selected",
                "caption_editor_waiting",
                "caption_editor_ready",
                "caption_write_started",
                "caption_write_verified",
                "topics_started",
                "topics_verified",
                "visibility_started",
                "visibility_verified",
                "post_ready_waiting",
                "form_snapshot_started",
                "form_snapshot_verified",
            ],
        )

    def test_caption_writing_focuses_editor_without_pointer_click(self) -> None:
        page = FakeTikTokPage()
        page.editor.click = AsyncMock(side_effect=TimeoutError("covered"))
        page.editor.focus = AsyncMock(
            side_effect=lambda: setattr(page, "active_editor", page.editor)
        )
        app = self.uploader(tags=[])

        with patch.object(
            tiktok_uploader,
            "_body_text",
            new=AsyncMock(return_value=""),
        ):
            receipt = asyncio.run(app.prepare_form(page, page.base))

        page.editor.focus.assert_awaited()
        page.editor.click.assert_not_awaited()
        self.assertEqual(receipt["plainCaption"], "Title\n\nBody")

    def test_post_ready_wait_uses_the_same_chinese_button_contract(self) -> None:
        button = FakeTikTokLeaf("发布")

        class ChinesePostBase:
            def locator(self, selector: str):
                if selector == 'button:has-text("发布")':
                    return FakeTikTokCollection([button])
                return FakeTikTokCollection([])

        page = FakeTikTokPage()
        app = TiktokVideo(
            "Title",
            "/tmp/chinese-post.mp4",
            [],
            0,
            "/not/used.json",
            execution_mode="platform_form_check",
        )
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        with patch.object(
            tiktok_uploader.asyncio,
            "sleep",
            new=AsyncMock(return_value=None),
        ):
            asyncio.run(app._wait_until_ready(page, ChinesePostBase()))

        app._wait_for_manual_intervention.assert_awaited_once_with(page)

    def test_normal_upload_preparation_never_reveals_browser(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"offline")
            page = FakeTikTokPage()
            app = TiktokVideo(
                "Title",
                str(video),
                [],
                0,
                "/not/used.json",
                description="Body",
                expected_account_reference="expected.user",
                execution_mode="platform_form_check",
            )
            app.external_browser = object()
            app.external_context = object()
            app.external_page = page
            app._base = AsyncMock(return_value=page.base)
            app._wait_for_manual_intervention = AsyncMock(return_value=None)
            app.prepare_form = AsyncMock(
                return_value={
                    "plainCaption": "Title\n\nBody",
                    "topicEntities": [],
                    "visibility": "public",
                }
            )
            with (
                patch.object(
                    tiktok_uploader,
                    "_body_text",
                    new=AsyncMock(return_value=""),
                ),
                patch.object(
                    tiktok_uploader,
                    "reveal_page_window",
                    new=AsyncMock(return_value=None),
                ) as reveal,
                patch.object(
                    tiktok_uploader,
                    "save_context_storage_state",
                    new=AsyncMock(return_value=None),
                ),
            ):
                asyncio.run(app.upload(None))

        reveal.assert_not_awaited()
        app.prepare_form.assert_awaited_once_with(page, page.base)

    def test_plain_hashtag_text_never_satisfies_topic_entity_readback(self) -> None:
        page = FakeTikTokPage(editor_text="Title\n\nBody #AI")
        page.candidates["AI"] = [
            FakeTikTokCandidate(page, "#AI", entity_values=None)
        ]
        app = self.uploader(tags=["AI"])

        with patch.object(
            tiktok_uploader,
            "_body_text",
            new=AsyncMock(return_value=""),
        ):
            with self.assertRaises(TikTokPublishError) as raised:
                asyncio.run(app.prepare_form(page, page.base))

        self.assertEqual(raised.exception.error_code, "tiktok_topic_entity_missing")

    def test_topic_entity_readback_waits_for_delayed_stable_react_nodes(self) -> None:
        page = FakeTikTokPage()
        page.candidates["AI"] = [
            FakeTikTokCandidate(page, "#AI", entity_values=None)
        ]
        page.editor.entity_snapshots = [[], ["AI"], ["AI"], ["AI"], ["AI"]]
        app = self.uploader(tags=["AI"])

        with patch.object(
            tiktok_uploader,
            "_body_text",
            new=AsyncMock(return_value=""),
        ):
            receipt = asyncio.run(app.prepare_form(page, page.base))

        self.assertEqual(receipt["topicEntities"], ["AI"])

    def test_candidate_wait_handles_verification_on_same_page_without_reupload(self) -> None:
        page = FakeTikTokPage()
        selected = False
        challenge_emitted = False
        body_reads = 0

        def select_topic() -> None:
            nonlocal selected
            selected = True
            page.editor.entity_nodes.append("AI")

        async def body_text(current_page) -> str:
            nonlocal body_reads, challenge_emitted
            self.assertIs(current_page, page)
            body_reads += 1
            if body_reads > 1 and not selected and not challenge_emitted:
                challenge_emitted = True
                return "captcha"
            return ""

        page.candidates["AI"] = [
            FakeTikTokLeaf("#AI", on_click=select_topic)
        ]
        app = self.uploader(tags=["AI"])

        with (
            patch.object(tiktok_uploader, "_body_text", new=body_text),
            patch.object(
                tiktok_uploader,
                "reveal_page_window",
                new=AsyncMock(return_value=None),
            ) as reveal,
            patch.object(
                tiktok_uploader.asyncio,
                "sleep",
                new=AsyncMock(return_value=None),
            ),
        ):
            receipt = asyncio.run(app.prepare_form(page, page.base))

        app._upload_file.assert_awaited_once_with(page, page.base)
        reveal.assert_awaited_once_with(page)
        self.assertTrue(challenge_emitted)
        self.assertEqual(receipt["topicEntities"], ["AI"])

    def test_topic_requires_one_exact_official_candidate(self) -> None:
        for labels, expected_code in (
            ([], "tiktok_topic_candidate_missing"),
            (["#AI", "#AI"], "tiktok_topic_candidate_ambiguous"),
        ):
            with self.subTest(labels=labels):
                page = FakeTikTokPage()
                self.add_candidates(page, "AI", *labels)
                app = self.uploader(tags=["AI"])
                with patch.object(
                    tiktok_uploader,
                    "_body_text",
                    new=AsyncMock(return_value=""),
                ):
                    with self.assertRaises(TikTokPublishError) as raised:
                        asyncio.run(app.prepare_form(page, page.base))
                self.assertEqual(raised.exception.error_code, expected_code)

    def test_topic_candidate_label_ignores_platform_work_count_suffix(self) -> None:
        page = FakeTikTokPage()
        self.add_candidates(page, "AI", "#AI 37.8M 个作品")
        app = self.uploader(tags=["AI"])

        with patch.object(
            tiktok_uploader,
            "_body_text",
            new=AsyncMock(return_value=""),
        ):
            receipt = asyncio.run(app.prepare_form(page, page.base))

        self.assertEqual(receipt["topicEntities"], ["AI"])

    def test_topic_candidate_supports_unscoped_role_option_fallback(self) -> None:
        candidate = FakeTikTokLeaf("#AI 37.8M 个作品")

        class CurrentTikTokCandidatePage(FakeTikTokPage):
            def locator(self, selector: str):
                if selector == '[role="option"]':
                    return FakeTikTokCollection([candidate])
                return super().locator(selector)

        class CurrentTikTokCandidateBase:
            def locator(self, selector: str):
                return FakeTikTokCollection([])

        page = CurrentTikTokCandidatePage()
        app = self.uploader(tags=["AI"])
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        candidates, labels = asyncio.run(
            app._stable_topic_candidates(
                page,
                CurrentTikTokCandidateBase(),
                topic="AI",
            )
        )

        self.assertEqual(candidates, [candidate])
        self.assertEqual(labels, ["AI"])

    def test_topic_candidate_wait_tolerates_slow_network_beyond_two_seconds(self) -> None:
        candidate = FakeTikTokLeaf("#AI 37.8M 个作品")

        class SlowCandidatePage:
            url = tiktok_uploader.UPLOAD_URL

            def __init__(self) -> None:
                self.waits = 0

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                self.waits += 1

        class SlowCandidateBase:
            def __init__(self, page) -> None:
                self.page = page

            def locator(self, selector: str):
                if (
                    selector == tiktok_uploader.TOPIC_CANDIDATE_SELECTORS[0]
                    and self.page.waits >= 25
                ):
                    return FakeTikTokCollection([candidate])
                return FakeTikTokCollection([])

        page = SlowCandidatePage()
        app = self.uploader(tags=["AI"])
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        candidates, labels = asyncio.run(
            app._stable_topic_candidates(page, SlowCandidateBase(page))
        )

        self.assertEqual(candidates, [candidate])
        self.assertEqual(labels, ["AI"])
        self.assertGreaterEqual(page.waits, 25)

    def test_target_topic_can_stabilize_while_unrelated_suggestions_change(self) -> None:
        class DynamicCandidatePage:
            url = tiktok_uploader.UPLOAD_URL

            def __init__(self) -> None:
                self.waits = 0

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                self.waits += 1

        class DynamicCandidateBase:
            def __init__(self, page) -> None:
                self.page = page

            def locator(self, selector: str):
                if selector != tiktok_uploader.TOPIC_CANDIDATE_SELECTORS[0]:
                    return FakeTikTokCollection([])
                return FakeTikTokCollection(
                    [
                        FakeTikTokLeaf("#AI 37.8M 个作品"),
                        FakeTikTokLeaf(f"#trend{self.page.waits} 1K 个作品"),
                    ]
                )

        page = DynamicCandidatePage()
        app = self.uploader(tags=["AI"])
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        candidates, labels = asyncio.run(
            app._stable_topic_candidates(
                page,
                DynamicCandidateBase(page),
                topic="AI",
            )
        )

        self.assertEqual(labels, ["AI"])
        self.assertEqual(len(candidates), 1)

    def test_exact_target_candidate_does_not_require_full_dom_list_stability(self) -> None:
        focused = FakeTikTokLeaf("#AI 37.8M 个作品")
        duplicate = FakeTikTokLeaf("#AI 37.8M 个作品")

        class RepaintingPage:
            url = tiktok_uploader.UPLOAD_URL

            def __init__(self) -> None:
                self.waits = 0

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                self.waits += 1

        class RepaintingBase:
            def __init__(self, page) -> None:
                self.page = page

            def locator(self, selector: str):
                if selector != tiktok_uploader.TOPIC_CANDIDATE_SELECTORS[0]:
                    return FakeTikTokCollection([])
                items = [focused]
                if self.page.waits % 2:
                    items.append(duplicate)
                return FakeTikTokCollection(items)

        page = RepaintingPage()
        app = self.uploader(tags=["AI"])
        app._wait_for_manual_intervention = AsyncMock(return_value=None)

        candidates, labels = asyncio.run(
            app._stable_topic_candidates(
                page,
                RepaintingBase(page),
                topic="AI",
            )
        )

        self.assertEqual(candidates, [focused])
        self.assertEqual(labels, ["AI"])
        self.assertEqual(page.waits, 0)

    def test_duplicate_topic_dom_nodes_require_one_focused_exact_candidate(self) -> None:
        page = FakeTikTokPage()
        focused = FakeTikTokCandidate(
            page,
            "#AI 37.8M 个作品",
            entity_values=["AI"],
            focused=True,
        )
        duplicate = FakeTikTokCandidate(
            page,
            "#AI 37.8M 个作品",
            entity_values=["AI"],
        )
        page.candidates["AI"] = [focused, duplicate]
        app = self.uploader(tags=["AI"])

        with patch.object(
            tiktok_uploader,
            "_body_text",
            new=AsyncMock(return_value=""),
        ):
            receipt = asyncio.run(app.prepare_form(page, page.base))

        self.assertEqual(receipt["topicEntities"], ["AI"])
        self.assertTrue(focused.clicked)
        self.assertFalse(duplicate.clicked)

    def test_current_draft_editor_mention_node_is_a_topic_entity(self) -> None:
        class CurrentDraftEditor:
            def locator(self, selector: str) -> FakeTikTokCollection:
                if selector == "span.mention":
                    return FakeTikTokCollection([FakeTikTokLeaf("#AI")])
                return FakeTikTokCollection([])

        app = self.uploader(tags=["AI"])

        entities = asyncio.run(app._read_topic_entities(CurrentDraftEditor()))

        self.assertEqual(entities, ["AI"])

    def test_duplicate_or_wrong_order_topic_entities_are_rejected(self) -> None:
        for existing, selected in (([], ["AI", "AI"]), (["其他"], ["AI"])):
            with self.subTest(existing=existing, selected=selected):
                page = FakeTikTokPage()
                page.editor.entity_nodes = list(existing)
                self.add_candidates(
                    page,
                    "AI",
                    "#AI",
                    entity_values=selected,
                )
                app = self.uploader(tags=["AI"])
                with patch.object(
                    tiktok_uploader,
                    "_body_text",
                    new=AsyncMock(return_value=""),
                ):
                    with self.assertRaises(TikTokPublishError) as raised:
                        asyncio.run(app.prepare_form(page, page.base))
                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_topic_entity_mismatch",
                )

    def test_caption_editor_must_be_unique(self) -> None:
        for count, expected_code in (
            (0, "tiktok_caption_editor_timeout"),
            (2, "tiktok_caption_editor_ambiguous"),
        ):
            with self.subTest(count=count):
                page = FakeTikTokPage(editor_count=count)
                app = self.uploader(tags=[])
                with patch.object(
                    tiktok_uploader,
                    "_body_text",
                    new=AsyncMock(return_value=""),
                ):
                    with self.assertRaises(TikTokPublishError) as raised:
                        asyncio.run(app.prepare_form(page, page.base))
                self.assertEqual(raised.exception.error_code, expected_code)

    def test_first_matching_editor_selector_cannot_fall_through_when_not_editable(self) -> None:
        page = FakeTikTokPage()
        not_editable = FakeTikTokLeaf(visible=True, editable=False)
        later_editable = FakeTikTokEditor(page, "")

        class SelectorBase:
            def locator(self, selector: str) -> FakeTikTokCollection:
                if selector == tiktok_uploader.CAPTION_EDITOR_SELECTORS[0]:
                    return FakeTikTokCollection([not_editable])
                if selector == tiktok_uploader.CAPTION_EDITOR_SELECTORS[1]:
                    return FakeTikTokCollection([later_editable])
                return FakeTikTokCollection([])

        app = self.uploader(tags=[])
        with self.assertRaises(TikTokPublishError) as raised:
            asyncio.run(app._resolve_caption_editor(SelectorBase()))

        self.assertEqual(raised.exception.error_code, "tiktok_caption_editor_invalid")

    def test_matching_editor_selector_check_exception_is_stably_rejected(self) -> None:
        class BrokenCollection:
            async def count(self) -> int:
                return 1

            def nth(self, index: int):
                del index
                raise RuntimeError("detached DOM")

        class BrokenBase:
            def locator(self, selector: str):
                if selector == tiktok_uploader.CAPTION_EDITOR_SELECTORS[0]:
                    return BrokenCollection()
                return FakeTikTokCollection([])

        app = self.uploader(tags=[])
        with self.assertRaises(TikTokPublishError) as raised:
            asyncio.run(app._resolve_caption_editor(BrokenBase()))

        self.assertEqual(raised.exception.error_code, "tiktok_caption_editor_invalid")

    def test_caption_clear_must_read_back_empty(self) -> None:
        page = FakeTikTokPage(clear_blocked=True)
        app = self.uploader(tags=[])

        with patch.object(
            tiktok_uploader,
            "_body_text",
            new=AsyncMock(return_value=""),
        ):
            with self.assertRaises(TikTokPublishError) as raised:
                asyncio.run(app.prepare_form(page, page.base))

        self.assertEqual(raised.exception.error_code, "tiktok_caption_clear_failed")

    def test_final_caption_must_match_canonical_composer(self) -> None:
        page = FakeTikTokPage()
        self.add_candidates(
            page,
            "AI",
            "#AI",
            mutate_text="Title\n\nBody #AI extra",
        )
        app = self.uploader(tags=["AI"])

        with patch.object(
            tiktok_uploader,
            "_body_text",
            new=AsyncMock(return_value=""),
        ):
            with self.assertRaises(TikTokPublishError) as raised:
                asyncio.run(app.prepare_form(page, page.base))

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_caption_final_mismatch",
        )

    def test_security_intervention_reveals_same_page_and_does_not_reupload(self) -> None:
        page = FakeTikTokPage()
        self.add_candidates(page, "AI", "#AI")
        app = self.uploader(tags=["AI"])
        seen_pages: list[object] = []

        async def fill(current_page, base, plain_caption):
            seen_pages.append(current_page)
            return await TiktokVideo._fill_plain_caption(
                app,
                current_page,
                base,
                plain_caption,
            )

        app._fill_plain_caption = fill
        with (
            patch.object(
                tiktok_uploader,
                "_body_text",
                new=AsyncMock(side_effect=["captcha", ""] + [""] * 50),
            ),
            patch.object(
                tiktok_uploader,
                "reveal_page_window",
                new=AsyncMock(return_value=None),
            ) as reveal,
            patch.object(
                tiktok_uploader.asyncio,
                "sleep",
                new=AsyncMock(return_value=None),
            ),
        ):
            asyncio.run(app.prepare_form(page, page.base))

        app._upload_file.assert_awaited_once_with(page, page.base)
        reveal.assert_awaited_once_with(page)
        self.assertEqual(seen_pages, [page])

    def test_ready_wait_handles_verification_then_resumes_same_stage(self) -> None:
        page = FakeTikTokPage()

        class ReadyButton:
            @property
            def first(self):
                return self

            async def count(self) -> int:
                return 1

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

        class ReadyBase:
            def locator(self, selector: str) -> ReadyButton:
                del selector
                return ReadyButton()

        app = self.uploader(tags=[])
        with (
            patch.object(
                tiktok_uploader,
                "_body_text",
                new=AsyncMock(side_effect=["captcha", ""]),
            ),
            patch.object(
                tiktok_uploader,
                "reveal_page_window",
                new=AsyncMock(return_value=None),
            ) as reveal,
            patch.object(
                tiktok_uploader.asyncio,
                "sleep",
                new=AsyncMock(return_value=None),
            ),
        ):
            asyncio.run(TiktokVideo._wait_until_ready(app, page, ReadyBase()))

        reveal.assert_awaited_once_with(page)

    def test_submit_once_clicks_once_without_uploading_again(self) -> None:
        app = self.uploader(tags=[], execution_mode="formal")
        app.publish_confirmed = True
        button = AsyncMock()
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        app._verify_form_snapshot = AsyncMock(
            return_value={
                "plainCaption": "Title\n\nBody",
                "topicEntities": [],
                "visibility": "public",
            }
        )
        app._post_button = AsyncMock(return_value=button)
        app._wait_for_publish_result = AsyncMock(
            return_value="platform_feedback:video posted successfully"
        )

        result = asyncio.run(app.submit_once(AsyncMock(), AsyncMock()))

        app._upload_file.assert_not_awaited()
        button.click.assert_awaited_once()
        self.assertEqual(result["status"], "published")

    def test_submit_once_is_consumed_after_click_or_result_failure(self) -> None:
        for failure_stage in ("click", "result"):
            with self.subTest(failure_stage=failure_stage):
                app = self.uploader(tags=[], execution_mode="formal")
                app.publish_confirmed = True
                button = AsyncMock()
                app._wait_for_manual_intervention = AsyncMock(return_value=None)
                app._verify_form_snapshot = AsyncMock(
                    return_value={
                        "plainCaption": "Title\n\nBody",
                        "topicEntities": [],
                        "visibility": "public",
                    }
                )
                app._post_button = AsyncMock(return_value=button)
                app._wait_for_publish_result = AsyncMock(
                    return_value="platform_feedback:video posted successfully"
                )
                if failure_stage == "click":
                    button.click.side_effect = RuntimeError("click failed")
                else:
                    app._wait_for_publish_result.side_effect = RuntimeError(
                        "result failed"
                    )

                with self.assertRaises(RuntimeError):
                    asyncio.run(app.submit_once(AsyncMock(), AsyncMock()))
                click_count = button.click.await_count

                with self.assertRaises(TikTokPublishError) as raised:
                    asyncio.run(app.submit_once(AsyncMock(), AsyncMock()))

                self.assertEqual(
                    raised.exception.error_code,
                    "tiktok_final_action_already_consumed",
                )
                self.assertEqual(button.click.await_count, click_count)


class OverseasVideoPublishSignalTests(unittest.TestCase):
    def test_tiktok_success_and_security_signals_are_specific(self) -> None:
        self.assertIsNotNone(
            tiktok_publish_success_signal(
                url="https://www.tiktok.com/tiktokstudio/upload",
                feedback_text="Video posted successfully",
            )
        )
        self.assertIsNone(
            tiktok_publish_success_signal(
                url="https://www.tiktok.com/tiktokstudio/upload",
                feedback_text="Posting...",
            )
        )
        self.assertIsNotNone(
            tiktok_security_intervention_reason(
                "https://www.tiktok.com/challenge/verify",
                "",
            )
        )

    def test_youtube_success_and_security_signals_are_specific(self) -> None:
        self.assertIsNotNone(
            youtube_publish_success_signal(
                url="https://studio.youtube.com/channel/test/videos/upload",
                feedback_text="Video saved",
                upload_dialog_visible=False,
                final_button_visible=False,
                visibility="public",
            )
        )
        self.assertIsNone(
            youtube_publish_success_signal(
                url="https://studio.youtube.com/channel/test/videos/upload",
                feedback_text="",
                upload_dialog_visible=False,
                final_button_visible=False,
                visibility="public",
            )
        )
        self.assertIsNone(
            youtube_publish_success_signal(
                url="https://studio.youtube.com/channel/test/videos/upload",
                feedback_text="Saving...",
                upload_dialog_visible=True,
                final_button_visible=True,
                visibility="public",
            )
        )
        self.assertIsNotNone(
            youtube_security_intervention_reason(
                "https://accounts.google.com/signin/v2/challenge/pwd",
                "",
            )
        )
        self.assertNotIn(
            "登录状态已失效",
            youtube_security_intervention_reason(
                "https://accounts.google.com/signin/v2/challenge/pwd",
                "",
            ),
        )
        rejected = youtube_security_intervention_reason(
            "https://accounts.google.com/v3/signin/rejected",
            "此浏览器或应用可能不安全。请尝试使用其他浏览器。",
        )
        self.assertIn("拒绝当前浏览器", rejected)

    def test_youtube_content_list_receipt_requires_new_matching_video(self) -> None:
        rows = [
            {"videoId": "old-id", "title": "目标视频", "visibility": "private"},
            {"videoId": "new-id", "title": "目标视频", "visibility": "private"},
        ]
        self.assertEqual(
            youtube_content_list_receipt(
                rows,
                title="目标视频",
                visibility="private",
                preexisting_video_ids={"old-id"},
            ),
            "studio_content_list:new-id:private",
        )
        self.assertIsNone(
            youtube_content_list_receipt(
                rows,
                title="目标视频",
                visibility="public",
                preexisting_video_ids={"old-id"},
            )
        )
        self.assertIsNone(
            youtube_content_list_receipt(
                rows,
                title="目标视频",
                visibility="private",
                preexisting_video_ids=None,
            )
        )
        signal = youtube_publish_success_signal(
            url="https://studio.youtube.com/channel/test/videos/upload",
            feedback_text="",
            upload_dialog_visible=False,
            final_button_visible=False,
            visibility="private",
            content_list_receipt="studio_content_list:new-id:private",
        )
        self.assertEqual(signal, "studio_content_list:new-id:private")

    def test_incomplete_content_baseline_is_unknown_instead_of_a_partial_id_set(self) -> None:
        app = YouTubeVideo("目标视频", "/not/used.mp4", [], "/not/used.json")
        page = FakeContentPage(
            [
                FakeContentRow(
                    video_id="old-id",
                    title="目标视频",
                    visibility="private",
                ),
                FakeContentRow(
                    video_id="unreadable-id",
                    title="其他视频",
                    visibility=None,
                ),
            ]
        )

        baseline = asyncio.run(app._content_list_video_ids(page))

        self.assertIsNone(baseline)

    def test_content_baseline_accepts_only_an_explicit_empty_state(self) -> None:
        app = YouTubeVideo("目标视频", "/not/used.mp4", [], "/not/used.json")

        explicit_empty = asyncio.run(
            app._content_list_video_ids(
                FakeContentPage([], empty_state_visible=True)
            )
        )
        unrecognized_empty = asyncio.run(
            app._content_list_video_ids(FakeContentPage([]))
        )

        self.assertEqual(explicit_empty, set())
        self.assertIsNone(unrecognized_empty)

    def test_incomplete_after_save_snapshot_cannot_generate_content_success(self) -> None:
        app = YouTubeVideo("目标视频", "/not/used.mp4", [], "/not/used.json")
        app.preexisting_video_ids = {"old-id"}
        page = FakeContentPage(
            [
                FakeContentRow(
                    video_id="new-id",
                    title="目标视频",
                    visibility="private",
                ),
                FakeContentRow(
                    video_id="unreadable-id",
                    title=None,
                    visibility="private",
                ),
            ]
        )

        receipt = asyncio.run(app._content_list_receipt(page))

        self.assertIsNone(receipt)

    def test_complete_before_and_after_snapshots_accept_only_new_matching_id(self) -> None:
        app = YouTubeVideo("目标视频", "/not/used.mp4", [], "/not/used.json")
        before = FakeContentPage(
            [
                FakeContentRow(
                    video_id="old-id",
                    title="目标视频",
                    visibility="private",
                )
            ]
        )
        app.preexisting_video_ids = asyncio.run(app._content_list_video_ids(before))
        after = FakeContentPage(
            [
                FakeContentRow(
                    video_id="old-id",
                    title="目标视频",
                    visibility="private",
                ),
                FakeContentRow(
                    video_id="new-id",
                    title="目标视频",
                    visibility="private",
                ),
            ]
        )

        receipt = asyncio.run(app._content_list_receipt(after))

        self.assertEqual(receipt, "studio_content_list:new-id:private")

    def test_content_baseline_is_captured_before_upload_route_and_file_selection(self) -> None:
        app = YouTubeVideo("目标视频", "/offline/video.mp4", [], "/not/used.json")
        self.assertTrue(
            hasattr(app, "_prepare_upload_mutation"),
            "Content baseline preparation boundary is not implemented",
        )
        events: list[object] = []

        class FileInput:
            @property
            def first(self):
                return self

            async def wait_for(self, *, state: str, timeout: int) -> None:
                events.append(("file_ready", state, timeout))

            async def set_input_files(self, path: str) -> None:
                events.append(("file_selected", path))

        class Page:
            url = "about:blank"

            async def goto(self, url: str, **kwargs: object) -> None:
                events.append(("goto", url, kwargs))
                self.url = url

            async def wait_for_timeout(self, milliseconds: int) -> None:
                events.append(("wait", milliseconds))

            def locator(self, selector: str):
                self.selector = selector
                return FileInput()

        async def capture(_page) -> set[str]:
            events.append("baseline_complete")
            return {"old-id"}

        app._content_list_video_ids = capture
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        with patch.object(
            youtube_uploader,
            "reveal_page_window",
            new=AsyncMock(return_value=None),
        ):
            asyncio.run(app._prepare_upload_mutation(Page()))

        baseline_index = events.index("baseline_complete")
        upload_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, tuple)
            and event[:2] == ("goto", youtube_uploader.UPLOAD_URL)
        )
        file_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, tuple) and event[0] == "file_selected"
        )
        self.assertLess(baseline_index, upload_index)
        self.assertLess(upload_index, file_index)

    def test_youtube_preflight_receipt_requires_every_requested_field(self) -> None:
        required = {"video", "title", "description", "tags", "audience", "visibility"}
        receipt = youtube_preflight_receipt(
            verified_fields=required,
            required_fields=required,
            visibility="private",
        )
        self.assertEqual(receipt["status"], "preflight_ready")
        self.assertEqual(receipt["platformMutation"], "private_upload")
        self.assertEqual(set(receipt["verifiedFields"]), required)
        with self.assertRaisesRegex(RuntimeError, "逐字段回读"):
            youtube_preflight_receipt(
                verified_fields=required - {"tags"},
                required_fields=required,
                visibility="private",
            )

    def test_youtube_security_challenge_stops_without_auto_resume(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.url_reads = 0

            @property
            def url(self) -> str:
                self.url_reads += 1
                if self.url_reads == 1:
                    return "https://accounts.google.com/signin/v2/challenge/pwd"
                return "https://studio.youtube.com/channel/test/videos/upload"

        app = YouTubeVideo("测试", "/not/used.mp4", [], "/not/used.json")
        sleep = AsyncMock(return_value=None)
        with (
            patch.object(youtube_uploader, "_body_text", new=AsyncMock(return_value="")),
            patch.object(
                youtube_uploader,
                "reveal_page_window",
                new=AsyncMock(return_value=None),
            ),
            patch.object(youtube_uploader.asyncio, "sleep", new=sleep),
        ):
            with self.assertRaises(
                youtube_uploader.YouTubeManualInterventionRequired
            ):
                asyncio.run(app._wait_for_manual_intervention(Page()))
        sleep.assert_not_awaited()

    def test_tiktok_final_button_is_clicked_once_then_verified(self) -> None:
        app = TiktokVideo(
            "测试",
            "/not/used.mp4",
            [],
            0,
            "/not/used.json",
            execution_mode="formal",
        )
        app.publish_confirmed = True
        button = AsyncMock()
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        app._verify_form_snapshot = AsyncMock(
            return_value={
                "plainCaption": "测试\n\n",
                "topicEntities": [],
                "visibility": "public",
            }
        )
        app._post_button = AsyncMock(return_value=button)
        app._wait_for_publish_result = AsyncMock(
            return_value="platform_feedback:video posted successfully"
        )
        result = asyncio.run(app._publish_formally(AsyncMock(), AsyncMock()))
        button.click.assert_awaited_once()
        self.assertEqual(result["status"], "published")

    def test_youtube_final_button_is_clicked_once_then_verified(self) -> None:
        app = YouTubeVideo("测试", "/not/used.mp4", [], "/not/used.json")
        app.publish_confirmed = True
        button = AsyncMock()
        app._wait_for_manual_intervention = AsyncMock(return_value=None)
        app._final_button = AsyncMock(return_value=button)
        app._wait_for_publish_result = AsyncMock(
            return_value="studio_upload_dialog_closed:private"
        )
        result = asyncio.run(app._publish_formally(AsyncMock()))
        button.click.assert_awaited_once()
        self.assertEqual(result["status"], "saved")


if __name__ == "__main__":
    unittest.main()
