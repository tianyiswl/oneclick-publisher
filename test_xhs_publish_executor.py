# -*- coding: utf-8 -*-

import unittest
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from app_core.xhs_publish_executor import (
    XhsPublishError,
    _identity_name_from_response,
    _publish_button_label,
    _schedule_time,
    _validate_declaration_policy,
    _validate_payload,
    _wait_for_platform_result,
)
from app_core.publish_service import _validate_payloads
from app_core.xhs_native_adapter import (
    _AI_LABELS,
    XhsNativeAdapter,
    XhsNativeAdapterError,
    _find_unique_official_topic_candidate,
    _find_video_cover_upload_input,
    _find_video_cover_trigger,
    _topic_candidate_matches,
    build_native_contract,
)


class _FinalButton:
    def __init__(self, tag="XHS-PUBLISH-BTN"):
        self.tag = tag
        self.click_kwargs = None

    async def evaluate(self, _script):
        return self.tag

    async def bounding_box(self):
        return {"x": 10, "y": 20, "width": 500, "height": 80}

    async def click(self, **kwargs):
        self.click_kwargs = kwargs


class XhsFinalButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_component_clicks_real_submit_area(self):
        button = _FinalButton()

        await XhsNativeAdapter.submit_final_button(None, button)

        self.assertEqual(
            button.click_kwargs,
            {
                "position": {"x": 320.0, "y": 40.0},
                "timeout": 10_000,
            },
        )


class _CoverItem:
    async def evaluate(self, _script):
        return True

    async def is_visible(self):
        return True


class _CoverLocator:
    def __init__(self, count):
        self.items = [_CoverItem() for _ in range(count)]

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    @property
    def first(self):
        return self.items[0]


class _CoverPage:
    def __init__(self, counts):
        self.counts = counts
        self.seen = []

    def locator(self, selector):
        self.seen.append(selector)
        value = self.counts.get(selector, 0)
        if isinstance(value, list):
            count = value.pop(0) if value else 0
        else:
            count = value
        return _CoverLocator(count)

    async def wait_for_timeout(self, _milliseconds):
        return None


class XhsCoverTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_current_default_row_cover_card(self):
        page = _CoverPage(
            {
                ".cover-plugin-preview .upload-cover": 0,
                ".cover-plugin-preview .default.row": 1,
            }
        )
        trigger = await _find_video_cover_trigger(page)
        self.assertIsInstance(trigger, _CoverItem)
        self.assertEqual(
            page.seen[:2],
            [
                ".cover-plugin-preview .upload-cover",
                ".cover-plugin-preview .default.row",
            ],
        )

    async def test_accepts_extension_based_cover_upload_input(self):
        image_input = _CoverItem()
        image_input.get_attribute = AsyncMock(return_value=".jpg,.jpeg,.png")
        locator = MagicMock()
        locator.count = AsyncMock(return_value=1)
        locator.nth = MagicMock(return_value=image_input)
        modal = MagicMock()
        modal.locator.return_value = locator

        selected = await _find_video_cover_upload_input(
            modal,
            max_wait_ms=0,
            poll_interval_ms=1,
        )

        self.assertIs(selected, image_input)

    async def test_rejects_video_upload_input_inside_cover_editor(self):
        video_input = _CoverItem()
        video_input.get_attribute = AsyncMock(return_value="video/mp4")
        locator = MagicMock()
        locator.count = AsyncMock(return_value=1)
        locator.nth = MagicMock(return_value=video_input)
        modal = MagicMock()
        modal.locator.return_value = locator

        with self.assertRaises(XhsNativeAdapterError) as raised:
            await _find_video_cover_upload_input(
                modal,
                max_wait_ms=0,
                poll_interval_ms=1,
            )

        self.assertEqual(
            raised.exception.error_code,
            "xhs_cover_upload_input_missing",
        )

    async def test_waits_for_cover_card_after_video_processing(self):
        page = _CoverPage(
            {
                ".cover-plugin-preview .upload-cover": [0, 0],
                ".cover-plugin-preview .default.row": [0, 1],
            }
        )
        trigger = await _find_video_cover_trigger(
            page,
            max_wait_ms=1000,
            poll_interval_ms=1,
        )
        self.assertIsInstance(trigger, _CoverItem)

    async def test_uses_unique_cover_preview_container_in_current_page(self):
        page = _CoverPage({".cover-plugin-preview": 1})
        trigger = await _find_video_cover_trigger(
            page,
            max_wait_ms=0,
            poll_interval_ms=1,
        )
        self.assertIsInstance(trigger, _CoverItem)


class XhsTopicCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_delayed_exact_official_topic_candidate(self):
        name = MagicMock()
        name.inner_text = AsyncMock(return_value="#AI编程")
        names = MagicMock()
        names.count = AsyncMock(return_value=1)
        names.first = name
        candidate = _CoverItem()
        candidate.locator = MagicMock(return_value=names)
        empty = MagicMock()
        empty.count = AsyncMock(return_value=0)
        available = MagicMock()
        available.count = AsyncMock(return_value=1)
        available.nth = MagicMock(return_value=candidate)
        page = MagicMock()
        page.locator = MagicMock(side_effect=[empty, available])
        page.wait_for_timeout = AsyncMock()

        selected = await _find_unique_official_topic_candidate(
            page,
            "AI编程",
            max_wait_ms=1_000,
            poll_interval_ms=1,
        )

        self.assertIs(selected, candidate)
        page.wait_for_timeout.assert_awaited_once_with(1)

    async def test_rejects_official_topics_inserted_before_body_end(self):
        """平台话题即使已是官方节点，插在正文中间也必须停止。"""

        helper = object.__new__(XhsNativeAdapter)
        helper.contract = {
            "description": "谈预算。\n算。",
            "topics": ["AI编程"],
        }
        topic_node = MagicMock()
        topic_node.inner_text = AsyncMock(return_value="#AI编程")
        topic_nodes = MagicMock()
        topic_nodes.count = AsyncMock(return_value=1)
        topic_nodes.nth = MagicMock(return_value=topic_node)
        editor = MagicMock()
        editor.click = AsyncMock()
        editor.locator = MagicMock(return_value=topic_nodes)
        editor.evaluate = AsyncMock(
            side_effect=[
                None,
                {
                    "prefixText": "谈预算。",
                    "entityTopics": ["#AI编程"],
                    "plainTextAfterFirstTopic": "算。",
                },
            ]
        )
        candidate = MagicMock()
        candidate.click = AsyncMock()
        page = MagicMock()
        page.locator = MagicMock()
        page.keyboard.press = AsyncMock()
        page.keyboard.insert_text = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        with patch(
            "app_core.xhs_native_adapter._first_visible",
            new_callable=AsyncMock,
            return_value=editor,
        ), patch(
            "app_core.xhs_native_adapter._find_unique_official_topic_candidate",
            new_callable=AsyncMock,
            return_value=candidate,
        ):
            with self.assertRaises(XhsNativeAdapterError) as raised:
                await helper.fill_official_topics(page)

        self.assertEqual(
            raised.exception.error_code,
            "xhs_topic_insert_position_invalid",
        )

    async def test_accepts_platform_topic_entity_marker_at_body_end(self):
        helper = object.__new__(XhsNativeAdapter)
        helper.contract = {
            "description": "完整正文。",
            "topics": ["硅基探索"],
        }
        editor = MagicMock()
        editor.click = AsyncMock()
        editor.evaluate = AsyncMock(
            side_effect=[
                None,
                {
                    "prefixText": "完整正文。",
                    "entityTopics": ["#硅基探索[话题]#"],
                    "plainTextAfterFirstTopic": "",
                },
            ]
        )
        candidate = MagicMock()
        candidate.click = AsyncMock()
        page = MagicMock()
        page.locator = MagicMock()
        page.keyboard.insert_text = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        with patch(
            "app_core.xhs_native_adapter._first_visible",
            new_callable=AsyncMock,
            return_value=editor,
        ), patch(
            "app_core.xhs_native_adapter._find_unique_official_topic_candidate",
            new_callable=AsyncMock,
            return_value=candidate,
        ):
            readback = await helper.fill_official_topics(page)

        self.assertEqual(readback, ["#硅基探索"])


class _DeclarationLocator:
    def __init__(self, items):
        self.items = list(items)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    @property
    def first(self):
        return self.items[0]


