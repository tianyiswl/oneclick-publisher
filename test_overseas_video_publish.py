# -*- coding: utf-8 -*-
"""TikTok/YouTube 正式发布的离线契约测试，不访问平台。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app_core import overseas_video_publish
from app_core.overseas_tiktok_publish import TikTokPublishError
from uploader.tk_uploader import main as tiktok_uploader
from uploader.youtube_uploader import main as youtube_uploader
from uploader.tk_uploader.main import (
    TiktokVideo,
    tiktok_publish_success_signal,
    tiktok_security_intervention_reason,
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

    async def is_editable(self) -> bool:
        return self.editable

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
        self.normalized_text_before_topics: str | None = None

    async def click(self) -> None:
        self.page.active_editor = self

    def locator(self, selector: str) -> FakeTikTokCollection:
        del selector
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
    ) -> None:
        def select() -> None:
            if entity_values is not None:
                page.editor.entity_nodes.extend(entity_values)
            if mutate_text is not None:
                page.editor.text = mutate_text

        super().__init__(label, visible=True, on_click=select)


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
    ) -> TiktokVideo:
        app = TiktokVideo(
            title,
            "/not/used.mp4",
            tags if tags is not None else ["AI", "效率"],
            0,
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
            (0, "tiktok_caption_editor_missing"),
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
            asyncio.run(app.prepare_form(page, page.base))

        app._upload_file.assert_awaited_once_with(page, page.base)
        reveal.assert_awaited_once_with(page)
        self.assertEqual(seen_pages, [page])

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
