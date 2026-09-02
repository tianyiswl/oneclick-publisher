# -*- coding: utf-8 -*-
"""Facebook Page Reel 表单的离线精确回读合同。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app_core import overseas_meta_content
from app_core.overseas_meta_errors import FacebookPagePublishError
from app_core.overseas_meta_page_identity import FacebookPageIdentity
from uploader.meta_uploader import content_list as meta_content_list
from uploader.meta_uploader.main import MetaManualInterventionRequired
from uploader.meta_uploader.page_form import (
    FacebookPageFormAdapter,
    FacebookPageFormExpectation,
    canonical_meta_caption,
)
from uploader.meta_uploader.content_list import (
    FacebookPageContentBaseline,
    FacebookPageContentReader,
    FacebookPlatformDecision,
    FacebookReelMatch,
    FacebookReelReceipt,
    FacebookReelRow,
    _current_table_timestamp,
    match_unique_new_facebook_reel,
)


class _FakeLocator:
    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    async def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> Any:
        return self._items[index]


class _FakePageRow:
    def __init__(
        self,
        page: "_FakeFacebookPage",
        page_id: str,
        page_name: str,
        *,
        can_manage_content: bool = True,
    ) -> None:
        self._page = page
        self.page_id = page_id
        self.page_name = page_name
        self.can_manage_content = can_manage_content

    async def get_attribute(self, name: str) -> str | None:
        return {
            "data-page-id": self.page_id,
            "data-page-name": self.page_name,
            "data-avatar-url": "",
            "data-can-manage-content": (
                "true" if self.can_manage_content else "false"
            ),
        }.get(name)

    async def inner_text(self) -> str:
        return self.page_name

    async def click(self) -> None:
        self._page.activation_attempts.append(self.page_id)
        if not self._page.switch_mismatch:
            self._page.active_page_id = self.page_id


class _FakeFacebookPage:
    def __init__(
        self,
        *,
        pages: tuple[tuple[str, str], ...],
        active_page_id: str,
        switch_mismatch: bool = False,
        generic_create_buttons: list[Any] | None = None,
    ) -> None:
        self.rows = [
            _FakePageRow(self, page_id, page_name)
            for page_id, page_name in pages
        ]
        self.active_page_id = active_page_id
        self.switch_mismatch = switch_mismatch
        self.activation_attempts: list[str] = []
        self.active_read_count = 0
        self.generic_create_buttons = list(generic_create_buttons or [])

    def locator(self, selector: str) -> _FakeLocator:
        if selector == "[data-page-id]":
            return _FakeLocator(self.rows)
        if selector == '[data-page-id][data-page-active="true"]':
            self.active_read_count += 1
            return _FakeLocator(
                [row for row in self.rows if row.page_id == self.active_page_id]
            )
        prefix = '[data-page-id="'
        if selector.startswith(prefix) and selector.endswith('"]'):
            page_id = selector[len(prefix) : -2]
            return _FakeLocator(
                [row for row in self.rows if row.page_id == page_id]
            )
        return _FakeLocator([])

    def get_by_role(
        self,
        role: str,
        *,
        name: str,
        exact: bool,
    ) -> _FakeLocator:
        if role == "button" and exact and name in {
            "Create reel",
            "Create Reel",
            "创建 Reels",
            "创建快拍",
        }:
            return _FakeLocator(
                [
                    button
                    for button in self.generic_create_buttons
                    if getattr(button, "label", None) == name
                ]
            )
        return _FakeLocator([])


class _FakeButton:
    def __init__(self, *, label: str = "Publish", enabled: bool = True) -> None:
        self.label = label
        self.enabled = enabled
        self.click_count = 0

    async def inner_text(self) -> str:
        return self.label

    async def get_attribute(self, name: str) -> str | None:
        if name == "aria-label":
            return self.label
        return None

    async def is_enabled(self) -> bool:
        return self.enabled

    async def click(self) -> None:
        self.click_count += 1


class _DomElement:
    def __init__(
        self,
        page: "_PlaywrightLikeFacebookPage",
        *,
        attributes: dict[str, str] | None = None,
        text: str = "",
        action: str = "",
        enabled: bool = True,
    ) -> None:
        self.page = page
        self.attributes = dict(attributes or {})
        self.text = text
        self.action = action
        self.enabled = enabled

    async def is_visible(self) -> bool:
        return True

    async def get_attribute(self, name: str) -> str | None:
        return self.attributes.get(name)

    async def inner_text(self) -> str:
        if self.action == "editor":
            return self.page.editor_value
        return self.text

    async def text_content(self) -> str:
        return await self.inner_text()

    async def input_value(self) -> str:
        if self.action != "editor":
            raise RuntimeError("not an input")
        return self.page.editor_value

    async def fill(self, value: str) -> None:
        if self.action != "editor":
            raise RuntimeError("not an editor")
        if value:
            self.page.caption_fill_count += 1
        else:
            self.page.clear_count += 1
        self.page.editor_value = value

    async def click(self) -> None:
        if self.action == "create":
            self.page.create_click_count += 1
            self.page.composer_open = True
            if self.page.drift_after_create:
                self.page.active_page_id = "1002"
        elif self.action == "final":
            self.page.final_click_count += 1

    async def set_input_files(self, file_path: str) -> None:
        if self.action != "upload":
            raise RuntimeError("not a file input")
        self.page.set_input_files_calls += 1
        self.page.uploaded_name = Path(file_path).name

    async def is_checked(self) -> bool:
        return self.action == "public" and self.page.visibility == "public"

    async def check(self) -> None:
        if self.action != "public":
            raise RuntimeError("not a visibility control")
        self.page.public_select_count += 1
        self.page.visibility = "public"

    async def is_enabled(self) -> bool:
        return self.enabled


class _PlaywrightLikeFacebookPage:
    """Selector-level fake that exercises the production adapter helpers."""

    def __init__(
        self,
        *,
        page_bound_create: bool = True,
        generic_create: bool = False,
        drift_after_create: bool = False,
        media_empty_count: int = 1,
        expose_media_collection: bool = False,
        expose_draft_evidence: bool = True,
    ) -> None:
        self.rows = [
            _FakePageRow(self, "1001", "One"),
            _FakePageRow(self, "1002", "Two"),
        ]
        self.active_page_id = "1001"
        self.switch_mismatch = False
        self.activation_attempts: list[str] = []
        self.active_read_count = 0
        self.page_bound_create = page_bound_create
        self.generic_create = generic_create
        self.drift_after_create = drift_after_create
        self.media_empty_count = media_empty_count
        self.expose_media_collection = expose_media_collection
        self.expose_draft_evidence = expose_draft_evidence
        self.composer_open = False
        self.editor_value = ""
        self.uploaded_name = ""
        self.visibility = "private"
        self.create_click_count = 0
        self.clear_count = 0
        self.set_input_files_calls = 0
        self.caption_fill_count = 0
        self.public_select_count = 0
        self.final_click_count = 0
        self.wait_timeout_calls = 0
        self._create = _DomElement(self, text="Create reel", action="create")
        self._editor = _DomElement(self, action="editor")
        self._upload = _DomElement(self, action="upload")
        self._public = _DomElement(self, action="public")
        self._final = _DomElement(self, text="Publish", action="final")

    def locator(self, selector: str) -> _FakeLocator:
        if selector == "[data-page-id]":
            return _FakeLocator(self.rows)
        if selector == '[data-page-id][data-page-active="true"]':
            self.active_read_count += 1
            return _FakeLocator(
                [row for row in self.rows if row.page_id == self.active_page_id]
            )
        if selector in {'[data-page-id="1001"]', '[data-page-id="1002"]'}:
            page_id = selector.removeprefix('[data-page-id="').removesuffix('"]')
            return _FakeLocator(
                [row for row in self.rows if row.page_id == page_id]
            )
        if selector == (
            '[data-page-id="1001"][data-page-active="true"] '
            "[data-meta-create-reel]"
        ):
            return _FakeLocator(
                [self._create]
                if self.page_bound_create and self.active_page_id == "1001"
                else []
            )
        if not self.composer_open:
            return _FakeLocator([])
        if selector == "[data-meta-content-kind]":
            return _FakeLocator(
                [_DomElement(self, attributes={"data-meta-content-kind": "reel"})]
            )
        if selector == '[data-meta-composer-state="fresh"]':
            return _FakeLocator(
                [_DomElement(self)] if self.expose_draft_evidence else []
            )
        if selector == '[data-meta-media-empty="true"]':
            return _FakeLocator(
                [_DomElement(self) for _ in range(self.media_empty_count)]
                if not self.uploaded_name
                else []
            )
        if selector == "[data-meta-media-collection]":
            return _FakeLocator(
                [
                    _DomElement(
                        self,
                        attributes={
                            "data-video-count": "1" if self.uploaded_name else "0"
                        },
                    )
                ]
                if self.expose_media_collection
                else []
            )
        if selector == "[data-meta-video-preview]" and self.uploaded_name:
            return _FakeLocator(
                [
                    _DomElement(
                        self,
                        attributes={
                            "data-video-name": self.uploaded_name,
                            "data-upload-status": "completed",
                        },
                    )
                ]
            )
        if selector == "[data-meta-caption-editor]":
            return _FakeLocator([self._editor])
        if selector == 'input[type="file"][accept*="video" i]':
            return _FakeLocator([self._upload])
        if selector == '[data-meta-visibility-option="public"]':
            return _FakeLocator([self._public])
        if selector == "[data-meta-visibility-current]":
            return _FakeLocator(
                [
                    _DomElement(
                        self,
                        attributes={"data-meta-visibility-current": self.visibility},
                    )
                ]
            )
        if selector == "[data-meta-final-action]":
            return _FakeLocator([self._final])
        return _FakeLocator([])

    def get_by_role(
        self,
        role: str,
        *,
        name: str,
        exact: bool,
    ) -> _FakeLocator:
        if (
            self.generic_create
            and role == "button"
            and exact
            and name == "Create reel"
        ):
            return _FakeLocator([self._create])
        return _FakeLocator([])

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        self.wait_timeout_calls += 1


class _HarnessAdapter(FacebookPageFormAdapter):
    def __init__(
        self,
        page: _FakeFacebookPage,
        *,
        wait_for_verification,
        content_kind: str = "reel",
        restored_draft: bool = False,
        initial_video_names: list[str] | None = None,
        video_names: list[str] | None = None,
        upload_statuses: list[str] | None = None,
        editor_before: str = "",
        editor_after: str | None = None,
        clear_after: str = "",
        visibility: str = "public",
        final_buttons: list[_FakeButton] | None = None,
        active_page_after_open: str | None = None,
        active_page_after_completed_preview: str | None = None,
    ) -> None:
        super().__init__(page, wait_for_verification=wait_for_verification)
        self.content_kind = content_kind
        self.restored_draft = restored_draft
        self.initial_video_names = list(initial_video_names or [])
        self.video_names = ["clip.mp4"] if video_names is None else list(video_names)
        self.upload_statuses = (
            ["completed"] * len(self.video_names)
            if upload_statuses is None
            else list(upload_statuses)
        )
        self.editor_value = editor_before
        self.editor_after = editor_after
        self.clear_after = clear_after
        self.visibility_value = visibility
        self.final_buttons = (
            [_FakeButton()] if final_buttons is None else list(final_buttons)
        )
        self.composer_open_count = 0
        self.clear_count = 0
        self.upload_count = 0
        self.write_count = 0
        self.public_select_count = 0
        self.active_page_after_open = active_page_after_open
        self.active_page_after_completed_preview = (
            active_page_after_completed_preview
        )

    async def _click_create_reel_entry(self, expected_page_id: str) -> None:
        self.composer_open_count += 1
        self.opened_for_page_id = expected_page_id
        if self.active_page_after_open is not None:
            self.page.active_page_id = self.active_page_after_open

    async def _read_content_kind(self) -> str:
        return self.content_kind

    async def _read_restored_draft(self) -> bool:
        return self.restored_draft

    async def _read_video_previews(self) -> list[tuple[str, str]]:
        if self.upload_count == 0:
            return [(name, "completed") for name in self.initial_video_names]
        previews = list(zip(self.video_names, self.upload_statuses, strict=False))
        if (
            self.active_page_after_completed_preview is not None
            and any(str(status).casefold() == "completed" for _, status in previews)
        ):
            self.page.active_page_id = self.active_page_after_completed_preview
        return previews

    async def _read_caption_editor(self) -> str:
        return self.editor_value

    async def _clear_caption_editor(self) -> None:
        self.clear_count += 1
        self.editor_value = self.clear_after

    async def _upload_video_once(self, file_path: str) -> None:
        self.upload_count += 1
        self.uploaded_file_path = file_path

    async def _write_caption_once(self, caption: str) -> None:
        self.write_count += 1
        self.editor_value = caption if self.editor_after is None else self.editor_after

    async def _select_public_visibility(self) -> None:
        self.public_select_count += 1

    async def _read_visibility(self) -> str:
        return self.visibility_value

    async def _final_action_buttons(self) -> list[Any]:
        return list(self.final_buttons)

    async def _sleep(self) -> None:
        return None


class FacebookPageFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_temp)
        self.video = Path(self.temp.name) / "clip.mp4"
        self.video.write_bytes(b"video")
        self.final_buttons: list[_FakeButton] = []

    async def _cleanup_temp(self) -> None:
        self.temp.cleanup()

    @staticmethod
    async def _no_verification(*_args, **_kwargs) -> None:
        return None

    def expectation(
        self,
        *,
        page_id: str = "1001",
        caption: str = "正文 #标签",
    ) -> FacebookPageFormExpectation:
        return FacebookPageFormExpectation(
            page_id=page_id,
            content_kind="reel",
            video_name=str(self.video),
            video_size=self.video.stat().st_size,
            video_sha256=hashlib.sha256(self.video.read_bytes()).hexdigest(),
            caption=caption,
            visibility="public",
        )

    def adapter(
        self,
        *,
        pages: tuple[tuple[str, str], ...] = (("1001", "Same Page"),),
        active_page_id: str = "1001",
        switch_mismatch: bool = False,
        wait_for_verification=None,
        **kwargs,
    ) -> _HarnessAdapter:
        page = _FakeFacebookPage(
            pages=pages,
            active_page_id=active_page_id,
            switch_mismatch=switch_mismatch,
        )

        async def no_verification(*_args, **_kwargs) -> None:
            return None

        adapter = _HarnessAdapter(
            page,
            wait_for_verification=wait_for_verification or no_verification,
            **kwargs,
        )
        self.final_buttons.extend(adapter.final_buttons)
        return adapter

    @property
    def final_button_click_count(self) -> int:
        return sum(button.click_count for button in self.final_buttons)

    async def assert_error_code(
        self,
        adapter: _HarnessAdapter,
        expected: FacebookPageFormExpectation,
        code: str,
    ) -> FacebookPagePublishError:
        with self.assertRaises(FacebookPagePublishError) as raised:
            await adapter.fill_and_readback(expected)
        self.assertEqual(raised.exception.error_code, code)
        return raised.exception

    async def test_fill_and_readback_requires_exact_page_video_caption_public_and_button(self):
        adapter = self.adapter(
            active_page_id="1001",
            video_names=["clip.mp4"],
            editor_before="",
            editor_after="正文 #标签",
            visibility="public",
            final_buttons=[_FakeButton(enabled=True)],
        )
        snapshot = await adapter.fill_and_readback(self.expectation(page_id="1001"))
        self.assertEqual(snapshot.page_id, "1001")
        self.assertEqual(snapshot.content_kind, "reel")
        self.assertEqual(snapshot.video_name, "clip.mp4")
        self.assertEqual(snapshot.video_count, 1)
        self.assertEqual(snapshot.caption, "正文 #标签")
        self.assertEqual(snapshot.visibility, "public")
        self.assertEqual(snapshot.final_action_label, "Publish")
        self.assertTrue(snapshot.final_action_ready)
        self.assertEqual(adapter.upload_count, 1)
        self.assertEqual(adapter.write_count, 1)
        self.assertEqual(self.final_button_click_count, 0)

    async def test_same_name_pages_select_only_the_expected_id(self) -> None:
        adapter = self.adapter(
            pages=(("1001", "Same Page"), ("1002", "Same Page")),
            active_page_id="1002",
        )
        snapshot = await adapter.fill_and_readback(self.expectation(page_id="1001"))
        self.assertEqual(snapshot.page_id, "1001")
        self.assertEqual(adapter.page.active_page_id, "1001")
        self.assertTrue(adapter.page.activation_attempts)
        self.assertEqual(set(adapter.page.activation_attempts), {"1001"})

    async def test_current_page_switch_mismatch_fails_before_composer(self) -> None:
        adapter = self.adapter(
            pages=(("1001", "One"), ("1002", "Two")),
            active_page_id="1002",
            switch_mismatch=True,
        )
        error = await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
        self.assertEqual(error.receipt.get("pageId"), "1001")
        self.assertNotIn("videoName", error.receipt)
        self.assertNotIn("videoSha256", error.receipt)
        self.assertNotIn("captionSha256", error.receipt)
        self.assertEqual(adapter.composer_open_count, 0)
        self.assertEqual(adapter.upload_count, 0)

    async def test_ambiguous_generic_create_reel_entries_are_never_used(self) -> None:
        generic = _FakeButton(label="Create reel")
        duplicate = _FakeButton(label="Create reel")
        page = _FakeFacebookPage(
            pages=(("1001", "One"),),
            active_page_id="1001",
            generic_create_buttons=[generic, duplicate],
        )
        adapter = FacebookPageFormAdapter(
            page,
            wait_for_verification=self._no_verification,
        )
        with self.assertRaises(RuntimeError):
            await adapter._click_create_reel_entry("1001")
        self.assertEqual(generic.click_count, 0)
        self.assertEqual(duplicate.click_count, 0)

    async def test_page_drift_after_open_fails_before_any_form_write(self) -> None:
        adapter = self.adapter(
            pages=(("1001", "One"), ("1002", "Two")),
            active_page_id="1001",
            active_page_after_open="1002",
        )
        error = await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
        self.assertFalse(error.receipt["platformWriteOccurred"])
        self.assertFalse(error.receipt["finalActionTriggered"])
        self.assertEqual(adapter.clear_count, 0)
        self.assertEqual(adapter.upload_count, 0)
        self.assertEqual(adapter.write_count, 0)

    async def test_page_drift_after_upload_readback_fails_before_caption(self) -> None:
        adapter = self.adapter(
            pages=(("1001", "One"), ("1002", "Two")),
            active_page_id="1001",
            active_page_after_completed_preview="1002",
        )
        error = await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
        self.assertTrue(error.receipt["platformWriteOccurred"])
        self.assertFalse(error.receipt["finalActionTriggered"])
        self.assertEqual(adapter.upload_count, 1)
        self.assertEqual(adapter.write_count, 0)

    async def test_restored_old_media_fails_without_deleting_or_overwriting(self) -> None:
        adapter = self.adapter(initial_video_names=["old.mp4"])
        await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
        self.assertEqual(adapter.initial_video_names, ["old.mp4"])
        self.assertEqual(adapter.clear_count, 0)
        self.assertEqual(adapter.upload_count, 0)
        self.assertEqual(adapter.write_count, 0)

    async def test_missing_zero_media_evidence_fails_before_any_write(self) -> None:
        page = _PlaywrightLikeFacebookPage(media_empty_count=0)
        adapter = FacebookPageFormAdapter(
            page,
            wait_for_verification=self._no_verification,
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            await adapter.fill_and_readback(self.expectation())
        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_form_readback_failed",
        )
        self.assertEqual(page.clear_count, 0)
        self.assertEqual(page.set_input_files_calls, 0)
        self.assertEqual(page.caption_fill_count, 0)

    async def test_ambiguous_zero_media_state_fails_before_any_write(self) -> None:
        page = _PlaywrightLikeFacebookPage(media_empty_count=2)
        adapter = FacebookPageFormAdapter(
            page,
            wait_for_verification=self._no_verification,
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            await adapter.fill_and_readback(self.expectation())
        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_form_readback_failed",
        )
        self.assertEqual(page.clear_count, 0)
        self.assertEqual(page.set_input_files_calls, 0)

    async def test_missing_fresh_or_restored_draft_evidence_fails_closed(self) -> None:
        page = _PlaywrightLikeFacebookPage(expose_draft_evidence=False)
        adapter = FacebookPageFormAdapter(
            page,
            wait_for_verification=self._no_verification,
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            await adapter.fill_and_readback(self.expectation())
        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_form_readback_failed",
        )
        self.assertEqual(page.clear_count, 0)
        self.assertEqual(page.set_input_files_calls, 0)

    async def test_production_dom_helpers_complete_one_safe_form_readback(self) -> None:
        page = _PlaywrightLikeFacebookPage(media_empty_count=1)
        adapter = FacebookPageFormAdapter(
            page,
            wait_for_verification=self._no_verification,
        )
        snapshot = await adapter.fill_and_readback(self.expectation())
        self.assertEqual(snapshot.page_id, "1001")
        self.assertEqual(snapshot.content_kind, "reel")
        self.assertEqual(snapshot.video_name, "clip.mp4")
        self.assertEqual(snapshot.video_count, 1)
        self.assertEqual(snapshot.caption, "正文 #标签")
        self.assertEqual(snapshot.visibility, "public")
        self.assertEqual(snapshot.final_action_label, "Publish")
        self.assertTrue(snapshot.final_action_ready)
        self.assertEqual(page.activation_attempts, ["1001"])
        self.assertGreaterEqual(page.active_read_count, 5)
        self.assertEqual(page.create_click_count, 1)
        self.assertEqual(page.clear_count, 1)
        self.assertEqual(page.set_input_files_calls, 1)
        self.assertEqual(page.caption_fill_count, 1)
        self.assertEqual(page.public_select_count, 1)
        self.assertEqual(page.final_click_count, 0)

    async def test_live_business_suite_reels_form_uses_current_controls_and_never_shares(
        self,
    ) -> None:
        """真实 Meta 形状必须走创建 Reels、文件选择器和描述框。"""

        from playwright.async_api import async_playwright

        page_id = "1001"
        identity = FacebookPageIdentity(
            page_id=page_id,
            page_name="墨白",
            can_manage_content=True,
        )

        class LiveDomAdapter(FacebookPageFormAdapter):
            async def select_expected_page(
                self,
                expected_page_id: str,
            ) -> FacebookPageIdentity:
                self.assert_expected_page_id = expected_page_id
                return identity

        async def exact_binding(page, account) -> FacebookPageIdentity:
            self.assertEqual(account["accountReference"], page_id)
            self.assertIn("/latest/reels_composer/", page.url)
            self.assertIn(f"asset_id={page_id}", page.url)
            return identity

        html = """
        <!doctype html>
        <html lang="zh-CN">
          <body>
            <button id="create" type="button" onclick="openComposer()">创建 Reels</button>
            <script>
              window.createClicks = 0;
              window.addVideoClicks = 0;
              window.shareClicks = 0;
              window.globalShareClicks = 0;
              function openComposer() {
                window.createClicks += 1;
                history.pushState({}, '', '/latest/reels_composer/?asset_id=1001');
                setTimeout(() => {
                  document.body.innerHTML = `
                    <main>
                      <button id="global-share" type="button"
                              onclick="window.globalShareClicks += 1">分享</button>
                      <section id="reel-composer">
                      <div role="combobox" aria-label="Facebook 墨白">Facebook 墨白</div>
                      <h1>创建 Reels</h1>
                      <input role="textbox" aria-label="为你的 Reels 添加标题" value="">
                      <div role="textbox" contenteditable="true"
                           aria-label="在对话框中输入内容，即可为帖子添加文字。"></div>
                      <input id="unrelated-file" type="file" accept="image/*" hidden>
                      <button id="add-video" type="button" onclick="chooseVideo()">添加视频</button>
                      <div id="share" role="button" aria-disabled="true"
                           onclick="if (this.getAttribute('aria-disabled') === 'false') window.shareClicks += 1">
                        <span>分享</span><span>​</span>
                      </div>
                      </section>
                    </main>`;
                }, 75);
              }
              function chooseVideo() {
                window.addVideoClicks += 1;
                const input = document.createElement('input');
                input.type = 'file';
                input.accept = 'video/*';
                input.hidden = true;
                input.addEventListener('change', () => {
                  const file = input.files[0];
                  const row = document.createElement('div');
                  row.className = 'media-row';
                  row.innerHTML = `<span class="filename"></span><span class="progress">100%</span>`;
                  row.querySelector('.filename').textContent = file.name;
                  document.body.appendChild(row);
                  document.querySelector('#share').setAttribute('aria-disabled', 'false');
                });
                document.body.appendChild(input);
                input.click();
              }
            </script>
          </body>
        </html>
        """

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.route(
                "https://business.facebook.com/**",
                lambda route: route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=html,
                ),
            )
            await page.goto(
                f"https://business.facebook.com/latest/home/?asset_id={page_id}"
            )
            adapter = LiveDomAdapter(
                page,
                wait_for_verification=self._no_verification,
            )
            with patch(
                "uploader.meta_uploader.page_form.validate_facebook_page_binding",
                side_effect=exact_binding,
            ):
                snapshot = await adapter.fill_and_readback(self.expectation())

            self.assertEqual(snapshot.page_id, page_id)
            self.assertEqual(snapshot.content_kind, "reel")
            self.assertEqual(snapshot.video_name, "clip.mp4")
            self.assertEqual(snapshot.video_count, 1)
            self.assertEqual(snapshot.caption, "正文 #标签")
            self.assertEqual(snapshot.visibility, "public")
            self.assertEqual(snapshot.final_action_label, "分享")
            self.assertTrue(snapshot.final_action_ready)
            self.assertEqual(await page.evaluate("window.createClicks"), 1)
            self.assertEqual(await page.evaluate("window.addVideoClicks"), 1)
            self.assertEqual(await page.evaluate("window.shareClicks"), 0)
            self.assertEqual(await page.evaluate("window.globalShareClicks"), 0)
            self.assertEqual(
                await page.get_by_role(
                    "textbox",
                    name="为你的 Reels 添加标题",
                    exact=True,
                ).input_value(),
                "",
            )
            self.assertEqual(
                await page.get_by_role(
                    "textbox",
                    name="在对话框中输入内容，即可为帖子添加文字。",
                    exact=True,
                ).inner_text(),
                "正文 #标签",
            )
            await browser.close()

    async def test_post_click_confirmation_is_observed_without_second_click(
        self,
    ) -> None:
        from playwright.async_api import async_playwright

        html = """
        <!doctype html><html><body>
          <main>
            <h1>创建 Reels</h1>
            <div role="textbox" contenteditable="true"
                 aria-label="在对话框中输入内容，即可为帖子添加文字。">正文 #标签</div>
            <button id="share" type="button" onclick="openConfirm()">分享</button>
          </main>
          <script>
            window.finalClicks = 0;
            window.confirmClicks = 0;
            function openConfirm() {
              window.finalClicks += 1;
              document.body.insertAdjacentHTML('beforeend', `
                <div role="dialog" aria-label="确认发布">
                  <h2>确认发布</h2>
                  <button type="button" onclick="window.confirmClicks += 1">发布</button>
                </div>`);
            }
          </script>
        </body></html>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.route(
                "https://business.facebook.com/**",
                lambda route: route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=html,
                ),
            )
            await page.goto(
                "https://business.facebook.com/latest/reels_composer/?asset_id=1001"
            )
            adapter = FacebookPageFormAdapter(
                page,
                wait_for_verification=self._no_verification,
            )
            adapter._current_expected = self.expectation()
            adapter._fresh_live_composer_page_id = "1001"
            button = await adapter.final_action_button()
            await button.click()

            observation = await adapter.observe_post_click_state("1001")

            self.assertEqual(observation, "confirmation_pending")
            self.assertEqual(await page.evaluate("window.finalClicks"), 1)
            self.assertEqual(await page.evaluate("window.confirmClicks"), 0)
            await browser.close()

    async def test_upload_wait_accepts_completion_after_legacy_six_second_window(
        self,
    ) -> None:
        """慢网下第 25 次以后才完成也不能被旧 6 秒上限误杀。"""

        class SlowUploadAdapter(_HarnessAdapter):
            preview_reads = 0

            async def _read_video_previews(self) -> list[tuple[str, str]]:
                if self.upload_count == 0:
                    return []
                self.preview_reads += 1
                state = "completed" if self.preview_reads >= 30 else "uploading"
                return [("clip.mp4", state)]

        page = _FakeFacebookPage(
            pages=(("1001", "One"),),
            active_page_id="1001",
        )
        adapter = SlowUploadAdapter(
            page,
            wait_for_verification=self._no_verification,
        )
        snapshot = await adapter.fill_and_readback(self.expectation())
        self.assertEqual(snapshot.video_name, "clip.mp4")
        self.assertGreaterEqual(adapter.preview_reads, 30)

    async def test_production_dom_page_drift_after_create_fails_before_write(self) -> None:
        page = _PlaywrightLikeFacebookPage(drift_after_create=True)
        adapter = FacebookPageFormAdapter(
            page,
            wait_for_verification=self._no_verification,
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            await adapter.fill_and_readback(self.expectation())
        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_form_readback_failed",
        )
        self.assertEqual(page.create_click_count, 1)
        self.assertEqual(page.clear_count, 0)
        self.assertEqual(page.set_input_files_calls, 0)
        self.assertEqual(page.caption_fill_count, 0)

    async def test_generic_create_requires_live_composer_route_before_any_write(self) -> None:
        page = _PlaywrightLikeFacebookPage(
            page_bound_create=False,
            generic_create=True,
        )
        adapter = FacebookPageFormAdapter(
            page,
            wait_for_verification=self._no_verification,
        )
        with self.assertRaises(FacebookPagePublishError):
            await adapter.fill_and_readback(self.expectation())
        self.assertEqual(page.create_click_count, 1)
        self.assertEqual(page.set_input_files_calls, 0)

    async def test_explicit_restored_draft_fails_even_when_fields_are_empty(self) -> None:
        adapter = self.adapter(restored_draft=True)
        await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
        self.assertEqual(adapter.clear_count, 0)
        self.assertEqual(adapter.upload_count, 0)

    async def test_nonempty_editor_before_fill_fails_without_clearing(self) -> None:
        adapter = self.adapter(editor_before="old user draft")
        await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
        self.assertEqual(adapter.editor_value, "old user draft")
        self.assertEqual(adapter.clear_count, 0)
        self.assertEqual(adapter.upload_count, 0)

    async def test_editor_clear_must_read_back_empty_before_upload(self) -> None:
        adapter = self.adapter(clear_after="platform kept old text")
        await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
        self.assertEqual(adapter.clear_count, 1)
        self.assertEqual(adapter.upload_count, 0)
        self.assertEqual(adapter.write_count, 0)

    async def test_caption_order_or_duplicate_mismatch_fails(self) -> None:
        expected = self.expectation(caption="第一行\n#AI #AI\n最后一行")
        for actual in (
            "最后一行\n#AI #AI\n第一行",
            "第一行\n#AI\n最后一行",
        ):
            with self.subTest(actual=actual):
                adapter = self.adapter(editor_after=actual)
                await self.assert_error_code(
                    adapter,
                    expected,
                    "facebook_page_form_readback_failed",
                )

    async def test_video_readback_rejects_missing_duplicate_wrong_or_incomplete(self) -> None:
        cases = (
            ([], [], "no video"),
            (["clip.mp4", "other.mp4"], ["completed", "completed"], "two"),
            (["wrong.mp4"], ["completed"], "wrong name"),
            (["clip.mp4"], ["uploading"], "incomplete"),
        )
        for video_names, statuses, label in cases:
            with self.subTest(case=label):
                adapter = self.adapter(
                    video_names=video_names,
                    upload_statuses=statuses,
                )
                error = await self.assert_error_code(
                    adapter,
                    self.expectation(),
                    "facebook_upload_failed",
                )
                self.assertEqual(error.receipt["videoName"], "clip.mp4")
                self.assertNotIn("caption", error.receipt)
                self.assertNotIn("dom", error.receipt)
                self.assertNotIn("cookie", error.receipt)
                self.assertTrue(error.receipt["platformWriteOccurred"])
                self.assertFalse(error.receipt["finalActionTriggered"])
                self.assertEqual(adapter.upload_count, 1)

    async def test_local_video_size_or_hash_change_fails_before_composer(self) -> None:
        original = self.expectation()
        changed = (
            FacebookPageFormExpectation(
                page_id=original.page_id,
                content_kind="reel",
                video_name=original.video_name,
                video_size=original.video_size + 1,
                video_sha256=original.video_sha256,
                caption=original.caption,
                visibility="public",
            ),
            FacebookPageFormExpectation(
                page_id=original.page_id,
                content_kind="reel",
                video_name=original.video_name,
                video_size=original.video_size,
                video_sha256="f" * 64,
                caption=original.caption,
                visibility="public",
            ),
        )
        for expected in changed:
            with self.subTest(expected=expected):
                adapter = self.adapter()
                await self.assert_error_code(
                    adapter,
                    expected,
                    "facebook_upload_failed",
                )
                self.assertEqual(adapter.composer_open_count, 0)
                self.assertEqual(adapter.upload_count, 0)

    async def test_non_public_visibility_fails(self) -> None:
        adapter = self.adapter(visibility="private")
        error = await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
        self.assertTrue(error.receipt["platformWriteOccurred"])
        self.assertFalse(error.receipt["finalActionTriggered"])

    async def test_final_button_must_be_unique_present_and_enabled(self) -> None:
        cases = (
            ([], "missing"),
            ([_FakeButton(enabled=False)], "disabled"),
            ([_FakeButton(), _FakeButton(label="Share")], "ambiguous"),
        )
        for buttons, label in cases:
            with self.subTest(case=label):
                adapter = self.adapter(final_buttons=buttons)
                error = await self.assert_error_code(
                    adapter,
                    self.expectation(),
                    "facebook_page_form_readback_failed",
                )
                self.assertTrue(error.receipt["platformWriteOccurred"])
                self.assertFalse(error.receipt["finalActionTriggered"])
                self.assertNotIn("caption", error.receipt)
                self.assertNotIn("dom", error.receipt)
                self.assertNotIn("session", error.receipt)
                self.assertEqual(sum(button.click_count for button in buttons), 0)

    async def test_final_button_label_must_be_a_final_publish_action(self) -> None:
        button = _FakeButton(label="Next", enabled=True)
        adapter = self.adapter(final_buttons=[button])
        error = await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
        self.assertTrue(error.receipt["platformWriteOccurred"])
        self.assertFalse(error.receipt["finalActionTriggered"])
        self.assertEqual(button.click_count, 0)

    async def test_final_form_verifier_rechecks_page_caption_video_and_visibility(self) -> None:
        cases = (
            ("page", lambda adapter: setattr(adapter.page, "active_page_id", "1002")),
            ("caption", lambda adapter: setattr(adapter, "editor_value", "changed")),
            (
                "video",
                lambda adapter: setattr(adapter, "video_names", ["changed.mp4"]),
            ),
            (
                "visibility",
                lambda adapter: setattr(adapter, "visibility_value", "private"),
            ),
        )
        expected = self.expectation()
        for label, mutate in cases:
            with self.subTest(drift=label):
                adapter = self.adapter(
                    pages=(("1001", "One"), ("1002", "Two")),
                    active_page_id="1001",
                )
                await adapter.fill_and_readback(expected)
                mutate(adapter)
                with self.assertRaises(FacebookPagePublishError) as raised:
                    await adapter.verify_final_form(expected)
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_page_form_readback_failed",
                )
                self.assertEqual(self.final_button_click_count, 0)

    async def test_verification_pause_rechecks_the_same_exact_page_without_reselecting(self) -> None:
        wait_count = 0

        async def verification_pause(*_args, **_kwargs) -> None:
            nonlocal wait_count
            wait_count += 1

        adapter = self.adapter(
            pages=(("1001", "One"), ("1002", "Two")),
            active_page_id="1001",
            wait_for_verification=verification_pause,
        )
        snapshot = await adapter.fill_and_readback(self.expectation())
        self.assertEqual(snapshot.page_id, "1001")
        self.assertEqual(adapter.page.active_page_id, "1001")
        self.assertEqual(adapter.page.activation_attempts, ["1001"])
        self.assertEqual(wait_count, 4)
        self.assertGreaterEqual(adapter.page.active_read_count, 2)

    async def test_create_reel_verification_timeout_keeps_frozen_error(self) -> None:
        wait_count = 0

        async def verification_pause(*_args, **_kwargs) -> None:
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                raise MetaManualInterventionRequired("verification timeout")

        adapter = self.adapter(wait_for_verification=verification_pause)
        try:
            await adapter.fill_and_readback(self.expectation())
        except Exception as error:  # noqa: BLE001 - contract inspects public error
            caught = error
        else:
            self.fail("verification timeout must stop the form")
        self.assertIsInstance(caught, MetaManualInterventionRequired)
        self.assertEqual(
            getattr(caught, "error_code", None),
            "facebook_verification_timeout",
        )
        self.assertEqual(adapter.clear_count, 0)
        self.assertEqual(adapter.upload_count, 0)
        self.assertEqual(adapter.write_count, 0)

    async def test_create_reel_verification_required_semantics_are_preserved(self) -> None:
        wait_count = 0

        async def verification_pause(*_args, **_kwargs) -> None:
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                raise FacebookPagePublishError(
                    "facebook_verification_required",
                    "verification required",
                )

        adapter = self.adapter(wait_for_verification=verification_pause)
        error = await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_verification_required",
        )
        self.assertFalse(error.receipt.get("platformWriteOccurred", False))
        self.assertEqual(adapter.clear_count, 0)
        self.assertEqual(adapter.upload_count, 0)
        self.assertEqual(adapter.write_count, 0)

    async def test_verification_pause_returning_on_another_page_fails_closed(self) -> None:
        holder: dict[str, _HarnessAdapter] = {}
        wait_count = 0

        async def verification_pause(*_args, **_kwargs) -> None:
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                holder["adapter"].page.active_page_id = "1002"

        adapter = self.adapter(
            pages=(("1001", "One"), ("1002", "Two")),
            active_page_id="1001",
            wait_for_verification=verification_pause,
        )
        holder["adapter"] = adapter
        await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
        self.assertEqual(adapter.page.active_page_id, "1002")
        self.assertEqual(adapter.page.activation_attempts, ["1001"])
        self.assertEqual(adapter.clear_count, 0)
        self.assertEqual(adapter.upload_count, 0)
        self.assertEqual(adapter.write_count, 0)

    async def test_generic_video_or_normal_post_composer_is_not_a_reel(self) -> None:
        for content_kind in ("video", "post", ""):
            with self.subTest(content_kind=content_kind):
                adapter = self.adapter(content_kind=content_kind)
                await self.assert_error_code(
                    adapter,
                    self.expectation(),
                    "facebook_page_form_readback_failed",
                )
                self.assertEqual(adapter.upload_count, 0)

    async def test_preflight_form_adapter_never_clicks_the_final_button(self) -> None:
        button = _FakeButton(label="Publish", enabled=True)
        adapter = self.adapter(final_buttons=[button])
        await adapter.fill_and_readback(self.expectation())
        self.assertEqual(button.click_count, 0)

    async def test_caption_canonicalization_changes_only_platform_spacing(self) -> None:
        raw = "  第一行\r\n#AI #AI\u202f  \r最后一行 \t "
        self.assertEqual(
            canonical_meta_caption(raw),
            "第一行\n#AI #AI\n最后一行",
        )
        self.assertEqual(canonical_meta_caption(None), "")
        self.assertIs(
            canonical_meta_caption,
            getattr(
                overseas_meta_content,
                "canonical_facebook_page_caption",
                None,
            ),
        )