class _DeclarationItem:
    def __init__(self, page, kind, text=""):
        self.page = page
        self.kind = kind
        self.text = text
        self.clicked = False

    async def is_visible(self):
        return self.kind != "dropdown" or self.page.opened

    async def evaluate(self, _script):
        return await self.is_visible()

    async def inner_text(self, **_kwargs):
        if self.kind == "trigger":
            return self.page.selected or self.text
        if self.kind == "selected-content":
            return self.page.selected or "添加内容类型声明"
        return self.text

    async def scroll_into_view_if_needed(self, **_kwargs):
        return None

    async def click(self, **_kwargs):
        self.clicked = True
        if self.kind == "trigger":
            self.page.opened = True
        elif self.kind == "option":
            self.page.selected = self.text
            self.page.opened = False

    def locator(self, selector):
        if self.kind == "trigger" and selector == ".d-select-placeholder":
            return _DeclarationLocator(
                [_DeclarationItem(self.page, "placeholder", "添加内容类型声明")]
            )
        if self.kind == "trigger" and selector == ".d-select-content":
            return _DeclarationLocator(
                [_DeclarationItem(self.page, "selected-content")]
            )
        if self.kind == "dropdown" and selector == ".d-option":
            return _DeclarationLocator(self.page.options)
        if self.kind == "option" and selector == ".d-option-name":
            return _DeclarationLocator(
                [_DeclarationItem(self.page, "option-name", self.text)]
            )
        return _DeclarationLocator([])


class _CurrentDeclarationPage:
    """复现 2026-08-25 真实页面：同一句文字嵌套出现多次。"""

    def __init__(self):
        self.opened = False
        self.selected = ""
        self.trigger = _DeclarationItem(self, "trigger", "添加内容类型声明")
        self.dropdown = _DeclarationItem(self, "dropdown")
        self.options = [
            _DeclarationItem(self, "option", "虚构演绎，仅供娱乐"),
            _DeclarationItem(self, "option", "笔记含AI合成内容"),
            _DeclarationItem(self, "option", "内容包含营销广告"),
            _DeclarationItem(self, "option", "内容来源声明"),
        ]

    def locator(self, selector):
        if selector == (
            ".publish-page-content-setting-content "
            ".d-select-wrapper.custom-select-44"
        ):
            return _DeclarationLocator([self.trigger])
        if selector == ".declaration-drop-down":
            return _DeclarationLocator([self.dropdown])
        if selector == "body":
            return _DeclarationItem(self, "body", self.selected)
        return _DeclarationLocator([])

    def get_by_text(self, text, exact=False):
        del exact
        # 旧实现从整页按文字取控件；真实页面会返回 wrapper、select、
        # content、placeholder 等多个嵌套节点，无法唯一识别。
        count = 6 if text == "添加内容类型声明" else 5
        return _DeclarationLocator(
            [_DeclarationItem(self, "nested-text", text) for _ in range(count)]
        )

    async def wait_for_timeout(self, _milliseconds):
        return None


class XhsDeclarationTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_ai_declaration_through_unique_current_dropdown(self):
        """退回全页面文本匹配会因嵌套节点重复而再次失败。"""

        helper = object.__new__(XhsNativeAdapter)
        helper.contract = {
            "aiDeclaration": True,
            "originalDeclaration": False,
        }
        page = _CurrentDeclarationPage()

        await helper.set_declarations(page)

        self.assertTrue(page.trigger.clicked)
        self.assertEqual(page.selected, "笔记含AI合成内容")
        self.assertTrue(page.options[1].clicked)

class XhsPublishExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.image_path = Path(self.temporary.name) / "fixture.png"
        self.image_path.write_bytes(b"offline-image-fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def _payload(self):
        target = datetime.now() + timedelta(days=1)
        return {
            "type": 1,
            "contentType": "article",
            "runtimeMode": "publish",
            "debugDryRun": False,
            "enableTimer": True,
            "scheduleTime": target.strftime("%Y-%m-%d %H:%M"),
            "title": "测试标题",
            "description": "测试正文",
            "fileList": [str(self.image_path)],
        }

    def test_accepts_article_publish_mode_and_future_schedule(self):
        payload = self._payload()
        _validate_payload(payload)
        self.assertGreater(_schedule_time(payload), datetime.now())

    def test_accepts_explicit_immediate_publish(self):
        payload = self._payload()
        payload["enableTimer"] = False
        payload["scheduleTime"] = None
        _validate_payload(payload)
        self.assertIsNone(_schedule_time(payload))

    def test_rejects_schedule_value_when_timer_is_disabled(self):
        payload = self._payload()
        payload["enableTimer"] = False
        with self.assertRaisesRegex(XhsPublishError, "未开启定时"):
            _validate_payload(payload)

    def test_accepts_video_publish(self):
        payload = self._payload()
        payload["contentType"] = "video"
        video_path = Path(self.temporary.name) / "fixture.mp4"
        video_path.write_bytes(b"offline-video-fixture")
        payload["fileList"] = [str(video_path)]
        try:
            _validate_payload(payload)
        finally:
            video_path.unlink(missing_ok=True)

    def test_rejects_xhs_text_publish(self):
        payload = self._payload()
        payload["contentType"] = "text"
        with self.assertRaisesRegex(XhsPublishError, "图文或视频"):
            _validate_payload(payload)

    def test_rejects_preflight_payload(self):
        payload = self._payload()
        payload["runtimeMode"] = "preflight"
        payload["debugDryRun"] = True
        with self.assertRaisesRegex(XhsPublishError, "runtimeMode"):
            _validate_payload(payload)

    def test_reads_xhs_name_from_nested_official_identity_response(self):
        self.assertEqual(
            _identity_name_from_response(
                {"success": True, "data": {"user": {"nickname": "海风"}}}
            ),
            "海风",
        )
        self.assertIsNone(_identity_name_from_response({"data": {"id": "123"}}))

    def test_reads_publish_label_from_web_component_attribute(self):
        self.assertEqual(
            _publish_button_label(
                {
                    "tag": "XHS-PUBLISH-BTN",
                    "text": "",
                    "attrs": {
                        "submitText": "定时发布",
                        "submitDisabled": "false",
                    },
                }
            ),
            "定时发布",
        )
        self.assertEqual(
            _publish_button_label({"tag": "BUTTON", "text": "发布"}),
            "发布",
        )

    def test_receipt_timeout_has_stable_error_code(self):
        class Body:
            async def inner_text(self, *, timeout):
                del timeout
                return "作品发布页"

        class EmptyDialogs:
            async def count(self):
                return 0

            def nth(self, _index):
                raise AssertionError("没有弹窗时不应读取节点")

        class Page:
            url = "https://creator.xiaohongshu.com/publish/publish"

            def is_closed(self):
                return False

            def locator(self, selector):
                if selector == "body":
                    return Body()
                return EmptyDialogs()

            async def wait_for_timeout(self, _milliseconds):
                return None

        with self.assertRaises(XhsPublishError) as raised:
            asyncio.run(
                _wait_for_platform_result(
                    Page(),
                    task_id=7,
                    schedule_text="2026-08-24 22:00",
                    scheduled=True,
                    timeout_seconds=1,
                )
            )
        self.assertEqual(raised.exception.error_code, "xhs_receipt_timeout")

    def test_ai_declaration_requires_evidence_and_authorization(self):
        payload = {
            "aiGenerated": True,
            "aiDisclosure": {
                "containsAiGeneratedContent": True,
                "allowPlatformAutoDeclaration": False,
            },
            "aiDeclarationExplicitlyConfirmed": False,
            "originalDeclaration": False,
        }
        with self.assertRaisesRegex(XhsPublishError, "未允许自动"):
            _validate_declaration_policy(payload)

        payload["aiDeclarationExplicitlyConfirmed"] = True
        _validate_declaration_policy(payload)

        payload["aiDeclarationExplicitlyConfirmed"] = False
        payload["aiDisclosure"]["allowPlatformAutoDeclaration"] = True
        _validate_declaration_policy(payload)

    def test_ai_evidence_requires_platform_declaration_confirmation(self):
        payload = {
            "aiGenerated": False,
            "aiDisclosure": {
                "containsAiGeneratedContent": True,
                "allowPlatformAutoDeclaration": False,
            },
            "originalDeclaration": False,
        }
        with self.assertRaisesRegex(XhsPublishError, "尚未确认"):
            _validate_declaration_policy(payload)

    def test_original_declaration_rejects_non_boolean_guess(self):
        with self.assertRaisesRegex(XhsPublishError, "原创声明"):
            _validate_declaration_policy({"originalDeclaration": "yes"})

    def test_native_contract_maps_local_fields_without_external_runtime(self):
        target = datetime.now() + timedelta(days=1)
        contract = build_native_contract(
            {
                "contentType": "article",
                "title": "离线映射测试",
                "description": "只验证本地字段，不访问小红书。",
                "fileList": [str(self.image_path)],
                "tags": ["测试", "#测试", "离线"],
                "enableTimer": True,
                "scheduleTime": target.strftime("%Y-%m-%d %H:%M"),
                "aiGenerated": True,
                "originalDeclaration": False,
            }
        )
        self.assertEqual(contract["formType"], "task")
        self.assertEqual(contract["visibleType"], 0)
        self.assertEqual(contract["topics"], ["测试", "离线"])
        self.assertEqual(contract["images"][0]["path"], str(self.image_path.resolve()))
        self.assertIsInstance(contract["scheduledTime"], int)
        self.assertTrue(contract["aiDeclaration"])
        self.assertEqual(
            contract["executionBackend"],
            "oneclick-official-creator-page",
        )
        self.assertFalse(contract["requiresYixiaoerClient"])
        self.assertFalse(contract["requiresYixiaoerGateway"])
        self.assertFalse(contract["usesPrivateSignatureService"])

    def test_native_contract_rejects_schema_limit_before_browser(self):
        with self.assertRaisesRegex(XhsNativeAdapterError, "标题"):
            build_native_contract(
                {
                    "contentType": "article",
                    "title": "超" * 21,
                    "description": "正文",
                    "fileList": [str(self.image_path)],
                }
            )

    def test_native_contract_rejects_more_than_ten_topics(self):
        with self.assertRaisesRegex(XhsNativeAdapterError, "10 个话题"):
            build_native_contract(
                {
                    "contentType": "article",
                    "title": "离线话题测试",
                    "description": "正文",
                    "fileList": [str(self.image_path)],
                    "tags": [f"话题{index}" for index in range(11)],
                }
            )

    def test_topic_candidate_matches_only_official_name_node(self):
        self.assertTrue(_topic_candidate_matches("#AI工具", "AI工具"))
        self.assertTrue(_topic_candidate_matches("  #AI工具\n", "#AI工具"))
        self.assertFalse(
            _topic_candidate_matches(
                "#AI工具 活动话题· 29.4亿浏览",
                "AI工具",
            )
        )
        self.assertFalse(_topic_candidate_matches("#AI工具箱", "AI工具"))

    def test_current_ai_declaration_label_is_supported(self):
        self.assertIn("笔记含AI合成内容", _AI_LABELS)

    def test_article_cover_is_first_image_without_duplication(self):
        second_image = Path(self.temporary.name) / "second.jpg"
        second_image.write_bytes(b"second-offline-image")
        contract = build_native_contract(
            {
                "contentType": "article",
                "title": "封面顺序测试",
                "description": "正文",
                "fileList": [str(second_image), str(self.image_path)],
                "coverPath": str(self.image_path),
            }
        )
        self.assertEqual(contract["coverMode"], "first-image")
        self.assertEqual(
            contract["images"][0]["path"],
            str(self.image_path.resolve()),
        )
        self.assertEqual(len(contract["images"]), 2)

    def test_video_contract_keeps_custom_cover_and_single_video(self):
        video_path = Path(self.temporary.name) / "fixture.mp4"
        video_path.write_bytes(b"offline-video-fixture")
        contract = build_native_contract(
            {
                "contentType": "video",
                "title": "视频离线测试",
                "description": "视频正文",
                "fileList": [str(video_path)],
                "coverPath": str(self.image_path),
            }
        )
        self.assertEqual(contract["nativePublishType"], "video")
        self.assertEqual(contract["video"]["path"], str(video_path.resolve()))
        self.assertEqual(contract["coverPath"], str(self.image_path.resolve()))

    def test_desktop_publish_service_accepts_xhs_immediate_video(self):
        video_path = Path(self.temporary.name) / "fixture.mp4"
        video_path.write_bytes(b"offline-video-fixture")
        payload = {
            "type": 1,
            "contentType": "video",
            "runtimeMode": "publish",
            "debugDryRun": False,
            "enableTimer": False,
            "scheduleTime": None,
            "title": "视频发布入口测试",
            "description": "只验证本地任务校验，不访问小红书。",
            "fileList": [str(video_path)],
        }
        validated = _validate_payloads([payload])
        self.assertEqual(validated[0]["contentType"], "video")

    def test_legacy_runtime_no_longer_calls_old_xhs_uploader(self):
        runtime_source = (
            Path(__file__).parent / "app_core" / "publish_runtime.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("post_video_xhs", runtime_source)
        self.assertIn("_run_oneclick_xhs", runtime_source)


if __name__ == "__main__":
    unittest.main()
