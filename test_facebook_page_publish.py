# -*- coding: utf-8 -*-
"""Facebook Page Reel 表单的离线精确回读合同。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app_core.overseas_meta_errors import FacebookPagePublishError
from uploader.meta_uploader.main import MetaManualInterventionRequired
from uploader.meta_uploader.page_form import (
    FacebookPageFormAdapter,
    FacebookPageFormExpectation,
    canonical_meta_caption,
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

    async def test_unbound_generic_create_reel_entry_is_never_used(self) -> None:
        generic = _FakeButton(label="Create reel")
        page = _FakeFacebookPage(
            pages=(("1001", "One"),),
            active_page_id="1001",
            generic_create_buttons=[generic],
        )
        adapter = FacebookPageFormAdapter(
            page,
            wait_for_verification=self._no_verification,
        )
        with self.assertRaises(RuntimeError):
            await adapter._click_create_reel_entry("1001")
        self.assertEqual(generic.click_count, 0)

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

    async def test_production_dom_generic_create_selector_cannot_replace_page_bound_entry(self) -> None:
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
        self.assertEqual(page.create_click_count, 0)
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
        raw = "  第一行\r\n#AI #AI  \r最后一行  "
        self.assertEqual(
            canonical_meta_caption(raw),
            "第一行\n#AI #AI\n最后一行",
        )
        self.assertEqual(canonical_meta_caption(None), "")


if __name__ == "__main__":
    unittest.main()
