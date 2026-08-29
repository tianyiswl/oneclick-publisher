# -*- coding: utf-8 -*-
"""Facebook Page Reel 表单的离线精确回读合同。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app_core.overseas_meta_errors import FacebookPagePublishError
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
    ) -> None:
        self.rows = [
            _FakePageRow(self, page_id, page_name)
            for page_id, page_name in pages
        ]
        self.active_page_id = active_page_id
        self.switch_mismatch = switch_mismatch
        self.activation_attempts: list[str] = []
        self.active_read_count = 0

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

    async def _click_create_reel_entry(self, expected_page_id: str) -> None:
        self.composer_open_count += 1
        self.opened_for_page_id = expected_page_id

    async def _read_content_kind(self) -> str:
        return self.content_kind

    async def _read_restored_draft(self) -> bool:
        return self.restored_draft

    async def _read_video_previews(self) -> list[tuple[str, str]]:
        if self.upload_count == 0:
            return [(name, "completed") for name in self.initial_video_names]
        return list(zip(self.video_names, self.upload_statuses, strict=False))

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
        await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )

    async def test_final_button_must_be_unique_present_and_enabled(self) -> None:
        cases = (
            ([], "missing"),
            ([_FakeButton(enabled=False)], "disabled"),
            ([_FakeButton(), _FakeButton(label="Share")], "ambiguous"),
        )
        for buttons, label in cases:
            with self.subTest(case=label):
                adapter = self.adapter(final_buttons=buttons)
                await self.assert_error_code(
                    adapter,
                    self.expectation(),
                    "facebook_page_form_readback_failed",
                )
                self.assertEqual(sum(button.click_count for button in buttons), 0)

    async def test_final_button_label_must_be_a_final_publish_action(self) -> None:
        button = _FakeButton(label="Next", enabled=True)
        adapter = self.adapter(final_buttons=[button])
        await self.assert_error_code(
            adapter,
            self.expectation(),
            "facebook_page_form_readback_failed",
        )
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
        self.assertEqual(wait_count, 3)
        self.assertGreaterEqual(adapter.page.active_read_count, 2)

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
        self.assertEqual(adapter.upload_count, 1)
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