class _ContentElement:
    def __init__(
        self,
        page: "_ContentPage",
        *,
        attributes: dict[str, str] | None = None,
        text: str = "",
        action: str = "",
        row: dict[str, str] | None = None,
        visible: bool = True,
    ) -> None:
        self.page = page
        self.attributes = dict(attributes or {})
        self.text = text
        self.action = action
        self.row = row
        self.visible = visible

    async def is_visible(self) -> bool:
        return self.visible

    async def get_attribute(self, name: str) -> str | None:
        return self.attributes.get(name)

    async def inner_text(self) -> str:
        return self.text

    async def text_content(self) -> str:
        return self.text

    async def is_enabled(self) -> bool:
        return True

    async def click(self) -> None:
        self.page.context.actions.append(self.action)
        if self.action == "next_page":
            self.page.page_index += 1

    def locator(self, selector: str) -> _FakeLocator:
        self.page.context.selectors.append(selector)
        if self.action == "content_main" and selector == 'a[href*="/reel/"]':
            return self.page.content_row_anchors()
        if self.action == "reel_anchor" and selector == "time[datetime]":
            count = int((self.row or {}).get("time_count", "1"))
            return _FakeLocator(
                [
                    _ContentElement(
                        self.page,
                        attributes={
                            "datetime": str((self.row or {}).get("published_at", ""))
                        },
                    )
                    for _ in range(count)
                ]
            )
        return _FakeLocator([])


