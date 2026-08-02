# -*- coding: utf-8 -*-
"""公众号图文预检的离线回归测试。"""

import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app_core.oneclick_preflight import (
    PreflightError,
    _WECHAT_AUTHOR_ONLY_OPERATION,
    _WECHAT_AUTHOR_LIST_SELECTORS,
    _WECHAT_AUTHOR_OPTION_SELECTORS,
    _WECHAT_AUTHOR_TRIGGER_SELECTORS,
    _WECHAT_COVER_ONLY_OPERATION,
    _WECHAT_MOBILE_TEMPLATES,
    _normalized_page_text,
    _wechat_author_display_name,
    _wechat_author_only_preflight,
    _wechat_cover_crop_snapshot,
    _wechat_cover_only_preflight,
    _wechat_markdown_to_html,
    _wechat_markdown_visible_text,
    _wechat_payload_text,
    _wechat_original_requested,
    _wechat_dialog_is_in_viewport,
    _wechat_insert_anchored_body_images,
    _wechat_insert_body_images,
    _wechat_normalize_image_url,
    _wechat_place_body_image_anchor,
    _wechat_prepare_article_images,
    _wechat_remove_temporary_cover,
    _wechat_resolve_body_image_anchors,
    _wechat_select_default_author,
    _wechat_select_cover_from_content,
    _wechat_verify_body_image_placements,
    _wechat_wait_cover_crop_ready,
    _wechat_wait_cover_return_to_editor,
    _validate_wechat_author_payload,
    _validate_wechat_cover_payload,
    run_wechat_author_preflight_sync,
    run_wechat_cover_preflight_sync,
)


class _ImageNodes:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return self.page.image_count


class _Editor:
    def __init__(self, page):
        self.page = page

    def locator(self, selector):
        if selector != "img":
            raise AssertionError(f"意外的正文选择器：{selector}")
        return _ImageNodes(self.page)


class _BodyImageInput:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return 1

    async def set_input_files(self, files):
        self.page.uploaded_files = list(files)
        self.page.image_count += len(files)


class _BodyImageTool:
    def __init__(self, page):
        self.page = page
        self.input = _BodyImageInput(page)

    async def count(self):
        return 1

    async def hover(self):
        self.page.hovered = True

    def locator(self, selector):
        if selector != "input[type=file]":
            raise AssertionError(f"意外的图片上传选择器：{selector}")
        return self.input


class _Page:
    def __init__(self):
        self.image_count = 0
        self.hovered = False
        self.uploaded_files = []
        self.tool = _BodyImageTool(self)

    def locator(self, selector):
        if selector != "#js_editor_insertimage":
            raise AssertionError(f"正文图片必须限定在工具栏入口，实际为：{selector}")
        return self.tool

    async def wait_for_timeout(self, _milliseconds):
        return None


class _AnchorEditor:
    def __init__(self, result):
        self.result = result
        self.requested_anchor = None

    async def evaluate(self, _script, anchor):
        self.requested_anchor = anchor
        return self.result


class _PlacementEditor:
    def __init__(self, states):
        self.states = states
        self.requested_anchors = None

    async def evaluate(self, _script, anchors):
        self.requested_anchors = list(anchors)
        return list(self.states)


class _AuthorNode:
    def __init__(
        self,
        text="",
        *,
        visible=True,
        enabled=True,
        attributes=None,
        input_value_error=True,
        on_click=None,
    ):
        self.text = text
        self.visible = visible
        self.enabled = enabled
        self.attributes = dict(attributes or {})
        self.input_value_error = input_value_error
        self.on_click = on_click
        self.clicked = False

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def get_attribute(self, name):
        return self.attributes.get(name)

    async def inner_text(self):
        return self.text

    async def input_value(self):
        if self.input_value_error:
            raise RuntimeError("不是输入框")
        return self.text

    async def click(self, **_kwargs):
        self.clicked = True
        if self.on_click:
            self.on_click()


class _AuthorPage:
    def __init__(self):
        self.waits = []

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class WechatPreflightTests(unittest.TestCase):
    def test_wechat_body_preserves_full_markdown_without_material_paths(self):
        body = (
            "第一段内容。\n\n"
            "## 结构标题\n\n"
            "这里有**重点**和[官方来源](https://example.com/report)。\n\n"
            "- 资料一\n"
            "- 资料二\n\n"
            "> 仅在原文已有引用语义时使用。\n\n"
            "### 三级标题\n\n"
            "`代码片段`\n\n"
            "---"
        )
        title, preserved = _wechat_payload_text(
            {"title": "测试标题", "description": body * 80}
        )
        self.assertEqual(title, "测试标题")
        self.assertEqual(preserved, body * 80)
        self.assertGreater(len(preserved), 900)

        html = _wechat_markdown_to_html(body)
        self.assertIn('data-oneclick-template="wechat-mobile-editorial-v2"', html)
        self.assertIn('data-oneclick-theme="warm-jade"', html)
        self.assertIn(">第一段内容。</p>", html)
        self.assertIn(">结构标题</h2>", html)
        self.assertIn(">重点</strong>", html)
        self.assertIn('<a href="https://example.com/report"', html)
        self.assertIn(">资料一</li>", html)
        self.assertIn(">资料二</li>", html)
        self.assertIn(">仅在原文已有引用语义时使用。</blockquote>", html)
        self.assertIn(">三级标题</h3>", html)
        self.assertIn(">代码片段</code>", html)
        self.assertIn("<hr ", html)
        self.assertIn("font-size:16px;line-height:1.92", html)
        self.assertIn("font-size:20px;line-height:1.45", html)
        self.assertIn("background:#fffcf6", html)
        self.assertIn("color:#514d47", html)
        self.assertIn("color:#315d54", html)
        self.assertIn("border-left:3px solid #6f9185", html)
        self.assertIn("border-bottom:1px solid #e7e0d3", html)
        self.assertIn("padding-left:1.45em", html)
        self.assertIn("color:#3f756a", html)
        self.assertIn("border-left:3px solid #86a399", html)
        self.assertIn("background:#f3f4ee", html)
        self.assertNotIn("font-size:24px", html)
        self.assertNotIn("background:#111", html)
        self.assertNotIn("linear-gradient", html)
        self.assertNotIn("## ", html)
        self.assertNotIn("**", html)
        self.assertNotIn("images/", html)
        self.assertEqual(
            _normalized_page_text(_wechat_markdown_visible_text(body)),
            _normalized_page_text(
                "第一段内容。 结构标题 这里有重点和官方来源。 资料一 资料二 "
                "仅在原文已有引用语义时使用。 三级标题 代码片段"
            ),
        )

    def test_two_mobile_candidates_are_restrained_and_semantically_identical(self):
        body = (
            "正文段落。\n\n"
            "## 章节标题\n\n"
            "> 原文引用。\n\n"
            "- 列表项\n\n"
            "[来源](https://example.com)"
        )
        expected_text = _normalized_page_text(_wechat_markdown_visible_text(body))
        for theme_id in ("warm-jade", "warm-umber"):
            html = _wechat_markdown_to_html(body, theme_id)
            self.assertIn(f'data-oneclick-theme="{theme_id}"', html)
            self.assertNotIn("linear-gradient", html)
            self.assertNotIn("font-size:24px", html)
            self.assertNotIn("background:#111", html)
            self.assertEqual(
                _normalized_page_text(_wechat_markdown_visible_text(body)),
                expected_text,
            )
        self.assertEqual(_WECHAT_MOBILE_TEMPLATES["warm-jade"]["name"], "暖纸青墨")
        self.assertEqual(_WECHAT_MOBILE_TEMPLATES["warm-umber"]["name"], "暖纸赭棕")

    def test_page_text_readback_ignores_only_layout_whitespace_and_zero_width_marks(self):
        expected = "第一段\n第二段"
        actual = "  第一段\u200b \n\n 第二段  "
        self.assertEqual(
            _normalized_page_text(actual),
            _normalized_page_text(expected),
        )
        self.assertNotEqual(
            _normalized_page_text("第一段\n不同正文"),
            _normalized_page_text(expected),
        )

    def test_hidden_dialog_node_does_not_block(self):
        self.assertFalse(
            _wechat_dialog_is_in_viewport(
                {
                    "hidden": False,
                    "ariaHidden": True,
                    "display": "block",
                    "visibility": "visible",
                    "opacity": "1",
                    "width": 600,
                    "height": 320,
                    "intersectionWidth": 600,
                    "intersectionHeight": 320,
                    "hitTestMatches": True,
                }
            )
        )
        self.assertFalse(
            _wechat_dialog_is_in_viewport(
                {
                    "hidden": False,
                    "ariaHidden": False,
                    "display": "block",
                    "visibility": "visible",
                    "opacity": "1",
                    "width": 600,
                    "height": 320,
                    "intersectionWidth": 600,
                    "intersectionHeight": 320,
                    "hitTestMatches": False,
                }
            )
        )

    def test_real_viewport_dialog_still_blocks(self):
        self.assertTrue(
            _wechat_dialog_is_in_viewport(
                {
                    "hidden": False,
                    "ariaHidden": False,
                    "display": "block",
                    "visibility": "visible",
                    "opacity": "1",
                    "width": 600,
                    "height": 320,
                    "intersectionWidth": 600,
                    "intersectionHeight": 320,
                    "hitTestMatches": True,
                }
            )
        )

    def test_editor_src_matches_cover_dialog_background_image(self):
        editor_src = (
            "https://mmbiz.qpic.cn/mmbiz_png/example/0?"
            "wx_fmt=png&from=appmsg"
        )
        dialog_background = (
            'url("https://mmbiz.qpic.cn/mmbiz_png/example/0?'
            'wx_fmt=png&tp=webp")'
        )
        self.assertEqual(
            _wechat_normalize_image_url(editor_src),
            _wechat_normalize_image_url(dialog_background),
        )
        self.assertEqual(
            _wechat_normalize_image_url(
                "url(&quot;https://mmbiz.qpic.cn/example/cover.png?x=1&quot;)"
            ),
            "https://mmbiz.qpic.cn/example/cover.png",
        )

    def test_body_images_are_uploaded_through_editor_toolbar_and_read_back(self):
        page = _Page()
        inserted = asyncio.run(
            _wechat_insert_body_images(
                page,
                _Editor(page),
                [Path("body-image-1.png"), Path("body-image-2.png")],
            )
        )
        self.assertTrue(page.hovered)
        self.assertEqual(page.uploaded_files, ["body-image-1.png", "body-image-2.png"])
        self.assertEqual(inserted, 2)
        self.assertEqual(page.image_count, 2)

    def test_cover_is_inserted_before_body_images_then_selected_from_content(self):
        events = []

        async def insert_images(_page, _editor, files):
            events.append(("insert", [str(path) for path in files]))
            return len(files)

        async def select_cover(_page, _editor, cover_image_index=0):
            events.append(("select-from-content", cover_image_index))

        async def remove_cover(_editor, first_image_index):
            events.append(("remove-temporary-cover", first_image_index))
            return 1

        async def insert_anchored(_page, _editor, files, anchors=None):
            events.append(
                (
                    "insert-anchored",
                    [str(path) for path in files],
                    list(anchors or []),
                )
            )
            return len(files)

        editor = _Editor(_Page())
        with (
            patch(
                "app_core.oneclick_preflight._wechat_insert_body_images",
                new=AsyncMock(side_effect=insert_images),
            ),
            patch(
                "app_core.oneclick_preflight._wechat_select_cover_from_content",
                new=AsyncMock(side_effect=select_cover),
            ),
            patch(
                "app_core.oneclick_preflight._wechat_remove_temporary_cover",
                new=AsyncMock(side_effect=remove_cover),
            ),
            patch(
                "app_core.oneclick_preflight._wechat_insert_anchored_body_images",
                new=AsyncMock(side_effect=insert_anchored),
            ),
        ):
            body_count = asyncio.run(
                _wechat_prepare_article_images(
                    object(),
                    editor,
                    Path("cover.png"),
                    [Path("body-1.png"), Path("body-2.png")],
                    ["对应论点", ""],
                )
            )

        self.assertEqual(
            events,
            [
                ("insert", ["cover.png"]),
                ("select-from-content", 0),
                ("remove-temporary-cover", 0),
                (
                    "insert-anchored",
                    ["body-1.png", "body-2.png"],
                    ["对应论点", ""],
                ),
            ],
        )
        self.assertEqual(body_count, 2)

    def test_default_body_image_anchor_is_before_first_heading(self):
        editor = _AnchorEditor("before-first-heading")
        mode = asyncio.run(_wechat_place_body_image_anchor(editor))
        self.assertEqual(mode, "before-first-heading")
        self.assertEqual(editor.requested_anchor, "")
        source = inspect.getsource(_wechat_place_body_image_anchor)
        self.assertIn("element.querySelector('h2')", source)
        self.assertIn("range.setStartBefore(target)", source)

    def test_real_author_dom_selectors_cover_entry_list_option_and_readback(self):
        self.assertEqual(
            _WECHAT_AUTHOR_TRIGGER_SELECTORS[0],
            "#js_author_area input#author",
        )
        self.assertIn(
            "#js_author_area .js_author_list .weui-desktop-dropdown-menu",
            _WECHAT_AUTHOR_LIST_SELECTORS,
        )
        self.assertIn(
            "li.js_item[data-idx].weui-desktop-dropdown__list-ele",
            _WECHAT_AUTHOR_OPTION_SELECTORS,
        )
        self.assertEqual(
            _WECHAT_AUTHOR_TRIGGER_SELECTORS[0],
            "#js_author_area input#author",
        )

    def test_after_intro_final_dom_position_is_independently_verified(self):
        editor = _PlacementEditor(
            [
                {
                    "index": 0,
                    "mode": "after_intro",
                    "anchor": "",
                    "hasFirstHeading": True,
                    "beforeFirstHeading": True,
                    "introBlocksBefore": 2,
                    "rendered": True,
                }
            ]
        )
        states = asyncio.run(
            _wechat_verify_body_image_placements(editor, 1, [""])
        )
        self.assertEqual(len(states), 1)
        self.assertEqual(editor.requested_anchors, [""])

    def test_after_intro_appended_after_first_section_is_rejected(self):
        editor = _PlacementEditor(
            [
                {
                    "index": 0,
                    "mode": "after_intro",
                    "anchor": "",
                    "hasFirstHeading": True,
                    "beforeFirstHeading": False,
                    "introBlocksBefore": 3,
                    "rendered": True,
                }
            ]
        )
        with self.assertRaisesRegex(PreflightError, "导语后、第一节前"):
            asyncio.run(
                _wechat_verify_body_image_placements(editor, 1, [""])
            )

    def test_after_intro_requires_intro_content_before_image(self):
        editor = _PlacementEditor(
            [
                {
                    "index": 0,
                    "mode": "after_intro",
                    "anchor": "",
                    "hasFirstHeading": True,
                    "beforeFirstHeading": True,
                    "introBlocksBefore": 0,
                    "rendered": True,
                }
            ]
        )
        with self.assertRaisesRegex(PreflightError, "未回读到导语内容"):
            asyncio.run(
                _wechat_verify_body_image_placements(editor, 1, [""])
            )

    def test_final_dom_position_count_must_match_assets(self):
        editor = _PlacementEditor([])
        with self.assertRaisesRegex(PreflightError, "数量不一致"):
            asyncio.run(
                _wechat_verify_body_image_placements(editor, 1, [""])
            )

    def test_final_dom_position_ignores_prosemirror_helper_image(self):
        source = inspect.getsource(_wechat_verify_body_image_placements)
        self.assertIn("ProseMirror-separator", source)
        self.assertIn("getAttribute('src')", source)

    def test_final_dom_position_requires_rendered_content_image(self):
        editor = _PlacementEditor(
            [
                {
                    "index": 0,
                    "mode": "after_intro",
                    "anchor": "",
                    "hasFirstHeading": True,
                    "beforeFirstHeading": True,
                    "introBlocksBefore": 1,
                    "rendered": False,
                }
            ]
        )
        with self.assertRaisesRegex(PreflightError, "最终回读不可见"):
            asyncio.run(
                _wechat_verify_body_image_placements(editor, 1, [""])
            )

    def test_explicit_body_image_anchor_must_match_instead_of_falling_back(self):
        editor = _AnchorEditor("after-explicit-anchor")
        mode = asyncio.run(_wechat_place_body_image_anchor(editor, "对应论点"))
        self.assertEqual(mode, "after-explicit-anchor")
        self.assertEqual(editor.requested_anchor, "对应论点")
        with self.assertRaisesRegex(Exception, "未找到插图锚点"):
            asyncio.run(
                _wechat_place_body_image_anchor(
                    _AnchorEditor("anchor-not-found"),
                    "不存在的段落",
                )
            )

    def test_cover_candidate_is_removed_before_anchored_body_images(self):
        prepare_source = inspect.getsource(_wechat_prepare_article_images)
        remove_source = inspect.getsource(_wechat_remove_temporary_cover)
        anchored_source = inspect.getsource(_wechat_insert_anchored_body_images)
        self.assertLess(
            prepare_source.index("_wechat_select_cover_from_content"),
            prepare_source.index("_wechat_remove_temporary_cover"),
        )
        self.assertLess(
            prepare_source.index("_wechat_remove_temporary_cover"),
            prepare_source.index("_wechat_insert_anchored_body_images"),
        )
        self.assertIn("allImages.slice(startIndex)", remove_source)
        self.assertIn("_wechat_place_body_image_anchor", anchored_source)

    def test_single_legacy_body_image_defaults_to_after_intro(self):
        anchors = _wechat_resolve_body_image_anchors(
            "导语第一段。\n\n## 第一节\n\n章节正文。",
            [Path("/tmp/body-1.png")],
            [],
        )
        self.assertEqual(anchors, [""])

    def test_explicit_heading_anchor_wins_and_legacy_remainder_is_distributed(self):
        markdown = (
            "导语。\n\n"
            "## 第一节\n\n第一节正文。\n\n"
            "## 第二节\n\n第二节正文。\n\n"
            "## 第三节\n\n第三节正文。"
        )
        files = [
            Path("/tmp/intro.png"),
            Path("/tmp/second.png"),
            Path("/tmp/auto.png"),
        ]
        placements = [
            {"path": str(files[0]), "placement": "after_intro"},
            {
                "path": str(files[1]),
                "placement": "after_heading",
                "anchor": "第二节",
            },
            {"path": str(files[2]), "placement": "auto_distribute"},
        ]
        anchors = _wechat_resolve_body_image_anchors(
            markdown,
            files,
            placements,
        )
        self.assertEqual(anchors[0], "")
        self.assertEqual(anchors[1], "第二节")
        self.assertIn(anchors[2], {"第一节", "第三节"})
        self.assertNotEqual(anchors[2], "第二节")

    def test_cover_selection_has_no_local_upload_or_publish_action(self):
        source = inspect.getsource(_wechat_select_cover_from_content)
        self.assertNotIn("set_input_files", source)
        self.assertNotIn("保存草稿", source)
        self.assertNotIn("预览", source)
        self.assertNotIn("发表", source)
        self.assertIn(".js_selectCoverFromContent", source)
        self.assertIn(".appmsg_content_img_item", source)
        self.assertIn("backgroundImage", source)
        self.assertIn("下一步", source)
        self.assertIn("_wechat_wait_cover_crop_ready", source)
        self.assertIn("_wechat_wait_cover_return_to_editor", source)
        self.assertNotIn(
            "wait_for_timeout(500)",
            source.split("await next_button.click", 1)[-1],
        )
        self.assertNotIn('get_by_text("上一步"', source)
        self.assertNotIn('dialog.locator("img[src]")', source)

    def test_cover_crop_loading_times_out_without_guessing(self):
        page = _AuthorPage()
        loading = {
            "dialogVisible": True,
            "dialogCount": 1,
            "loading": True,
            "usableControls": [],
            "hiddenControls": [],
            "coverReady": False,
            "editorVisible": True,
        }
        with patch(
            "app_core.oneclick_preflight._wechat_cover_crop_snapshot",
            new=AsyncMock(return_value=loading),
        ):
            with self.assertRaisesRegex(PreflightError, "加载超时"):
                asyncio.run(
                    _wechat_wait_cover_crop_ready(
                        page,
                        attempts=2,
                        interval_ms=25,
                    )
                )
        self.assertEqual(page.waits, [25, 25])

    def test_cover_crop_delayed_loading_waits_for_visible_confirm(self):
        page = _AuthorPage()
        loading = {
            "dialogVisible": True,
            "dialogCount": 1,
            "loading": True,
            "usableControls": [],
            "hiddenControls": [],
            "coverReady": False,
            "editorVisible": True,
        }
        ready = {
            "dialogVisible": True,
            "dialogCount": 1,
            "loading": False,
            "usableControls": ["完成"],
            "hiddenControls": [],
            "coverReady": False,
            "editorVisible": True,
        }
        with patch(
            "app_core.oneclick_preflight._wechat_cover_crop_snapshot",
            new=AsyncMock(side_effect=[loading, ready]),
        ):
            mode, snapshot = asyncio.run(
                _wechat_wait_cover_crop_ready(
                    page,
                    attempts=3,
                    interval_ms=25,
                )
            )
        self.assertEqual(mode, "confirm")
        self.assertEqual(snapshot["usableControls"], ["完成"])
        self.assertEqual(page.waits, [25])

    def test_cover_crop_accepts_verified_platform_auto_complete(self):
        page = _AuthorPage()
        complete = {
            "dialogVisible": False,
            "dialogCount": 0,
            "loading": False,
            "usableControls": [],
            "hiddenControls": [],
            "coverReady": True,
            "editorVisible": True,
        }
        with patch(
            "app_core.oneclick_preflight._wechat_cover_crop_snapshot",
            new=AsyncMock(return_value=complete),
        ):
            mode, snapshot = asyncio.run(
                _wechat_wait_cover_crop_ready(page, attempts=1)
            )
        self.assertEqual(mode, "auto-complete")
        self.assertTrue(snapshot["coverReady"])
        self.assertEqual(page.waits, [])

    def test_cover_crop_hidden_confirm_is_never_clicked(self):
        page = _AuthorPage()
        hidden = {
            "dialogVisible": True,
            "dialogCount": 1,
            "loading": False,
            "usableControls": [],
            "hiddenControls": ["完成"],
            "coverReady": False,
            "editorVisible": True,
        }
        with patch(
            "app_core.oneclick_preflight._wechat_cover_crop_snapshot",
            new=AsyncMock(return_value=hidden),
        ):
            with self.assertRaisesRegex(PreflightError, "不可见或不可用"):
                asyncio.run(
                    _wechat_wait_cover_crop_ready(
                        page,
                        attempts=1,
                        interval_ms=25,
                    )
                )

    def test_cover_return_waits_until_dialog_closed_and_editor_visible(self):
        page = _AuthorPage()
        dialog_open = {
            "dialogVisible": True,
            "coverReady": False,
            "editorVisible": True,
        }
        complete = {
            "dialogVisible": False,
            "coverReady": True,
            "editorVisible": True,
        }
        with patch(
            "app_core.oneclick_preflight._wechat_cover_crop_snapshot",
            new=AsyncMock(side_effect=[dialog_open, complete]),
        ):
            snapshot = asyncio.run(
                _wechat_wait_cover_return_to_editor(
                    page,
                    attempts=2,
                    interval_ms=25,
                )
            )
        self.assertTrue(snapshot["coverReady"])
        self.assertEqual(page.waits, [25])

    def test_cover_crop_snapshot_explicitly_excludes_back_button(self):
        source = inspect.getsource(_wechat_cover_crop_snapshot)
        self.assertIn("['完成', '确定', '确认']", source)
        self.assertNotIn("'上一步'", source)
        self.assertIn("getBoundingClientRect", source)
        self.assertIn("aria-disabled", source)
        self.assertIn("const rendered", source)
        self.assertIn("backgroundImage", source)
        self.assertIn("!source.startsWith('data:image/svg+xml')", source)
        self.assertIn("background !== 'url(\"\")'", source)
        self.assertIn("!background.includes('data:image/svg+xml')", source)
        self.assertIn("const coverLoading", source)
        self.assertIn("coverVisualReady && !coverLoading", source)
        self.assertIn("location.pathname.includes('/cgi-bin/appmsg')", source)

    def test_author_display_name_ignores_ui_metadata(self):
        self.assertEqual(
            _wechat_author_display_name("默认作者\n作者甲\n已选择"),
            "作者甲",
        )
        self.assertEqual(_wechat_author_display_name("默认"), "")

    def test_default_author_selects_first_available_and_reads_back(self):
        page = _AuthorPage()
        trigger = _AuthorNode("")
        author_list = _AuthorNode("停用作者\n作者甲\n作者乙")
        disabled = _AuthorNode("停用作者", enabled=False)
        first = _AuthorNode("作者甲\n默认")
        second = _AuthorNode("作者乙")
        first.on_click = lambda: setattr(trigger, "text", "作者甲")

        with patch(
            "app_core.oneclick_preflight._wechat_first_visible_nodes",
            new=AsyncMock(side_effect=[[trigger], [trigger]]),
        ), patch(
            "app_core.oneclick_preflight._wechat_wait_visible_nodes",
            new=AsyncMock(
                side_effect=[[author_list], [disabled, first, second]]
            ),
        ):
            selected = asyncio.run(_wechat_select_default_author(page))

        self.assertEqual(selected, "作者甲")
        self.assertTrue(first.clicked)
        self.assertFalse(second.clicked)
        self.assertEqual(page.waits, [400, 300])

    def test_author_empty_list_stops_safely(self):
        page = _AuthorPage()
        trigger = _AuthorNode("")
        author_list = _AuthorNode("暂无作者")
        with (
            patch(
                "app_core.oneclick_preflight._wechat_first_visible_nodes",
                new=AsyncMock(return_value=[trigger]),
            ),
            patch(
                "app_core.oneclick_preflight._wechat_wait_visible_nodes",
                new=AsyncMock(return_value=[author_list]),
            ),
        ):
            with self.assertRaisesRegex(PreflightError, "没有可用作者"):
                asyncio.run(_wechat_select_default_author(page))

    def test_author_list_load_failure_stops_safely(self):
        page = _AuthorPage()
        trigger = _AuthorNode("")
        with (
            patch(
                "app_core.oneclick_preflight._wechat_first_visible_nodes",
                new=AsyncMock(return_value=[trigger]),
            ),
            patch(
                "app_core.oneclick_preflight._wechat_wait_visible_nodes",
                new=AsyncMock(return_value=[]),
            ),
        ):
            with self.assertRaisesRegex(PreflightError, "列表加载失败"):
                asyncio.run(_wechat_select_default_author(page))

    def test_ambiguous_author_control_stops_without_clicking(self):
        page = _AuthorPage()
        first = _AuthorNode("")
        second = _AuthorNode("")
        with patch(
            "app_core.oneclick_preflight._wechat_first_visible_nodes",
            new=AsyncMock(return_value=[first, second]),
        ):
            with self.assertRaisesRegex(PreflightError, "控件不唯一"):
                asyncio.run(_wechat_select_default_author(page))
        self.assertFalse(first.clicked)
        self.assertFalse(second.clicked)

    def test_author_management_entry_is_never_selected(self):
        page = _AuthorPage()
        trigger = _AuthorNode("")
        author_list = _AuthorNode("添加作者")
        management = _AuthorNode("添加作者")
        with (
            patch(
                "app_core.oneclick_preflight._wechat_first_visible_nodes",
                new=AsyncMock(return_value=[trigger]),
            ),
            patch(
                "app_core.oneclick_preflight._wechat_wait_visible_nodes",
                new=AsyncMock(side_effect=[[author_list], [management]]),
            ),
        ):
            with self.assertRaisesRegex(PreflightError, "选项未识别"):
                asyncio.run(_wechat_select_default_author(page))
        self.assertFalse(management.clicked)

    def test_author_readback_mismatch_stops_safely(self):
        page = _AuthorPage()
        trigger = _AuthorNode("作者乙")
        author_list = _AuthorNode("作者甲")
        first = _AuthorNode("作者甲")
        with (
            patch(
                "app_core.oneclick_preflight._wechat_first_visible_nodes",
                new=AsyncMock(side_effect=[[trigger], [trigger]]),
            ),
            patch(
                "app_core.oneclick_preflight._wechat_wait_visible_nodes",
                new=AsyncMock(side_effect=[[author_list], [first]]),
            ),
        ):
            with self.assertRaisesRegex(PreflightError, "回读不一致"):
                asyncio.run(_wechat_select_default_author(page))

    def test_author_only_operation_does_not_call_article_workflow(self):
        page = object()
        payload = {
            "type": 10,
            "contentType": "article",
            "accountList": ["wechat.json"],
            "preflightOperation": _WECHAT_AUTHOR_ONLY_OPERATION,
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "publishAllowed": False,
        }
        with (
            patch(
                "app_core.oneclick_preflight._wechat_open_article_editor",
                new=AsyncMock(),
            ),
            patch(
                "app_core.oneclick_preflight._wechat_select_default_author",
                new=AsyncMock(return_value="作者甲"),
            ),
            patch(
                "app_core.oneclick_preflight._wechat_preflight",
                new=AsyncMock(),
            ) as full_preflight,
        ):
            message = asyncio.run(_wechat_author_only_preflight(page, payload))
        self.assertIn("作者甲", message)
        self.assertIn("未保存草稿", message)
        full_preflight.assert_not_awaited()
        source = inspect.getsource(_wechat_author_only_preflight)
        self.assertNotIn("_wechat_fill_rich_text", source)
        self.assertNotIn("_wechat_prepare_article_images", source)
        self.assertNotIn("_wechat_select_cover_from_content", source)

    def test_author_only_sync_channel_requires_exact_safe_payload(self):
        valid = {
            "type": 10,
            "contentType": "article",
            "accountList": ["wechat.json"],
            "preflightOperation": _WECHAT_AUTHOR_ONLY_OPERATION,
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "publishAllowed": False,
        }

        def finish_without_browser(coroutine):
            coroutine.close()
            return {"type": 10, "ok": True}

        with patch(
            "app_core.oneclick_preflight.asyncio.run",
            side_effect=finish_without_browser,
        ) as runner:
            self.assertTrue(run_wechat_author_preflight_sync(valid)["ok"])
            runner.assert_called_once()
        for field, value in (
            ("type", 5),
            ("contentType", "text"),
            ("preflightOperation", "full"),
            ("runtimeMode", "publish"),
            ("debugDryRun", False),
            ("publishAllowed", True),
            ("accountList", []),
            ("accountList", ["wechat-a.json", "wechat-b.json"]),
        ):
            unsafe = dict(valid)
            unsafe[field] = value
            with self.assertRaises(PreflightError):
                run_wechat_author_preflight_sync(unsafe)

    def test_author_only_async_path_cannot_bypass_safe_payload_validation(self):
        valid = {
            "type": 10,
            "contentType": "article",
            "accountList": ["wechat.json"],
            "preflightOperation": _WECHAT_AUTHOR_ONLY_OPERATION,
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "publishAllowed": False,
        }
        _validate_wechat_author_payload(valid)
        for field, value in (
            ("runtimeMode", ""),
            ("debugDryRun", None),
            ("publishAllowed", True),
            ("accountList", "wechat.json"),
        ):
            unsafe = dict(valid)
            unsafe[field] = value
            with self.assertRaises(PreflightError):
                _validate_wechat_author_payload(unsafe)

    def test_cover_only_channel_requires_exact_safe_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            cover = Path(temporary) / "cover.png"
            cover.write_bytes(b"offline-cover")
            valid = {
                "type": 10,
                "contentType": "article",
                "accountList": ["wechat.json"],
                "coverPath": str(cover),
                "preflightOperation": _WECHAT_COVER_ONLY_OPERATION,
                "runtimeMode": "preflight",
                "debugDryRun": True,
                "publishAllowed": False,
            }
            self.assertEqual(_validate_wechat_cover_payload(valid), cover.resolve())

            def finish_without_browser(coroutine):
                coroutine.close()
                return {"type": 10, "ok": True}

            with patch(
                "app_core.oneclick_preflight.asyncio.run",
                side_effect=finish_without_browser,
            ) as runner:
                self.assertTrue(run_wechat_cover_preflight_sync(valid)["ok"])
                runner.assert_called_once()
            for field, value in (
                ("type", 5),
                ("contentType", "text"),
                ("preflightOperation", "full"),
                ("runtimeMode", "publish"),
                ("debugDryRun", False),
                ("publishAllowed", True),
                ("accountList", []),
                ("accountList", ["wechat-a.json", "wechat-b.json"]),
                ("coverPath", str(Path(temporary) / "missing.png")),
            ):
                unsafe = dict(valid)
                unsafe[field] = value
                with self.assertRaises(PreflightError):
                    _validate_wechat_cover_payload(unsafe)

    def test_cover_only_operation_does_not_touch_other_article_fields(self):
        source = inspect.getsource(_wechat_cover_only_preflight)
        self.assertIn("_wechat_select_cover_from_content", source)
        self.assertIn("_wechat_wait_cover", inspect.getsource(_wechat_select_cover_from_content))
        self.assertNotIn("_wechat_fill_rich_text", source)
        self.assertNotIn("_wechat_select_default_author", source)
        self.assertNotIn("_wechat_insert_anchored_body_images", source)
        action_source = source.split("return (", 1)[0]
        self.assertNotIn("保存草稿", action_source)
        self.assertNotIn("预览", action_source)
        self.assertNotIn("发表", action_source)

    def test_full_wechat_preflight_only_selects_author_for_explicit_original(self):
        source = inspect.getsource(
            __import__(
                "app_core.oneclick_preflight",
                fromlist=["_wechat_preflight"],
            )._wechat_preflight
        )
        image_step = source.index("_wechat_prepare_article_images")
        position_readback = source.index("_wechat_verify_body_image_placements")
        author_step = source.index("_wechat_select_default_author")
        self.assertLess(image_step, position_readback)
        self.assertLess(position_readback, author_step)
        self.assertIn("_wechat_original_requested(payload)", source)
        self.assertIn("原创作者已选择并回读", source)
        self.assertIn("未勾选原创，作者流程已完全跳过", source)
        self.assertNotIn("保存为草稿", source.split("return (", 1)[-1])

    def test_original_flag_must_be_explicit_boolean(self):
        self.assertFalse(_wechat_original_requested({}))
        self.assertFalse(_wechat_original_requested({"originalDeclaration": False}))
        self.assertTrue(_wechat_original_requested({"originalDeclaration": True}))
        for unsafe in ("true", 1, None):
            with self.assertRaises(PreflightError):
                _wechat_original_requested({"originalDeclaration": unsafe})


if __name__ == "__main__":
    unittest.main()