class _ContentPage:
    def __init__(self, context: "_ContentContext") -> None:
        self.context = context
        self.rows = [
            _FakePageRow(self, "1001", "One"),
            _FakePageRow(self, "1002", "Two"),
        ]
        self.active_page_id = context.active_page_id
        self.switch_mismatch = context.switch_mismatch
        self.activation_attempts: list[str] = []
        self.active_read_count = 0
        self.mode = "blank"
        self.url = "about:blank"
        self.page_index = 0
        self.detail_reel_id = ""
        self.closed = False

    async def goto(self, url: str, **_kwargs) -> None:
        self.context.goto_urls.append(url)
        remaining_failures = int(self.context.fail_url_counts.get(url, 0))
        if remaining_failures > 0:
            self.context.fail_url_counts[url] = remaining_failures - 1
            raise RuntimeError("transient navigation failure")
        if url in self.context.fail_urls:
            raise RuntimeError("navigation failed")
        if "/latest/content" in url:
            self.mode = "list"
            self.page_index = 0
            self.url = self.context.content_reported_url or url
            return
        if "/reel/" in url:
            self.mode = "detail"
            self.detail_reel_id = url.rstrip("/").rsplit("/", 1)[-1]
            detail = self.context.details.get(self.detail_reel_id, {})
            self.url = str(detail.get("reported_url", url))
            return
        self.mode = "generic"
        self.url = url

    async def close(self) -> None:
        self.closed = True

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        self.context.wait_timeout_calls += 1

    def locator(self, selector: str) -> _FakeLocator:
        self.context.selectors.append(selector)
        if selector == "[data-page-id]":
            return _FakeLocator(self.rows)
        if selector == '[data-page-id][data-page-active="true"]':
            self.active_read_count += 1
            return _FakeLocator(
                [row for row in self.rows if row.page_id == self.active_page_id]
            )
        if selector in {'[data-page-id="1001"]', '[data-page-id="1002"]'}:
            page_id = selector.removeprefix('[data-page-id="').removesuffix('"]')
            return _FakeLocator(
                [row for row in self.rows if row.page_id == page_id]
            )
        if self.mode == "list":
            return self._list_locator(selector)
        if self.mode == "detail":
            return self._detail_locator(selector)
        return _FakeLocator([])

    def _list_locator(self, selector: str) -> _FakeLocator:
        if selector == "main":
            return _FakeLocator(
                [
                    _ContentElement(
                        self,
                        action="content_main",
                    )
                    for _ in range(self.context.root_count)
                ]
            )
        return _FakeLocator([])

    def content_row_anchors(self) -> _FakeLocator:
        page_rows = (
            self.context.pages[self.page_index]
            if self.page_index < len(self.context.pages)
            else []
        )
        return _FakeLocator(
            [
                _ContentElement(
                    self,
                    attributes={"href": str(row.get("url", ""))},
                    action="reel_anchor",
                    row=row,
                )
                for row in page_rows
            ]
        )

    def _detail_locator(self, selector: str) -> _FakeLocator:
        detail = self.context.details.get(self.detail_reel_id)
        if selector == 'meta[property="og:url"][content]' and detail is not None:
            count = int(detail.get("url_count", 1))
            return _FakeLocator(
                [
                    _ContentElement(
                        self,
                        attributes={"content": str(detail.get("url", self.url))},
                        visible=False,
                    )
                    for _ in range(count)
                ]
            )
        if selector == 'meta[property="og:description"][content]':
            if detail is None or detail.get("caption") is None:
                return _FakeLocator([])
            count = int(detail.get("caption_count", 1))
            return _FakeLocator(
                [
                    _ContentElement(
                        self,
                        attributes={"content": str(detail["caption"])},
                        visible=False,
                    )
                    for _ in range(count)
                ]
            )
        return _FakeLocator([])

    def get_by_role(
        self,
        role: str,
        *,
        name: str | None = None,
        exact: bool = False,
    ) -> _FakeLocator:
        self.context.role_queries += 1
        if (
            self.mode == "generic"
            and role == "link"
            and exact
            and name == "Content"
        ):
            return _FakeLocator(
                [
                    _ContentElement(
                        self,
                        attributes={
                            "href": "https://business.facebook.com/latest/content?asset_id=1001"
                        },
                    )
                ]
            )
        if self.mode == "decision" and role in {"alert", "status"} and name is None:
            return _FakeLocator(
                [
                    _ContentElement(self, text=text)
                    for message_role, text in self.context.decision_messages
                    if message_role == role
                ]
            )
        if self.mode != "list":
            return _FakeLocator([])
        on_last_page = self.page_index == len(self.context.pages) - 1
        if role == "status" and name is None:
            if self.context.explicit_empty and not any(self.context.pages):
                return _FakeLocator(
                    [_ContentElement(self, text=self.context.empty_text)]
                )
            if self.context.complete and on_last_page:
                return _FakeLocator(
                    [
                        _ContentElement(self, text=self.context.terminal_text)
                        for _ in range(self.context.terminal_count)
                    ]
                )
            return _FakeLocator(
                [_ContentElement(self, text=text) for text in self.context.extra_statuses]
            )
        has_next = self.page_index + 1 < len(self.context.pages)
        if (
            role == "button"
            and exact
            and name == self.context.next_label
            and has_next
        ):
            return _FakeLocator([_ContentElement(self, action="next_page")])
        return _FakeLocator([])


class _ContentContext:
    def __init__(
        self,
        *,
        pages: list[list[dict[str, str]]],
        details: dict[str, dict[str, object]] | None = None,
        complete: bool = True,
        explicit_empty: bool = False,
        root_count: int = 1,
        terminal_count: int = 1,
        active_page_id: str = "1001",
        switch_mismatch: bool = False,
        fail_urls: set[str] | None = None,
        fail_url_counts: dict[str, int] | None = None,
        content_reported_url: str = "",
        terminal_text: str = "No more results",
        empty_text: str = "No content yet",
        next_label: str = "Load more",
        extra_statuses: list[str] | None = None,
        decision_messages: list[tuple[str, str]] | None = None,
    ) -> None:
        self.pages = pages
        self.details = dict(details or {})
        self.complete = complete
        self.explicit_empty = explicit_empty
        self.root_count = root_count
        self.terminal_count = terminal_count
        self.active_page_id = active_page_id
        self.switch_mismatch = switch_mismatch
        self.fail_urls = set(fail_urls or set())
        self.fail_url_counts = dict(fail_url_counts or {})
        self.content_reported_url = content_reported_url
        self.terminal_text = terminal_text
        self.empty_text = empty_text
        self.next_label = next_label
        self.extra_statuses = list(extra_statuses or [])
        self.decision_messages = list(decision_messages or [])
        self.created_pages: list[_ContentPage] = []
        self.goto_urls: list[str] = []
        self.actions: list[str] = []
        self.selectors: list[str] = []
        self.role_queries = 0
        self.wait_timeout_calls = 0

    async def new_page(self) -> _ContentPage:
        page = _ContentPage(self)
        self.created_pages.append(page)
        return page


class FacebookPageContentListTests(unittest.IsolatedAsyncioTestCase):
    page_id = "1001"
    captured_at = "2026-08-30T02:00:00+00:00"
    clicked_at = "2026-08-30T02:01:00+00:00"
    expected_caption = "正文\n#AI"

    @property
    def expected_caption_hash(self) -> str:
        return hashlib.sha256(self.expected_caption.encode("utf-8")).hexdigest()

    @staticmethod
    async def no_verification(*_args, **_kwargs) -> None:
        return None

    def row(
        self,
        reel_id: str,
        *,
        caption: str = "旧内容",
        page_id: str = "1001",
        url: str | None = None,
        published_at: str = "2026-08-30T02:02:00+00:00",
        caption_sha256: str | None = None,
    ) -> FacebookReelRow:
        return FacebookReelRow(
            page_id=page_id,
            reel_id=reel_id,
            url=(
                f"https://www.facebook.com/reel/{reel_id}"
                if url is None
                else url
            ),
            caption_sha256=(
                hashlib.sha256(
                    canonical_meta_caption(caption).encode("utf-8")
                ).hexdigest()
                if caption_sha256 is None
                else caption_sha256
            ),
            published_at=published_at,
        )

    def baseline(
        self,
        rows: list[FacebookReelRow],
        *,
        page_id: str = "1001",
        captured_at: str | None = None,
    ) -> FacebookPageContentBaseline:
        captured = captured_at or self.captured_at
        safe_rows = [
            {
                "captionSha256": row.caption_sha256,
                "pageId": row.page_id,
                "publishedAt": row.published_at,
                "reelId": row.reel_id,
                "url": row.url,
            }
            for row in sorted(rows, key=lambda item: item.reel_id)
        ]
        payload = {
            "capturedAt": captured,
            "pageId": page_id,
            "rows": safe_rows,
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return FacebookPageContentBaseline(
            page_id=page_id,
            rows=tuple(rows),
            captured_at=captured,
            snapshot_sha256=digest,
        )

    def list_row(
        self,
        reel_id: str,
        *,
        page_id: str = "1001",
        url: str | None = None,
        published_at: str = "2026-08-30T02:02:00+00:00",
        preview: str = "truncated...",
    ) -> dict[str, str]:
        return {
            "page_id": page_id,
            "reel_id": reel_id,
            "url": url or f"https://www.facebook.com/reel/{reel_id}",
            "published_at": published_at,
            "preview": preview,
        }

    def match(
        self,
        baseline: FacebookPageContentBaseline,
        rows: list[FacebookReelRow],
    ) -> FacebookReelMatch:
        return match_unique_new_facebook_reel(
            baseline=baseline,
            current_rows=rows,
            expected_page_id=self.page_id,
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at=self.clicked_at,
        )

    def assert_baseline_error(self, callable_) -> None:
        with self.assertRaises(FacebookPagePublishError) as raised:
            callable_()
        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_baseline_read_failed",
        )

    def test_dataclasses_are_immutable_and_baseline_hash_is_order_deterministic(self) -> None:
        first = self.row("a")
        second = self.row("b")
        baseline = self.baseline([second, first])
        reordered = self.baseline([first, second])
        self.assertEqual(baseline.snapshot_sha256, reordered.snapshot_sha256)
        self.assertNotIn("旧内容", json.dumps(baseline, default=str))
        with self.assertRaises(FrozenInstanceError):
            first.reel_id = "changed"  # type: ignore[misc]
        receipt = FacebookReelReceipt("1001", "new", "https://www.facebook.com/reel/new", self.clicked_at)
        with self.assertRaises(FrozenInstanceError):
            receipt.reel_id = "changed"  # type: ignore[misc]

    def test_exactly_one_new_same_page_reel_is_success(self) -> None:
        baseline = self.baseline([self.row("old", caption="旧内容")])
        match = self.match(
            baseline,
            [
                self.row("old", caption="旧内容"),
                self.row("new", caption=self.expected_caption),
            ],
        )
        self.assertEqual(match.status, "unique")
        self.assertIsNotNone(match.receipt)
        self.assertEqual(match.receipt.reel_id, "new")
        self.assertEqual(match.new_count, 1)
        self.assertEqual(match.matching_count, 1)

    def test_minute_precision_timestamp_in_click_minute_is_success(self) -> None:
        match = match_unique_new_facebook_reel(
            baseline=self.baseline([]),
            current_rows=[
                self.row(
                    "new",
                    caption=self.expected_caption,
                    published_at="2026-08-30T22:24:00+00:00",
                )
            ],
            expected_page_id=self.page_id,
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at="2026-08-30T22:24:02.710000+00:00",
        )
        self.assertEqual(match.status, "unique")
        self.assertIsNotNone(match.receipt)
        self.assertEqual(match.receipt.reel_id, "new")

    def test_real_reel_types_pass_task5_lazy_exact_type_gate(self) -> None:
        from app_core import controlled_publish

        match = self.match(
            self.baseline([]),
            [self.row("new", caption=self.expected_caption)],
        )
        receipt = controlled_publish._safe_facebook_reel_match(
            match,
            expected_page_reference="1001",
            baseline={"pageId": "1001", "rows": []},
            form_snapshot={"captionSha256": self.expected_caption_hash},
            clicked_at=self.clicked_at,
        )
        self.assertEqual(receipt["reelId"], "new")
        self.assertEqual(receipt["pageId"], "1001")
        self.assertEqual(receipt["phase"], "published_readback_confirmed")

    def test_explicit_empty_baseline_can_match_one_new_reel(self) -> None:
        match = self.match(
            self.baseline([]),
            [self.row("new", caption=self.expected_caption)],
        )
        self.assertEqual(match.status, "unique")

    def test_zero_new_rows_is_none_even_when_indexing_may_be_delayed(self) -> None:
        old = self.row("old")
        match = self.match(self.baseline([old]), [old])
        self.assertEqual(match, FacebookReelMatch("none", None, 0, 0))

    def test_visible_unrelated_or_multiple_matching_rows_are_mismatch(self) -> None:
        cases = (
            [self.row("unrelated", caption="别的内容")],
            [
                self.row("new-1", caption=self.expected_caption),
                self.row("new-2", caption=self.expected_caption),
            ],
        )
        for rows in cases:
            with self.subTest(rows=rows):
                match = self.match(self.baseline([]), rows)
                self.assertEqual(match.status, "mismatch")
                self.assertIsNone(match.receipt)
                self.assertEqual(match.new_count, len(rows))

    def test_one_matching_row_remains_unique_among_unrelated_concurrent_rows(self) -> None:
        match = self.match(
            self.baseline([]),
            [
                self.row("unrelated", caption="别的内容"),
                self.row("target", caption=self.expected_caption),
            ],
        )
        self.assertEqual(match.status, "unique")
        self.assertEqual(match.new_count, 2)
        self.assertEqual(match.matching_count, 1)

    def test_unidentifiable_or_wrong_page_new_row_prevents_otherwise_unique_match(self) -> None:
        cases = (
            [
                self.row("", caption=self.expected_caption, url=""),
                self.row("target", caption=self.expected_caption),
            ],
            [
                self.row("duplicate", caption=self.expected_caption),
                self.row("duplicate", caption=self.expected_caption),
            ],
            [
                self.row("wrong-page", page_id="1002", caption="other"),
                self.row("target", caption=self.expected_caption),
            ],
            [
                self.row("unreadable", caption_sha256=""),
                self.row("target", caption=self.expected_caption),
            ],
        )
        for rows in cases:
            with self.subTest(rows=rows):
                match = self.match(self.baseline([]), rows)
                self.assertEqual(match.status, "mismatch")
                self.assertIsNone(match.receipt)

    def test_wrong_page_early_timestamp_missing_id_or_noncanonical_url_mismatch(self) -> None:
        cases = (
            self.row("wrong-page", page_id="1002", caption=self.expected_caption),
            self.row(
                "early",
                caption=self.expected_caption,
                published_at="2026-08-30T02:00:59+00:00",
            ),
            self.row("", caption=self.expected_caption, url=""),
            self.row(
                "generic",
                caption=self.expected_caption,
                url="https://business.facebook.com/latest/content",
            ),
        )
        for row in cases:
            with self.subTest(row=row):
                match = self.match(self.baseline([]), [row])
                self.assertEqual(match.status, "mismatch")
                self.assertIsNone(match.receipt)

    def test_invalid_or_tampered_baseline_fails_closed(self) -> None:
        duplicate = self.baseline([self.row("same"), self.row("same")])
        wrong_page = self.baseline([], page_id="1002")
        valid = self.baseline([])
        tampered = FacebookPageContentBaseline(
            page_id=valid.page_id,
            rows=valid.rows,
            captured_at=valid.captured_at,
            snapshot_sha256="f" * 64,
        )
        for baseline in (duplicate, wrong_page, tampered):
            with self.subTest(baseline=baseline):
                self.assert_baseline_error(lambda: self.match(baseline, []))

    async def test_reader_accepts_explicit_complete_empty_baseline(self) -> None:
        context = _ContentContext(
            pages=[[]],
            complete=True,
            explicit_empty=True,
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        baseline = await reader.capture_baseline(expected_page_id="1001")
        self.assertEqual(baseline.page_id, "1001")
        self.assertEqual(baseline.rows, ())
        self.assertRegex(baseline.snapshot_sha256, r"^[0-9a-f]{64}$")

    async def _capture_current_table_html(
        self,
        table_body: str,
        *,
        controls_html: str = "",
        page_script: str = "",
        detail_html: str = "",
        max_samples: int = 20,
        current_table_wait_ms: int = 0,
        trusted_display_timezone: ZoneInfo | None = None,
    ) -> FacebookPageContentBaseline:
        from playwright.async_api import async_playwright

        home_html = """
        <!doctype html><html><body>
          <div data-page-id="1001" data-page-name="墨白"
               data-can-manage-content="true" data-page-active="true">墨白</div>
          <a role="link" aria-label="内容"
             href="https://business.facebook.com/latest/posts?asset_id=1001">内容</a>
        </body></html>
        """
        posts_html = f"""
        <!doctype html><html><body>
          <script>
            history.replaceState({{}}, '',
              '/latest/posts/published_posts/?asset_id=1001');
            {page_script}
          </script>
          {controls_html}
          <table>
            <tr role="row">
              <th role="columnheader">标题</th>
              <th role="columnheader">发布日期</th>
            </tr>
            {table_body}
          </table>
        </body></html>
        """

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            async def serve(route) -> None:
                body = (
                    posts_html
                    if "/latest/posts" in route.request.url
                    else home_html
                )
                await route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=body,
                )

            await context.route("https://business.facebook.com/**", serve)
            if detail_html:
                await context.route(
                    "https://www.facebook.com/reel/**",
                    lambda route: route.fulfill(
                        status=200,
                        headers={"Content-Type": "text/html; charset=utf-8"},
                        body=detail_html,
                    ),
                )
            reader_options = {}
            if trusted_display_timezone is not None:
                reader_options["trusted_display_timezone"] = trusted_display_timezone
            reader = FacebookPageContentReader(
                context,
                wait_for_verification=self.no_verification,
                **reader_options,
            )
            try:
                with (
                    patch(
                        "uploader.meta_uploader.content_list._CURRENT_TABLE_MAX_SAMPLES",
                        max_samples,
                    ),
                    patch(
                        "uploader.meta_uploader.content_list._CURRENT_TABLE_WAIT_MS",
                        current_table_wait_ms,
                    ),
                    patch(
                        "uploader.meta_uploader.content_list._CURRENT_EMPTY_MIN_SETTLEMENT_MS",
                        0,
                    ),
                    patch(
                        "uploader.meta_uploader.content_list._READBACK_SETTLEMENT_WAIT_MS",
                        0,
                    ),
                ):
                    return await reader.capture_baseline(expected_page_id="1001")
            finally:
                await context.close()
                await browser.close()

    async def test_current_posts_table_uses_trusted_context_timezone_when_meta_omits_it(
        self,
    ) -> None:
        """受控浏览器已锁定时区时，Meta 可省略重复的页面时区标记。"""

        baseline = await self._capture_current_table_html(
            """
            <tr role="row" data-index="0">
              <td><input type="checkbox" aria-label="选择编号为823456789012345的项目"></td>
              <td aria-colindex="2"><div>封面照片</div><span>照片</span></td>
              <td aria-colindex="3">8月30日 18:50</td>
            </tr>
            """,
            trusted_display_timezone=ZoneInfo("Asia/Shanghai"),
        )

        self.assertEqual(baseline.rows, ())

    async def test_current_posts_table_waits_for_transient_incomplete_virtual_row(
        self,
    ) -> None:
        """React 虚拟表格重绘中的半成品行不能让安全基线永久失败。"""

        baseline = await self._capture_current_table_html(
            '<div id="empty" role="status">No content yet</div>',
            controls_html="""
              <span data-meta-timezone="Asia/Shanghai" hidden></span>
              <button id="date-range" type="button"
                      aria-label="过去90天：2026年6月1日–2026年8月29日"
                      onclick="document.querySelector('#all-time').hidden = false">
                过去90天：2026年6月1日–2026年8月29日
              </button>
              <button id="all-time" type="button" hidden
                      onclick="selectAllTime()">创建至今</button>
            """,
            page_script="""
              function selectAllTime() {
                const range = document.querySelector('#date-range');
                range.textContent = '创建至今：2026年8月30日';
                range.setAttribute('aria-label', '创建至今：2026年8月30日');
                document.querySelector('#all-time').hidden = true;
                document.querySelector('#empty').remove();
                document.querySelector('tbody').insertAdjacentHTML(
                  'beforeend',
                  `<tr id="transient-row" role="row" data-index="0">
                     <td><input type="checkbox" aria-label="选择编号为923456789012345的项目"></td>
                     <td aria-colindex="2"><div>头像照片</div><span>照片</span></td>
                   </tr>`
                );
                setTimeout(() => {
                  document.querySelector('#transient-row').insertAdjacentHTML(
                    'beforeend',
                    '<td aria-colindex="3">8月30日 18:48</td>'
                  );
                }, 400);
              }
            """,
            max_samples=6,
            current_table_wait_ms=100,
        )

        self.assertEqual(baseline.rows, ())

    async def test_current_posts_date_scope_waits_for_delayed_all_time_option(
        self,
    ) -> None:
        """日期菜单异步渲染时必须等到唯一“创建至今”选项。"""

        baseline = await self._capture_current_table_html(
            '<div id="empty" role="status">No content yet</div>',
            controls_html="""
              <button id="date-range" type="button"
                      aria-label="过去90天：2026年6月1日–2026年8月29日"
                      onclick="showAllTimeLater()">
                过去90天：2026年6月1日–2026年8月29日
              </button>
              <button id="all-time" type="button" hidden
                      onclick="selectAllTime()">创建至今</button>
            """,
            page_script="""
              function showAllTimeLater() {
                setTimeout(() => {
                  document.querySelector('#all-time').hidden = false;
                }, 400);
              }
              function selectAllTime() {
                const range = document.querySelector('#date-range');
                range.textContent = '创建至今：2026年8月30日';
                range.setAttribute('aria-label', '创建至今：2026年8月30日');
                document.querySelector('#all-time').hidden = true;
                document.querySelector('#empty').remove();
                document.querySelector('tbody').insertAdjacentHTML(
                  'beforeend',
                  `<tr role="row" data-index="0">
                     <td><input type="checkbox" aria-label="选择编号为723456789012345的项目"></td>
                     <td aria-colindex="2"><div>历史照片</div><span>照片</span></td>
                     <td aria-colindex="3"><time datetime="2026-08-30T10:50:00+00:00">8月30日 18:50</time></td>
                   </tr>`
                );
              }
            """,
            max_samples=6,
            current_table_wait_ms=25,
        )

        self.assertEqual(baseline.rows, ())

    async def test_current_posts_date_scope_retries_when_first_menu_click_is_ignored(
        self,
    ) -> None:
        """Meta 吞掉首次只读点击时，应重新展开日期菜单后再读取基线。"""

        with patch(
            "uploader.meta_uploader.content_list._CURRENT_FILTER_WAIT_MS",
            10,
        ):
            baseline = await self._capture_current_table_html(
                '<div id="empty" role="status">No content yet</div>',
                controls_html="""
                  <button id="date-range" type="button"
                          aria-label="过去90天：2026年6月1日–2026年8月29日"
                          onclick="openAllTime()">
                    过去90天：2026年6月1日–2026年8月29日
                  </button>
                  <button id="all-time" type="button" hidden
                          onclick="selectAllTime()">创建至今</button>
                """,
                page_script="""
                  let dateMenuClicks = 0;
                  function openAllTime() {
                    dateMenuClicks += 1;
                    if (dateMenuClicks >= 2) {
                      document.querySelector('#all-time').hidden = false;
                    }
                  }
                  function selectAllTime() {
                    const range = document.querySelector('#date-range');
                    range.textContent = '创建至今：2026年8月30日';
                    range.setAttribute('aria-label', '创建至今：2026年8月30日');
                    document.querySelector('#all-time').hidden = true;
                  }
                """,
                max_samples=2,
                current_table_wait_ms=0,
            )

        self.assertEqual(baseline.rows, ())

    async def test_current_table_scroll_survives_row_replacement_during_scroll(
        self,
    ) -> None:
        """滚动期间替换虚拟行时，滚动动作不能依赖已经失效的第 N 行。"""

        from playwright.async_api import async_playwright

        html = """
        <!doctype html><html><body>
          <div id="viewport" style="height:80px;overflow-y:auto">
            <table><tbody id="rows"></tbody></table>
          </div>
          <script>
            const rows = document.querySelector('#rows');
            rows.innerHTML = Array.from({length: 12}, (_, index) =>
              `<tr role="row" data-index="${index}"><td>row ${index}</td></tr>`
            ).join('');
            document.querySelector('#viewport').addEventListener('scroll', () => {
              window.virtualRowsReplaced = true;
              rows.innerHTML = '<tr role="row" data-index="12"><td>replacement</td></tr>';
            }, {once: true});
          </script>
        </body></html>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html)
            try:
                at_bottom = await FacebookPageContentReader._scroll_current_table_to_bottom(
                    page
                )
                await page.wait_for_timeout(50)
                self.assertTrue(at_bottom)
                self.assertTrue(await page.evaluate("window.virtualRowsReplaced"))
            finally:
                await browser.close()

    async def test_current_posts_table_accepts_one_explicit_empty_status(self) -> None:
        baseline = await self._capture_current_table_html(
            '<div role="status">No content yet</div>'
        )
        self.assertEqual(baseline.page_id, "1001")
        self.assertEqual(baseline.rows, ())

    async def test_current_posts_table_headers_without_empty_status_fail_closed(
        self,
    ) -> None:
        with self.assertRaises(FacebookPagePublishError) as raised:
            await self._capture_current_table_html("", max_samples=2)
        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_baseline_read_failed",
        )

    async def test_current_posts_table_rejects_duplicate_empty_status(self) -> None:
        with self.assertRaises(FacebookPagePublishError) as raised:
            await self._capture_current_table_html(
                '<div role="status">No content yet</div>'
                '<div role="status">No content yet</div>',
                max_samples=2,
            )
        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_baseline_read_failed",
        )

    def test_current_table_relative_timestamps_use_stable_minute_precision(
        self,
    ) -> None:
        observed_at = datetime(
            2026,
            8,
            30,
            14,
            24,
            59,
            987654,
            tzinfo=timezone.utc,
        )
        cases = (
            ("刚刚", "2026-08-30T14:24:00+00:00"),
            ("1 分钟前", "2026-08-30T14:23:00+00:00"),
            ("1 minute ago", "2026-08-30T14:23:00+00:00"),
            ("1 hour ago", "2026-08-30T13:24:00+00:00"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(
                    _current_table_timestamp(raw, observed_at=observed_at),
                    expected,
                )

    def test_meta_absolute_timestamp_uses_explicit_page_timezone_at_boundary(
        self,
    ) -> None:
        observed_at = datetime(
            2026,
            8,
            31,
            0,
            2,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        self.assertEqual(
            _current_table_timestamp(
                "8月30日 08:55",
                observed_at=observed_at,
                display_timezone=ZoneInfo("America/Los_Angeles"),
            ),
            "2026-08-30T15:55:00+00:00",
        )
        with self.assertRaises(ValueError):
            _current_table_timestamp(
                "8月30日 08:55",
                observed_at=observed_at,
                display_timezone=None,
            )

    async def test_current_posts_search_is_cleared_before_empty_is_complete(
        self,
    ) -> None:
        reel_id = "323456789012345"
        caption = "搜索筛选清理验证\n#AI"
        baseline = await self._capture_current_table_html(
            '<div id="empty" role="status">No content yet</div>',
            controls_html=(
                '<input role="searchbox" aria-label="按编号或配文搜索" '
                'placeholder="按编号或配文搜索" value="FB0526-P3">'
            ),
            page_script=f"""
              document.addEventListener('input', event => {{
                if (event.target.getAttribute('role') !== 'searchbox' ||
                    event.target.value !== '') return;
                document.querySelector('#empty').remove();
                document.querySelector('tbody').insertAdjacentHTML(
                  'beforeend',
                  `<tr role="row" data-index="0">
                     <td><input type="checkbox" aria-label="选择编号为{reel_id}的项目"></td>
                     <td aria-colindex="2"><div>搜索筛选清理验证</div><span>Reels</span></td>
                     <td aria-colindex="3"><time datetime="2026-08-30T15:55:00+00:00">8月30日 08:55</time></td>
                   </tr>`
                );
              }});
            """,
            detail_html=f"""
              <!doctype html><html><head>
                <meta property="og:url" content="https://www.facebook.com/reel/{reel_id}">
                <meta property="og:description" content="{caption}">
              </head><body></body></html>
            """,
        )
        self.assertEqual([row.reel_id for row in baseline.rows], [reel_id])

    async def test_current_posts_date_scope_is_all_time_before_complete_read(
        self,
    ) -> None:
        reel_id = "423456789012345"
        caption = "日期范围归一验证\n#AI"
        baseline = await self._capture_current_table_html(
            '<div id="empty" role="status">No content yet</div>',
            controls_html="""
              <button id="date-range" type="button"
                      aria-label="过去90天：2026年6月1日–2026年8月29日"
                      onclick="document.querySelector('#all-time').hidden = false">
                过去90天：2026年6月1日–2026年8月29日
              </button>
              <button id="all-time" type="button" hidden
                      onclick="selectAllTime()">创建至今</button>
            """,
            page_script=f"""
              function selectAllTime() {{
                const range = document.querySelector('#date-range');
                range.textContent = '创建至今：2026年8月30日';
                range.setAttribute('aria-label', '创建至今：2026年8月30日');
                document.querySelector('#all-time').hidden = true;
                document.querySelector('#empty').remove();
                document.querySelector('tbody').insertAdjacentHTML(
                  'beforeend',
                  `<tr role="row" data-index="0">
                     <td><input type="checkbox" aria-label="选择编号为{reel_id}的项目"></td>
                     <td aria-colindex="2"><div>日期范围归一验证</div><span>Reels</span></td>
                     <td aria-colindex="3"><time datetime="2026-08-30T15:55:00+00:00">8月30日 08:55</time></td>
                   </tr>`
                );
              }}
            """,
            detail_html=f"""
              <!doctype html><html><head>
                <meta property="og:url" content="https://www.facebook.com/reel/{reel_id}">
                <meta property="og:description" content="{caption}">
              </head><body></body></html>
            """,
        )
        self.assertEqual([row.reel_id for row in baseline.rows], [reel_id])

    async def test_current_table_relative_timestamp_is_stable_across_samples(
        self,
    ) -> None:
        baseline = await self._capture_current_table_html(
            """
            <tr role="row" data-index="0">
              <td><input type="checkbox" aria-label="选择编号为122093996289432772的项目"></td>
              <td aria-colindex="2"><div>墨白更换了封面照片</div><span>照片</span></td>
              <td aria-colindex="3"><div>刚刚</div></td>
            </tr>
            """
        )
        self.assertEqual(baseline.rows, ())

    async def test_live_business_suite_current_posts_table_proves_empty_reel_baseline(
        self,
    ) -> None:
        """当前 Meta 内容表只有照片时，Reel 基线必须明确为空。"""

        from playwright.async_api import async_playwright

        home_html = """
        <!doctype html><html><body>
          <div data-page-id="1001" data-page-name="墨白"
               data-can-manage-content="true" data-page-active="true">墨白</div>
          <a role="link" aria-label="内容"
             href="https://business.facebook.com/latest/posts?asset_id=1001">内容</a>
        </body></html>
        """
        posts_html = """
        <!doctype html><html><body>
          <span data-meta-timezone="Asia/Shanghai" hidden></span>
          <script>
            history.replaceState({}, '',
              '/latest/posts/published_posts/?asset_id=1001');
          </script>
          <table>
            <tr role="row">
              <th role="columnheader">标题</th>
              <th role="columnheader">发布日期</th>
              <th role="columnheader">状态</th>
            </tr>
            <tr role="row" data-index="0">
              <td><input type="checkbox" aria-label="选择编号为122093996289432772的项目"></td>
              <td aria-colindex="2"><div>墨白更换了封面照片</div><span>照片</span></td>
              <td aria-colindex="3"><div>8月30日 18:50</div></td>
            </tr>
            <tr role="row" data-index="1">
              <td><input type="checkbox" aria-label="选择编号为122093995317432772的项目"></td>
              <td aria-colindex="2"><div>墨白更换了头像。</div><span>照片</span></td>
              <td aria-colindex="3"><div>8月30日 18:48</div></td>
            </tr>
          </table>
        </body></html>
        """

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            async def serve(route) -> None:
                body = posts_html if "/latest/posts" in route.request.url else home_html
                await route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=body,
                )

            await context.route("https://business.facebook.com/**", serve)
            reader = FacebookPageContentReader(
                context,
                wait_for_verification=self.no_verification,
            )
            try:
                baseline = await reader.capture_baseline(expected_page_id="1001")
            finally:
                await context.close()
                await browser.close()

        self.assertEqual(baseline.page_id, "1001")
        self.assertEqual(baseline.rows, ())

    async def test_live_business_suite_waits_for_delayed_current_table_rows(
        self,
    ) -> None:
        """表头先出现时，不能在延迟的 Reel 数据行渲染前判定列表为空。"""

        from playwright.async_api import async_playwright

        reel_id = "123456789012345"
        caption = "延迟加载验证\n#AI"
        home_html = """
        <!doctype html><html><body>
          <div data-page-id="1001" data-page-name="墨白"
               data-can-manage-content="true" data-page-active="true">墨白</div>
          <a role="link" aria-label="内容"
             href="https://business.facebook.com/latest/posts?asset_id=1001">内容</a>
        </body></html>
        """
        posts_html = f"""
        <!doctype html><html><body>
          <script>
            history.replaceState({{}}, '',
              '/latest/posts/published_posts/?asset_id=1001');
            setTimeout(() => {{
              document.querySelector('tbody').insertAdjacentHTML(
                'beforeend',
                `<tr role="row" data-index="0">
                   <td><input type="checkbox" aria-label="选择编号为{reel_id}的项目"></td>
                   <td aria-colindex="2"><div>延迟加载验证</div><span>Reels</span></td>
                   <td aria-colindex="3"><time datetime="2026-08-30T14:24:00+00:00">8月30日 22:24</time></td>
                 </tr>`
              );
            }}, 1200);
          </script>
          <table><tbody>
            <tr role="row">
              <th role="columnheader">标题</th>
              <th role="columnheader">发布日期</th>
            </tr>
          </tbody></table>
        </body></html>
        """
        detail_html = f"""
        <!doctype html><html><head>
          <meta property="og:url" content="https://www.facebook.com/reel/{reel_id}">
          <meta property="og:description" content="{caption}">
        </head><body></body></html>
        """

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            async def serve_business(route) -> None:
                body = posts_html if "/latest/posts" in route.request.url else home_html
                await route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=body,
                )

            async def serve_reel(route) -> None:
                await route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=detail_html,
                )

            await context.route("https://business.facebook.com/**", serve_business)
            await context.route("https://www.facebook.com/reel/**", serve_reel)
            reader = FacebookPageContentReader(
                context,
                wait_for_verification=self.no_verification,
            )
            try:
                baseline = await reader.capture_baseline(expected_page_id="1001")
            finally:
                await context.close()
                await browser.close()

        self.assertEqual(len(baseline.rows), 1)
        self.assertEqual(baseline.rows[0].reel_id, reel_id)

    async def test_current_table_waits_past_transient_empty_status_for_row(
        self,
    ) -> None:
        """短暂的“暂无内容”不能覆盖随后完成渲染的已有 Reel。"""

        from playwright.async_api import async_playwright

        reel_id = "223456789012345"
        caption = "空状态后延迟加载\n#AI"
        home_html = """
        <!doctype html><html><body>
          <div data-page-id="1001" data-page-name="墨白"
               data-can-manage-content="true" data-page-active="true">墨白</div>
          <a role="link" aria-label="内容"
             href="https://business.facebook.com/latest/posts?asset_id=1001">内容</a>
        </body></html>
        """
        posts_html = f"""
        <!doctype html><html><body>
          <script>
            history.replaceState({{}}, '',
              '/latest/posts/published_posts/?asset_id=1001');
            setTimeout(() => {{
              document.querySelector('[role="status"]').remove();
              document.querySelector('tbody').insertAdjacentHTML(
                'beforeend',
                `<tr role="row" data-index="0">
                   <td><input type="checkbox" aria-label="选择编号为{reel_id}的项目"></td>
                   <td aria-colindex="2"><div>空状态后延迟加载</div><span>Reels</span></td>
                   <td aria-colindex="3"><time datetime="2026-08-30T14:24:00+00:00">8月30日 22:24</time></td>
                 </tr>`
              );
            }}, 1200);
          </script>
          <div role="status">No content yet</div>
          <table><tbody>
            <tr role="row">
              <th role="columnheader">标题</th>
              <th role="columnheader">发布日期</th>
            </tr>
          </tbody></table>
        </body></html>
        """
        detail_html = f"""
        <!doctype html><html><head>
          <meta property="og:url" content="https://www.facebook.com/reel/{reel_id}">
          <meta property="og:description" content="{caption}">
        </head><body></body></html>
        """

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            async def serve_business(route) -> None:
                body = posts_html if "/latest/posts" in route.request.url else home_html
                await route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=body,
                )

            async def serve_reel(route) -> None:
                await route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=detail_html,
                )

            await context.route("https://business.facebook.com/**", serve_business)
            await context.route("https://www.facebook.com/reel/**", serve_reel)
            reader = FacebookPageContentReader(
                context,
                wait_for_verification=self.no_verification,
            )
            try:
                baseline = await reader.capture_baseline(expected_page_id="1001")
            finally:
                await context.close()
                await browser.close()

        self.assertEqual(len(baseline.rows), 1)
        self.assertEqual(baseline.rows[0].reel_id, reel_id)
        self.assertEqual(
            baseline.rows[0].caption_sha256,
            hashlib.sha256(caption.encode("utf-8")).hexdigest(),
        )

    async def test_live_business_suite_waits_for_delayed_current_content_entry(
        self,
    ) -> None:
        """Page 身份先出现时，不能因侧栏入口延迟而退回失效旧地址。"""

        from playwright.async_api import async_playwright

        home_html = """
        <!doctype html><html><body>
          <div data-page-id="1001" data-page-name="墨白"
               data-can-manage-content="true" data-page-active="true">墨白</div>
          <script>
            setTimeout(() => {
              const link = document.createElement('a');
              link.setAttribute('role', 'link');
              link.setAttribute('aria-label', '内容');
              link.href = 'https://business.facebook.com/latest/posts?asset_id=1001';
              link.textContent = '内容';
              document.body.appendChild(link);
            }, 500);
          </script>
        </body></html>
        """
        posts_html = """
        <!doctype html><html><body>
          <span data-meta-timezone="Asia/Shanghai" hidden></span>
          <script>
            history.replaceState({}, '',
              '/latest/posts/published_posts/?asset_id=1001');
          </script>
          <table>
            <tr role="row">
              <th role="columnheader">标题</th>
              <th role="columnheader">发布日期</th>
            </tr>
            <tr role="row" data-index="0">
              <td><input type="checkbox" aria-label="选择编号为122093996289432772的项目"></td>
              <td aria-colindex="2"><div>墨白更换了封面照片</div><span>照片</span></td>
              <td aria-colindex="3"><div>8月30日 18:50</div></td>
            </tr>
          </table>
        </body></html>
        """
        legacy_html = """
        <!doctype html><html><body>
          <script>history.replaceState({}, '', '/latest/home/?typo_redirect=1');</script>
        </body></html>
        """
        requests: list[str] = []

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            async def serve(route) -> None:
                url = route.request.url
                requests.append(url)
                if "/latest/content" in url:
                    body = legacy_html
                elif "/latest/posts" in url:
                    body = posts_html
                else:
                    body = home_html
                await route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=body,
                )

            await context.route("https://business.facebook.com/**", serve)
            reader = FacebookPageContentReader(
                context,
                wait_for_verification=self.no_verification,
            )
            try:
                with (
                    patch(
                        "uploader.meta_uploader.content_list._CONTENT_SURFACE_WAIT_ATTEMPTS",
                        200,
                    ),
                    patch(
                        "uploader.meta_uploader.content_list._CONTENT_SURFACE_WAIT_MS",
                        5,
                    ),
                ):
                    baseline = await reader.capture_baseline(
                        expected_page_id="1001"
                    )
            finally:
                await context.close()
                await browser.close()

        self.assertEqual(baseline.rows, ())
        self.assertTrue(any("/latest/posts?" in url for url in requests))
        self.assertFalse(any("/latest/content?" in url for url in requests))

    async def test_current_posts_table_ignores_late_row_action_text(self) -> None:
        """异步出现的“创建广告”等操作文案不能被当成内容身份变化。"""

        from playwright.async_api import async_playwright

        home_html = """
        <!doctype html><html><body>
          <div data-page-id="1001" data-page-name="墨白"
               data-can-manage-content="true" data-page-active="true">墨白</div>
          <a role="link" aria-label="内容"
             href="https://business.facebook.com/latest/posts?asset_id=1001">内容</a>
        </body></html>
        """
        posts_html = """
        <!doctype html><html><body>
          <span data-meta-timezone="Asia/Shanghai" hidden></span>
          <script>
            history.replaceState({}, '',
              '/latest/posts/published_posts/?asset_id=1001');
            setTimeout(() => {
              const action = document.createElement('div');
              action.textContent = '创建广告';
              document.querySelector('#title-cell').appendChild(action);
            }, 250);
          </script>
          <table>
            <tr role="row">
              <th role="columnheader">标题</th>
              <th role="columnheader">发布日期</th>
            </tr>
            <tr role="row" data-index="0">
              <td><input type="checkbox" aria-label="选择编号为122093996289432772的项目"></td>
              <td id="title-cell" aria-colindex="2"><div>墨白更换了封面照片</div><span>照片</span></td>
              <td aria-colindex="3"><div>8月30日 18:50</div></td>
            </tr>
          </table>
        </body></html>
        """

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            async def serve(route) -> None:
                body = posts_html if "/latest/posts" in route.request.url else home_html
                await route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=body,
                )

            await context.route("https://business.facebook.com/**", serve)
            reader = FacebookPageContentReader(
                context,
                wait_for_verification=self.no_verification,
            )
            try:
                baseline = await reader.capture_baseline(expected_page_id="1001")
            finally:
                await context.close()
                await browser.close()

        self.assertEqual(baseline.rows, ())

    async def test_live_business_suite_current_posts_table_reads_one_reel_detail(
        self,
    ) -> None:
        """新版内容表的 Reel 编号必须回到公开详情页核对完整文案。"""

        from playwright.async_api import async_playwright

        home_html = """
        <!doctype html><html><body>
          <div data-page-id="1001" data-page-name="墨白"
               data-can-manage-content="true" data-page-active="true">墨白</div>
          <a role="link" aria-label="内容"
             href="https://business.facebook.com/latest/posts?asset_id=1001">内容</a>
        </body></html>
        """
        posts_html = """
        <!doctype html><html><body>
          <span data-meta-timezone="Asia/Shanghai" hidden></span>
          <script>
            history.replaceState({}, '',
              '/latest/posts/published_posts/?asset_id=1001');
          </script>
          <table>
            <tr role="row">
              <th role="columnheader">标题</th>
              <th role="columnheader">发布日期</th>
              <th role="columnheader">状态</th>
            </tr>
            <tr role="row" data-index="0">
              <td><input type="checkbox" aria-label="选择编号为998877665544的项目"></td>
              <td aria-colindex="2"><div>正文 #AI</div><span>Reel</span></td>
              <td aria-colindex="3"><div>8月30日 20:02</div></td>
            </tr>
          </table>
        </body></html>
        """
        detail_html = """
        <!doctype html><html><head>
          <meta property="og:url" content="https://www.facebook.com/reel/998877665544">
          <meta property="og:description" content=" 正文\r\n#AI ">
        </head><body></body></html>
        """

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            async def serve_business(route) -> None:
                body = posts_html if "/latest/posts" in route.request.url else home_html
                await route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=body,
                )

            await context.route("https://business.facebook.com/**", serve_business)
            await context.route(
                "https://www.facebook.com/reel/**",
                lambda route: route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=detail_html,
                ),
            )
            reader = FacebookPageContentReader(
                context,
                wait_for_verification=self.no_verification,
            )
            try:
                baseline = await reader.capture_baseline(expected_page_id="1001")
            finally:
                await context.close()
                await browser.close()

        self.assertEqual([row.reel_id for row in baseline.rows], ["998877665544"])
        self.assertEqual(
            baseline.rows[0].url,
            "https://www.facebook.com/reel/998877665544",
        )
        self.assertEqual(
            baseline.rows[0].caption_sha256,
            hashlib.sha256("正文\n#AI".encode("utf-8")).hexdigest(),
        )

    async def test_reader_rejects_incomplete_wrong_page_duplicate_missing_id_and_url_failure(self) -> None:
        reel_url = "https://www.facebook.com/reel/old"
        cases = (
            _ContentContext(
                pages=[[self.list_row("old")]],
                details={"old": {"caption": "旧内容"}},
                complete=False,
            ),
            _ContentContext(
                pages=[[self.list_row("old")]],
                details={"old": {"caption": "旧内容"}},
                active_page_id="1002",
                switch_mismatch=True,
            ),
            _ContentContext(
                pages=[[self.list_row("old"), self.list_row("old")]],
                details={"old": {"caption": "旧内容"}},
            ),
            _ContentContext(
                pages=[[self.list_row("")]],
                details={},
            ),
            _ContentContext(
                pages=[[
                    self.list_row(
                        "old",
                        url="https://business.facebook.com/latest/content",
                    )
                ]],
                details={},
            ),
            _ContentContext(
                pages=[[self.list_row("old")]],
                details={"old": {"caption": "旧内容"}},
                fail_urls={reel_url},
            ),
            _ContentContext(
                pages=[[self.list_row("old")]],
                details={"old": {"caption": "旧内容"}},
                fail_urls={
                    "https://business.facebook.com/latest/content?asset_id=1001"
                },
            ),
            _ContentContext(
                pages=[[self.list_row("old")]],
                details={"old": {"caption": "旧内容"}},
                content_reported_url="https://business.facebook.com/latest/content",
            ),
            _ContentContext(
                pages=[[self.list_row("old")]],
                details={"old": {"caption": "旧内容"}},
                content_reported_url=(
                    "https://business.facebook.com/latest/content"
                    "?asset_id=1001&view=all"
                ),
            ),
        )
        for context in cases:
            with self.subTest(context=context):
                reader = FacebookPageContentReader(
                    context,
                    wait_for_verification=self.no_verification,
                )
                with self.assertRaises(FacebookPagePublishError) as raised:
                    await reader.capture_baseline(expected_page_id="1001")
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_page_baseline_read_failed",
                )

    async def test_reader_rejects_duplicate_id_reintroduced_on_later_page(self) -> None:
        context = _ContentContext(
            pages=[
                [self.list_row("old")],
                [self.list_row("old"), self.list_row("new")],
            ],
            details={
                "old": {"caption": "旧内容"},
                "new": {"caption": "新内容"},
            },
            next_label="Next",
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            await reader.capture_baseline(expected_page_id="1001")
        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_baseline_read_failed",
        )

    async def test_load_more_rejects_shrunk_or_replaced_prior_dom(self) -> None:
        cases = (
            [self.list_row("a")],
            [self.list_row("a"), self.list_row("c")],
        )
        for terminal_rows in cases:
            with self.subTest(terminal_rows=terminal_rows):
                context = _ContentContext(
                    pages=[
                        [self.list_row("a"), self.list_row("b")],
                        terminal_rows,
                    ],
                    details={
                        "a": {"caption": "A"},
                        "b": {"caption": "B"},
                        "c": {"caption": "C"},
                    },
                )
                reader = FacebookPageContentReader(
                    context,
                    wait_for_verification=self.no_verification,
                )
                with self.assertRaises(FacebookPagePublishError) as raised:
                    await reader.capture_baseline(expected_page_id="1001")
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_page_baseline_read_failed",
                )

    async def test_load_more_accepts_full_prior_dom_plus_new_terminal_row(self) -> None:
        context = _ContentContext(
            pages=[
                [self.list_row("a"), self.list_row("b")],
                [self.list_row("a"), self.list_row("b"), self.list_row("c")],
            ],
            details={
                "a": {"caption": "A"},
                "b": {"caption": "B"},
                "c": {"caption": "C"},
            },
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        baseline = await reader.capture_baseline(expected_page_id="1001")
        self.assertEqual([row.reel_id for row in baseline.rows], ["a", "b", "c"])
        self.assertEqual(context.actions, ["next_page"])

    async def test_load_more_accepts_unchanged_full_dom_at_explicit_terminal(self) -> None:
        context = _ContentContext(
            pages=[
                [self.list_row("a"), self.list_row("b")],
                [self.list_row("a"), self.list_row("b")],
            ],
            details={
                "a": {"caption": "A"},
                "b": {"caption": "B"},
            },
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        baseline = await reader.capture_baseline(expected_page_id="1001")
        self.assertEqual([row.reel_id for row in baseline.rows], ["a", "b"])
        self.assertEqual(context.actions, ["next_page"])

    async def test_reader_rejects_missing_ambiguous_or_generic_list_selector(self) -> None:
        cases = (
            _ContentContext(pages=[[]], root_count=0, explicit_empty=True),
            _ContentContext(pages=[[]], root_count=2, explicit_empty=True),
            _ContentContext(
                pages=[[self.list_row("old")]],
                details={"old": {"caption": "旧内容"}},
                terminal_count=2,
            ),
        )
        for context in cases:
            with self.subTest(context=context):
                reader = FacebookPageContentReader(
                    context,
                    wait_for_verification=self.no_verification,
                )
                with self.assertRaises(FacebookPagePublishError) as raised:
                    await reader.capture_baseline(expected_page_id="1001")
                self.assertEqual(
                    raised.exception.error_code,
                    "facebook_page_baseline_read_failed",
                )

    async def test_reader_uses_full_detail_caption_not_truncated_list_preview(self) -> None:
        context = _ContentContext(
            pages=[[self.list_row("old")]],
            details={"old": {"caption": "旧内容"}},
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        baseline = await reader.capture_baseline(expected_page_id="1001")
        context.pages = [[
            self.list_row("old"),
            self.list_row("new", preview="正文..."),
        ]]
        context.details["new"] = {"caption": "  正文\r\n#AI  "}
        match = await reader.readback_unique_reel(
            baseline=baseline,
            expected_page_id="1001",
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at=self.clicked_at,
        )
        self.assertEqual(match.status, "unique")
        self.assertEqual(match.receipt.reel_id, "new")
        self.assertNotEqual("truncated...", self.expected_caption)

    async def test_truncated_list_caption_without_complete_detail_stays_mismatch(self) -> None:
        context = _ContentContext(
            pages=[[]],
            explicit_empty=True,
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        baseline = await reader.capture_baseline(expected_page_id="1001")
        context.explicit_empty = False
        context.pages = [[self.list_row("new", preview=self.expected_caption)]]
        context.details["new"] = {"caption": None}
        match = await reader.readback_unique_reel(
            baseline=baseline,
            expected_page_id="1001",
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at=self.clicked_at,
        )
        self.assertEqual(match.status, "mismatch")
        self.assertEqual(match.new_count, 1)
        self.assertEqual(match.matching_count, 0)

    async def test_two_complete_detail_caption_candidates_stay_mismatch(self) -> None:
        context = _ContentContext(
            pages=[[]],
            explicit_empty=True,
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        baseline = await reader.capture_baseline(expected_page_id="1001")
        context.explicit_empty = False
        context.pages = [[self.list_row("new")]]
        context.details["new"] = {
            "caption": self.expected_caption,
            "caption_count": 2,
        }
        match = await reader.readback_unique_reel(
            baseline=baseline,
            expected_page_id="1001",
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at=self.clicked_at,
        )
        self.assertEqual(match.status, "mismatch")
        self.assertEqual(match.matching_count, 0)

    async def test_verification_resumes_same_context_and_rechecks_exact_page(self) -> None:
        contexts_seen: list[_ContentContext] = []

        async def verification(page, *_args, **_kwargs) -> None:
            contexts_seen.append(page.context)

        context = _ContentContext(
            pages=[
                [self.list_row("old")],
                [self.list_row("new", published_at="2026-08-30T02:03:00+00:00")],
            ],
            details={
                "old": {"caption": "旧内容"},
                "new": {"caption": self.expected_caption},
            },
            next_label="Next",
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=verification,
        )
        baseline = self.baseline([self.row("old")])
        match = await reader.readback_unique_reel(
            baseline=baseline,
            expected_page_id="1001",
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at=self.clicked_at,
        )
        self.assertEqual(match.status, "unique")
        self.assertGreaterEqual(len(contexts_seen), 3)
        self.assertEqual(set(map(id, contexts_seen)), {id(context)})
        self.assertTrue(
            all(page.context is context for page in context.created_pages)
        )
        self.assertEqual(context.actions, ["next_page"])

    async def test_content_list_reuses_one_page_independent_from_composer(self) -> None:
        """Baseline and readback stay off the clicked composer page."""

        context = _ContentContext(
            pages=[[]],
            explicit_empty=True,
        )
        composer_page = _ContentPage(context)
        composer_page.mode = "composer"
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )

        baseline = await reader.capture_baseline(expected_page_id="1001")
        match = await reader.readback_unique_reel(
            baseline=baseline,
            expected_page_id="1001",
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at=self.clicked_at,
        )

        self.assertEqual(match.status, "none")
        self.assertEqual(len(context.created_pages), 1)
        self.assertIsNot(context.created_pages[0], composer_page)
        self.assertIs(reader._list_page, context.created_pages[0])
        self.assertIs(context.created_pages[0].context, context)

    async def test_verification_returning_on_another_page_fails_before_list_read(self) -> None:
        wait_count = 0

        async def verification(page, *_args, **_kwargs) -> None:
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                page.active_page_id = "1002"

        context = _ContentContext(
            pages=[[]],
            explicit_empty=True,
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=verification,
        )
        with self.assertRaises(FacebookPagePublishError) as raised:
            await reader.capture_baseline(expected_page_id="1001")
        self.assertEqual(
            raised.exception.error_code,
            "facebook_page_baseline_read_failed",
        )
        self.assertNotIn('a[href*="/reel/"]', context.selectors)

    async def test_readback_wait_is_bounded_and_returns_none_without_new_ids(self) -> None:
        context = _ContentContext(
            pages=[[self.list_row("old")]],
            details={},
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        match = await reader.readback_unique_reel(
            baseline=self.baseline([self.row("old")]),
            expected_page_id="1001",
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at=self.clicked_at,
        )
        self.assertEqual(match, FacebookReelMatch("none", None, 0, 0))
        self.assertEqual(context.wait_timeout_calls, 24)
        self.assertEqual(len(context.created_pages), 1)

    async def test_slow_network_readback_waits_past_four_empty_samples(self) -> None:
        """Meta 一分钟以上才显示新 Reel 时，不能沿用旧的四次短等待提前收口。"""

        context = _ContentContext(pages=[[]], explicit_empty=True)
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        sample_count = 0

        async def delayed_complete_list(_expected_page_id: str):
            nonlocal sample_count
            sample_count += 1
            if sample_count <= 4:
                return ()
            return (
                FacebookReelRow(
                    page_id="1001",
                    reel_id="slow-network-reel",
                    url="https://www.facebook.com/reel/slow-network-reel",
                    caption_sha256="",
                    published_at="2026-08-30T02:02:00+00:00",
                ),
            )

        async def complete_caption_hash(*_args, **_kwargs):
            return self.expected_caption_hash

        async def no_real_pause() -> None:
            context.wait_timeout_calls += 1

        reader._read_complete_list = delayed_complete_list  # type: ignore[method-assign]
        reader._read_full_caption_hash = complete_caption_hash  # type: ignore[method-assign]
        reader._bounded_pause = no_real_pause  # type: ignore[method-assign]

        match = await reader.readback_unique_reel(
            baseline=self.baseline([]),
            expected_page_id="1001",
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at=self.clicked_at,
        )

        self.assertEqual(match.status, "unique")
        self.assertEqual(match.receipt.reel_id, "slow-network-reel")
        self.assertEqual(sample_count, 5)
        self.assertEqual(context.wait_timeout_calls, 4)

    def test_live_meta_wait_budgets_cover_two_minute_network_delay(self) -> None:
        self.assertGreaterEqual(
            meta_content_list._CONTENT_SURFACE_WAIT_ATTEMPTS
            * meta_content_list._CONTENT_SURFACE_WAIT_MS,
            120_000,
        )
        self.assertGreaterEqual(
            (meta_content_list._DEFAULT_SETTLEMENT_ATTEMPTS - 1)
            * meta_content_list._READBACK_SETTLEMENT_WAIT_MS,
            120_000,
        )

    async def test_post_click_readback_retries_one_transient_list_failure(self) -> None:
        """A one-off list error must not tear down the post-click browser session."""

        context = _ContentContext(
            pages=[[self.list_row("new")]],
            details={"new": {"caption": self.expected_caption}},
            fail_url_counts={
                "https://business.facebook.com/latest/home/": 1,
            },
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )

        match = await reader.readback_unique_reel(
            baseline=self.baseline([]),
            expected_page_id="1001",
            expected_caption_sha256=self.expected_caption_hash,
            clicked_at=self.clicked_at,
        )

        self.assertEqual(match.status, "unique")
        self.assertIsNotNone(match.receipt)
        self.assertEqual(match.receipt.reel_id, "new")
        self.assertEqual(context.wait_timeout_calls, 1)

    async def test_production_selectors_paginate_and_never_query_mutating_actions(self) -> None:
        context = _ContentContext(
            pages=[
                [self.list_row("b")],
                [self.list_row("b"), self.list_row("a")],
            ],
            details={
                "a": {"caption": "A"},
                "b": {"caption": "B"},
            },
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        baseline = await reader.capture_baseline(expected_page_id="1001")
        self.assertEqual([row.reel_id for row in baseline.rows], ["a", "b"])
        self.assertEqual(context.actions, ["next_page"])
        queried = " ".join(context.selectors).casefold()
        self.assertNotIn("data-meta-", queried)
        for forbidden in ("create", "publish", "delete", "final-action"):
            self.assertNotIn(forbidden, queried)
        self.assertGreater(context.role_queries, 0)

    async def test_only_internal_dom_parser_can_create_trusted_platform_decision(self) -> None:
        from app_core import controlled_publish

        forged = {
            "kind": "rejected_no_creation",
            "page_id": "1001",
            "observed_at": "2026-08-30T02:02:00+00:00",
            "evidence_sha256": "f" * 64,
        }
        with self.assertRaises(TypeError):
            FacebookPlatformDecision(**forged)
        self.assertEqual(
            controlled_publish._safe_facebook_platform_decision(
                forged,
                expected_page_reference="1001",
            ),
            {},
        )

        forged_exact = object.__new__(FacebookPlatformDecision)
        for name, value in (
            ("kind", "rejected_no_creation"),
            ("page_id", "1001"),
            ("observed_at", "2026-08-30T02:02:00+00:00"),
            ("evidence_sha256", "f" * 64),
        ):
            try:
                object.__setattr__(forged_exact, name, value)
            except (AttributeError, TypeError):
                pass
        for name, value in (
            ("_kind", "rejected_no_creation"),
            ("_page_id", "1001"),
            ("_observed_at", "2026-08-30T02:02:00+00:00"),
            ("_evidence_sha256", "f" * 64),
            ("_sealed", True),
        ):
            try:
                object.__setattr__(forged_exact, name, value)
            except (AttributeError, TypeError):
                pass
        with self.assertRaises(controlled_publish.ControlledPublishError):
            controlled_publish._safe_facebook_platform_decision(
                forged_exact,
                expected_page_reference="1001",
            )

        context = _ContentContext(
            pages=[[]],
            explicit_empty=True,
            decision_messages=[("alert", "We couldn't publish your reel")],
        )
        reader = FacebookPageContentReader(
            context,
            wait_for_verification=self.no_verification,
        )
        decision_page = await context.new_page()
        decision_page.mode = "decision"
        decision = await reader.read_platform_decision(
            expected_page_id="1001",
            page=decision_page,
        )
        self.assertIs(type(decision), FacebookPlatformDecision)
        self.assertEqual(decision.kind, "rejected_no_creation")
        projected = controlled_publish._safe_facebook_platform_decision(
            decision,
            expected_page_reference="1001",
        )
        self.assertEqual(projected["kind"], "rejected_no_creation")
        self.assertRegex(projected["evidenceSha256"], r"^[0-9a-f]{64}$")

    async def test_internal_decision_parser_seals_accepted_rejected_and_unknown(self) -> None:
        from app_core import controlled_publish

        cases = (
            ([("status", "Your reel is being published")], "accepted"),
            ([("alert", "We couldn't publish your reel")], "rejected_no_creation"),
            ([("status", "Saved")], "unknown"),
            ([], "unknown"),
        )
        for messages, expected_kind in cases:
            with self.subTest(messages=messages):
                context = _ContentContext(
                    pages=[[]],
                    decision_messages=messages,
                )
                page = await context.new_page()
                page.mode = "decision"
                reader = FacebookPageContentReader(
                    context,
                    wait_for_verification=self.no_verification,
                )
                decision = await reader.read_platform_decision(
                    expected_page_id="1001",
                    page=page,
                )
                self.assertEqual(decision.kind, expected_kind)
                projected = controlled_publish._safe_facebook_platform_decision(
                    decision,
                    expected_page_reference="1001",
                )
                self.assertEqual(projected["kind"], expected_kind)


if __name__ == "__main__":
    unittest.main()
